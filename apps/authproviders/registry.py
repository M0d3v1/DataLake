from apps.authproviders.base import AuthProvider

_REGISTRY: dict[str, type[AuthProvider]] = {}


def register(provider_cls: type[AuthProvider]) -> type[AuthProvider]:
    """Class decorator: register an AuthProvider under its `type_key`."""
    if not getattr(provider_cls, "type_key", None):
        raise ValueError(f"{provider_cls!r} must define a non-empty type_key")
    _REGISTRY[provider_cls.type_key] = provider_cls
    return provider_cls


def get_auth_provider(type_key: str) -> AuthProvider:
    try:
        provider_cls = _REGISTRY[type_key]
    except KeyError as exc:
        raise KeyError(
            f"No AuthProvider registered for type_key={type_key!r}. "
            f"Known types: {sorted(_REGISTRY)}"
        ) from exc
    return provider_cls()


def available_auth_provider_types() -> list[str]:
    return sorted(_REGISTRY)
