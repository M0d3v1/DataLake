from django.apps import AppConfig


class ConnectorsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.connectors"
    label = "connectors"

    def ready(self) -> None:
        # Importing these subpackages registers all built-in connectors
        # (decorated with @register_source / @register_destination).
        import apps.connectors.destinations  # noqa: F401
        import apps.connectors.sources  # noqa: F401
