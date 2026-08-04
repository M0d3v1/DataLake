"""Importing this package registers all built-in AuthProvider implementations."""

from apps.authproviders.providers import api_key, basic, bearer, multi_step, token_endpoint

__all__ = ["api_key", "basic", "bearer", "token_endpoint", "multi_step"]
