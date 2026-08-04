from django.apps import AppConfig


class AuthProvidersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.authproviders"
    label = "authproviders"

    def ready(self) -> None:
        # Importing this subpackage registers all built-in providers
        # (decorated with @register) with the AuthProvider registry.
        import apps.authproviders.providers  # noqa: F401
