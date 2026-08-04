"""Minimal REST API source connector.

Pulls a JSON list from a single endpoint using page-number pagination,
with authentication applied via the `apps.authproviders` registry.
Cursor-based pagination, custom query bodies, and richer parameter
configuration are Milestone 2 work.
"""

from collections.abc import Iterator
from typing import Any

import httpx

from apps.authproviders.registry import get_auth_provider
from apps.connectors.base import FetchResult, SourceCapabilities, SourceConnector
from apps.connectors.registry import register_source
from apps.core.exceptions import ConnectionTestFailed, FetchFailed


@register_source
class RestApiSourceConnector(SourceConnector):
    """Config:
      - base_url (str): e.g. "https://api.example-insurer.test"
      - path (str): e.g. "/v1/policies"
      - records_key (str, default "results"): top-level JSON key holding
        the list of records in each response.
      - page_param (str, default "page")
      - page_size_param (str, default "page_size")
      - page_size (int, optional)
      - auth_provider_type (str, optional): registry key from
        apps.authproviders, e.g. "bearer".
      - timeout_seconds (float, default 30)

    `credential` is passed through to the configured AuthProvider as-is.
    """

    type_key = "rest_api"
    capabilities = SourceCapabilities(supports_incremental=False, supports_pagination=True)

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.config["base_url"],
            timeout=self.config.get("timeout_seconds", 30),
        )

    def _build_request(
        self, client: httpx.Client, credential: dict[str, Any], page: int
    ) -> httpx.Request:
        params = {self.config.get("page_param", "page"): page}
        if "page_size" in self.config:
            params[self.config.get("page_size_param", "page_size")] = self.config["page_size"]
        request = client.build_request("GET", self.config["path"], params=params)

        auth_provider_type = self.config.get("auth_provider_type")
        if auth_provider_type:
            provider = get_auth_provider(auth_provider_type)
            request = provider.prepare_request(request, credential or {})
        return request

    def test_connection(self, credential: dict[str, Any]) -> None:
        try:
            with self._client() as client:
                request = self._build_request(client, credential, page=1)
                response = client.send(request)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ConnectionTestFailed(str(exc)) from exc

    def fetch(
        self, credential: dict[str, Any], cursor: str | None = None
    ) -> Iterator[FetchResult]:
        page = int(cursor) if cursor else 1
        records_key = self.config.get("records_key", "results")

        with self._client() as client:
            while True:
                request = self._build_request(client, credential, page=page)
                try:
                    response = client.send(request)
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    raise FetchFailed(str(exc)) from exc

                data = response.json()
                records = data.get(records_key, []) if isinstance(data, dict) else data

                yield FetchResult(
                    records=records,
                    next_cursor=str(page + 1) if records else None,
                    raw_payload=response.content,
                    raw_content_type=response.headers.get("content-type", "application/json"),
                )

                if not records:
                    break
                page += 1
