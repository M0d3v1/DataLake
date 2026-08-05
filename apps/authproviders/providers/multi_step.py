from typing import Any

import httpx

from apps.authproviders.registry import register
from apps.authproviders.token_support import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_MULTI_STEPS,
    CachedToken,
    TokenCachingAuthProvider,
    extract_expires_at,
    extract_value,
    substitute_placeholders,
    validate_extract_config,
    validate_method,
    validate_timeout,
    validate_url,
)
from apps.core.exceptions import AuthenticationError, ConfigurationError
from apps.core.outbound_http import build_client, default_timeout, guarded_send


@register
class MultiStepTokenAuthProvider(TokenCachingAuthProvider):
    """Multi-step token acquisition (e.g. obtain -> exchange -> refresh),
    as a small, bounded, declarative sequence of HTTP requests -- not
    arbitrary code. Each step may reference values produced by earlier
    steps or by the credential itself via `{{steps.<name>}}` /
    `{{credential.<field>}}` placeholders (plain string substitution, no
    template execution -- see apps.authproviders.token_support).

    Expected `credential` fields:
      - steps (list, required, 1..5 entries): each a dict with
          - url (str, required): absolute http(s) URL, may contain placeholders.
          - method (str, default "POST")
          - headers (dict, optional): may contain placeholders.
          - json_body (dict, optional): may contain placeholders.
          - extract (dict, optional): {"as": "<name>", "from": "json"|"header",
            "field"/"header": ...} -- names the value for later steps and
            for `final_token_from`.
      - final_token_from (str, required): the `extract.as` name (from any
        step) whose value becomes the acquired token.
      - expires_in_field (str, optional): looked up on the *last* step's
        response.
      - inject_header (str, default "Authorization")
      - inject_prefix (str, default "Bearer ")
      - timeout_seconds (float, default 30, max 120), applied per step.
    """

    type_key = "multi_step_token"

    def _validate_steps(self, credential: dict[str, Any]) -> list[dict[str, Any]]:
        steps = credential.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ConfigurationError("credential.steps must be a non-empty list")
        if len(steps) > MAX_MULTI_STEPS:
            raise ConfigurationError(
                f"credential.steps has {len(steps)} entries, exceeding the maximum of "
                f"{MAX_MULTI_STEPS}"
            )
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ConfigurationError(f"credential.steps[{i}] must be an object")
            validate_url(step.get("url"), label=f"credential.steps[{i}].url")
            validate_method(step.get("method", "POST"), label=f"credential.steps[{i}].method")
            if "extract" in step:
                validate_extract_config(step["extract"], label=f"credential.steps[{i}].extract")
                if not step["extract"].get("as"):
                    raise ConfigurationError(f"credential.steps[{i}].extract.as is required")
        if not credential.get("final_token_from"):
            raise ConfigurationError("credential.final_token_from is required")
        return steps

    def _acquire(self, credential: dict[str, Any]) -> CachedToken:
        steps = self._validate_steps(credential)
        read_timeout = validate_timeout(
            credential.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
            label="credential.timeout_seconds",
        )
        base_timeout = default_timeout()
        timeout = httpx.Timeout(
            connect=base_timeout.connect,
            read=read_timeout,
            write=base_timeout.write,
            pool=base_timeout.pool,
        )
        context: dict[str, dict[str, Any]] = {"credential": credential, "steps": {}}
        final_body: bytes | None = None

        with build_client(timeout=timeout) as client:
            for i, step in enumerate(steps):
                method = validate_method(
                    step.get("method", "POST"), label=f"credential.steps[{i}].method"
                )
                url = substitute_placeholders(step["url"], context)
                headers = substitute_placeholders(step.get("headers", {}), context)
                body = step.get("json_body")
                if body is not None:
                    body = substitute_placeholders(body, context)

                request_kwargs: dict[str, Any] = {"headers": headers}
                if method == "POST" and body is not None:
                    request_kwargs["json"] = body

                try:
                    request = client.build_request(method, url, **request_kwargs)
                    response, response_body = guarded_send(client, request)
                except httpx.HTTPError as exc:
                    raise AuthenticationError(
                        f"multi-step auth step {i} request failed: {exc}"
                    ) from exc

                if response.status_code >= 400:
                    raise AuthenticationError(
                        f"multi-step auth step {i} returned HTTP {response.status_code}"
                    )

                if "extract" in step:
                    value = extract_value(response, response_body, step["extract"])
                    context["steps"][step["extract"]["as"]] = value

                final_body = response_body

        final_token_from = credential["final_token_from"]
        token_value = context["steps"].get(final_token_from)
        if token_value is None:
            raise AuthenticationError(
                f"multi-step auth: {final_token_from!r} was never produced by any step"
            )

        expires_at = (
            extract_expires_at(final_body, credential.get("expires_in_field"))
            if final_body is not None
            else None
        )
        return CachedToken(value=token_value, expires_at=expires_at)
