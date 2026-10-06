"""``RailyardClient`` — a thin client for the Railyard REST API.

Auth mirrors the ``railyard-mcp`` server exactly: a personal access token (``ry_…``) sent as
``Authorization: Bearer <token>``, and the target organisation selected with the ``X-Org-Id``
header (its value is the org **id**). The endpoints used:

    GET /api/orgs                     -> [{id, name, slug, role, personal, ...}]
    GET /api/projects                 -> [{id, name, slug, updatedAt}]        (needs X-Org-Id)
    GET /api/projects/{id-or-slug}    -> the full Project JSON document        (needs X-Org-Id)

A ``session`` (anything exposing ``request(method, url, headers=, params=, timeout=)`` and
returning an object with ``status_code``/``json()``/``text``, i.e. a ``requests.Session``) can be
injected, which is how the tests avoid real network access.
"""

from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import quote

from .errors import (
    RailyardAPIError,
    RailyardForbiddenError,
    RailyardNotFoundError,
    RailyardTokenRejectedError,
)

TOKEN_PREFIX = "ry_"


class _Response(Protocol):  # the subset of requests.Response we rely on
    status_code: int

    @property
    def text(self) -> str: ...

    def json(self) -> Any: ...


class _Session(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> _Response: ...


def _default_session() -> _Session:
    import requests  # imported lazily so the package imports without requests when a session is injected

    return requests.Session()


class RailyardClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        org: str | None = None,
        session: _Session | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not token:
            raise ValueError("token is required")
        if not token.startswith(TOKEN_PREFIX):
            # Not fatal — the server is the authority — but almost always a mistake worth surfacing.
            raise ValueError(f"Railyard token should start with {TOKEN_PREFIX!r}")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._session = session or _default_session()
        self._timeout = timeout
        # A caller-supplied default org reference (id, slug or name); resolved lazily to an id.
        self._org_ref = org
        self._org_id: str | None = None

    # -- low level ----------------------------------------------------------

    def _headers(self, org_id: str | None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        if org_id:
            headers["X-Org-Id"] = org_id
        return headers

    def _get(self, path: str, *, org_id: str | None = None, params: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        resp = self._session.request("GET", url, headers=self._headers(org_id), params=params, timeout=self._timeout)
        status = resp.status_code
        if status == 401:
            raise RailyardTokenRejectedError(
                "Railyard rejected the personal access token (HTTP 401): it is wrong, revoked or expired. "
                "Railyard tokens expire, so create a new token in Railyard (User settings → Personal access "
                "tokens) and update the plugin's railyard_token / railyard_token_file setting.",
                status=status,
            )
        if status == 403:
            raise RailyardForbiddenError(
                f"Railyard refused access (HTTP 403) for {path}: the token is valid, but its user may not "
                "read this organisation or project. Add that user to the organisation in Railyard.",
                status=status,
            )
        if status == 404:
            raise RailyardNotFoundError(f"Not found (HTTP 404): {path}", status=status)
        if status < 200 or status >= 300:
            body = ""
            try:
                body = resp.text[:400]
            except Exception:  # pragma: no cover - defensive
                pass
            raise RailyardAPIError(f"Railyard API error (HTTP {status}) for {path}: {body}", status=status)
        return resp.json()

    # -- orgs ---------------------------------------------------------------

    def whoami(self) -> dict:
        return self._get("/api/me")

    def list_orgs(self) -> list[dict]:
        return list(self._get("/api/orgs") or [])

    def find_org(self, ref: str) -> dict:
        """The org record matching an id, slug or name (case-insensitive). Ids and slugs are checked
        before names, so a name that happens to equal another org's slug can't shadow it."""
        needle = (ref or "").strip().lower()
        if not needle:
            raise ValueError("org ref is required")
        orgs = self.list_orgs()
        for key in ("id", "slug", "name"):
            for org in orgs:
                if str(org.get(key, "")).lower() == needle:
                    return org
        raise RailyardNotFoundError(f"No org matched {ref!r} (by id, slug or name).")

    def resolve_org(self, ref: str | None) -> str | None:
        """Resolve an org id/slug/name to its id. ``None`` ref → ``None`` (the server then defaults
        to the caller's personal org)."""
        if ref is None or ref == "":
            return None
        return str(self.find_org(ref)["id"])

    def org_id(self) -> str | None:
        """The resolved id of the client's default org (memoised)."""
        if self._org_id is None and self._org_ref is not None:
            self._org_id = self.resolve_org(self._org_ref)
        return self._org_id

    # -- projects -----------------------------------------------------------

    def list_projects(self, org_id: str | None = None) -> list[dict]:
        return list(self._get("/api/projects", org_id=org_id or self.org_id()) or [])

    def get_project(self, ref: str, org_id: str | None = None) -> dict:
        """Fetch a project's FULL JSON document by id or URL slug."""
        if not ref:
            raise ValueError("project ref is required")
        # Escape the ref as a single path segment so it can't address another endpoint.
        return self._get(f"/api/projects/{quote(str(ref), safe='')}", org_id=org_id or self.org_id())
