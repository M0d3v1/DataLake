from django.db import models


class TimeStampedModel(models.Model):
    """Abstract base adding created/updated timestamps.

    Used by both shared (public-schema) and tenant-scoped models; being
    abstract, it contributes no table or migration of its own.
    """

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
