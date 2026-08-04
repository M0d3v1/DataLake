"""Views for the internal operator UI.

Two url modules front this file (see apps.opsui.urls_public /
apps.opsui.urls_tenant): the raw payload migration tool (public schema,
platform-operator-facing, an organization is picked from a server-rendered
list -- never a free-text schema string) and the pipeline run/continuation
pages (tenant schema, resolved by the domain the request arrived on).

All mutating endpoints are POST-only (`require_POST`) and rely on
Django's normal session-cookie CSRF protection (`CsrfViewMiddleware`,
already in MIDDLEWARE) -- no endpoint here disables it.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.auditing.service import record_audit_event
from apps.core.exceptions import ConfigurationError
from apps.execution.dispatch import is_continuable, trigger_manual_run
from apps.execution.models import PipelineRun
from apps.opsui import services
from apps.opsui.audit import record_migration_audit_event
from apps.opsui.models import RawPayloadMigrationJob
from apps.opsui.permissions import (
    CONTINUATION_ROLES,
    is_platform_operator,
    require_org_member,
    require_org_role,
    require_platform_operator,
)
from apps.opsui.tasks import run_raw_payload_migration_task
from apps.orgs.models import Membership, Organization
from apps.pipelines.models import Pipeline

# --- raw payload migration (public schema) ---------------------------------


@login_required
def org_list(request):
    if is_platform_operator(request.user):
        organizations = Organization.objects.all().order_by("name")
    else:
        organizations = Organization.objects.filter(
            memberships__user=request.user,
            memberships__role__in=[Membership.Role.OWNER, Membership.Role.ADMIN],
        ).order_by("name")
    return render(request, "opsui/org_list.html", {"organizations": organizations})


@login_required
def migration_org_detail(request, org_id: int):
    organization = services.resolve_org_or_404(org_id)
    require_org_role(
        request.user, organization, frozenset({Membership.Role.OWNER, Membership.Role.ADMIN})
    )

    job = services.get_active_job(organization) or services.latest_job(organization)
    return render(
        request,
        "opsui/migration_org_detail.html",
        {
            "organization": organization,
            "job": job,
            "can_operate": is_platform_operator(request.user),
        },
    )


@require_POST
@login_required
def migration_start(request, org_id: int):
    organization = services.resolve_org_or_404(org_id)
    require_platform_operator(request.user)

    dry_run = request.POST.get("mode", "dry_run") != "real"

    job, created = services.start_migration_job(
        organization, initiated_by=request.user, dry_run=dry_run
    )

    if not created:
        messages.info(
            request,
            f"An active migration job already exists for {organization.name} "
            "-- showing its progress instead of starting a new one.",
        )
        return redirect("opsui:migration_job_detail", job_id=job.id)

    record_migration_audit_event(
        organization,
        actor=request.user,
        action="raw_payload_migration.dry_run_requested"
        if dry_run
        else "raw_payload_migration.started",
        target=job,
        metadata={"dry_run": dry_run, "job_id": str(job.id)},
    )
    run_raw_payload_migration_task.delay(job_id=str(job.id))
    return redirect("opsui:migration_job_detail", job_id=job.id)


@login_required
def migration_job_detail(request, job_id):
    job = get_object_or_404(
        RawPayloadMigrationJob.objects.select_related("organization"), pk=job_id
    )
    require_org_role(
        request.user, job.organization, frozenset({Membership.Role.OWNER, Membership.Role.ADMIN})
    )
    return render(
        request,
        "opsui/migration_job_detail.html",
        {
            "job": job,
            "organization": job.organization,
            "can_operate": is_platform_operator(request.user),
        },
    )


@login_required
def migration_job_progress(request, job_id):
    """HTMX polling target -- always returns just the progress fragment.
    Stops polling itself (by omitting hx-trigger once the job reaches a
    terminal status) rather than requiring the client to guess when to
    stop."""
    job = get_object_or_404(
        RawPayloadMigrationJob.objects.select_related("organization"), pk=job_id
    )
    require_org_role(
        request.user, job.organization, frozenset({Membership.Role.OWNER, Membership.Role.ADMIN})
    )
    return render(request, "opsui/_migration_job_progress.html", {"job": job})


@require_POST
@login_required
def migration_job_retry(request, job_id):
    job = get_object_or_404(
        RawPayloadMigrationJob.objects.select_related("organization"), pk=job_id
    )
    require_platform_operator(request.user)

    try:
        new_job, _created = services.retry_migration_job(job, initiated_by=request.user)
    except ConfigurationError as exc:
        messages.error(request, str(exc))
        return redirect("opsui:migration_job_detail", job_id=job.id)

    record_migration_audit_event(
        job.organization,
        actor=request.user,
        action="raw_payload_migration.dry_run_requested"
        if new_job.dry_run
        else "raw_payload_migration.started",
        target=new_job,
        metadata={"dry_run": new_job.dry_run, "job_id": str(new_job.id), "retry_of": str(job.id)},
    )
    run_raw_payload_migration_task.delay(job_id=str(new_job.id))
    return redirect("opsui:migration_job_detail", job_id=new_job.id)


@require_POST
@login_required
def migration_job_cancel(request, job_id):
    job = get_object_or_404(
        RawPayloadMigrationJob.objects.select_related("organization"), pk=job_id
    )
    require_platform_operator(request.user)

    try:
        services.request_cancel(job)
    except ConfigurationError as exc:
        messages.error(request, str(exc))
    return redirect("opsui:migration_job_detail", job_id=job.id)


@login_required
def migration_job_export(request, job_id):
    job = get_object_or_404(
        RawPayloadMigrationJob.objects.select_related("organization"), pk=job_id
    )
    require_org_role(
        request.user, job.organization, frozenset({Membership.Role.OWNER, Membership.Role.ADMIN})
    )

    lines = [
        f"Raw payload migration job {job.id}",
        f"Organization: {job.organization.name} ({job.organization.schema_name})",
        f"Mode: {'dry-run' if job.dry_run else 'migration'}",
        f"Status: {job.get_status_display()}",
        f"Started: {job.started_at or '-'}",
        f"Finished: {job.finished_at or '-'}",
        "",
        f"Records inspected:   {job.records_inspected}",
        f"Already migrated:    {job.already_migrated}",
        f"Legacy unprefixed:   {job.legacy_unprefixed}",
        f"Schema-prefixed:     {job.schema_prefixed}",
        f"Missing objects:     {job.missing_objects}",
        f"Checksum conflicts:  {job.checksum_conflicts}",
        f"Ready for migration: {job.ready_for_migration}",
        f"Records migrated:    {job.records_migrated}",
        f"Records skipped:     {job.records_skipped}",
    ]
    if job.error_message:
        lines += ["", f"Error: {job.error_message}"]
    body = "\n".join(lines) + "\n"

    response = HttpResponse(body, content_type="text/plain")
    response["Content-Disposition"] = f'attachment; filename="migration-job-{job.id}.txt"'
    return response


# --- pipeline runs / continuation (tenant schema) ---------------------------


@login_required
def tenant_home(request):
    organization = request.tenant
    require_org_member(request.user, organization)
    pipelines = Pipeline.objects.order_by("name")
    return render(
        request, "opsui/tenant_home.html", {"pipelines": pipelines, "organization": organization}
    )


@login_required
def run_list(request, pipeline_id):
    organization = request.tenant
    require_org_member(request.user, organization)
    pipeline = get_object_or_404(Pipeline, pk=pipeline_id)
    runs = pipeline.runs.order_by("-created_at")[:50]
    return render(request, "opsui/run_list.html", {"pipeline": pipeline, "runs": runs})


@login_required
def run_detail(request, pipeline_id, run_id):
    organization = request.tenant
    membership = require_org_member(request.user, organization)
    pipeline = get_object_or_404(Pipeline, pk=pipeline_id)
    run = get_object_or_404(PipelineRun, pk=run_id, pipeline=pipeline)

    existing_continuation = PipelineRun.objects.filter(continued_from=run).first()
    can_continue = (
        is_continuable(run)
        and existing_continuation is None
        and (
            is_platform_operator(request.user)
            or (membership and membership.role in CONTINUATION_ROLES)
        )
    )
    return render(
        request,
        "opsui/run_detail.html",
        {
            "pipeline": pipeline,
            "run": run,
            "existing_continuation": existing_continuation,
            "can_continue": can_continue,
            "is_continuable": is_continuable(run),
        },
    )


@require_POST
@login_required
def run_continue(request, pipeline_id, run_id):
    organization = request.tenant
    require_org_role(request.user, organization, CONTINUATION_ROLES)
    pipeline = get_object_or_404(Pipeline, pk=pipeline_id)
    failed_run = get_object_or_404(PipelineRun, pk=run_id, pipeline=pipeline)

    existing_continuation = PipelineRun.objects.filter(continued_from=failed_run).first()
    if existing_continuation is not None:
        record_audit_event(
            actor=request.user,
            action="pipeline_run.continuation_reused",
            target=existing_continuation,
            metadata={
                "failed_run_id": str(failed_run.id),
                "existing_continuation_id": str(existing_continuation.id),
            },
        )
        messages.info(request, "A continuation run already exists for this failed run.")
        return redirect(
            "opsui_tenant:run_detail", pipeline_id=pipeline.id, run_id=existing_continuation.id
        )

    record_audit_event(
        actor=request.user,
        action="pipeline_run.continuation_requested",
        target=failed_run,
        metadata={"pipeline_id": str(pipeline.id), "failed_run_id": str(failed_run.id)},
    )

    try:
        new_run, _dispatched = trigger_manual_run(
            pipeline, schema_name=organization.schema_name, continue_from=failed_run
        )
    except ConfigurationError as exc:
        messages.error(request, str(exc))
        return redirect("opsui_tenant:run_detail", pipeline_id=pipeline.id, run_id=failed_run.id)

    record_audit_event(
        actor=request.user,
        action="pipeline_run.continuation_created",
        target=new_run,
        metadata={
            "pipeline_id": str(pipeline.id),
            "continued_from_run_id": str(failed_run.id),
            "new_run_id": str(new_run.id),
        },
    )
    messages.success(request, f"Continuation run {new_run.id} dispatched.")
    return redirect("opsui_tenant:run_detail", pipeline_id=pipeline.id, run_id=new_run.id)
