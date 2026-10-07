"""The read side the DCIM loaders share: authenticated GETs and paginated lists against one NetBox or Nautobot,
with typed errors (:mod:`.errors`) worded by :func:`railyard_sync.dcim_http.failure`.

The token is never logged, and never appears in an exception message or a loader's ``repr``. Pagination follows
only the *query* of a page's ``next`` link: requests always go to the configured URL, because behind a proxy
``next`` is often built with the wrong scheme or host, and the token must never be sent anywhere else.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Iterator
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from .. import dcim_http as http
from ..log import log_http, response_size
from .errors import DCIMAuthError, DCIMConnectionError, DCIMError, DCIMNotFoundError

#: Ids per request when filtering by a list of ids, keeping URLs short.
ID_CHUNK = 100

_KINDS = {
    http.AUTH: DCIMAuthError,
    http.PERMISSION: DCIMAuthError,
    http.NOT_FOUND: DCIMNotFoundError,
    http.SERVER: DCIMError,
    http.OTHER: DCIMError,
}


def chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def unique(items: Iterable[str | None]) -> list[str]:
    """``items`` without ``None`` and repeats, in first-seen order."""
    seen: dict[str, None] = {}
    for item in items:
        if item is not None:
            seen.setdefault(item, None)
    return list(seen)


class RESTReader:
    """Authenticated, paginated reads from one DCIM. Subclasses set ``product`` and may override
    :meth:`auth_header`; ``log`` is the logger HTTP exchanges are recorded on."""

    product: http.Product = http.NETBOX
    id_chunk = ID_CHUNK

    def __init__(
        self,
        url: str,
        token: str,
        *,
        session: http.Session | None = None,
        verify: bool | str = True,
        timeout: float = 30,
        page_size: int = 1000,
        log: logging.Logger | None = None,
    ) -> None:
        self.url = http.check_url(url, token, self.product)
        if page_size < 1:
            raise ValueError("page_size must be at least 1")
        self._token = token
        self._session = session or http.default_session()
        self._verify = verify
        self._timeout = timeout
        self._page_size = page_size
        self._log = log or logging.getLogger(__name__)
        self.version = ""

    def __repr__(self) -> str:
        return f"{type(self).__name__}(url={self.url!r})"

    def auth_header(self) -> str:
        return f"Token {self._token}"

    def _scrub(self, text: str) -> str:
        return http.scrub_token(self._token, text)

    def _get(self, path: str, params: list[tuple[str, Any]] | None = None) -> Any:
        name = self.product.name
        url = f"{self.url}{path}"
        headers = {"Authorization": self.auth_header(), "Accept": "application/json"}
        shown = path + (f"?{urlencode(params, doseq=True)}" if params else "")
        started = time.monotonic()
        try:
            resp = self._session.request(
                "GET", url, headers=headers, params=params, timeout=self._timeout, verify=self._verify
            )
        except http.connection_errors() as exc:
            log_http(self._log, name, "GET", shown, "no response", time.monotonic() - started)
            raise DCIMConnectionError(
                self._scrub(http.unreachable(self.product, self.url, exc)), method="GET", path=path
            ) from None
        status = resp.status_code
        rid = http.request_id(resp)
        log_http(
            self._log,
            name,
            "GET",
            shown,
            status,
            time.monotonic() - started,
            request_id=rid,
            received=response_size(resp),
        )
        context = {"status": status, "request_id": rid, "method": "GET", "path": path}
        if 200 <= status < 300:
            try:
                return resp.json()
            except ValueError:
                raise DCIMError(
                    f"{name} returned a response that is not JSON for {path}: is {self.url} a {name} server?",
                    **context,
                ) from None
        kind, message = http.failure(
            self.product, "GET", path, status, self._scrub(http.detail(resp, self.product)), rid
        )
        raise _KINDS[kind](message, **context)

    def _list(self, path: str, params: list[tuple[str, Any]] | None = None) -> Iterator[dict]:
        """Every object a list endpoint returns, following ``next`` page by page (its query only)."""
        query: list[tuple[str, Any]] = list(params or []) + [("limit", self._page_size), ("offset", 0)]
        while True:
            page = self._get(path, query)
            if not isinstance(page, dict) or not isinstance(page.get("results"), list):
                raise DCIMError(f"{self.product.name} returned an unexpected response for {path} (no 'results').")
            yield from page["results"]
            nxt = page.get("next")
            if not nxt or not page["results"]:
                return
            query = parse_qsl(urlsplit(nxt).query, keep_blank_values=True)

    def _list_by_ids(
        self, path: str, key: str, ids: list[str], extra: list[tuple[str, Any]] | None = None
    ) -> Iterator[dict]:
        """``_list`` filtered by a list of ids, a chunk of ids per request."""
        for chunk in chunks(ids, self.id_chunk):
            yield from self._list(path, [(key, i) for i in chunk] + list(extra or []))
