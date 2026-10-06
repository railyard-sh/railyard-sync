"""``RailyardClient`` — a thin client for the Railyard REST API.

Auth mirrors the ``railyard-mcp`` server exactly: a personal access token (``ry_…``) sent as
``Authorization: Bearer <token>``, and the target organisation selected with the ``X-Org-Id``
header (its value is the org **id**). The endpoints used:

    GET /api/orgs                     -> [{id, name, slug, role, personal, ...}]
    GET /api/projects                 -> [{id, name, slug, updatedAt}]        (needs X-Org-Id)
    GET /api/projects/{id-or-slug}    -> the full Project JSON document        (needs X-Org-Id)
                                         ETag: the document's revision, as a quoted integer
    PUT /api/projects/{id}            -> create (no If-Match) or replace (If-Match: "<revision>")
                                         -> {id, slug, orgId}; ETag: the revision written
    POST /api/projects/{id}/versions  -> a named version of the current revision
                                         body {title, expectedCurrentRevision}
    POST /api/projects/{id}/deliverables/{kind}
                                      -> a deliverable generated from the stored design (JSON for
                                         ``netbox-sync``); body {changeRequestId?, options?}

Failures map to the typed errors in :mod:`railyard_sync.errors`, keyed on the HTTP status and, where
the server sends one, its stable ``code`` (``plan_limit``, ``plan_required``, ``name_taken``,
``project_id_taken``, ``terms_not_accepted``…). Every 402 is parsed in one place (:func:`_plan_error`) into
the :class:`~railyard_sync.errors.RailyardPlanError` family, whether a save went past the rack cap or a paid
deliverable was refused. The token never appears in an error message.

A ``session`` (anything exposing ``request(method, url, headers=, params=, timeout=, json=)`` and
returning an object with ``status_code``/``json()``/``text``/``headers``, i.e. a ``requests.Session``) can be
injected, which is how the tests avoid real network access.
"""

from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import quote

from .errors import (
    RailyardAPIError,
    RailyardConflictError,
    RailyardForbiddenError,
    RailyardNotFoundError,
    RailyardPlanError,
    RailyardPlanLimitError,
    RailyardPlanRequiredError,
    RailyardPreconditionError,
    RailyardTermsError,
    RailyardTokenRejectedError,
    RailyardTooLargeError,
)

TOKEN_PREFIX = "ry_"
NETBOX_SYNC = "netbox-sync"  # the deliverable kind of the NetBox sync document


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

    def _send(
        self,
        method: str,
        path: str,
        *,
        org_id: str | None = None,
        params: dict | None = None,
        body: Any = None,
        extra_headers: dict[str, str] | None = None,
    ) -> _Response:
        """Send one request and return the response, raising the typed error for any non-2xx status."""
        url = f"{self.base_url}{path}"
        headers = self._headers(org_id)
        if extra_headers:
            headers.update(extra_headers)
        kwargs: dict[str, Any] = {"headers": headers, "params": params, "timeout": self._timeout}
        if body is not None:
            headers["Content-Type"] = "application/json"
            kwargs["json"] = body
        resp = self._session.request(method, url, **kwargs)
        if not 200 <= resp.status_code < 300:
            raise self._error(method, path, resp)
        return resp

    def _get(self, path: str, *, org_id: str | None = None, params: dict | None = None) -> Any:
        return self._send("GET", path, org_id=org_id, params=params).json()

    def _scrub(self, text: str) -> str:
        """Never let the token reach a message, whatever a server or proxy echoed back."""
        return text.replace(self._token, "[token]") if self._token else text

    def _error(self, method: str, path: str, resp: _Response) -> RailyardAPIError:
        """The typed error for a failed response. Messages carry the server's ``error`` text (its
        stable ``code`` picks the class) and never the token."""
        status = resp.status_code
        payload = _json_body(resp)
        code = str(payload.get("code") or "")
        said = self._scrub(str(payload.get("error") or "")).strip()
        if not said and not payload:
            try:
                said = self._scrub(resp.text[:400]).strip()
            except Exception:  # pragma: no cover - defensive
                said = ""
        suffix = f": {said}" if said else ""
        writing = method != "GET"

        if status == 401:
            return RailyardTokenRejectedError(
                "Railyard rejected the personal access token (HTTP 401): it is wrong, revoked or expired. "
                "Railyard tokens expire, so create a new token in Railyard (User settings → Personal access "
                "tokens) and update the plugin's railyard_token / railyard_token_file setting.",
                status=status,
            )
        if status == 403 and code == "terms_not_accepted":
            return RailyardTermsError(
                "Railyard refused the change (HTTP 403): the token's user has not accepted the current Terms of "
                "Service. Sign in to Railyard in a browser, accept them, then try again.",
                status=status,
            )
        if status == 403:
            action = "change" if writing else "read"
            return RailyardForbiddenError(
                f"Railyard refused access (HTTP 403) for {path}: the token is valid, but its user may not "
                f"{action} this organisation or project. Add that user to the organisation in Railyard"
                + (" with an editor role" if writing else "")
                + "."
                + (f" Railyard said: {said}" if said else ""),
                status=status,
            )
        if status == 404:
            return RailyardNotFoundError(f"Not found (HTTP 404): {path}{suffix}", status=status)
        if status == 402:
            return _plan_error(payload, said)
        if status == 409:
            return RailyardConflictError(f"Railyard refused the change (HTTP 409){suffix}", code=code)
        if status in (412, 428):
            why = "the project changed since it was read" if status == 412 else "the save did not say which revision"
            return RailyardPreconditionError(
                f"Railyard refused the save (HTTP {status}): {why}. Read the project again and retry."
                + (f" Railyard said: {said}" if said else ""),
                status=status,
            )
        if status == 413:
            return RailyardTooLargeError(
                f"Railyard refused the document (HTTP 413): it is too large{suffix}",
                limit=_int_or_none(payload.get("limit")),
                size=_int_or_none(payload.get("size")),
            )
        return RailyardAPIError(f"Railyard API error (HTTP {status}) for {path}{suffix}", status=status)

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

    def get_project_with_revision(self, ref: str, org_id: str | None = None) -> tuple[dict, int]:
        """A project's full document and the revision it is at (its ETag), for a later
        :meth:`put_project` with ``if_match``."""
        if not ref:
            raise ValueError("project ref is required")
        resp = self._send("GET", f"/api/projects/{quote(str(ref), safe='')}", org_id=org_id or self.org_id())
        return resp.json(), _revision(resp)

    def put_project(self, project: dict, *, if_match: int | None, org_id: str | None = None) -> int:
        """Save a full project document under its own ``id`` and return the revision written.

        ``if_match=None`` creates a new project (the server refuses it with 428 when the id is already a
        project, and with 409 ``project_id_taken`` when another organisation holds it); an integer
        replaces the project at exactly that revision (412 when it has moved on)."""
        project_id = str(project.get("id") or "")
        if not project_id:
            raise ValueError("the project document needs an id")
        extra = {"If-Match": f'"{int(if_match)}"'} if if_match is not None else None
        resp = self._send(
            "PUT",
            f"/api/projects/{quote(project_id, safe='')}",
            org_id=org_id or self.org_id(),
            body=project,
            extra_headers=extra,
        )
        return _revision(resp)

    def name_version(self, project_id: str, title: str, expected_revision: int, org_id: str | None = None) -> dict:
        """Record the project's current revision as a named version (``POST …/versions``). Needs
        version control on the project (409 ``version_control_disabled`` otherwise); 412 when the
        project has moved past ``expected_revision``."""
        if not project_id:
            raise ValueError("project id is required")
        resp = self._send(
            "POST",
            f"/api/projects/{quote(str(project_id), safe='')}/versions",
            org_id=org_id or self.org_id(),
            body={"title": title, "expectedCurrentRevision": int(expected_revision)},
        )
        return resp.json()

    # -- deliverables -------------------------------------------------------

    def deliverable_json(
        self,
        project_ref: str,
        kind: str,
        *,
        options: dict | None = None,
        change_request_id: str | None = None,
        org_id: str | None = None,
    ) -> Any:
        """Generate a JSON deliverable for a project (by id or URL slug) and return the decoded document.

        ``options`` are the generator's (for example ``{"netboxVersion": "4.4"}``); ``change_request_id``
        generates it from a merge request's draft instead of main. Deliverables are paid on hosted
        Railyard: a plan without them is refused with :class:`RailyardPlanRequiredError`, an estate past
        the plan's rack cap with :class:`RailyardPlanLimitError` (a self-hosted server with billing off
        allows every deliverable). Other failures are the client's usual typed errors."""
        if not project_ref:
            raise ValueError("project ref is required")
        if not kind:
            raise ValueError("deliverable kind is required")
        body: dict[str, Any] = {}
        if change_request_id:
            body["changeRequestId"] = change_request_id
        if options:
            body["options"] = dict(options)
        path = f"/api/projects/{quote(str(project_ref), safe='')}/deliverables/{quote(kind, safe='')}"
        resp = self._send("POST", path, org_id=org_id or self.org_id(), body=body)
        try:
            return resp.json()
        except ValueError:
            raise RailyardAPIError(
                f"Railyard returned a {kind} deliverable that is not JSON.", status=resp.status_code
            ) from None

    def netbox_sync_document(
        self,
        project_ref: str,
        *,
        netbox_version: str | None = None,
        change_request_id: str | None = None,
        org_id: str | None = None,
    ) -> dict:
        """The project's NetBox sync document (``POST …/deliverables/netbox-sync``), the source of
        :func:`railyard_sync.export.run.sync_to_netbox`. ``netbox_version`` is the NetBox release the
        document is written for (``"4.4"``; Railyard's default line otherwise)."""
        options = {"netboxVersion": netbox_version} if netbox_version else None
        doc = self.deliverable_json(
            project_ref, NETBOX_SYNC, options=options, change_request_id=change_request_id, org_id=org_id
        )
        if not isinstance(doc, dict):
            raise RailyardAPIError("Railyard returned a NetBox sync document that is not a JSON object.")
        return doc

    def get_json(self, path: str, params: dict | None = None) -> Any:
        """GET any Railyard API path (``/api/…``) in the client's org and return the decoded JSON.
        The importer's catalogue lookup (``/api/catalogue/search``) goes through this."""
        if not path.startswith("/api/") or "://" in path or ".." in path:
            raise ValueError(f"not a Railyard API path: {path!r}")
        return self._get(path, org_id=self.org_id(), params=params)


def _json_body(resp: _Response) -> dict:
    """The response body as a dict, or {} when it is not a JSON object."""
    try:
        payload = resp.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _plan_error(payload: dict, said: str) -> RailyardPlanError:
    """The typed error for a 402: every plan refusal the server sends, saves and deliverables alike."""
    code = str(payload.get("code") or "")
    fields: dict[str, Any] = {
        "plan": str(payload.get("plan") or ""),
        "resource": str(payload.get("resource") or ""),
        "limit": _int_or_none(payload.get("limit")),
        "current": _int_or_none(payload.get("current")),
        "scope": str(payload.get("scope") or ""),
        "required_plans": [str(p) for p in payload.get("requiredPlans") or []],
        "project_pass": bool(payload.get("projectPass")),
        "feature": str(payload.get("feature") or ""),
        "deliverable": str(payload.get("deliverable") or ""),
    }
    if code == "plan_limit":
        return RailyardPlanLimitError(
            said or "Railyard refused the request: it goes past the plan's limit (HTTP 402).", **fields
        )
    if code == "plan_required":
        what = f"the {fields['deliverable']} deliverable" if fields["deliverable"] else "it"
        return RailyardPlanRequiredError(
            said or f"Railyard refused the request: the plan does not include {what} (HTTP 402).", **fields
        )
    return RailyardPlanError(
        "Railyard refused the request (HTTP 402)" + (f": {said}" if said else ""), code=code, **fields
    )


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _header(resp: _Response, name: str) -> str:
    headers = getattr(resp, "headers", None) or {}
    value = headers.get(name)
    if value is None:  # a plain dict (tests) is case-sensitive; requests' headers are not
        lowered = name.lower()
        value = next((v for k, v in headers.items() if str(k).lower() == lowered), None)
    return str(value or "")


def _revision(resp: _Response) -> int:
    """The revision in a response's ETag (``"42"``, possibly weak ``W/"42"``)."""
    tag = _header(resp, "ETag").strip()
    if tag.startswith("W/"):
        tag = tag[2:]
    tag = tag.strip('"')
    try:
        return int(tag)
    except ValueError:
        raise RailyardAPIError(
            "Railyard's response had no usable ETag, so the project's revision is unknown", status=resp.status_code
        ) from None
