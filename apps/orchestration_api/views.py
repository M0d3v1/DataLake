"""The internal HTTP API Airflow (and only Airflow, today) uses to
discover pipelines and drive runs -- see
docs/decisions/0010-airflow-orchestration.md for why this exists and why
it's the *only* way an external orchestrator touches this platform (no
direct DB access, no Docker-socket exec).

Every view here does the same three things, in order: check the bearer
token, resolve `schema_name` against a real `Organization` (never trust
it blindly, even from an authenticated caller -- defense in depth, same
posture as apps.opsui), then enter that tenant's schema for the actual
work. Responses only ever contain safe identifiers/status fields -- the
same discipline `manage.py run_pipeline` and the operator UI already
follow.
"""

import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django_tenants.utils import schema_context

from apps.core.exceptions import ConfigurationError
from apps.execution.dispatch import trigger_manual_run
from apps.execution.models import PipelineRun
from apps.orchestration_api.authentication import check_orchestration_token
from apps.orgs.models import Organization
from apps.pipelines.models import Pipeline


def _resolve_organization(schema_name: str | None) -> Organization | None:
    if not schema_name:
        return None
    return Organization.objects.filter(schema_name=schema_name).first()


@require_http_methods(["GET"])
def list_pipelines(request):
    """Every active Pipeline across every tenant -- what the Airflow DAG
    factory polls at parse time to decide which DAGs to build. Read-only,
    no side effects."""
    auth_error = check_orchestration_token(request)
    if auth_error is not None:
        return auth_error

    pipelines = []
    for organization in Organization.objects.exclude(schema_name="public"):
        with schema_context(organization.schema_name):
            for pipeline in Pipeline.objects.filter(is_active=True).only(
                "id", "name", "schedule_cron"
            ):
                pipelines.append(
                    {
                        "schema_name": organization.schema_name,
                        "pipeline_id": str(pipeline.id),
                        "name": pipeline.name,
                        "schedule_cron": pipeline.schedule_cron,
                    }
                )
    return JsonResponse({"pipelines": pipelines})


@csrf_exempt
@require_http_methods(["POST"])
def trigger_pipeline_run(request, pipeline_id):
    """Dispatch a run for `pipeline_id`. Wraps
    `apps.execution.dispatch.trigger_manual_run` -- the exact function
    the web UI and `manage.py run_pipeline` call, so Airflow is a third
    caller of one dispatch path, never a second implementation of it."""
    auth_error = check_orchestration_token(request)
    if auth_error is not None:
        return auth_error

    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "malformed JSON body"}, status=400)

    organization = _resolve_organization(payload.get("schema_name"))
    if organization is None:
        return JsonResponse({"error": "unknown schema_name"}, status=404)

    continue_from_run_id = payload.get("continue_from_run_id")

    with schema_context(organization.schema_name):
        pipeline = Pipeline.objects.filter(pk=pipeline_id).first()
        if pipeline is None:
            return JsonResponse({"error": "unknown pipeline_id for this schema_name"}, status=404)

        continue_from = None
        if continue_from_run_id:
            continue_from = PipelineRun.objects.filter(pk=continue_from_run_id).first()
            if continue_from is None:
                return JsonResponse({"error": "unknown continue_from_run_id"}, status=404)

        try:
            run, dispatched = trigger_manual_run(
                pipeline, schema_name=organization.schema_name, continue_from=continue_from
            )
        except ConfigurationError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

    return JsonResponse({"run_id": str(run.id), "dispatched": dispatched}, status=201)


@require_http_methods(["GET"])
def run_status(request, run_id):
    """Safe fields only, for an Airflow sensor to poll until terminal."""
    auth_error = check_orchestration_token(request)
    if auth_error is not None:
        return auth_error

    organization = _resolve_organization(request.GET.get("schema_name"))
    if organization is None:
        return JsonResponse({"error": "unknown schema_name"}, status=404)

    with schema_context(organization.schema_name):
        run = PipelineRun.objects.filter(pk=run_id).first()
        if run is None:
            return JsonResponse({"error": "unknown run_id for this schema_name"}, status=404)

        return JsonResponse(
            {
                "run_id": str(run.id),
                "status": run.status,
                "error_category": run.error_category,
                "error_is_retryable": run.error_is_retryable,
                "is_terminal": run.status
                in (PipelineRun.Status.SUCCEEDED, PipelineRun.Status.FAILED),
            }
        )
