"""The Nautobot DiffSync *target* adapter over Nautobot 2.x's REST API.

The Nautobot twin of :mod:`.netbox_rest`, with the same ownership rules (the NetBox plugin's), so the semantics
users rely on hold whichever DCIM they export to:

- **Owned objects** carry the project's ownership tag (``policy.ownership_tag``; in Nautobot, a tag recognised by
  its exact description, enabled for every model the sync tags). ``load()`` reads back only those, each with its
  Nautobot id, and ``update``/``delete`` act only on that exact object, re-fetched by id and checked first.
  Nautobot's organisational models have no tags — location types, statuses, manufacturers and roles — so for
  them ownership is the ``railyard_owner`` custom field holding the tag's slug. When the token may not create
  that field, those objects are created without an owner and are, from then on, shared objects used as they are.
- **Shared objects that already exist** (location types, statuses, manufacturers, device types, roles,
  locations, racks) are *used* as they are, never changed or deleted. A status, role or location type used as it
  is must already be enabled for what uses it (a rack status for ``dcim.rack``…); when it is not, the objects
  that need it are reported as conflicts rather than written and refused.
- **Conflicts** — a device of the same name already in the location, a port that is already cabled — are reported
  and skipped, together with whatever depends on them. The sync never adopts them.
- **Components on a device the sync owns** that already exist by name (created from the device type's component
  templates when the sync created the device) are adopted and tagged.
- **Renames**: a device the sync owns whose Railyard id (the ``railyard_id`` custom field) now has a different
  name in Railyard is renamed in place.
- **Deletes** (opt-in, run by ``run.sync_to_nautobot``) are refused — the object is kept and reported — when they
  would cascade to, modify or disconnect anything the sync doesn't own; Nautobot itself refuses a delete its
  ``PROTECT`` relations forbid, and that refusal keeps the object too.

The REST API is not transactional: a failed write is recorded in ``OwnershipReport.errors`` and the sync carries on
with what does not depend on it; running it again converges. (The Nautobot app runs the same adapter in-process,
inside one database transaction.)

A ``session`` (``requests.Session``-like) can be injected, which is how the tests run against an in-memory
Nautobot and how the app talks to its own API without HTTP. The token never appears in a log, an error or a
``repr``.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections import Counter
from collections.abc import Callable, Iterable
from typing import Any

from diffsync import Adapter
from diffsync.exceptions import ObjectAlreadyExists, ObjectNotCreated, ObjectNotUpdated

from .. import dcim_http as http
from ..dcim_http import NAUTOBOT, APIError, RESTClient
from . import nautobot_models as models
from .netbox_rest import CT_MODEL, OwnershipReport, _noun as _netbox_noun

log = logging.getLogger(__name__)

MIN_VERSION = (2, 0)
MAX_TESTED_VERSION = (2, 4)
MAX_MAJOR = 3
CUSTOM_FIELD = "railyard_id"
OWNER_FIELD = "railyard_owner"

#: diffsync model name -> REST endpoint (below ``/api/``).
ENDPOINT = {
    "location_type": "dcim/location-types",
    "status": "extras/statuses",
    "manufacturer": "dcim/manufacturers",
    "device_type": "dcim/device-types",
    "role": "extras/roles",
    "location": "dcim/locations",
    "rack": "dcim/racks",
    "device": "dcim/devices",
    "interface": "dcim/interfaces",
    "rear_port": "dcim/rear-ports",
    "front_port": "dcim/front-ports",
    "power_outlet": "dcim/power-outlets",
    "power_port": "dcim/power-ports",
    "cable": "dcim/cables",
}
#: Nautobot content type of each synced model.
CONTENT_TYPE = {
    "location_type": "dcim.locationtype",
    "status": "extras.status",
    "manufacturer": "dcim.manufacturer",
    "device_type": "dcim.devicetype",
    "role": "extras.role",
    "location": "dcim.location",
    "rack": "dcim.rack",
    "device": "dcim.device",
    "interface": "dcim.interface",
    "rear_port": "dcim.rearport",
    "front_port": "dcim.frontport",
    "power_outlet": "dcim.poweroutlet",
    "power_port": "dcim.powerport",
    "cable": "dcim.cable",
}
#: The models the ownership tag marks (it must be enabled for each), and those the owner custom field marks.
TAGGED = tuple(t for t in models.TOP_LEVEL if t not in models.UNTAGGED)
TAG_CONTENT_TYPES = sorted(CONTENT_TYPE[t] for t in TAGGED)
OWNER_CONTENT_TYPES = sorted(CONTENT_TYPE[t] for t in models.UNTAGGED)
COMPONENT_TYPES = ("interface", "rear_port", "front_port", "power_outlet", "power_port")
CABLE_STATUS = "Connected"


# ---- errors --------------------------------------------------------------------------------------


class NautobotError(APIError):
    """A Nautobot API request failed (status, method, path and request id when there was a response)."""


class NautobotConnectionError(NautobotError):
    """Nautobot could not be reached (DNS, TLS, refused, timed out)."""


class NautobotAuthError(NautobotError):
    """401, or 403 for the token itself — Nautobot did not accept the API token."""


class NautobotPermissionError(NautobotError):
    """403 — the token is valid but may not do this."""


class NautobotNotFoundError(NautobotError):
    """404 — the object or endpoint does not exist."""


class NautobotVersionError(NautobotError):
    """The Nautobot release is not one this sync supports."""


# ---- HTTP client ---------------------------------------------------------------------------------


def parse_version(text: str) -> tuple[int, int]:
    """``"2.4.43"`` / ``"v2.3.0-beta.1"`` -> ``(2, 4)``; ``(0, 0)`` when unreadable."""
    match = re.match(r"v?(\d+)\.(\d+)", (text or "").strip())
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


class NautobotClient(RESTClient):
    """A small Nautobot REST client: list (paginated), get, create, update (PATCH), delete. Ids are UUIDs."""

    product = NAUTOBOT
    logger = log
    errors = {
        "base": NautobotError,
        "connection": NautobotConnectionError,
        http.AUTH: NautobotAuthError,
        http.PERMISSION: NautobotPermissionError,
        http.NOT_FOUND: NautobotNotFoundError,
    }

    def object_id(self, obj_id: Any) -> str:
        return str(uuid.UUID(str(obj_id)))  # refuses anything that is not a UUID, so a path can't be steered

    def status(self) -> dict:
        data = self.request("GET", "/api/status/")
        if not isinstance(data, dict):
            raise NautobotError(f"Nautobot returned an unexpected status for {self.url}: is it a Nautobot server?")
        self.version = str(data.get("nautobot-version") or "")
        return data


def check_version(client: NautobotClient, warnings: list[str]) -> None:
    """Refuse Nautobot releases the sync does not support; warn for newer ones than it was tested with."""
    version = parse_version(client.version)
    if version < MIN_VERSION:
        raise NautobotVersionError(
            f"Nautobot {client.version or '(unknown version)'} is not supported: 2.0 or later (1.x has sites)."
        )
    if version[0] > MAX_MAJOR:
        raise NautobotVersionError(f"Nautobot {client.version} is not supported yet: tested with 2.0 to 2.4.")
    if version > MAX_TESTED_VERSION:
        warnings.append(f"Nautobot {client.version} is newer than this railyard-sync was tested with (2.4).")


# ---- value helpers -------------------------------------------------------------------------------


def _ref_id(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("id")
    return str(value) if value else None


def _ref_name(value: Any, key: str = "name") -> str:
    return str(value.get(key) or "") if isinstance(value, dict) else ""


def _choice(value: Any) -> Any:
    """A choice field (``{"value", "label"}``) as its value; blank (``None``) as ``""``."""
    if isinstance(value, dict):
        value = value.get("value")
    return "" if value is None else value


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def tag_ids(obj: dict) -> list[str]:
    return [i for i in (_ref_id(t) for t in obj.get("tags") or []) if i]


def content_types(obj: dict) -> list[str]:
    return sorted(str(t) for t in obj.get("content_types") or [])


# ---- write helpers -------------------------------------------------------------------------------


def _label(model_type: str, unique_id: str) -> str:
    return f"{model_type.replace('_', ' ')} {unique_id}"


def _fail(adapter: NautobotRESTAdapter, exc_type: type[Exception], what: str, why: str):
    message = adapter.client.scrub(f"{what}: {why}")
    adapter.report.errors.append(message)
    return exc_type(message)


def _create(cls, adapter: NautobotRESTAdapter, ids: dict, attrs: dict, build: Callable[[], dict]):
    """Run ``build() -> Nautobot object`` for a create, turning Nautobot's refusal into ObjectNotCreated."""
    what = f"create {_label(cls.get_type(), cls.create_unique_id(**ids))}"
    try:
        obj = build()
    except NautobotError as exc:
        raise _fail(adapter, ObjectNotCreated, what, str(exc)) from None
    model = super(cls, cls).create(adapter, ids=ids, attrs=attrs)
    model.nb_id = obj.get("id")
    adapter.counts["create"][cls.get_type()] += 1
    adapter.remember(cls.get_type(), model.get_unique_id(), obj)
    return model


def _update(model, payload: dict | Callable[[], dict]) -> None:
    """PATCH the owned object behind ``model`` with ``payload`` (or what ``payload()`` builds)."""
    adapter: NautobotRESTAdapter = model.adapter
    what = f"update {_label(model.get_type(), model.get_unique_id())}"
    try:
        adapter.owned_object(model)
        data = payload() if callable(payload) else payload
        if data:
            adapter.client.update(ENDPOINT[model.get_type()], model.nb_id, data)
    except NautobotError as exc:
        raise _fail(adapter, ObjectNotUpdated, what, str(exc)) from None
    adapter.counts["update"][model.get_type()] += 1


def _delete(model) -> bool:
    """Delete an owned object unless that would reach beyond what the sync owns (then keep it)."""
    adapter: NautobotRESTAdapter = model.adapter
    label = _label(model.get_type(), model.get_unique_id())
    endpoint = ENDPOINT[model.get_type()]
    obj = adapter.client.get(endpoint, model.nb_id) if model.nb_id else None
    if obj is None:
        return False  # already gone (e.g. with a device deleted before its components)
    if not adapter.owns(model.get_type(), obj):
        adapter.report.kept.append(f"{label}: no longer carries the ownership mark")
        return False
    try:
        blockers = adapter.deletion_blockers(model.get_type(), obj)
    except NautobotError as exc:
        blockers = [adapter.client.scrub(f"could not check what depends on it: {exc}")]
    if blockers:
        adapter.report.kept.append(f"{label}: {'; '.join(blockers)}")
        return False
    try:
        adapter.client.delete(endpoint, model.nb_id)
    except NautobotError as exc:
        adapter.report.kept.append(adapter.client.scrub(f"{label}: Nautobot refused the delete: {exc}"))
        return False
    adapter.counts["delete"][model.get_type()] += 1
    return True


class _DeleteMixin:
    """``delete()`` for every target model: delete if safe, else keep and report (never raises)."""

    def delete(self):
        _delete(self)
        super().delete()
        return self


# ---- target models -------------------------------------------------------------------------------


class NautobotLocationType(_DeleteMixin, models.LocationType):
    nb_id: str | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        def build():
            payload = {
                "name": ids["name"],
                "parent": adapter.ref_id("location_type", attrs.get("parent")),
                "content_types": list(attrs.get("content_types") or []),
                "nestable": bool(attrs.get("nestable")),
                **adapter.owner_fields(),
            }
            return adapter.client.create(ENDPOINT["location_type"], payload)

        return _create(cls, adapter, ids, attrs, build)

    def update(self, attrs):
        def payload():
            data = {k: attrs[k] for k in ("content_types", "nestable") if k in attrs}
            if "parent" in attrs:
                data["parent"] = self.adapter.ref_id("location_type", attrs["parent"])
            return data

        _update(self, payload)
        return super().update(attrs)


class NautobotStatus(_DeleteMixin, models.Status):
    nb_id: str | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        payload = {
            "name": ids["name"],
            "content_types": list(attrs.get("content_types") or []),
            "color": "9e9e9e",
            **adapter.owner_fields(),
        }
        return _create(cls, adapter, ids, attrs, lambda: adapter.client.create(ENDPOINT["status"], payload))

    def update(self, attrs):
        _update(self, {k: attrs[k] for k in ("content_types",) if k in attrs})
        return super().update(attrs)


class NautobotManufacturer(_DeleteMixin, models.Manufacturer):
    nb_id: str | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        payload = {"name": ids["name"], **adapter.owner_fields()}
        return _create(cls, adapter, ids, attrs, lambda: adapter.client.create(ENDPOINT["manufacturer"], payload))

    def update(self, attrs):
        return super().update(attrs)  # a manufacturer has nothing but its name, its identity


class NautobotDeviceType(_DeleteMixin, models.DeviceType):
    nb_id: str | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        def build():
            payload = {
                "manufacturer": adapter.require_id("manufacturer", ids["manufacturer"]),
                "model": ids["model"],
                "u_height": attrs.get("u_height", 1),
                "is_full_depth": attrs.get("is_full_depth", True),
                "tags": adapter.tags_for(),
            }
            return adapter.client.create(ENDPOINT["device_type"], payload)

        return _create(cls, adapter, ids, attrs, build)

    def update(self, attrs):
        _update(self, {k: attrs[k] for k in ("u_height", "is_full_depth") if k in attrs})
        return super().update(attrs)


class NautobotRole(_DeleteMixin, models.Role):
    nb_id: str | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        payload = {
            "name": ids["name"],
            "content_types": list(attrs.get("content_types") or []),
            "color": "9e9e9e",
            **adapter.owner_fields(),
        }
        return _create(cls, adapter, ids, attrs, lambda: adapter.client.create(ENDPOINT["role"], payload))

    def update(self, attrs):
        _update(self, {k: attrs[k] for k in ("content_types",) if k in attrs})
        return super().update(attrs)


class NautobotLocation(_DeleteMixin, models.Location):
    nb_id: str | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        def build():
            payload = {
                "name": ids["name"],
                "location_type": adapter.require_id("location_type", attrs["location_type"]),
                "parent": adapter.ref_id("location", attrs.get("parent")),
                "status": adapter.require_id("status", attrs.get("status") or "Active"),
                "facility": attrs.get("facility", ""),
                "tags": adapter.tags_for(),
            }
            return adapter.client.create(ENDPOINT["location"], payload)

        return _create(cls, adapter, ids, attrs, build)

    def update(self, attrs):
        adapter: NautobotRESTAdapter = self.adapter

        def payload():
            data = {k: attrs[k] for k in ("facility",) if k in attrs}
            if "location_type" in attrs:
                data["location_type"] = adapter.require_id("location_type", attrs["location_type"])
            if "parent" in attrs:
                data["parent"] = adapter.ref_id("location", attrs["parent"])
            if "status" in attrs:
                data["status"] = adapter.require_id("status", attrs["status"])
            return data

        _update(self, payload)
        return super().update(attrs)


class NautobotRack(_DeleteMixin, models.Rack):
    nb_id: str | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        def build():
            payload = {
                "name": ids["name"],
                "location": adapter.require_id("location", ids["location"]),
                "status": adapter.require_id("status", attrs.get("status") or "Active"),
                "role": adapter.ref_id("role", attrs.get("role")),
                "type": attrs.get("type", ""),
                "width": attrs.get("width", 19),
                "u_height": attrs.get("u_height", 42),
                "desc_units": attrs.get("desc_units", False),
                "comments": attrs.get("comments", ""),
                "tags": adapter.tags_for(attrs.get("tags")),
            }
            return adapter.client.create(ENDPOINT["rack"], payload)

        return _create(cls, adapter, ids, attrs, build)

    def update(self, attrs):
        adapter: NautobotRESTAdapter = self.adapter

        def payload():
            data = {k: attrs[k] for k in ("type", "width", "u_height", "desc_units", "comments") if k in attrs}
            if "status" in attrs:
                data["status"] = adapter.require_id("status", attrs["status"])
            if "role" in attrs:
                data["role"] = adapter.ref_id("role", attrs["role"])
            if "tags" in attrs:
                data["tags"] = adapter.tags_for(attrs["tags"])
            return data

        _update(self, payload)
        return super().update(attrs)


class NautobotDevice(_DeleteMixin, models.Device):
    nb_id: str | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        def build():
            rack = adapter.ref_id("rack", attrs.get("location"), attrs.get("rack")) if attrs.get("rack") else None
            payload = {
                "name": ids["name"],
                "device_type": adapter.require_id("device_type", attrs["manufacturer"], attrs["device_type"]),
                "role": adapter.require_id("role", attrs["role"]),
                "status": adapter.require_id("status", attrs.get("status") or "Active"),
                "location": adapter.require_id("location", attrs.get("location")),
                "rack": rack,
                "position": attrs.get("position"),
                "face": attrs.get("face") or "",
                "comments": attrs.get("comments", ""),
                "tags": adapter.tags_for(attrs.get("tags")),
            }
            if adapter.custom_field and attrs.get("railyard_id"):
                payload["custom_fields"] = {CUSTOM_FIELD: attrs["railyard_id"]}
            return adapter.client.create(ENDPOINT["device"], payload)

        return _create(cls, adapter, ids, attrs, build)

    def update(self, attrs):
        adapter: NautobotRESTAdapter = self.adapter

        def payload():
            data = {k: attrs[k] for k in ("position", "face", "comments") if k in attrs}
            if "role" in attrs:
                data["role"] = adapter.require_id("role", attrs["role"])
            if "status" in attrs:
                data["status"] = adapter.require_id("status", attrs["status"])
            if "device_type" in attrs or "manufacturer" in attrs:
                mfr, model = attrs.get("manufacturer", self.manufacturer), attrs.get("device_type", self.device_type)
                data["device_type"] = adapter.require_id("device_type", mfr, model)
            location = attrs.get("location", self.location)
            if "location" in attrs or "rack" in attrs:
                data["location"] = adapter.require_id("location", location)
                rack = attrs.get("rack", self.rack)
                data["rack"] = adapter.require_id("rack", location, rack) if rack else None
            if "tags" in attrs:
                data["tags"] = adapter.tags_for(attrs["tags"])
            if "railyard_id" in attrs and adapter.custom_field:
                data["custom_fields"] = {CUSTOM_FIELD: attrs["railyard_id"] or None}
            if "position" in data and data["position"] is None:
                data["face"] = ""  # a device without a U position has no face
            return data

        _update(self, payload)
        return super().update(attrs)


# Components: (device, name, type) plus a little per kind. Module functions rather than a mixin with an
# underscore class attribute, because DiffSyncModel is Pydantic and would make it private.


def _ensure_component(cls, adapter: NautobotRESTAdapter, ids: dict, attrs: dict, payload: Callable[[], dict]):
    """Create a component on a device the sync owns, or adopt a same-named one already on it (tag it and bring
    its synced fields in line, leaving the rest as Nautobot has it)."""
    model_type = cls.get_type()
    endpoint = ENDPOINT[model_type]

    def build():
        device = adapter.get_or_none("device", ids["device"])
        if device is None or device.nb_id is None:
            raise NautobotError(f"device {ids['device']} is not managed by this sync")
        data = payload()
        existing = adapter.client.first(endpoint, device=device.nb_id, name=ids["name"])
        if existing is None:
            return adapter.client.create(endpoint, {"device": device.nb_id, "name": ids["name"], **data})
        data["tags"] = sorted(set(tag_ids(existing)) | {adapter.tag_id})
        obj = adapter.client.update(endpoint, existing["id"], data)
        label = f"{model_type.replace('_', ' ')} {ids['device']}:{ids['name']}"
        if label not in adapter.report.adopted:
            adapter.report.adopted.append(label)
        return obj

    return _create(cls, adapter, ids, attrs, build)


class NautobotInterface(_DeleteMixin, models.Interface):
    nb_id: str | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        def payload():
            return {
                "type": attrs.get("type") or "other",
                "status": adapter.require_id("status", attrs.get("status") or "Active"),
                "tags": adapter.tags_for(),
            }

        return _ensure_component(cls, adapter, ids, attrs, payload)

    def update(self, attrs):
        def payload():
            data = {k: attrs[k] for k in ("type",) if k in attrs}
            if "status" in attrs:
                data["status"] = self.adapter.require_id("status", attrs["status"])
            return data

        _update(self, payload)
        return super().update(attrs)


class NautobotRearPort(_DeleteMixin, models.RearPort):
    nb_id: str | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        def payload():
            return {
                "type": attrs.get("type") or "8p8c",
                "positions": attrs.get("positions", 1),
                "tags": adapter.tags_for(),
            }

        return _ensure_component(cls, adapter, ids, attrs, payload)

    def update(self, attrs):
        _update(self, {k: attrs[k] for k in ("type", "positions") if k in attrs})
        return super().update(attrs)


class NautobotFrontPort(_DeleteMixin, models.FrontPort):
    nb_id: str | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        def payload():
            rear = adapter.rear_port_id(ids["device"], attrs.get("rear_port", ""))
            if rear is None:  # Nautobot 2 couples every front port to a rear port
                raise NautobotError(f"its rear port {attrs.get('rear_port')!r} is not synced")
            return {
                "type": attrs.get("type") or "8p8c",
                "rear_port": rear,
                "rear_port_position": attrs.get("rear_port_position", 1),
                "tags": adapter.tags_for(),
            }

        return _ensure_component(cls, adapter, ids, attrs, payload)

    def update(self, attrs):
        adapter: NautobotRESTAdapter = self.adapter

        def payload():
            data = {k: attrs[k] for k in ("type", "rear_port_position") if k in attrs}
            if "rear_port" in attrs:
                rear = adapter.rear_port_id(self.device, attrs["rear_port"])
                if rear is None:
                    raise NautobotError(f"its rear port {attrs['rear_port']!r} is not synced")
                data["rear_port"] = rear
            return data

        _update(self, payload)
        return super().update(attrs)


class NautobotPowerOutlet(_DeleteMixin, models.PowerOutlet):
    nb_id: str | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        return _ensure_component(
            cls, adapter, ids, attrs, lambda: {"type": attrs.get("type") or "iec-60320-c13", "tags": adapter.tags_for()}
        )

    def update(self, attrs):
        _update(self, {k: attrs[k] for k in ("type",) if k in attrs})
        return super().update(attrs)


class NautobotPowerPort(_DeleteMixin, models.PowerPort):
    nb_id: str | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        return _ensure_component(
            cls, adapter, ids, attrs, lambda: {"type": attrs.get("type") or "iec-60320-c14", "tags": adapter.tags_for()}
        )

    def update(self, attrs):
        _update(self, {k: attrs[k] for k in ("type",) if k in attrs})
        return super().update(attrs)


class NautobotCable(_DeleteMixin, models.Cable):
    nb_id: str | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        def build():
            payload: dict[str, Any] = {}
            for side in ("a", "b"):
                device, ctype, name = ids[f"{side}_device"], ids[f"{side}_type"], ids[f"{side}_name"]
                term = adapter.owned_termination(device, ctype, name)
                if term is None:
                    raise NautobotError(f"{device}:{name} is not a port this sync manages")
                if _ref_id(term.get("cable")) is not None:
                    raise NautobotError(f"{device}:{name} is already cabled")
                payload[f"termination_{side}_type"] = ctype
                payload[f"termination_{side}_id"] = term["id"]
            payload.update(
                {
                    "status": adapter.require_id("status", attrs.get("status") or CABLE_STATUS),
                    "label": attrs.get("label", ""),
                    "tags": adapter.tags_for(),
                }
            )
            cable_type = attrs.get("type") or ("power" if attrs.get("is_power") else "")
            if cable_type:
                payload["type"] = cable_type
            if attrs.get("color"):
                payload["color"] = attrs["color"]
            return adapter.client.create(ENDPOINT["cable"], payload)

        return _create(cls, adapter, ids, attrs, build)

    def update(self, attrs):
        def payload():
            data = {k: attrs[k] for k in ("label", "color", "type") if k in attrs}
            if "status" in attrs:
                data["status"] = self.adapter.require_id("status", attrs["status"])
            return data

        _update(self, payload)
        return super().update(attrs)


# ---- deletion guard ------------------------------------------------------------------------------

_PROTECT = "still in use by {n} {what} this sync doesn't delete"
_CASCADE = "would also delete {n} {what} not managed by this sync"
_MODIFY = "would modify {n} {what} not managed by this sync"
_CONNECTED = "connected by {n} {what} not managed by this sync"

#: What deleting one of these does to other objects in Nautobot: (endpoint, filter, the field to re-check on each
#: result — ``None`` when the filter is exact — and how it is affected). Nautobot refuses a delete its PROTECT
#: relations forbid, which keeps the object too; they are checked here so a dry run can say so.
_DEPENDENTS: dict[str, list[tuple[str, str, str | None, str]]] = {
    "location_type": [
        ("dcim/locations", "location_type", "location_type", _PROTECT),
        ("dcim/location-types", "parent", "parent", _PROTECT),
    ],
    "status": [
        ("dcim/locations", "status", "status", _PROTECT),
        ("dcim/racks", "status", "status", _PROTECT),
        ("dcim/devices", "status", "status", _PROTECT),
        ("dcim/interfaces", "status", "status", _PROTECT),
        ("dcim/cables", "status", "status", _PROTECT),
    ],
    "manufacturer": [
        ("dcim/device-types", "manufacturer", "manufacturer", _PROTECT),
        ("dcim/platforms", "manufacturer", "manufacturer", _MODIFY),
    ],
    "device_type": [("dcim/devices", "device_type", "device_type", _PROTECT)],
    "role": [
        ("dcim/devices", "role", "role", _PROTECT),
        ("dcim/racks", "role", "role", _PROTECT),
    ],
    "location": [
        ("dcim/locations", "parent", "parent", _CASCADE),
        ("dcim/racks", "location", "location", _PROTECT),
        ("dcim/devices", "location", "location", _PROTECT),
        ("dcim/power-panels", "location", "location", _PROTECT),
    ],
    "rack": [
        ("dcim/devices", "rack", "rack", _PROTECT),
        ("dcim/power-feeds", "rack", "rack", _PROTECT),
        ("dcim/rack-reservations", "rack", "rack", _CASCADE),
    ],
    "device": [
        ("dcim/cables", "device_id", None, _CONNECTED),
        ("ipam/ip-addresses", "device_id", None, _MODIFY),
        ("ipam/services", "device", "device", _CASCADE),
    ],
    "interface": [
        ("ipam/ip-address-to-interface", "interface", "interface", _MODIFY),
        ("dcim/interfaces", "parent_interface", "parent_interface", _MODIFY),
        ("dcim/interfaces", "lag", "lag", _MODIFY),
        ("dcim/interfaces", "bridge", "bridge", _MODIFY),
    ],
    "power_port": [("dcim/power-outlets", "power_port", "power_port", _MODIFY)],
    "rear_port": [("dcim/front-ports", "rear_port", "rear_port", _CASCADE)],
}


_NOUNS = {"ip-address-to-interface": ("IP address assignment", "IP address assignments")}


def _noun(endpoint: str, n: int) -> str:
    name = endpoint.split("/")[-1]
    if name in _NOUNS:
        return _NOUNS[name][n != 1]
    return _netbox_noun(endpoint, n)


# ---- adapter -------------------------------------------------------------------------------------


class NautobotRESTAdapter(Adapter):
    location_type = NautobotLocationType
    status = NautobotStatus
    manufacturer = NautobotManufacturer
    device_type = NautobotDeviceType
    role = NautobotRole
    location = NautobotLocation
    rack = NautobotRack
    device = NautobotDevice
    interface = NautobotInterface
    rear_port = NautobotRearPort
    front_port = NautobotFrontPort
    power_outlet = NautobotPowerOutlet
    power_port = NautobotPowerPort
    cable = NautobotCable

    top_level = models.TOP_LEVEL

    def __init__(
        self,
        client: NautobotClient,
        *,
        tag: dict | None,
        tag_slug: str,
        user_tags: dict[str, str] | None = None,
        custom_field: bool = True,
        owner_field: bool = True,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.client = client
        self.tag = tag  # None before the first real sync: nothing is owned yet
        self.tag_id: str | None = str(tag["id"]) if tag else None
        self.tag_slug = tag_slug  # the owner custom field's value on untagged models
        self.user_tags = dict(user_tags or {})  # user tag name -> Nautobot id
        self.custom_field = custom_field  # the railyard_id custom field exists on devices
        self.owner_field = owner_field  # the railyard_owner custom field exists on the untagged models
        self.source: Adapter | None = None
        self.report = OwnershipReport()
        self.counts: dict[str, Counter] = {"create": Counter(), "update": Counter(), "delete": Counter()}
        self.renames: list[tuple[str, str, str]] = []  # (old name, new name, Nautobot id)
        #: (model type, unique id) -> the Nautobot object a lookup, a load or a create found.
        self._known: dict[tuple[str, str], dict] = {}
        self._names: dict[tuple[str, str], str] = {}  # (endpoint, id) -> name, for ids nested without one
        #: (model type, name) -> content types an existing (used as-is) status, role or location type lacks.
        self.unusable: dict[tuple[str, str], list[str]] = {}

    # -- ownership -----------------------------------------------------------------------------

    def owns(self, model_type: str, obj: dict) -> bool:
        """Whether a Nautobot object is this project's: its ownership tag, or for an untagged model the owner
        custom field."""
        if model_type in models.UNTAGGED:
            return self.owner_field and (obj.get("custom_fields") or {}).get(OWNER_FIELD) == self.tag_slug
        return self.tag_id is not None and self.tag_id in tag_ids(obj)

    def owner_fields(self) -> dict:
        """What marks a new untagged object as owned (nothing when the custom field is not available)."""
        return {"custom_fields": {OWNER_FIELD: self.tag_slug}} if self.owner_field else {}

    def owned_object(self, model) -> dict:
        """Re-fetch the Nautobot object behind a loaded model by id, checking it is still owned."""
        obj = self.client.get(ENDPOINT[model.get_type()], model.nb_id) if model.nb_id else None
        if obj is None or not self.owns(model.get_type(), obj):
            raise NautobotError("it is no longer a Railyard-owned object in Nautobot")
        return obj

    def tags_for(self, user_tags: Iterable[str] | None = None) -> list[str]:
        """The tag ids an owned object carries: the ownership tag plus its user tags that exist."""
        ids = [self.tag_id] if self.tag_id is not None else []
        ids += [self.user_tags[t] for t in user_tags or [] if t in self.user_tags]
        return ids

    # -- lookups -------------------------------------------------------------------------------

    def remember(self, model_type: str, unique_id: str, obj: dict) -> None:
        self._known[(model_type, unique_id)] = obj

    def find(self, model_type: str, *key: str | None) -> dict | None:
        """The Nautobot object a model's natural key names, owned or not (``None`` when absent)."""
        if not key[-1]:
            return None
        unique_id = "__".join(str(k) for k in key)
        if (model_type, unique_id) in self._known:
            return self._known[(model_type, unique_id)]
        endpoint = ENDPOINT[model_type]
        found: dict | None = None
        if model_type in ("location_type", "status", "manufacturer", "role"):
            found = self.client.first(endpoint, name=key[0])
        elif model_type == "device_type":
            mfr = self.find("manufacturer", key[0])
            if mfr is not None:
                found = self.client.first(endpoint, manufacturer=mfr["id"], model=key[1], depth=1)
        elif model_type == "location":
            found = self._unique_location(str(key[0]))
        elif model_type == "rack":
            location = self.find("location", key[0])
            if location is not None:
                racks = self.client.list(endpoint, location=location["id"], name=key[1], depth=1)
                found = next((r for r in racks if _ref_id(r.get("location")) == location["id"]), None)
        else:
            raise ValueError(model_type)
        if found is not None:
            self._known[(model_type, unique_id)] = found
        return found

    def _unique_location(self, name: str, parent: str | None = None) -> dict | None:
        """The location called ``name`` (under ``parent``, by name, when given): ``None`` when there is none or
        when several are and nothing tells them apart."""
        found = self.client.list(ENDPOINT["location"], name=name, depth=1)
        if parent is not None:
            found = [o for o in found if _ref_name(o.get("parent")) == parent]
        return found[0] if len(found) == 1 else None

    def require(self, model_type: str, *key: str | None) -> dict:
        found = self.find(model_type, *key)
        if found is None:
            raise NautobotError(f"{model_type.replace('_', ' ')} {' / '.join(str(k) for k in key)} not found")
        return found

    def require_id(self, model_type: str, *key: str | None) -> str:
        return str(self.require(model_type, *key)["id"])

    def ref_id(self, model_type: str, *key: str | None) -> str | None:
        """The id of an optional reference: ``None`` for a blank name, an error when it is missing."""
        if not key[-1]:
            return None
        return self.require_id(model_type, *key)

    def name_of(self, endpoint: str, ref: Any) -> str:
        """The name of a nested reference, fetching it once when Nautobot nested only its id."""
        name = _ref_name(ref)
        obj_id = _ref_id(ref)
        if name or obj_id is None:
            return name
        if (endpoint, obj_id) not in self._names:
            obj = self.client.get(endpoint, obj_id)
            self._names[(endpoint, obj_id)] = str((obj or {}).get("name") or "")
        return self._names[(endpoint, obj_id)]

    def rear_port_id(self, device: str, name: str) -> str | None:
        if not name:
            return None
        rear = self.get_or_none("rear_port", {"device": device, "name": name})
        if rear is not None and rear.nb_id is not None:
            return rear.nb_id
        owner = self.get_or_none("device", device)
        if owner is None or owner.nb_id is None:
            return None
        found = self.client.first(ENDPOINT["rear_port"], device=owner.nb_id, name=name)
        return found["id"] if found else None

    def owned_termination(self, device: str, ct: str, name: str) -> dict | None:
        """A cable end on a device the sync owns (``None`` if the device or component isn't there)."""
        model_type = CT_MODEL.get(ct)
        owner = self.get_or_none("device", device)
        if model_type is None or owner is None or owner.nb_id is None:
            return None
        known = self.get_or_none(model_type, {"device": device, "name": name})
        if known is not None and known.nb_id is not None:
            return self.client.get(ENDPOINT[model_type], known.nb_id)
        return self.client.first(ENDPOINT[model_type], device=owner.nb_id, name=name)

    # -- load ----------------------------------------------------------------------------------

    def _add(self, model) -> None:
        try:
            self.add(model)
        except ObjectAlreadyExists:
            self.report.warnings.append(
                f"two Railyard-owned objects share the identity {model.get_type()} {model.get_unique_id()}; "
                f"Nautobot {model.nb_id} is ignored (neither updated nor deleted)"
            )

    def _user_tags(self, obj: dict) -> list[str]:
        return sorted(self.name_of("extras/tags", t) for t in obj.get("tags") or [] if _ref_id(t) != self.tag_id)

    def _owned(self, model_type: str, **extra) -> list[dict]:
        endpoint = ENDPOINT[model_type]
        if model_type in models.UNTAGGED:
            if not self.owner_field:
                return []
            found = self.client.list(endpoint, depth=1, **{f"cf_{OWNER_FIELD}": self.tag_slug}, **extra)
        else:
            found = self.client.list(endpoint, tags=self.tag_id, depth=1, **extra)
        return [o for o in found if self.owns(model_type, o)]  # a loose custom-field filter matches substrings

    def load(self) -> None:
        """Load only the objects this project owns, each with its Nautobot id, so a re-sync diffs to no change and
        writes go to exactly these objects."""
        if self.owner_field:
            for o in self._owned("location_type"):
                self._load(
                    self.location_type(
                        name=o["name"],
                        parent=self.name_of(ENDPOINT["location_type"], o.get("parent")),
                        content_types=content_types(o),
                        nestable=bool(o.get("nestable")),
                        nb_id=o["id"],
                    ),
                    o,
                )
            for o in self._owned("status"):
                self._load(self.status(name=o["name"], content_types=content_types(o), nb_id=o["id"]), o)
            for o in self._owned("manufacturer"):
                self._load(self.manufacturer(name=o["name"], nb_id=o["id"]), o)
            for o in self._owned("role"):
                self._load(self.role(name=o["name"], content_types=content_types(o), nb_id=o["id"]), o)
        if self.tag is None:
            return
        for o in self._owned("device_type"):
            self._load(
                self.device_type(
                    manufacturer=self.name_of(ENDPOINT["manufacturer"], o.get("manufacturer")),
                    model=o.get("model") or "",
                    u_height=_int(o.get("u_height"), 0),
                    is_full_depth=bool(o.get("is_full_depth")),
                    nb_id=o["id"],
                ),
                o,
            )
        for o in self._owned("location"):
            self._load(
                self.location(
                    name=o["name"],
                    location_type=self.name_of(ENDPOINT["location_type"], o.get("location_type")),
                    parent=self.name_of(ENDPOINT["location"], o.get("parent")),
                    status=self.name_of(ENDPOINT["status"], o.get("status")),
                    facility=o.get("facility") or "",
                    nb_id=o["id"],
                ),
                o,
            )
        for o in self._owned("rack"):
            self._load(
                self.rack(
                    location=self.name_of(ENDPOINT["location"], o.get("location")),
                    name=o["name"],
                    status=self.name_of(ENDPOINT["status"], o.get("status")),
                    role=self.name_of(ENDPOINT["role"], o.get("role")),
                    type=_choice(o.get("type")),
                    width=_int(_choice(o.get("width")), 19),
                    u_height=_int(o.get("u_height"), 42),
                    desc_units=bool(o.get("desc_units")),
                    comments=o.get("comments") or "",
                    tags=self._user_tags(o),
                    nb_id=o["id"],
                ),
                o,
            )
        for o in self._owned("device"):
            dtype = o.get("device_type") or {}
            position = o.get("position")
            self._load(
                self.device(
                    name=o.get("name") or "",
                    device_type=_ref_name(dtype, "model") or self.name_of(ENDPOINT["device_type"], dtype),
                    manufacturer=self.name_of(ENDPOINT["manufacturer"], dtype.get("manufacturer"))
                    if isinstance(dtype, dict)
                    else "",
                    role=self.name_of(ENDPOINT["role"], o.get("role")),
                    status=self.name_of(ENDPOINT["status"], o.get("status")),
                    location=self.name_of(ENDPOINT["location"], o.get("location")),
                    rack=self.name_of(ENDPOINT["rack"], o.get("rack")) or None,
                    position=_int(position) if position is not None else None,
                    face=_choice(o.get("face")),
                    railyard_id=str((o.get("custom_fields") or {}).get(CUSTOM_FIELD) or "")
                    if self.custom_field
                    else "",
                    comments=o.get("comments") or "",
                    tags=self._user_tags(o),
                    nb_id=o["id"],
                ),
                o,
            )
        for o in self._owned("rear_port"):
            self._load(
                self.rear_port(
                    device=self.name_of(ENDPOINT["device"], o.get("device")),
                    name=o["name"],
                    type=_choice(o.get("type")),
                    positions=_int(o.get("positions"), 1),
                    nb_id=o["id"],
                ),
                o,
            )
        for o in self._owned("front_port"):
            self._load(
                self.front_port(
                    device=self.name_of(ENDPOINT["device"], o.get("device")),
                    name=o["name"],
                    type=_choice(o.get("type")),
                    rear_port=self.name_of(ENDPOINT["rear_port"], o.get("rear_port")),
                    rear_port_position=_int(o.get("rear_port_position"), 1),
                    nb_id=o["id"],
                ),
                o,
            )
        for o in self._owned("interface"):
            self._load(
                self.interface(
                    device=self.name_of(ENDPOINT["device"], o.get("device")),
                    name=o["name"],
                    type=_choice(o.get("type")),
                    status=self.name_of(ENDPOINT["status"], o.get("status")),
                    nb_id=o["id"],
                ),
                o,
            )
        for model_type in ("power_outlet", "power_port"):
            for o in self._owned(model_type):
                model = getattr(self, model_type)
                device = self.name_of(ENDPOINT["device"], o.get("device"))
                self._load(model(device=device, name=o["name"], type=_choice(o.get("type")), nb_id=o["id"]), o)
        for o in self._owned("cable"):
            ends = [self._termination(o, side) for side in ("a", "b")]
            if None in ends:
                continue  # not a shape this sync creates (a circuit, a power feed…)
            (a_dev, a_type, a_name), (b_dev, b_type, b_name) = ends
            cable_type = _choice(o.get("type"))
            self._load(
                self.cable(
                    a_device=a_dev,
                    a_type=a_type,
                    a_name=a_name,
                    b_device=b_dev,
                    b_type=b_type,
                    b_name=b_name,
                    is_power=cable_type == "power" or bool({a_type, b_type} & {"dcim.powerport", "dcim.poweroutlet"}),
                    label=o.get("label") or "",
                    type=cable_type,
                    status=self.name_of(ENDPOINT["status"], o.get("status")),
                    color=(o.get("color") or "").lower(),
                    nb_id=o["id"],
                ),
                o,
            )

    def _load(self, model, obj: dict) -> None:
        self._add(model)
        self.remember(model.get_type(), model.get_unique_id(), obj)

    def _termination(self, cable: dict, side: str) -> tuple[str, str, str] | None:
        ct = str(cable.get(f"termination_{side}_type") or "")
        model_type = CT_MODEL.get(ct)
        if model_type is None:
            return None
        term = cable.get(f"termination_{side}")
        if not isinstance(term, dict) or "name" not in term:
            term = self.client.get(ENDPOINT[model_type], cable.get(f"termination_{side}_id") or _ref_id(term))
        if not term:
            return None
        device = self.name_of(ENDPOINT["device"], term.get("device"))
        return (device, ct, str(term.get("name") or "")) if device else None

    # -- ownership pre-pass --------------------------------------------------------------------

    def _owns_model(self, model) -> bool:
        return self.get_or_none(model.get_type(), model.get_unique_id()) is not None

    def reconcile(self, source: Adapter, *, allow_deletes: bool = False) -> OwnershipReport:
        """Decide, before anything is written, what to do about Railyard objects the sync doesn't own yet but that
        already exist in Nautobot, and drop from ``source`` everything it must not create. Read-only, so a dry run
        reports exactly what a real run will skip, rename or use as they are."""
        self.source = source
        report = self.report
        self._detect_renames(source)
        drop: list = []
        referenced_racks: list = []
        skipped_locations: set[str] = set()
        skipped_racks: set[tuple[str, str]] = set()
        skipped_devices: set[str] = set()
        skipped_ends: set[tuple[str, str, str]] = set()

        def reference(model, what: str) -> None:
            report.referenced.append(f"{what} (exists; used as-is, not modified)")
            drop.append(model)

        def conflict(model, why: str) -> None:
            report.conflicts.append(why)
            drop.append(model)

        def new(type_name: str) -> list:
            return [m for m in source.get_all(type_name) if not self._owns_model(m)]

        # Organisational objects: used as they are when they exist. One that is not enabled for what uses it
        # can't be used, and makes those objects conflicts below.
        needs = self._needed_content_types(source)
        for type_name in ("location_type", "status", "role"):
            for m in new(type_name):
                existing = self.find(type_name, m.name)
                if existing is None:
                    continue
                reference(m, f"{type_name.replace('_', ' ')} {m.name}")
                missing = sorted(set(needs.get((type_name, m.name), ())) - set(content_types(existing)))
                if missing:
                    self.unusable[(type_name, m.name)] = missing
        for type_name in ("status", "role"):  # statuses and roles the document names but does not list
            for (needed_type, name), cts in needs.items():
                if needed_type != type_name or source.get_or_none(type_name, name) is not None:
                    continue
                if self._owns_model_named(type_name, name):
                    continue
                existing = self.find(type_name, name)
                if existing is None:
                    self.unusable[(type_name, name)] = ["(it does not exist)"]
                    continue
                missing = sorted(set(cts) - set(content_types(existing)))
                if missing:
                    self.unusable[(type_name, name)] = missing
        for (type_name, name), missing in self.unusable.items():
            if missing == ["(it does not exist)"]:
                report.conflicts.append(
                    f"{type_name} {name} doesn't exist in Nautobot; the objects that use it are skipped"
                )
            else:
                report.conflicts.append(
                    f"{type_name.replace('_', ' ')} {name} exists but isn't enabled for {', '.join(missing)}; the "
                    "objects that need it are skipped (add those content types to it in Nautobot)"
                )
        for m in new("manufacturer"):
            if self.find("manufacturer", m.name) is not None:
                reference(m, f"manufacturer {m.name}")
        for dt in new("device_type"):
            if self.find("device_type", dt.manufacturer, dt.model) is not None:
                reference(dt, f"device type {dt.manufacturer} {dt.model}")

        def unusable(*refs: tuple[str, str | None]) -> str:
            for type_name, name in refs:
                if name and (type_name, name) in self.unusable:
                    return f"its {type_name.replace('_', ' ')} {name} can't be used"
            return ""

        for loc in new("location"):
            existing = self._unique_location(loc.name, loc.parent)
            if existing is not None:
                self.remember("location", loc.name, existing)
                reference(loc, f"location {loc.name}")
            elif why := unusable(("location_type", loc.location_type), ("status", loc.status)):
                skipped_locations.add(loc.name)
                conflict(loc, f"location {loc.name}: skipped because {why}")
        for rack in new("rack"):
            loc_type = self._location_type_of(source, rack.location)
            why = "its location was skipped" if rack.location in skipped_locations else ""
            why = (
                why
                or unusable(("status", rack.status), ("role", rack.role))
                or self._cannot_hold(loc_type, "dcim.rack")
            )
            if why:
                skipped_racks.add((rack.location, rack.name))
                conflict(rack, f"rack {rack.location}/{rack.name}: skipped because {why}")
            elif self.find("rack", rack.location, rack.name) is not None:
                referenced_racks.append(rack)
                reference(rack, f"rack {rack.location}/{rack.name}")
        # Nautobot puts a racked device in its rack's location; in a rack used as-is that is the rack's real
        # location, which need not be the one Railyard drew.
        used_as_is = {(r.location, r.name) for r in referenced_racks}
        for dev in source.get_all("device"):
            if dev.rack and (dev.location, dev.rack) in used_as_is:
                dev.location = _ref_name(self.find("rack", dev.location, dev.rack).get("location")) or dev.location

        for dev in new("device"):
            loc_type = self._location_type_of(source, dev.location)
            if dev.location in skipped_locations or (dev.location, dev.rack) in skipped_racks:
                why = "its location or rack was skipped"
            else:
                why = unusable(("status", dev.status), ("role", dev.role)) or self._cannot_hold(loc_type, "dcim.device")
            if why:
                skipped_devices.add(dev.name)
                conflict(dev, f"device {dev.name}: skipped because {why}")
                continue
            existing = self._device_in_location(dev.location, dev.name)
            if existing is not None:
                skipped_devices.add(dev.name)
                why = (
                    "carries this sync's tag but was not read back"
                    if self.tag_id in tag_ids(existing)
                    else "already exists there and is not managed by this sync"
                )
                conflict(dev, f"device {dev.name} in location {dev.location} {why}")

        for type_name in COMPONENT_TYPES:
            ct = CONTENT_TYPE[type_name]
            for comp in new(type_name):
                status = getattr(comp, "status", "")
                if comp.device in skipped_devices or (status and unusable(("status", status))):
                    if comp.device in skipped_devices:
                        report.dependents_skipped += 1
                    else:
                        report.conflicts.append(
                            f"{type_name} {comp.device}:{comp.name}: {unusable(('status', status))}"
                        )
                    skipped_ends.add((comp.device, ct, comp.name))
                    drop.append(comp)
                    continue
                owner = self.get_or_none("device", comp.device)
                if owner is None or owner.nb_id is None:
                    continue  # its device is created by this run, so the component can't exist yet
                if self.client.first(ENDPOINT[type_name], device=owner.nb_id, name=comp.name) is not None:
                    report.adopted.append(f"{type_name.replace('_', ' ')} {comp.device}:{comp.name}")

        doomed_cables = (
            {c.nb_id for c in self.get_all("cable") if source.get_or_none("cable", c.get_unique_id()) is None}
            if allow_deletes
            else set()
        )
        for cab in new("cable"):
            ends = [(cab.a_device, cab.a_type, cab.a_name), (cab.b_device, cab.b_type, cab.b_name)]
            if any(end[0] in skipped_devices or end in skipped_ends for end in ends):
                report.dependents_skipped += 1
                drop.append(cab)
                continue
            if why := unusable(("status", cab.status)):
                conflict(cab, f"cable {cab.label or cab.get_unique_id()}: skipped because {why}")
                continue
            for dev_name, ct, name in ends:
                term = self.owned_termination(dev_name, ct, name)
                cable_id = _ref_id(term.get("cable")) if term else None
                if cable_id is not None and cable_id not in doomed_cables:
                    conflict(cab, f"cable {cab.label or cab.get_unique_id()}: {dev_name}:{name} is already cabled")
                    break

        for model in drop:
            source.remove(model)
        return report

    def _owns_model_named(self, type_name: str, name: str) -> bool:
        return self.get_or_none(type_name, name) is not None

    @staticmethod
    def _needed_content_types(source: Adapter) -> dict[tuple[str, str], set[str]]:
        """Which content types each status, role and location type must be enabled for, by what uses it."""
        needs: dict[tuple[str, str], set[str]] = {}

        def need(type_name: str, name: str | None, ct: str) -> None:
            if name:
                needs.setdefault((type_name, name), set()).add(ct)

        for loc in source.get_all("location"):
            need("status", loc.status, "dcim.location")
        for rack in source.get_all("rack"):
            need("status", rack.status, "dcim.rack")
            need("role", rack.role, "dcim.rack")
        for dev in source.get_all("device"):
            need("status", dev.status, "dcim.device")
            need("role", dev.role, "dcim.device")
        for iface in source.get_all("interface"):
            need("status", iface.status, "dcim.interface")
        for cable in source.get_all("cable"):
            need("status", cable.status, "dcim.cable")
        return needs

    def _location_type_of(self, source: Adapter, location: str) -> tuple[str, list[str] | None]:
        """(location type name, its content types) of a location by name; content types ``None`` when unknown."""
        owned = self.get_or_none("location", location)
        existing = None if owned is not None else self._known.get(("location", location))
        if owned is not None:
            type_name = owned.location_type
        elif existing is not None:  # used as it is: its own type, whatever Railyard drew
            type_name = self.name_of(ENDPOINT["location_type"], existing.get("location_type"))
        elif (drawn := source.get_or_none("location", location)) is not None:
            type_name = drawn.location_type
        else:
            existing = self.find("location", location)
            type_name = self.name_of(ENDPOINT["location_type"], existing.get("location_type")) if existing else ""
        if not type_name:
            return "", None
        if (owned_type := self.get_or_none("location_type", type_name)) is not None:
            return type_name, list(owned_type.content_types)
        if (found := self._known.get(("location_type", type_name))) is not None:  # used as it is
            return type_name, content_types(found)
        if (drawn_type := source.get_or_none("location_type", type_name)) is not None:  # created by this run
            return type_name, list(drawn_type.content_types)
        existing = self.find("location_type", type_name)
        return type_name, content_types(existing) if existing else None

    @staticmethod
    def _cannot_hold(loc_type: tuple[str, list[str] | None], ct: str) -> str:
        name, cts = loc_type
        if cts is None or ct in cts:
            return ""
        return f"its location's type {name} isn't enabled for {ct}"

    def _device_in_location(self, location: str, name: str) -> dict | None:
        loc = self.find("location", location)
        if loc is None:
            return None
        found = self.client.list(ENDPOINT["device"], location=loc["id"], name__ie=name)
        return next((d for d in found if _ref_id(d.get("location")) == loc["id"]), None)

    def _detect_renames(self, source: Adapter) -> None:
        """Owned devices whose Railyard id now has another name: re-key them (with their components and cables)
        under the new name, so the diff lines up, and remember the rename for ``apply_renames``."""
        by_rid = {d.railyard_id: d for d in source.get_all("device") if d.railyard_id}
        for dev in list(self.get_all("device")):
            new = by_rid.get(dev.railyard_id) if dev.railyard_id else None
            if new is None or new.name == dev.name or dev.nb_id is None:
                continue
            if source.get_or_none("device", dev.name) is not None or self.get_or_none("device", new.name) is not None:
                continue  # names swapped between devices: leave it to create/update/delete
            existing = self._device_in_location(new.location, new.name)
            if existing is not None and existing.get("id") != dev.nb_id:
                continue  # the new name is taken in Nautobot: reconcile reports the conflict
            self._rekey_device(dev.name, new.name)
            self.renames.append((dev.name, new.name, dev.nb_id))
            self.report.renamed.append(f"device {dev.name} → {new.name}")

    def _rekey_device(self, old: str, new: str) -> None:
        def copy(model, **changes):
            data = {**model.get_identifiers(), **model.get_attrs(), "nb_id": model.nb_id, **changes}
            return type(model)(**data)

        moved = []
        for type_name in ("device", *COMPONENT_TYPES, "cable"):
            for model in list(self.get_all(type_name)):
                changes = {}
                if type_name == "device" and model.name == old:
                    changes["name"] = new
                elif type_name in COMPONENT_TYPES and model.device == old:
                    changes["device"] = new
                elif type_name == "cable":
                    changes = {f"{s}_device": new for s in ("a", "b") if getattr(model, f"{s}_device") == old}
                if changes:
                    self.remove(model)
                    moved.append(copy(model, **changes))
        for model in moved:
            self._add(model)

    def apply_renames(self) -> None:
        """Rename the Nautobot devices ``reconcile`` found renamed in Railyard (a real run only)."""
        for old, new, nb_id in self.renames:
            try:
                obj = self.client.get(ENDPOINT["device"], nb_id)
                if obj is None or not self.owns("device", obj):
                    raise NautobotError("it is no longer a Railyard-owned object in Nautobot")
                self.client.update(ENDPOINT["device"], nb_id, {"name": new})
            except NautobotError as exc:
                self.report.errors.append(self.client.scrub(f"rename device {old} → {new}: {exc}"))
                continue
            self.counts["update"]["device"] += 1

    # -- deletes -------------------------------------------------------------------------------

    def delete_candidates(self, source: Adapter) -> list:
        """Owned objects no longer in Railyard, in a safe deletion order (dependents first)."""
        out = []
        for type_name in reversed(self.top_level):
            out += [m for m in self.get_all(type_name) if source.get_or_none(type_name, m.get_unique_id()) is None]
        return out

    def preview_delete(self, model, scheduled: set[tuple[str, str]]) -> list[str]:
        """Why deleting ``model`` would be refused (empty if it wouldn't), ignoring blockers this run deletes
        first. Read-only; used by the dry run."""
        obj = self.client.get(ENDPOINT[model.get_type()], model.nb_id) if model.nb_id else None
        if obj is None or not self.owns(model.get_type(), obj):
            return ["no longer a Railyard-owned object"]
        return self.deletion_blockers(model.get_type(), obj, scheduled)

    def deletion_blockers(self, model_type: str, obj: dict, scheduled: set[tuple[str, str]] = frozenset()) -> list[str]:
        """Reasons deleting ``obj`` would reach beyond what this sync owns. ``scheduled`` lists (endpoint, id) this
        run deletes first, which therefore don't block."""
        reasons: list[str] = []
        owned_types = {v: k for k, v in ENDPOINT.items()}

        def foreign(endpoint: str, found: list[dict]) -> int:
            model_type_ = owned_types.get(endpoint)
            return sum(
                1
                for o in found
                if (endpoint, str(o.get("id"))) not in scheduled and not (model_type_ and self.owns(model_type_, o))
            )

        def query(endpoint: str, **filters) -> list[dict] | None:
            try:
                return self.client.list(endpoint, **filters)
            except NautobotNotFoundError:
                return []  # an endpoint this Nautobot release doesn't have
            except NautobotError as exc:
                reasons.append(self.client.scrub(f"could not check its {_noun(endpoint, 2)}: {exc}"))
                return None

        for endpoint, param, check, template in _DEPENDENTS.get(model_type, []):
            found = query(endpoint, **{param: obj["id"]})
            if not found:
                continue
            if check is not None:
                found = [o for o in found if _ref_id(o.get(check)) == str(obj["id"])]
            if n := foreign(endpoint, found):
                reasons.append(template.format(n=n, what=_noun(endpoint, n)))
        if model_type == "device":  # child devices in its bays are uninstalled with it
            bays = query("dcim/device-bays", device=obj["id"]) or []
            if n := sum(1 for b in bays if _ref_id(b.get("installed_device"))):
                reasons.append(_MODIFY.format(n=n, what="child device" if n == 1 else "child devices"))
        if model_type in COMPONENT_TYPES and (cable_id := _ref_id(obj.get("cable"))) is not None:
            cable = self.client.get(ENDPOINT["cable"], cable_id)
            if cable is not None and foreign(ENDPOINT["cable"], [cable]):
                reasons.append(_CONNECTED.format(n=1, what="cable"))
        return reasons


__all__ = [
    "CUSTOM_FIELD",
    "ENDPOINT",
    "OWNER_FIELD",
    "NautobotAuthError",
    "NautobotClient",
    "NautobotConnectionError",
    "NautobotError",
    "NautobotNotFoundError",
    "NautobotPermissionError",
    "NautobotRESTAdapter",
    "NautobotVersionError",
    "check_version",
    "parse_version",
]
