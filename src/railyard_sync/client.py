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

import json
import logging
import secrets
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import quote, urlencode

from .errors import (
    RailyardAPIError,
    RailyardBadRequestError,
    RailyardBusyError,
    RailyardConflictError,
    RailyardConnectionError,
    RailyardForbiddenError,
    RailyardNotFoundError,
    RailyardPlanError,
    RailyardPlanLimitError,
    RailyardPlanRequiredError,
    RailyardPreconditionError,
    RailyardServerError,
    RailyardTermsError,
    RailyardTokenRejectedError,
    RailyardTooLargeError,
)
from .log import header, human_size, log_http, response_size
from .problems import ProblemNamer, as_problems, listing

log = logging.getLogger(__name__)

TOKEN_PREFIX = "ry_"
NETBOX_SYNC = "netbox-sync"  # the deliverable kind of the NetBox sync document
NAUTOBOT_SYNC = "nautobot-sync"  # and of the Nautobot sync document


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


def _connection_errors() -> tuple[type[BaseException], ...]:
    try:
        import requests
    except ImportError:  # pragma: no cover - requests is a dependency
        return (OSError,)
    return (requests.RequestException, OSError)


def new_request_id() -> str:
    """An id for one request, sent as ``X-Request-ID``; Railyard adopts it (and logs under it) unless the
    edge replaced it, so the id is known even when no response arrives."""
    return f"rys-{secrets.token_hex(8)}"


class RailyardClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        org: str | None = None,
        session: _Session | None = None,
        timeout: float = 30.0,
        max_attempts: int = 3,
        max_retry_wait: float = 30.0,
        sleep: Callable[[float], None] | None = None,
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
        # Retries of a busy (429/503) answer: idempotent requests only, at most max_attempts in all, and
        # never a wait longer than max_retry_wait seconds.
        self.max_attempts = max(1, int(max_attempts))
        self.max_retry_wait = max_retry_wait
        self._sleep = sleep or time.sleep
        # A caller-supplied default org reference (id, slug or name); resolved lazily to an id.
        self._org_ref = org
        self._org_id: str | None = None

    def __repr__(self) -> str:
        return f"RailyardClient(base_url={self.base_url!r})"

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
        retry: bool | None = None,
    ) -> _Response:
        """Send one request and return the response, raising the typed error for any non-2xx status.

        A busy answer (429/503) is retried, after the wait the server asks for (``Retry-After``) or a short
        backoff, when the request is safe to repeat: a GET, a PUT that carries ``If-Match`` (a repeat can
        only apply to the revision it names), or a caller's ``retry=True`` (a stateless compute request).
        A connection failure is retried for the same requests except a PUT, whose first attempt may have
        been applied. Each exchange is logged at DEBUG: method, path, status, time, sizes and request id,
        never a header or a body."""
        url = f"{self.base_url}{path}"
        headers = self._headers(org_id)
        if extra_headers:
            headers.update(extra_headers)
        kwargs: dict[str, Any] = {"headers": headers, "params": params, "timeout": self._timeout}
        sent = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            kwargs["json"] = body
            sent = len(json.dumps(body).encode("utf-8"))  # what requests sends; logged as a size only
        if retry is None:
            retry = method == "GET" or (method == "PUT" and "If-Match" in headers)
        shown = path + (f"?{urlencode(params, doseq=True)}" if params else "")
        note = f"If-Match {headers['If-Match']}" if "If-Match" in headers else ""

        for attempt in range(1, self.max_attempts + 1):
            request_id = new_request_id()
            kwargs["headers"] = {**headers, "X-Request-ID": request_id}
            started = time.monotonic()
            try:
                resp = self._session.request(method, url, **kwargs)
            except _connection_errors() as exc:
                elapsed = time.monotonic() - started
                reason = self._scrub(f"{type(exc).__name__}: {exc}")
                log_http(log, "Railyard", method, shown, "no response", elapsed, request_id=request_id, sent=sent)
                if retry and method != "PUT" and attempt < self.max_attempts:
                    wait = _backoff(attempt)
                    log.warning(
                        "Could not reach Railyard (%s); retrying in %s (attempt %d of %d)…",
                        reason,
                        _wait_text(wait),
                        attempt + 1,
                        self.max_attempts,
                    )
                    self._sleep(wait)
                    continue
                raise RailyardConnectionError(
                    f"Could not reach Railyard at {self.base_url} for {method} {path}: {reason}",
                    method=method,
                    path=path,
                    request_id=request_id,
                    hints=[
                        "Check --railyard-url, your network and any proxy; railyard.sh's status is at "
                        "https://railyard.sh. A timeout on a large estate may need a faster connection."
                    ],
                ) from None
            elapsed = time.monotonic() - started
            request_id = header(resp, "X-Request-ID") or request_id
            status = resp.status_code
            etag = header(resp, "ETag")
            log_http(
                log,
                "Railyard",
                method,
                shown,
                status,
                elapsed,
                request_id=request_id,
                sent=sent,
                received=response_size(resp),
                note=", ".join(filter(None, [note, f"ETag {etag}" if etag else ""])),
            )
            if 200 <= status < 300:
                return resp
            if status in (429, 503) and retry and attempt < self.max_attempts:
                wait = _retry_after(resp)
                wait = _backoff(attempt) if wait is None else wait
                if wait <= self.max_retry_wait:
                    log.warning(
                        "Railyard is busy (HTTP %d for %s %s); retrying in %s (attempt %d of %d)…",
                        status,
                        method,
                        path,
                        _wait_text(wait),
                        attempt + 1,
                        self.max_attempts,
                    )
                    self._sleep(wait)
                    continue
            raise self._error(
                method, path, resp, request_id=request_id, body=body, sent=sent, attempts=attempt, headers=headers
            )
        raise AssertionError("unreachable")  # pragma: no cover

    def _get(self, path: str, *, org_id: str | None = None, params: dict | None = None) -> Any:
        return self._send("GET", path, org_id=org_id, params=params).json()

    def _scrub(self, text: str) -> str:
        """Never let the token reach a message, whatever a server or proxy echoed back."""
        return text.replace(self._token, "[token]") if self._token else text

    def _error(self, method: str, path: str, resp: _Response, **kwargs: Any) -> RailyardAPIError:
        """The typed error for a failed response, with everything that helps act on it: the method and
        path, the status, the server's ``error`` and ``code``, its structured details, the request id, and
        what to do about it. Never the token. Details the message does not already render are listed."""
        error = self._typed_error(method, path, resp, **kwargs)
        rest = {k: v for k, v in error.details.items() if k not in _RENDERED_DETAILS}
        if rest and not isinstance(error, RailyardPlanError):
            error.hints.append(
                "Details: "
                + "; ".join(f"{k}: {self._scrub(json.dumps(v, ensure_ascii=False))}" for k, v in rest.items())
            )
        return error

    def _typed_error(
        self,
        method: str,
        path: str,
        resp: _Response,
        *,
        request_id: str = "",
        body: Any = None,
        sent: int | None = None,
        attempts: int = 1,
        headers: dict[str, str] | None = None,
    ) -> RailyardAPIError:
        status = resp.status_code
        payload = _json_body(resp)
        code = str(payload.get("code") or "")
        said = self._scrub(str(payload.get("error") or "")).strip()
        if not said and not payload:
            try:
                said = " ".join(self._scrub(resp.text[:400]).split())
            except Exception:  # pragma: no cover - defensive
                said = ""
        details = {k: v for k, v in payload.items() if k not in ("error", "code")}
        context: dict[str, Any] = {
            "status": status,
            "method": method,
            "path": path,
            "code": code,
            "server_message": said,
            "request_id": request_id,
            "details": details,
        }
        where = f"{method} {path}"
        coded = f", {code}" if code else ""
        suffix = f": {said}" if said else ""
        writing = method != "GET"
        document = isinstance(body, dict) and "schemaVersion" in body

        if status == 401:
            return RailyardTokenRejectedError(
                f"Railyard rejected the personal access token (HTTP 401) for {where}: it is wrong, revoked or expired.",
                hints=[
                    "Railyard tokens expire: create a new one in Railyard under User settings → API tokens, and "
                    "use it in place of the old one (RAILYARD_TOKEN for railyard-sync; the NetBox plugin's "
                    "railyard_token or railyard_token_file setting)."
                ],
                **context,
            )
        if status == 403:
            return _forbidden(where, said, code, writing, context)
        if status == 404:
            return RailyardNotFoundError(
                f"Not found (HTTP 404) for {where}{suffix}", hints=_not_found_hints(path), **context
            )
        if status == 402:
            return _plan_error(payload, said, context)
        if status == 400:
            problems = [p for p in details.get("problems") or [] if isinstance(p, dict)]
            what = "the document" if document else "the request"
            hints: list[str] = []
            if problems:
                namer = ProblemNamer(body if document else None)
                hints.append("The problems Railyard named:")
                hints += [f"  - {line}" for line in listing(problems, namer)]
            if details.get("field"):
                hints.append(f"Field: {details['field']}")
            if code == "device-naming.collision":
                hints.append(
                    "Railyard needs every device name in an estate to be unique (ignoring case and spaces); NetBox "
                    "only needs them unique within a site. Rename one of the two devices, in Railyard or in NetBox, "
                    "or update railyard-sync: it keeps imported names apart and reports clashes before saving."
                )
            if document:
                hints.append(
                    "railyard-sync built this document from the DCIM: fix what is named in NetBox (or in Railyard, "
                    "for objects designed there) and import again. If nothing in your data is named, it is a "
                    "railyard-sync bug: report it with the document that was sent."
                )
            return RailyardBadRequestError(
                f"Railyard refused {what} (HTTP 400{coded}) for {where}{suffix}", hints=hints, **context
            )
        if status == 409:
            return RailyardConflictError(
                f"Railyard refused the change (HTTP 409{coded}) for {where}{suffix}",
                hints=_conflict_hints(code),
                **context,
            )
        if status in (412, 428):
            if status == 412:
                why = "the estate changed in Railyard after it was read"
                hint = "Nothing was overwritten. Run the import again: it reads the latest revision and merges onto it."
            else:
                why = "the save did not say which revision it replaces, and an estate with this id already exists"
                hint = "Refresh that estate with --project instead of creating it again."
            return RailyardPreconditionError(
                f"Railyard refused the save (HTTP {status}) for {where}: {why}."
                + (f" Railyard said: {said}" if said else ""),
                hints=[hint, "Read the project again and retry."] if status == 412 else [hint],
                **context,
            )
        if status == 413:
            limit = _int_or_none(payload.get("limit"))
            size = _int_or_none(payload.get("size")) or sent
            measure = ", ".join(
                filter(
                    None,
                    [f"{human_size(size)} sent" if size else "", f"the limit is {human_size(limit)}" if limit else ""],
                )
            )
            return RailyardTooLargeError(
                f"Railyard refused the document (HTTP 413) for {where}: it is too large"
                + (f" ({measure})" if measure else "")
                + suffix,
                limit=limit,
                size=size,
                hints=[
                    "Split the sites into separate estates (run the import once per site, each with its own --name), "
                    "or ask Railyard about a larger limit."
                ],
                **context,
            )
        if status in (429, 503):
            wait = _retry_after(resp)
            hints = []
            if attempts > 1:
                hints.append(f"Tried {attempts} times.")
            hints.append(f"Try again in {_wait_text(wait)}." if wait is not None else "Try again in a minute.")
            return RailyardBusyError(
                f"Railyard is busy (HTTP {status}) for {where}{suffix}", retry_after=wait, hints=hints, **context
            )
        if status >= 500:
            hints = [
                "This is a Railyard bug, not a fault in your data: the server could not finish the request. "
                "Please report it to Railyard."
            ]
            if method == "PUT" and "/api/projects/" in path:
                conditional = bool(headers and "If-Match" in headers)
                hints.append(
                    "Running the import again is safe: the save names the revision it replaces, so it can never "
                    "overwrite a change made meanwhile."
                    if conditional
                    else "Before running it again, check in Railyard whether the estate was created."
                )
            return RailyardServerError(
                f"Railyard failed while {_action(method, path)} (HTTP {status} for {where}){suffix}",
                hints=hints,
                **context,
            )
        return RailyardAPIError(f"Railyard API error (HTTP {status}) for {where}{suffix}", **context)

    # -- validation ---------------------------------------------------------

    def validate(self, project: dict) -> dict:
        """Railyard's check of a whole document (``POST /api/validate``): ``{"problems": [...], "truncated"}``,
        each problem ``{severity, code, message, rackId?, placementId?}``. It saves nothing; a document it
        cannot load at all is refused with :class:`RailyardBadRequestError` (400)."""
        resp = self._send("POST", "/api/validate", body=project, retry=True)
        data = _json_body(resp)
        return {"problems": as_problems(data.get("problems")), "truncated": bool(data.get("truncated"))}

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
        known = ", ".join(str(o.get("slug") or o.get("id") or "") for o in orgs) or "none"
        raise RailyardNotFoundError(
            f"No org matched {ref!r} (by id, slug or name).",
            hints=[f"The token's user belongs to: {known}."],
        )

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

    def nautobot_sync_document(
        self, project_ref: str, *, change_request_id: str | None = None, org_id: str | None = None
    ) -> dict:
        """The project's Nautobot sync document (``POST …/deliverables/nautobot-sync``), the source of
        :func:`railyard_sync.export.run.sync_to_nautobot`: the rows of Railyard's Nautobot 2.x bundle. A paid
        deliverable on hosted Railyard, refused like :meth:`deliverable_json`."""
        doc = self.deliverable_json(project_ref, NAUTOBOT_SYNC, change_request_id=change_request_id, org_id=org_id)
        if not isinstance(doc, dict):
            raise RailyardAPIError("Railyard returned a Nautobot sync document that is not a JSON object.")
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


def _plan_error(payload: dict, said: str, context: dict | None = None) -> RailyardPlanError:
    """The typed error for a 402: every plan refusal the server sends, saves and deliverables alike."""
    code = str(payload.get("code") or "")
    context = {k: v for k, v in (context or {}).items() if k not in ("status", "code")}
    fields: dict[str, Any] = {
        **context,
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


def _revision(resp: _Response) -> int:
    """The revision in a response's ETag (``"42"``, possibly weak ``W/"42"``)."""
    tag = header(resp, "ETag").strip()
    if tag.startswith("W/"):
        tag = tag[2:]
    tag = tag.strip('"')
    try:
        return int(tag)
    except ValueError:
        raise RailyardAPIError(
            "Railyard's response had no usable ETag, so the project's revision is unknown", status=resp.status_code
        ) from None


#: Details the error messages already render (the 400 problems and field, 413 sizes, 402 plan fields).
_RENDERED_DETAILS = {
    "problems",
    "truncated",
    "field",
    "limit",
    "size",
    "plan",
    "resource",
    "current",
    "scope",
    "requiredPlans",
    "projectPass",
    "feature",
    "deliverable",
}


def _action(method: str, path: str) -> str:
    """What a request was doing, for "Railyard failed while …"."""
    if path == "/api/validate":
        return "checking the document"
    if "/deliverables/" in path:
        return f"generating the {path.rsplit('/', 1)[-1]} deliverable"
    if path.endswith("/versions"):
        return "naming the version"
    if path.startswith("/api/projects/"):
        return "saving the estate" if method == "PUT" else "reading the estate"
    if path == "/api/projects":
        return "listing the estates"
    if path == "/api/orgs":
        return "listing your organisations"
    if path.startswith("/api/catalogue"):
        return "searching the device catalogue"
    return f"handling {method} {path}"


def _forbidden(where: str, said: str, code: str, writing: bool, context: dict) -> RailyardAPIError:
    """403, saying which: the Terms of Service, the role, the organisation or the estate."""
    lowered = said.lower()
    if code == "terms_not_accepted":
        return RailyardTermsError(
            f"Railyard refused the change (HTTP 403, terms_not_accepted) for {where}: the token's user has not "
            "accepted the current Terms of Service.",
            hints=["Sign in to Railyard in a browser, accept them, then try again."],
            **context,
        )
    if "read-only" in lowered or "role" in lowered:
        why = "the token's user has a read-only role in this organisation"
        hint = "Ask an organisation owner to make the user an editor (Organisation settings → Members)."
    elif "member" in lowered:
        why = "the token's user is not a member of this organisation"
        hint = "Check --org, or ask an organisation owner to add the user."
    elif "/api/projects/" in where:
        action = "change" if writing else "read"
        why = f"the token's user may not {action} this estate"
        hint = "Ask the estate's owner for access (the estate's Share settings)" + (
            ", with edit rights." if writing else "."
        )
    else:
        action = "change" if writing else "read"
        why = f"the token is valid, but its user may not {action} this organisation or project"
        hint = "Add that user to the organisation in Railyard" + (" with an editor role." if writing else ".")
    return RailyardForbiddenError(
        f"Railyard refused access (HTTP 403) for {where}: {why}." + (f" Railyard said: {said}" if said else ""),
        hints=[hint],
        **context,
    )


def _not_found_hints(path: str) -> list[str]:
    if path == "/api/validate":
        return ["This Railyard has no /api/validate (an older self-hosted release)."]
    if path.startswith("/api/projects/"):
        return [
            "Check --project: an estate's id, or the slug in its URL (/o/<org>/p/<slug>), in the organisation "
            "--org names. A renamed estate has a new slug."
        ]
    return []


def _conflict_hints(code: str) -> list[str]:
    return {
        "name_taken": [
            "Another estate in this organisation already has this name: refresh that one with --project, or "
            "choose another --name."
        ],
        "project_id_taken": [
            "The estate's id is already in use on this Railyard. Run the import again: a new estate gets a new id."
        ],
        "history_quota": [
            "The estate's history is full: remove old versions or change requests in Railyard, then try again."
        ],
        "version_control_disabled": [
            "Version control is off for this estate: switch it on in Railyard, or leave out --name-version."
        ],
    }.get(code, [])


def _retry_after(resp: _Response) -> float | None:
    """The wait a ``Retry-After`` header asks for, in seconds (a number, or an HTTP date)."""
    value = header(resp, "Retry-After").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def _backoff(attempt: int) -> float:
    return float(2 ** (attempt - 1))  # 1 s, 2 s, 4 s…


def _wait_text(wait: float | None) -> str:
    if wait is None:
        return "a moment"
    whole = round(wait)
    return f"{whole} s" if whole >= 1 else "under a second"
