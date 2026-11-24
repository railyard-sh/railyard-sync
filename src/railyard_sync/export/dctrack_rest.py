"""A small Sunbird dcTrack REST client: what ``railyard-sync export sunbird`` reads and writes.

dcTrack's REST API (9.x) is https only and refuses a URL with a trailing slash (from 9.1). It takes HTTP basic auth
on every call, or an OAuth2 client-credentials token (9.3 on; ``POST /oauth/token``). The calls used:

    GET  /api/v1/locations                          -> {"locations": [{id, tiLocationCode, tiLocationName, …}]}
    POST /api/v2/quicksearch/items?pageNumber=&pageSize=
                                                    -> {"totalRows", "pageNumber", "searchResults": {"items": […]}}
    GET  /api/v2/items/cabinetItems/{cabinetId}     -> {"cabinetItems": [{id, tiName, className, tiRackUnits,
                                                         cmbUPosition, radioRailsUsed, …}]}
    GET  /api/v2/dcimoperations/items/{id}          -> {"item": {…, tiSerialNumber}}
    POST /api/v2/dcimoperations/items/bulk          {"bodies": [{id, tiSerialNumber}], "method": "PUT",
                                                     "proceedOnWarning": true}
                                                    -> {"httpCode", "errorList", "children": [{httpCode, errorList,
                                                         request, response}]}  (207 when some rows fail)

A failure's body is ``{"errorList": […], "warningList": […], "message", "trace"}`` (older releases: ``errors``).

**Every assumption about dcTrack that its public documentation leaves open is in the section "dcTrack's answers"
below**, so a real dcTrack that answers differently is fixed in one place.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

from ..dcim_http import (
    AUTH,
    NOT_FOUND,
    PERMISSION,
    APIError,
    Product,
    RESTClient,
    Session,
    connection_errors,
    detail,
    failure,
    reporting,
    request_id,
    unreachable,
)
from ..log import log_http, response_size

log = logging.getLogger(__name__)

PASSWORD_ENV = "DCTRACK_PASSWORD"
CLIENT_SECRET_ENV = "DCTRACK_CLIENT_SECRET"

DCTRACK = Product(
    name="dcTrack",
    token_env=PASSWORD_ENV,
    url_flag="--dctrack-url",
    tokens_where="Administration → Users",
    permissions_where="Administration → Users and Groups",
    write_enabled="",
)

#: Rows per bulk request. dcTrack documents no maximum; this keeps each request (and each failure) small.
BULK_BATCH = 100
#: Rows per quick-search page (``pageSize``; dcTrack returns 50 when it is not given).
SEARCH_PAGE = 500
_MAX_PAGES = 1000


class DcTrackError(APIError):
    """A dcTrack API request failed."""


class DcTrackConnectionError(DcTrackError):
    """dcTrack could not be reached (DNS, TLS, refused, timed out)."""


class DcTrackAuthError(DcTrackError):
    """401: dcTrack did not accept the credentials."""


class DcTrackPermissionError(DcTrackError):
    """403: the user may not do this."""


class DcTrackNotFoundError(DcTrackError):
    """404: no such object, or no such API in this dcTrack release (a trailing slash also gives 404)."""


# ---- dcTrack's answers: every field name and value this export relies on ---------------------------------------
#
# Confirmed by the 9.3 API guide: the paths above, ``locations[].tiLocationCode``, the cabinetItems fields (id, tiName,
# className, tiRackUnits, cmbUPosition as an int, radioRailsUsed), the quick-search request and response shapes, the
# bulk request and its ``children[].httpCode`` / ``errorList``, and that U positions -9 (none), -2 (above), -1
# (below) are not positions. Not confirmed, and chosen here:
#
# - a quick search's ``eq`` on a string is a "contains" match, so results are checked again here (``_same_location``),
#   and a cabinet's ``cmbLocation`` may read "PARENT > CODE" for a nested location;
# - the cabinetItems answer carries no serial, so serials come from a quick search of the cabinet's items selecting
#   ``tiSerialNumber``, and from the item itself (``GET /api/v2/dcimoperations/items/{id}``) when the search leaves one
#   out;
# - ``radioRailsUsed`` is Both, Front or Back ("Rear" is read as Back); a value missing or unknown is read as Both;
# - an update that sends only ``{"id", "tiSerialNumber"}`` changes only the serial (a partial update), in bulk too;
# - the bulk endpoint answers 200 or 207 with one child per body, in order, also when every row fails;
# - the File Import's ``Object`` column takes the item's class name (``IMPORT_OBJECTS``).

CABINET_CLASS = "Cabinet"
NO_POSITION = 0  # what _position returns for -9 (none), -2 (above), -1 (below) and the invalid 0

#: dcTrack item class (``className`` / ``tiClass``) -> the File Import ``Object`` value that edits it.
IMPORT_OBJECTS = {
    "device": "Device",
    "network": "Network",
    "data panel": "Data Panel",
    "rack pdu": "Rack PDU",
    "probe": "Probe",
    "passive": "Passive",
    "power outlet": "Power Outlet",
    "ups": "UPS",
    "floor pdu": "Floor PDU",
    "hvac": "HVAC",
    "cabinet": "Cabinet",
}


def import_object(item_class: str) -> str:
    """The File Import ``Object`` for an item of ``item_class`` (``"Device / Standard"`` -> ``"Device"``); an
    unknown class is passed through as dcTrack names it."""
    base = (item_class or "").split("/", 1)[0].strip()
    return IMPORT_OBJECTS.get(base.casefold(), base)


def _rails(value: Any) -> str:
    """``Front``, ``Back`` or ``Both``."""
    text = str(value or "").strip().casefold()
    if text == "front":
        return "Front"
    if text in ("back", "rear"):
        return "Back"
    return "Both"


def _position(value: Any) -> int:
    """An item's bottom U (``cmbUPosition``, an int or a numeric string), or :data:`NO_POSITION`."""
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return NO_POSITION
    return number if number > 0 else NO_POSITION


def _height(row: dict) -> int | None:
    for key in ("tiRackUnits", "tiRUs"):
        try:
            number = int(float(str(row.get(key)).strip()))
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return None


def _id(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _same_location(value: Any, code: str) -> bool:
    """Whether a search result's ``cmbLocation`` is the location ``code`` (also as the last part of "A > B")."""
    text = str(value or "").strip()
    if not text:
        return True  # the search filtered on it; nothing more to check
    return text.casefold() == code.casefold() or text.rsplit(">", 1)[-1].strip().casefold() == code.casefold()


def _error_text(data: Any) -> str:
    """dcTrack's words for a failure: its ``errorList``, else ``message``, else the deprecated ``errors``."""
    if not isinstance(data, dict):
        return ""
    for key in ("errorList", "errors"):
        value = data.get(key)
        items = value if isinstance(value, list) else list(value.values()) if isinstance(value, dict) else [value]
        texts = [str(v).strip() for v in items if v not in (None, "", [], {})]
        if texts:
            return "; ".join(texts)
    return str(data.get("message") or "").strip()


# ---- what the export reads ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Location:
    id: str
    code: str
    name: str = ""


@dataclass(frozen=True)
class DcCabinet:
    id: str
    name: str


@dataclass(frozen=True)
class DcItem:
    """An item in a cabinet, as the export matches it."""

    id: str
    name: str
    item_class: str
    u_position: int  # its bottom U; NO_POSITION when it has none
    rails: str  # Front | Back | Both
    height: int | None


@dataclass(frozen=True)
class RowResult:
    """One row of a bulk update: whether dcTrack took it, and its words when not."""

    id: str
    ok: bool
    status: int | None
    message: str = ""


class DcTrackClient(RESTClient):
    """dcTrack over REST, with basic auth (``user`` and ``password``) or an OAuth2 client (``client_id`` and
    ``client_secret``). ``verify`` is ``True``, ``False`` or a CA bundle path. Plain http is refused unless
    ``allow_http``: the credentials would cross the network in clear text."""

    product = DCTRACK
    logger = log
    errors = {
        "base": DcTrackError,
        "connection": DcTrackConnectionError,
        AUTH: DcTrackAuthError,
        PERMISSION: DcTrackPermissionError,
        NOT_FOUND: DcTrackNotFoundError,
    }

    def __init__(
        self,
        url: str,
        *,
        user: str = "",
        password: str = "",
        client_id: str = "",
        client_secret: str = "",
        session: Session | None = None,
        verify: bool | str = True,
        timeout: float = 60,
        allow_http: bool = False,
        on_secret: Any = None,
    ) -> None:
        if bool(user) == bool(client_id):
            raise ValueError("dcTrack needs a user and password, or an OAuth2 client id and secret (one of them)")
        secret = password if user else client_secret
        if not secret:
            raise ValueError(
                f"set {PASSWORD_ENV} (or use a password file) for the dcTrack user"
                if user
                else f"set {CLIENT_SECRET_ENV} for the dcTrack OAuth2 client"
            )
        super().__init__(url, secret, session=session, verify=verify, timeout=timeout)
        if urlsplit(self.url).scheme != "https" and not allow_http:
            raise ValueError("the dcTrack URL must be https (dcTrack's API is https only); --insecure-http allows http")
        self._user = user
        self._client_id = client_id
        self._bearer = ""
        self._on_secret = on_secret or (lambda secret: None)
        self._basic = base64.b64encode(f"{user}:{password}".encode()).decode("ascii") if user else ""
        self._on_secret(self._basic)

    def __repr__(self) -> str:
        return f"DcTrackClient(url={self.url!r})"

    # -- auth ---------------------------------------------------------------------------------

    @property
    def auth_kind(self) -> str:
        return "basic" if self._user else "oauth2"

    def auth_header(self) -> str:
        if self._user:
            return f"Basic {self._basic}"
        if not self._bearer:
            self._bearer = self._oauth_token()
        return f"Bearer {self._bearer}"

    def scrub(self, text: str) -> str:
        out = super().scrub(text)
        for secret in (self._basic, self._bearer):
            if secret:
                out = out.replace(secret, "***")
        return out

    def _oauth_token(self) -> str:
        """An OAuth2 access token for the client (``POST /oauth/token``, client credentials; it lasts an hour)."""
        path = "/oauth/token"
        form = {
            "grant_type": "client_credentials",
            "scope": "user_permissions",
            "client_id": self._client_id,
            "client_secret": self._token,
        }
        started = time.monotonic()
        try:
            resp = self._session.request(
                "POST",
                f"{self.url}{path}",
                headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
                data=form,
                timeout=self._timeout,
                verify=self._verify,
            )
        except connection_errors() as exc:
            log_http(self.logger, "dcTrack", "POST", path, "no response", time.monotonic() - started)
            raise DcTrackConnectionError(
                self.scrub(unreachable(self.product, self.url, exc)), method="POST", path=path
            ) from None
        rid = request_id(resp)
        status = resp.status_code
        elapsed = time.monotonic() - started
        log_http(self.logger, "dcTrack", "POST", path, status, elapsed, request_id=rid, received=response_size(resp))
        token = ""
        if 200 <= status < 300:
            try:
                data = resp.json()
            except ValueError:
                data = None
            token = str(data.get("access_token") or "") if isinstance(data, dict) else ""
        if not token:
            said = self.scrub(self.explain(resp)) if status >= 400 else "no access_token in the answer"
            kind = AUTH if status in (400, 401, 403) or 200 <= status < 300 else "base"
            raise self._error(kind)(
                f"dcTrack did not give an OAuth2 token to client {self._client_id!r} (HTTP {status}) for POST {path}"
                f"{': ' + said if said else ''}. Check --dctrack-client-id and {CLIENT_SECRET_ENV} (OAuth2 clients "
                f"need dcTrack 9.3 or later).{reporting(rid, self.product)}",
                status=status,
                request_id=rid,
                method="POST",
                path=path,
            )
        self._on_secret(token)
        return token

    def request(
        self, method: str, path: str, *, params: Any = None, json: Any = None, allow: tuple[int, ...] = ()
    ) -> Any:
        try:
            return super().request(method, path, params=params, json=json, allow=allow)
        except DcTrackAuthError:
            if self._user or not self._bearer:
                raise
            log.debug("dcTrack refused the OAuth2 token (expired?); asking for a new one")
            self._bearer = ""
            return super().request(method, path, params=params, json=json, allow=allow)

    # -- wording ------------------------------------------------------------------------------

    def explain(self, resp: Any) -> str:
        try:
            data = resp.json()
        except Exception:
            data = None
        text = _error_text(data)
        return text[:400] if text else detail(resp, self.product)

    def refusal(self, method: str, path: str, status: int, said: str, rid: str) -> tuple[str, str]:
        suffix = f": {said}" if said else ""
        report = reporting(rid, self.product)
        if status == 401:
            fix = (
                f"Check --dctrack-user and the password ({PASSWORD_ENV} or --dctrack-password-file)"
                if self._user
                else f"Check --dctrack-client-id and {CLIENT_SECRET_ENV}"
            )
            return AUTH, (
                f"dcTrack did not accept the credentials (HTTP 401) for {method} {path}{suffix}. {fix}; a SAML "
                f"account cannot use dcTrack's API.{report}"
            )
        if status == 403:
            needs = "view and edit" if method in ("POST", "PUT") and "/dcimoperations/" in path else "view"
            return PERMISSION, (
                f"dcTrack refused {method} {path} (HTTP 403){suffix}. The dcTrack user needs permission to {needs} "
                f"these items (dcTrack: {self.product.permissions_where}).{report}"
            )
        if status == 404:
            return NOT_FOUND, (
                f"Not found in dcTrack (HTTP 404): {method} {path}{suffix}. Check --dctrack-url (the dcTrack server, "
                f"without /api); this export needs dcTrack 9.1 or later.{report}"
            )
        return failure(self.product, method, path, status, said, rid)

    # -- reads --------------------------------------------------------------------------------

    def locations(self) -> list[Location]:
        data = self.request("GET", "/api/v1/locations")
        rows = data.get("locations") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise DcTrackError("dcTrack returned an unexpected answer for /api/v1/locations (no 'locations').")
        out = []
        for row in rows:
            if isinstance(row, dict) and str(row.get("tiLocationCode") or "").strip():
                out.append(
                    Location(
                        id=_id(row.get("id")),
                        code=str(row["tiLocationCode"]).strip(),
                        name=str(row.get("tiLocationName") or "").strip(),
                    )
                )
        return out

    def search_items(self, columns: list[dict], selected: list[str]) -> list[dict]:
        """Every item a quick search finds (``columns``: ``[{"name", "filter": {"eq": …}}]``), page by page."""
        body = {"columns": columns, "selectedColumns": [{"name": name} for name in selected]}
        out: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            data = self.request(
                "POST", "/api/v2/quicksearch/items", params={"pageNumber": page, "pageSize": SEARCH_PAGE}, json=body
            )
            results = data.get("searchResults") if isinstance(data, dict) else None
            items = results.get("items") if isinstance(results, dict) else results
            if not isinstance(items, list):
                raise DcTrackError("dcTrack returned an unexpected answer for /api/v2/quicksearch/items.")
            out.extend(i for i in items if isinstance(i, dict))
            total = data.get("totalRows")
            if len(items) < SEARCH_PAGE or (isinstance(total, int) and len(out) >= total):
                break
        return out

    def cabinets(self, location: str) -> list[DcCabinet]:
        """The cabinets in the location with code ``location``."""
        rows = self.search_items(
            [{"name": "tiClass", "filter": {"eq": CABINET_CLASS}}, {"name": "cmbLocation", "filter": {"eq": location}}],
            ["id", "tiName", "tiClass", "cmbLocation"],
        )
        out = []
        for row in rows:
            item_class = str(row.get("tiClass") or CABINET_CLASS).split("/", 1)[0].strip()
            if item_class.casefold() != CABINET_CLASS.casefold():
                continue
            if not _same_location(row.get("cmbLocation"), location):
                continue
            if _id(row.get("id")) and str(row.get("tiName") or "").strip():
                out.append(DcCabinet(id=_id(row["id"]), name=str(row["tiName"]).strip()))
        return out

    def cabinet_items(self, cabinet_id: str) -> list[DcItem]:
        data = self.request("GET", f"/api/v2/items/cabinetItems/{quote(cabinet_id, safe='')}")
        rows = data.get("cabinetItems") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise DcTrackError(
                f"dcTrack returned an unexpected answer for cabinet {cabinet_id}'s items (no 'cabinetItems')."
            )
        return [
            DcItem(
                id=_id(row.get("id")),
                name=str(row.get("tiName") or "").strip(),
                item_class=str(row.get("className") or row.get("tiClass") or "").strip(),
                u_position=_position(row.get("cmbUPosition")),
                rails=_rails(row.get("radioRailsUsed")),
                height=_height(row),
            )
            for row in rows
            if isinstance(row, dict) and _id(row.get("id"))
        ]

    def serials(self, location: str, cabinet: str, ids: list[str]) -> dict[str, str]:
        """``{item id: serial}`` for the items ``ids`` in a cabinet: one quick search for the cabinet, then the item
        itself for any the search left out."""
        wanted = set(ids)
        found: dict[str, str] = {}
        if wanted:
            columns = [
                {"name": "cmbLocation", "filter": {"eq": location}},
                {"name": "cmbCabinet", "filter": {"eq": cabinet}},
            ]
            rows = self.search_items(columns, ["id", "tiName", "tiSerialNumber", "cmbCabinet", "cmbLocation"])
            for row in rows:
                item_id = _id(row.get("id"))
                if item_id in wanted and "tiSerialNumber" in row:
                    found[item_id] = str(row.get("tiSerialNumber") or "").strip()
        for item_id in sorted(wanted - set(found)):
            data = self.request("GET", f"/api/v2/dcimoperations/items/{quote(item_id, safe='')}")
            item = data.get("item") if isinstance(data, dict) and isinstance(data.get("item"), dict) else data
            if isinstance(item, dict):
                found[item_id] = str(item.get("tiSerialNumber") or "").strip()
        return found

    # -- writes -------------------------------------------------------------------------------

    def bulk_update(self, rows: list[tuple[str, dict]]) -> list[RowResult]:
        """Update items in one bulk request (``rows``: ``(item id, fields)``) and say how each fared."""
        bodies = [{"id": int(item_id) if item_id.isdigit() else item_id, **fields} for item_id, fields in rows]
        payload = {"bodies": bodies, "method": "PUT", "proceedOnWarning": True}
        data = self.request("POST", "/api/v2/dcimoperations/items/bulk", json=payload, allow=(400, 207))
        children = data.get("children") if isinstance(data, dict) else None
        if not isinstance(children, list) or len(children) != len(rows):
            said = _error_text(data) or "its answer did not list a result per row"
            return [RowResult(item_id, False, None, said) for item_id, _ in rows]
        out = []
        for (item_id, _), child in zip(rows, children, strict=True):
            status = child.get("httpCode") if isinstance(child, dict) else None
            status = status if isinstance(status, int) else None
            ok = status is not None and 200 <= status < 300
            out.append(RowResult(item_id, ok, status, "" if ok else _error_text(child)))
        return out
