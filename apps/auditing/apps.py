from django.apps import AppConfig


class AuditingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.auditing"
    label = "auditing"
