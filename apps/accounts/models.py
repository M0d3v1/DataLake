from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Platform user.

    Lives in the public (shared) schema: a user must be resolvable before
    django-tenants knows which tenant schema to route into, so identity
    cannot itself be tenant-scoped. Which organizations a user belongs to,
    and with what role, is expressed by `apps.orgs.models.Membership`.
    """

    email = models.EmailField(unique=True)

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = ["email"]
