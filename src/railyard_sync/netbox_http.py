"""What the NetBox clients (the import's loader and the export's REST client) share about NetBox's answers:
its error ``detail``, its request id, and which permission a refused request needed."""

from __future__ import annotations

from typing import Any

from .log import header

#: NetBox answers every API request with its id in this header (NetBox 3.x and 4.x).
REQUEST_ID_HEADER = "X-Request-ID"

_ACTIONS = {"GET": "view", "HEAD": "view", "POST": "add", "PUT": "change", "PATCH": "change", "DELETE": "delete"}


def request_id(resp: Any) -> str:
    return header(resp, REQUEST_ID_HEADER)


def object_type(path: str) -> str:
    """``/api/dcim/device-types/12/`` -> ``dcim.devicetype``, the name NetBox's permissions use; ``""`` for
    a path that is not an object endpoint."""
    parts = [p for p in path.split("?", 1)[0].split("/") if p]
    if len(parts) < 3 or parts[0] != "api":
        return ""
    app, endpoint = parts[1], parts[2].replace("-", "")
    if endpoint.endswith("s"):
        endpoint = endpoint[:-1]
    return f"{app}.{endpoint}"


def permission(method: str, path: str) -> tuple[str, str]:
    """``("view", "dcim.rack")``: the action and object type a request needs, ``("", "")`` when unknown."""
    kind = object_type(path)
    return (_ACTIONS.get(method.upper(), ""), kind) if kind else ("", "")


def detail(resp: Any, limit: int = 400) -> str:
    """NetBox's explanation of a failure: its ``detail``, its per-field validation errors
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
            text = "an HTML page, not NetBox's API (check the NetBox URL and any proxy in front of it)"
    return text[:limit]


def _flatten(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(_flatten(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}: {_flatten(v)}" for k, v in value.items())
    return str(value)


def permission_hint(method: str, path: str) -> str:
    """What to grant for a 403: the action on the object type, and, for a write, a token with writes on."""
    action, kind = permission(method, path)
    if not kind:
        return "The token's user lacks a permission NetBox requires for this request (Admin → Permissions)."
    hint = (
        f"The token's user needs the {action!r} permission on {kind} "
        f"(in NetBox: Admin → Permissions, object type {kind}, action {action})."
    )
    if action != "view":
        hint += " The token itself must also have Write enabled."
    return hint


def reporting(request_id_: str) -> str:
    return f" Quote NetBox request id {request_id_} when reporting this." if request_id_ else ""
