"""Bearer-token authentication for the internal orchestration API.

Deliberately not Django session/CSRF auth -- every caller here is a
machine (Airflow), not a browser with a logged-in user. A single static
token (`settings.ORCHESTRATION_API_TOKEN`) is compared using a
constant-time check. See docs/decisions/0010-airflow-orchestration.md
for why this surface exists and its known limitation (one shared token,
not per-tenant credentials).
"""

import hmac

from django.conf import settings
from django.http import JsonResponse


def check_orchestration_token(request) -> JsonResponse | None:
    """Return a 401 JsonResponse if the request isn't authorized,
    otherwise None. Call at the top of every view in this app."""
    configured_token = getattr(settings, "ORCHESTRATION_API_TOKEN", "")
    if not configured_token:
        return JsonResponse(
            {"error": "orchestration API is not configured on this deployment"}, status=503
        )

    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        return JsonResponse({"error": "missing or malformed Authorization header"}, status=401)

    provided_token = header[len(prefix) :]
    if not hmac.compare_digest(provided_token, configured_token):
        return JsonResponse({"error": "invalid token"}, status=401)

    return None
