from django.urls import path

from apps.orchestration_api import views

app_name = "orchestration_api"

urlpatterns = [
    path("pipelines/", views.list_pipelines, name="list_pipelines"),
    path(
        "pipelines/<uuid:pipeline_id>/trigger/",
        views.trigger_pipeline_run,
        name="trigger_pipeline_run",
    ),
    path("runs/<uuid:run_id>/status/", views.run_status, name="run_status"),
]
