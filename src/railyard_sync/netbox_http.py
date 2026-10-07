"""What the NetBox clients (the import's loader and the export's REST client) share about NetBox's answers:
its error ``detail``, its request id, and which permission a refused request needed.

The wording is :mod:`railyard_sync.dcim_http`'s, which Nautobot shares; this module keeps NetBox's names for it."""

from __future__ import annotations

from typing import Any

from . import dcim_http
from .dcim_http import NETBOX, REQUEST_ID_HEADER, object_type, permission, request_id

__all__ = ["REQUEST_ID_HEADER", "detail", "object_type", "permission", "permission_hint", "reporting", "request_id"]


def detail(resp: Any, limit: int = 400) -> str:
    """NetBox's explanation of a failure: its ``detail``, its per-field validation errors
    (``name: This field is required.``), or the start of the body."""
    return dcim_http.detail(resp, NETBOX, limit)


def permission_hint(method: str, path: str) -> str:
    """What to grant for a 403: the action on the object type, and, for a write, a token with writes on."""
    return dcim_http.permission_hint(method, path, NETBOX)


def reporting(request_id_: str) -> str:
    return dcim_http.reporting(request_id_, NETBOX)
