"""What the DCIM REST clients share, for NetBox and Nautobot alike: how each product's failures are explained (its
error ``detail``, its request id, which permission a refused request needed, where to make a new token) and the
REST client the export targets write through.

The import loaders (``dcim/netbox.py``, ``dcim/nautobot.py``) and the export targets (``export/netbox_rest.py``,
``export/nautobot_rest.py``) raise their own error types, but word every failure through :func:`failure`, so a
NetBox and a Nautobot refusal read the same way and name the same things: the request, the status, the product's
own words, the permission to grant, the request id to quote.
"""

from __future__ import annotations

import json as _json
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from .log import header, log_http, response_size

#: NetBox answers every API request with its id in this header; Nautobot does when a proxy or middleware adds one.
REQUEST_ID_HEADER = "X-Request-ID"

_ACTIONS = {"GET": "view", "HEAD": "view", "POST": "add", "PUT": "change", "PATCH": "change", "DELETE": "delete"}


@dataclass(frozen=True)
class Product:
    """A DCIM product, as its messages name it."""

    name: str  # "NetBox"
    token_env: str  # the environment variable the CLI reads the token from
    url_flag: str  # the CLI flag naming its URL
    tokens_where: str  # where a user creates an API token
    permissions_where: str  # where an administrator grants permissions
    write_enabled: str  # what a token needs to write


NETBOX = Product(
    name="NetBox",
    token_env="NETBOX_TOKEN",
    url_flag="--netbox-url",
    tokens_where="your user menu → API Tokens",
    permissions_where="Admin → Permissions",
    write_enabled="The token itself must also have Write enabled.",
)
NAUTOBOT = Product(
    name="Nautobot",
    token_env="NAUTOBOT_TOKEN",
    url_flag="--nautobot-url",
    tokens_where="your user menu → Profile → API Tokens",
    permissions_where="Admin → Users → Permissions",
    write_enabled="The token itself must also have Write enabled.",
)


def request_id(resp: Any) -> str:
    return header(resp, REQUEST_ID_HEADER)


def object_type(path: str) -> str:
    """``/api/dcim/device-types/12/`` -> ``dcim.devicetype``, ``/api/extras/statuses/`` -> ``extras.status``: the
    name NetBox's and Nautobot's permissions use; ``""`` for a path that is not an object endpoint."""
    parts = [p for p in path.split("?", 1)[0].split("/") if p]
    if len(parts) < 3 or parts[0] != "api":
        return ""
    app, endpoint = parts[1], parts[2].replace("-", "")
    if endpoint.endswith(("statuses", "addresses")):
        endpoint = endpoint[:-2]
    elif endpoint.endswith("s"):
        endpoint = endpoint[:-1]
    return f"{app}.{endpoint}"


def permission(method: str, path: str) -> tuple[str, str]:
    """``("view", "dcim.rack")``: the action and object type a request needs, ``("", "")`` when unknown."""
    kind = object_type(path)
    return (_ACTIONS.get(method.upper(), ""), kind) if kind else ("", "")


def detail(resp: Any, product: Product = NETBOX, limit: int = 400) -> str:
    """The product's explanation of a failure: its ``detail``, its per-field validation errors
    (``name: This field is required.``), or the start of the body."""
    try:
        data = resp.json()
    except Exception:
        data = None
    if isinstance(data, dict):
        if "detail" in data and len(data) == 1:
            text = str(data["detail"])
        else:
            text = "; ".join(f"{key}: {_flatten(value)}" for key, value in data.items())
    elif isinstance(data, list):
        text = "; ".join(_flatten(item) for item in data if item)
    else:
        try:
            text = " ".join((resp.text or "").split())
        except Exception:  # pragma: no cover - defensive
            text = ""
        if text.startswith("<"):
            text = f"an HTML page, not {product.name}'s API (check the {product.name} URL and any proxy in front of it)"
    return text[:limit]


def _flatten(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(_flatten(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}: {_flatten(v)}" for k, v in value.items())
    return str(value)


def permission_hint(method: str, path: str, product: Product = NETBOX) -> str:
    """What to grant for a 403: the action on the object type, and, for a write, a token with writes on."""
    action, kind = permission(method, path)
    if not kind:
        return (
            f"The token's user lacks a permission {product.name} requires for this request "
            f"({product.permissions_where})."
        )
    hint = (
        f"The token's user needs the {action!r} permission on {kind} "
        f"(in {product.name}: {product.permissions_where}, object type {kind}, action {action})."
    )
    if action != "view":
        hint += " " + product.write_enabled
    return hint


def reporting(request_id_: str, product: Product = NETBOX) -> str:
    return f" Quote {product.name} request id {request_id_} when reporting this." if request_id_ else ""


# ---- failures ----------------------------------------------------------------------------------------------

AUTH, PERMISSION, NOT_FOUND, SERVER, OTHER = "auth", "permission", "not-found", "server", "other"


def failure(product: Product, method: str, path: str, status: int, said: str, rid: str) -> tuple[str, str]:
    """``(kind, message)`` for a failed request: ``kind`` is one of :data:`AUTH` (the token is refused),
    :data:`PERMISSION`, :data:`NOT_FOUND`, :data:`SERVER` and :data:`OTHER`; the message names the request, the
    status, the product's own words (``said``, already scrubbed of the token), what to do and the request id."""
    name = product.name
    suffix = f": {said}" if said else ""
    report = reporting(rid, product)
    if status == 401 or (status == 403 and "token" in said.lower()):
        return AUTH, (
            f"{name} did not accept the API token (HTTP {status}) for {method} {path}{suffix}. It is wrong, expired or "
            f"revoked: create a new one in {name} ({product.tokens_where}) and set {product.token_env} to it.{report}"
        )
    if status == 403:
        return PERMISSION, (
            f"{name} refused {method} {path} (HTTP 403){suffix}. {permission_hint(method, path, product)}{report}"
        )
    if status == 404:
        return NOT_FOUND, f"Not found in {name} (HTTP 404): {method} {path}{suffix}.{report}"
    if status >= 500:
        return SERVER, (
            f"{name} failed (HTTP {status}) for {method} {path}{suffix}. This is {name}'s own fault (see its logs), "
            f"not railyard-sync's.{report}"
        )
    return OTHER, f"{name} refused {method} {path} (HTTP {status}){suffix}.{report}"


def unreachable(product: Product, url: str, exc: BaseException) -> str:
    return (
        f"Could not reach {product.name} at {url}: {type(exc).__name__}: {exc} Check {product.url_flag}, your network "
        "and any proxy (or --insecure for a self-signed certificate)."
    )


def check_url(url: str, token: str, product: Product) -> str:
    """The product's base URL from ``url`` (no trailing slash, no ``/api``), refusing what must not be used."""
    if not url:
        raise ValueError(f"{product.name} URL is required")
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"{product.name} URL must be an http:// or https:// URL")
    if parts.username or parts.password:
        raise ValueError(f"{product.name} URL must not contain credentials; pass the token separately")
    if not token:
        raise ValueError(f"{product.name} API token is required")
    base = url.strip().rstrip("/")
    if base.endswith("/api"):  # a common slip: the API root rather than the product's own URL
        base = base[: -len("/api")]
    return base


def scrub_token(token: str, text: str) -> str:
    """``text`` with the token, and each part of a dotted token (NetBox v2: ``nbt_<key>.<secret>``), replaced by
    ``***``. Longest first, so the whole token is never left half-replaced."""
    out = text or ""
    secrets = {token, *token.split(".")}
    for secret in sorted((s for s in secrets if len(s) >= 4), key=len, reverse=True):
        out = out.replace(secret, "***")
    return out


# ---- the export targets' REST client ------------------------------------------------------------------------


class _Response(Protocol):
    status_code: int

    @property
    def text(self) -> str: ...

    def json(self) -> Any: ...


class Session(Protocol):
    """The request side of ``requests.Session``; tests and the Nautobot app inject their own."""

    def request(self, method: str, url: str, **kwargs: Any) -> _Response: ...


def default_session() -> Session:
    import requests  # imported lazily so the module imports without requests when a session is injected

    return requests.Session()


def connection_errors() -> tuple[type[BaseException], ...]:
    try:
        import requests
    except ImportError:  # pragma: no cover - requests is a dependency
        return (OSError,)
    return (requests.RequestException, OSError)


class APIError(Exception):
    """A DCIM API request failed. Carries the HTTP status when there was a response, and the request's method, path
    and request id (``X-Request-ID``) when there was one."""

    def __init__(
        self, message: str, *, status: int | None = None, request_id: str = "", method: str = "", path: str = ""
    ) -> None:
        super().__init__(message)
        self.status = status
        self.request_id = request_id
        self.method = method
        self.path = path


class RESTClient:
    """A small DCIM REST client: list (paginated), get, create, update (PATCH), delete.

    Subclasses name the product, the error types each failure raises (``errors``: ``base``, ``connection``,
    ``auth``, ``permission``, ``not-found``) and how an object id goes into a path (``object_id``)."""

    product: Product = NETBOX
    errors: dict[str, type[APIError]] = {}
    logger = logging.getLogger(__name__)

    def __init__(
        self,
        url: str,
        token: str,
        *,
        session: Session | None = None,
        verify: bool | str = True,
        timeout: float = 30,
        page_size: int = 1000,
    ) -> None:
        self.url = check_url(url, token, self.product)
        self._token = token
        self._session = session or default_session()
        self._verify = verify
        self._timeout = timeout
        self._page_size = max(1, int(page_size))
        self.version = ""

    def __repr__(self) -> str:
        return f"{type(self).__name__}(url={self.url!r})"

    def auth_header(self) -> str:
        return f"Token {self._token}"

    def scrub(self, text: str) -> str:
        return scrub_token(self._token, text)

    def object_id(self, obj_id: Any) -> str:
        return str(int(obj_id))

    def _error(self, kind: str) -> type[APIError]:
        return self.errors.get(kind) or self.errors["base"]

    def request(self, method: str, path: str, *, params: Any = None, json: Any = None) -> Any:
        name = self.product.name
        url = f"{self.url}{path}"
        headers = {"Authorization": self.auth_header(), "Accept": "application/json"}
        sent = None
        if json is not None:
            headers["Content-Type"] = "application/json"
            sent = len(_json.dumps(json).encode("utf-8"))  # logged as a size only, never the body
        shown = path + (f"?{urlencode(params, doseq=True)}" if params else "")
        started = time.monotonic()
        try:
            resp = self._session.request(
                method, url, headers=headers, params=params, json=json, timeout=self._timeout, verify=self._verify
            )
        except connection_errors() as exc:
            log_http(self.logger, name, method, shown, "no response", time.monotonic() - started, sent=sent)
            raise self._error("connection")(
                self.scrub(unreachable(self.product, self.url, exc)), method=method, path=path
            ) from None
        status = resp.status_code
        rid = request_id(resp)
        log_http(
            self.logger,
            name,
            method,
            shown,
            status,
            time.monotonic() - started,
            request_id=rid,
            sent=sent,
            received=response_size(resp),
        )
        context = {"status": status, "request_id": rid, "method": method, "path": path}
        if 200 <= status < 300:
            if status == 204 or method == "DELETE":
                return None
            try:
                return resp.json()
            except ValueError:
                raise self._error("base")(
                    f"{name} returned a response that is not JSON for {path}: is {self.url} a {name} server?",
                    **context,
                ) from None
        kind, message = failure(self.product, method, path, status, self.scrub(detail(resp, self.product)), rid)
        raise self._error(kind)(message, **context)

    # -- verbs ---------------------------------------------------------------------------------

    def list(self, endpoint: str, **filters: Any) -> list[dict]:
        """Every object a list endpoint returns for ``filters``, page by page.

        Pages are requested by ``offset`` against the configured URL, never by following ``next``: behind a
        proxy ``next`` is often built with the wrong scheme or host, and the token must never be sent anywhere
        else."""
        params = query_params(filters)
        out: list[dict] = []
        offset = 0
        while True:
            page = self.request(
                "GET", f"/api/{endpoint}/", params=params + [("limit", self._page_size), ("offset", offset)]
            )
            if not isinstance(page, dict) or not isinstance(page.get("results"), list):
                raise self._error("base")(
                    f"{self.product.name} returned an unexpected response for /api/{endpoint}/ (no 'results')."
                )
            results = page["results"]
            out.extend(r for r in results if isinstance(r, dict))
            offset += len(results)
            if not results or not page.get("next") or offset >= int(page.get("count") or 0):
                return out

    def first(self, endpoint: str, **filters: Any) -> dict | None:
        found = self.list(endpoint, **filters)
        return found[0] if found else None

    def get(self, endpoint: str, obj_id: Any, **params: Any) -> dict | None:
        try:
            return self.request("GET", f"/api/{endpoint}/{self.object_id(obj_id)}/", params=query_params(params))
        except self._error(NOT_FOUND):
            return None

    def create(self, endpoint: str, data: dict) -> dict:
        return self.request("POST", f"/api/{endpoint}/", json=data)

    def update(self, endpoint: str, obj_id: Any, data: dict) -> dict:
        return self.request("PATCH", f"/api/{endpoint}/{self.object_id(obj_id)}/", json=data)

    def delete(self, endpoint: str, obj_id: Any) -> None:
        self.request("DELETE", f"/api/{endpoint}/{self.object_id(obj_id)}/")


def query_params(filters: dict) -> list[tuple[str, Any]]:
    """``{"site_id": [1, 2], "q": "x"}`` -> ``[("site_id", 1), ("site_id", 2), ("q", "x")]``."""
    out: list[tuple[str, Any]] = []
    for key, value in (filters or {}).items():
        for item in value if isinstance(value, list | tuple) else [value]:
            out.append((key, item))
    return out
