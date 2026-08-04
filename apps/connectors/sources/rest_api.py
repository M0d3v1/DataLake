"""REST API source connector.

Pulls records from a single JSON endpoint using page-number pagination,
with authentication applied via the `apps.authproviders` registry. Not a
general workflow engine: one method, one URL, static params/headers/body,
one nested records path, page-number pagination only. Cursor-based
pagination and multi-endpoint workflows are out of scope.
"""

from collections.abc import Iterator
from typing import Any

import httpx

from apps.authproviders.base import AuthProvider
from apps.authproviders.registry import get_auth_provider
from apps.connectors.base import FetchResult, SourceCapabilities, SourceConnector
from apps.connectors.registry import register_source
from apps.core.exceptions import (
    ConfigurationError,
    ConnectionTestFailed,
    DataLakeError,
    FetchFailed,
    MalformedResponseError,
)
from apps.core.logging import get_logger
from apps.core.paths import get_by_path

log = get_logger(__name__)

_ALLOWED_METHODS = {"GET", "POST"}
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_PAGES = 500
_RETRYABLE_STATUS_CODES = {429}


@register_source
class RestApiSourceConnector(SourceConnector):
    """Config:
      - base_url (str), path (str): e.g. "https://api.example-insurer.test", "/v1/policies"
      - method (str, default "GET"): "GET" or "POST"
      - query_params (dict, optional): static, non-secret query parameters
      - headers (dict, optional): static, non-secret request headers
      - json_body (dict, optional): request body for POST
      - records_path (str, optional): dotted JSON path to the records list
        in each response, e.g. "data.items". Falls back to `records_key`
        (a bare top-level key, M1-compatible) then "results".
      - page_param (str, default "page"), page_size_param (str, default "page_size")
      - page_size (int, optional): when set, a page returning fewer
        records than this is treated as the last page.
      - start_page (int, default 1)
      - max_pages (int, default 500): hard safety cap on pages per fetch() call.
      - retain_empty_terminal_page (bool, default False): store the final
        confirmatory empty page for audit completeness instead of
        silently stopping. Off by default to avoid a wasted request being
        treated as meaningful.
      - auth_provider_type (str, optional): registry key from apps.authproviders.
      - timeout_seconds (float, default 30)

    `credential` is passed through to the configured AuthProvider as-is.
    """

    type_key = "rest_api"
    capabilities = SourceCapabilities(supports_incremental=False, supports_pagination=True)

    def _validate_config(self) -> None:
        if not self.config.get("base_url"):
            raise ConfigurationError("rest_api source requires 'base_url'")
        if not self.config.get("path"):
            raise ConfigurationError("rest_api source requires 'path'")

        method = self.config.get("method", "GET").upper()
        if method not in _ALLOWED_METHODS:
            raise ConfigurationError(
                f"rest_api source: unsupported method {method!r} "
                f"(allowed: {sorted(_ALLOWED_METHODS)})"
            )

        max_pages = self.config.get("max_pages", DEFAULT_MAX_PAGES)
        if not isinstance(max_pages, int) or isinstance(max_pages, bool) or max_pages < 1:
            raise ConfigurationError("rest_api source: 'max_pages' must be a positive integer")

        start_page = self.config.get("start_page", 1)
        if not isinstance(start_page, int) or isinstance(start_page, bool) or start_page < 1:
            raise ConfigurationError("rest_api source: 'start_page' must be a positive integer")

        timeout = self.config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        if not isinstance(timeout, int | float) or isinstance(timeout, bool) or timeout <= 0:
            raise ConfigurationError("rest_api source: 'timeout_seconds' must be a positive number")

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.config["base_url"],
            timeout=self.config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
        )

    def _resolve_auth_provider(self) -> AuthProvider | None:
        auth_provider_type = self.config.get("auth_provider_type")
        return get_auth_provider(auth_provider_type) if auth_provider_type else None

    def _build_request(
        self,
        client: httpx.Client,
        provider: AuthProvider | None,
        credential: dict[str, Any],
        page: int,
    ) -> httpx.Request:
        params = dict(self.config.get("query_params", {}))
        params[self.config.get("page_param", "page")] = page
        if "page_size" in self.config:
            params[self.config.get("page_size_param", "page_size")] = self.config["page_size"]

        headers = dict(self.config.get("headers", {}))
        method = self.config.get("method", "GET").upper()
        request_kwargs: dict[str, Any] = {"params": params, "headers": headers}
        if method == "POST" and self.config.get("json_body") is not None:
            request_kwargs["json"] = self.config["json_body"]

        request = client.build_request(method, self.config["path"], **request_kwargs)
        if provider is not None:
            request = provider.prepare_request(request, credential or {})
        return request

    def _send_with_auth_retry(
        self,
        client: httpx.Client,
        provider: AuthProvider | None,
        credential: dict[str, Any],
        page: int,
    ) -> httpx.Response:
        request = self._build_request(client, provider, credential, page)
        response = self._send(client, request)

        if response.status_code in (401, 403) and provider is not None:
            log.warning(
                "rest_api_source.reauthenticating", status_code=response.status_code, page=page
            )
            provider.invalidate(credential or {})
            retry_request = self._build_request(client, provider, credential, page)
            response = self._send(client, retry_request)

        if response.status_code in _RETRYABLE_STATUS_CODES or 500 <= response.status_code < 600:
            raise FetchFailed(f"source returned HTTP {response.status_code}", retryable=True)
        if response.status_code >= 400:
            raise FetchFailed(f"source returned HTTP {response.status_code}", retryable=False)
        return response

    def _send(self, client: httpx.Client, request: httpx.Request) -> httpx.Response:
        try:
            return client.send(request)
        except httpx.TimeoutException as exc:
            raise FetchFailed(f"timeout calling source: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise FetchFailed(f"network error calling source: {exc}", retryable=True) from exc

    def _parse_json(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise MalformedResponseError(f"source returned invalid JSON: {exc}") from exc

    def _extract_records(self, data: Any, path: str) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            raise MalformedResponseError(
                f"expected a JSON object or array in the response, got {type(data).__name__}"
            )
        value, found = get_by_path(data, path)
        if not found:
            raise MalformedResponseError(f"response is missing the expected records field {path!r}")
        if not isinstance(value, list):
            raise MalformedResponseError(f"records field {path!r} did not contain a list")
        return value

    def _records_path(self) -> str:
        return self.config.get("records_path") or self.config.get("records_key", "results")

    def test_connection(self, credential: dict[str, Any]) -> None:
        self._validate_config()
        provider = self._resolve_auth_provider()
        try:
            with self._client() as client:
                self._send_with_auth_retry(
                    client, provider, credential, self.config.get("start_page", 1)
                )
        except DataLakeError as exc:
            raise ConnectionTestFailed(str(exc)) from exc

    def fetch(self, credential: dict[str, Any], cursor: str | None = None) -> Iterator[FetchResult]:
        self._validate_config()
        page = int(cursor) if cursor else self.config.get("start_page", 1)
        max_pages = self.config.get("max_pages", DEFAULT_MAX_PAGES)
        retain_empty = self.config.get("retain_empty_terminal_page", False)
        records_path = self._records_path()
        provider = self._resolve_auth_provider()

        pages_yielded = 0
        with self._client() as client:
            while True:
                response = self._send_with_auth_retry(client, provider, credential, page)
                data = self._parse_json(response)
                records = self._extract_records(data, records_path)
                content_type = response.headers.get("content-type", "application/json")

                if not records:
                    if retain_empty:
                        yield FetchResult(
                            records=[],
                            next_cursor=None,
                            raw_payload=response.content,
                            raw_content_type=content_type,
                            cursor_used=str(page),
                            source_path=self.config["path"],
                            http_status=response.status_code,
                        )
                    break

                page_size = self.config.get("page_size")
                is_last_page = page_size is not None and len(records) < page_size
                next_cursor = None if is_last_page else str(page + 1)

                yield FetchResult(
                    records=records,
                    next_cursor=next_cursor,
                    raw_payload=response.content,
                    raw_content_type=content_type,
                    cursor_used=str(page),
                    source_path=self.config["path"],
                    http_status=response.status_code,
                )
                pages_yielded += 1

                if is_last_page:
                    break
                if pages_yielded >= max_pages:
                    log.warning(
                        "rest_api_source.max_pages_reached", max_pages=max_pages, last_page=page
                    )
                    break
                page += 1
