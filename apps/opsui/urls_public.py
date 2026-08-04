"""Public-schema URLs: the platform-operator-facing raw payload
migration tool. See config/urls_public.py and
docs/decisions/0008-internal-operator-ui.md."""

from django.urls import path

from apps.opsui import views

app_name = "opsui"

urlpatterns = [
    path("", views.org_list, name="org_list"),
    path("orgs/<int:org_id>/", views.migration_org_detail, name="migration_org_detail"),
    path("orgs/<int:org_id>/start/", views.migration_start, name="migration_start"),
    path("jobs/<uuid:job_id>/", views.migration_job_detail, name="migration_job_detail"),
    path(
        "jobs/<uuid:job_id>/progress/", views.migration_job_progress, name="migration_job_progress"
    ),
    path("jobs/<uuid:job_id>/retry/", views.migration_job_retry, name="migration_job_retry"),
    path("jobs/<uuid:job_id>/cancel/", views.migration_job_cancel, name="migration_job_cancel"),
    path("jobs/<uuid:job_id>/export/", views.migration_job_export, name="migration_job_export"),
]
