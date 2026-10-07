"""The NetBox DiffSync *target* adapter over NetBox's REST API (NetBox 4.0 to 4.6).

This is the command-line twin of the NetBox plugin's ORM target (``netbox_railyard/target.py``), with
the same ownership rules, so a NetBox can be synced by either:

- **Owned objects** carry the project's ownership tag (``policy.ownership_tag``). ``load()`` reads back
  only those (``?tag=<slug>``), each remembering its NetBox id, and ``update``/``delete`` act only on
  that exact object, re-fetched by id and checked for the tag first. An object without the tag is
  never updated or deleted.
- **Shared objects that already exist** (manufacturers, device types, roles, sites, locations, racks)
  are *used* as they are — a device may be placed in an existing site or rack — but never tagged,
  changed or deleted (``OwnershipReport.referenced``).
- **Conflicts** — a device of the same name already in the site, a device type whose slug another
  type uses, a port that is already cabled — are reported and skipped, together with whatever depends
  on them. The sync never adopts them.
- **Components on a device the sync owns** that already exist by name (created from the device type's
  component templates when the sync created the device) are adopted and tagged.
- **Renames**: a device the sync owns whose Railyard id (the ``railyard_id`` custom field) now has a
  different name in Railyard is renamed in place, keeping its components, cables and history.
- **Deletes** (opt-in, run by ``run.sync_to_netbox``) are refused — the object is kept and reported —
  when NetBox would protect it, or cascade to, modify or disconnect anything the sync doesn't own.
  Deleting an owned device removes its components with it, as NetBox always does.

The REST API is not transactional: a failed write is recorded in ``OwnershipReport.errors`` and the
sync carries on with what does not depend on it; running it again converges.

Version differences handled here (the rest of the API is stable across 4.0 to 4.6):

- Up to 4.4 a front port has ``rear_port`` + ``rear_port_position``. From 4.5 the mapping is a list,
  ``rear_ports: [{position, rear_port, rear_port_position}]``, and a front port has ``positions``.
  Both are read; writes use the shape of the connected NetBox (:func:`front_port_mappings`).
- Tokens: ``Authorization: Token <key>`` for v1 tokens, ``Bearer <token>`` for the v2 tokens NetBox 4.5
  introduced (they start ``nbt_``). The token never appears in a log, an error or a ``repr``.

A ``session`` (anything with ``request(method, url, headers=, params=, json=, timeout=, verify=)``
returning an object with ``status_code``/``json()``/``text``, i.e. a ``requests.Session``) can be
injected, which is how the tests run against an in-memory NetBox.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable, Iterable
from typing import Any

from diffsync import Adapter
from diffsync.exceptions import ObjectAlreadyExists, ObjectNotCreated, ObjectNotUpdated

from .. import dcim_http as http
from ..dcim_http import NETBOX, APIError, RESTClient
from . import models
from .devicetype_library import DeviceTypeLibrary
from .mappings import slugify
from .ownership import COMPONENT_TYPES, OwnershipMixin, OwnershipReport

log = logging.getLogger(__name__)

MIN_VERSION = (4, 0)
MAX_TESTED_VERSION = (4, 6)
#: The first NetBox release whose front ports map onto rear ports through a ``rear_ports`` list.
FRONT_PORT_MAPPINGS_FROM = (4, 5)
V2_TOKEN_PREFIX = "nbt_"
CUSTOM_FIELD = "railyard_id"

#: diffsync model name -> REST endpoint (below ``/api/``).
ENDPOINT = {
    "manufacturer": "dcim/manufacturers",
    "device_type": "dcim/device-types",
    "device_role": "dcim/device-roles",
    "site": "dcim/sites",
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

#: cable-termination content type -> the diffsync component model it lands on.
CT_MODEL = {
    "dcim.interface": "interface",
    "dcim.frontport": "front_port",
    "dcim.rearport": "rear_port",
    "dcim.powerport": "power_port",
    "dcim.poweroutlet": "power_outlet",
}


# ---- errors --------------------------------------------------------------------------------------


class NetBoxError(APIError):
    """A NetBox API request failed. Carries the HTTP status when there was a response, and the request's
    method, path and NetBox request id (``X-Request-ID``) when there was one."""


class NetBoxConnectionError(NetBoxError):
    """NetBox could not be reached (DNS, TLS, refused, timed out)."""


class NetBoxAuthError(NetBoxError):
    """401 — NetBox did not accept the API token."""


class NetBoxPermissionError(NetBoxError):
    """403 — the token is valid but may not do this."""


class NetBoxNotFoundError(NetBoxError):
    """404 — the object or endpoint does not exist (an endpoint may be missing in an older release)."""


class NetBoxVersionError(NetBoxError):
    """The NetBox release is not one this sync supports."""


# ---- HTTP client ---------------------------------------------------------------------------------


def parse_version(text: str) -> tuple[int, int]:
    """``"4.5.2"`` / ``"v4.6.0-Docker-3.4"`` -> ``(4, 5)``; ``(0, 0)`` when unreadable."""
    match = re.match(r"v?(\d+)\.(\d+)", (text or "").strip())
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


class NetBoxClient(RESTClient):
    """A small NetBox REST client: list (paginated), get, create, update (PATCH), delete."""

    product = NETBOX
    logger = log
    errors = {
        "base": NetBoxError,
        "connection": NetBoxConnectionError,
        http.AUTH: NetBoxAuthError,
        http.PERMISSION: NetBoxPermissionError,
        http.NOT_FOUND: NetBoxNotFoundError,
    }

    def auth_header(self) -> str:
        return f"Bearer {self._token}" if self._token.startswith(V2_TOKEN_PREFIX) else f"Token {self._token}"

    def status(self) -> dict:
        data = self.request("GET", "/api/status/")
        if not isinstance(data, dict):
            raise NetBoxError(f"NetBox returned an unexpected status for {self.url}: is it a NetBox server?")
        self.version = str(data.get("netbox-version") or "")
        return data


# ---- value helpers -------------------------------------------------------------------------------


def _choice(value: Any) -> Any:
    """A NetBox choice field (``{"value", "label"}``) as its value; blank (``None``) as ``""``."""
    if isinstance(value, dict):
        value = value.get("value")
    return "" if value is None else value


def _ref_id(value: Any) -> int | None:
    if isinstance(value, dict):
        value = value.get("id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _ref_name(value: Any, key: str = "name") -> str:
    return str(value.get(key) or "") if isinstance(value, dict) else ""


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def tag_slugs(obj: dict) -> list[str]:
    out = []
    for tag in obj.get("tags") or []:
        slug = tag.get("slug") if isinstance(tag, dict) else None
        if slug:
            out.append(str(slug))
    return out


def tag_ids(obj: dict) -> list[int]:
    return [i for i in (_ref_id(t) for t in obj.get("tags") or []) if i is not None]


def front_port_mappings(version: str | tuple[int, int]) -> bool:
    """Whether a NetBox release maps front ports onto rear ports through a ``rear_ports`` list."""
    parsed = parse_version(version) if isinstance(version, str) else version
    return parsed >= FRONT_PORT_MAPPINGS_FROM


def front_port_rear(obj: dict) -> tuple[int | None, int]:
    """A front port's (rear port id, rear port position) in either API shape."""
    for key in ("rear_ports", "mappings"):
        mappings = obj.get(key)
        if isinstance(mappings, list):
            if not mappings:
                return None, 1
            first = min(mappings, key=lambda m: _int(m.get("position"), 1) if isinstance(m, dict) else 0)
            return _ref_id(first.get("rear_port")), _int(first.get("rear_port_position"), 1)
    return _ref_id(obj.get("rear_port")), _int(obj.get("rear_port_position"), 1)


# ---- write helpers -------------------------------------------------------------------------------


def _label(model_type: str, unique_id: str) -> str:
    return f"{model_type.replace('_', ' ')} {unique_id}"


def _fail(adapter: NetBoxRESTAdapter, exc_type: type[Exception], what: str, why: str):
    message = adapter.client.scrub(f"{what}: {why}")
    adapter.report.errors.append(message)
    return exc_type(message)


def _created(cls, adapter: NetBoxRESTAdapter, ids: dict, attrs: dict, obj: dict):
    model = super(cls, cls).create(adapter, ids=ids, attrs=attrs)
    model.nb_id = obj.get("id")
    adapter.counts["create"][cls.get_type()] += 1
    return model


def _create(cls, adapter: NetBoxRESTAdapter, ids: dict, attrs: dict, build):
    """Run ``build() -> NetBox object`` for a create, turning NetBox's refusal into ObjectNotCreated."""
    what = f"create {_label(cls.get_type(), cls.create_unique_id(**ids))}"
    try:
        obj = build()
    except NetBoxError as exc:
        raise _fail(adapter, ObjectNotCreated, what, str(exc)) from None
    return _created(cls, adapter, ids, attrs, obj)


def _owned_object(model) -> dict:
    """Re-fetch the NetBox object behind a loaded model by id, checking it still carries the tag."""
    adapter: NetBoxRESTAdapter = model.adapter
    obj = adapter.client.get(ENDPOINT[model.get_type()], model.nb_id) if model.nb_id else None
    if obj is None or adapter.tag_slug not in tag_slugs(obj):
        raise NetBoxError("it is no longer a Railyard-owned object in NetBox")
    return obj


def _update(model, attrs: dict, payload: dict | Callable[[], dict]) -> None:
    """PATCH the owned object behind ``model`` with ``payload`` (or what ``payload()`` builds, when it
    has to look things up); NetBox refusing either becomes ObjectNotUpdated."""
    adapter: NetBoxRESTAdapter = model.adapter
    what = f"update {_label(model.get_type(), model.get_unique_id())}"
    try:
        _owned_object(model)
        data = payload() if callable(payload) else payload
        if data:
            adapter.client.update(ENDPOINT[model.get_type()], model.nb_id, data)
    except NetBoxError as exc:
        raise _fail(adapter, ObjectNotUpdated, what, str(exc)) from None
    adapter.counts["update"][model.get_type()] += 1


def _delete(model) -> bool:
    """Delete an owned object unless that would reach beyond what the sync owns (then keep it)."""
    adapter: NetBoxRESTAdapter = model.adapter
    label = _label(model.get_type(), model.get_unique_id())
    endpoint = ENDPOINT[model.get_type()]
    obj = adapter.client.get(endpoint, model.nb_id) if model.nb_id else None
    if obj is None:
        return False  # already gone (e.g. with a device deleted before its components)
    if adapter.tag_slug not in tag_slugs(obj):
        adapter.report.kept.append(f"{label}: no longer carries the ownership tag")
        return False
    try:
        blockers = adapter.deletion_blockers(model.get_type(), obj)
    except NetBoxError as exc:
        blockers = [adapter.client.scrub(f"could not check what depends on it: {exc}")]
    if blockers:
        adapter.report.kept.append(f"{label}: {'; '.join(blockers)}")
        return False
    try:
        adapter.client.delete(endpoint, model.nb_id)
    except NetBoxError as exc:
        adapter.report.kept.append(adapter.client.scrub(f"{label}: NetBox refused the delete: {exc}"))
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


class NetBoxManufacturer(_DeleteMixin, models.Manufacturer):
    nb_id: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        payload = {"name": ids["name"], "slug": attrs.get("slug") or slugify(ids["name"]), "tags": adapter.tags_for()}
        return _create(cls, adapter, ids, attrs, lambda: adapter.client.create(ENDPOINT["manufacturer"], payload))

    def update(self, attrs):
        _update(self, attrs, {k: attrs[k] for k in ("slug",) if attrs.get(k)})
        return super().update(attrs)


class NetBoxDeviceType(_DeleteMixin, models.DeviceType):
    nb_id: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        def build():
            mfr = adapter.find("manufacturer", ids["manufacturer"])
            if mfr is None:
                raise NetBoxError(f"manufacturer {ids['manufacturer']} not found")
            obj = adapter.client.create(
                ENDPOINT["device_type"],
                {
                    "manufacturer": mfr["id"],
                    "model": ids["model"],
                    "slug": attrs.get("slug") or slugify(f"{ids['manufacturer']}-{ids['model']}"),
                    "u_height": attrs.get("u_height", 1),
                    "is_full_depth": attrs.get("is_full_depth", True),
                    "part_number": attrs.get("part_number", ""),
                    "tags": adapter.tags_for(),
                },
            )
            adapter.import_templates(obj, ids, attrs)
            return obj

        return _create(cls, adapter, ids, attrs, build)

    def update(self, attrs):
        fields = ("slug", "u_height", "is_full_depth", "part_number")
        _update(self, attrs, {k: attrs[k] for k in fields if k in attrs})
        return super().update(attrs)


class NetBoxDeviceRole(_DeleteMixin, models.DeviceRole):
    nb_id: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        payload = {
            "name": ids["name"],
            "slug": attrs.get("slug") or slugify(ids["name"]),
            "color": attrs.get("color") or "9e9e9e",
            "tags": adapter.tags_for(),
        }
        return _create(cls, adapter, ids, attrs, lambda: adapter.client.create(ENDPOINT["device_role"], payload))

    def update(self, attrs):
        _update(self, attrs, {k: attrs[k] for k in ("slug", "color") if attrs.get(k)})
        return super().update(attrs)


class NetBoxSite(_DeleteMixin, models.Site):
    nb_id: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        payload = {
            "name": ids["name"],
            "slug": attrs.get("slug") or slugify(ids["name"]),
            "status": attrs.get("status") or "active",
            "facility": attrs.get("facility", ""),
            "tags": adapter.tags_for(),
        }
        return _create(cls, adapter, ids, attrs, lambda: adapter.client.create(ENDPOINT["site"], payload))

    def update(self, attrs):
        _update(self, attrs, {k: attrs[k] for k in ("slug", "status", "facility") if k in attrs})
        return super().update(attrs)


class NetBoxLocation(_DeleteMixin, models.Location):
    nb_id: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        def build():
            site = adapter.require("site", ids["site"])
            parent = adapter.require("location", ids["site"], attrs["parent"]) if attrs.get("parent") else None
            return adapter.client.create(
                ENDPOINT["location"],
                {
                    "name": ids["name"],
                    "slug": attrs.get("slug") or slugify(ids["name"]),
                    "site": site["id"],
                    "parent": parent["id"] if parent else None,
                    "status": attrs.get("status") or "active",
                    "facility": attrs.get("facility", ""),
                    "tags": adapter.tags_for(),
                },
            )

        return _create(cls, adapter, ids, attrs, build)

    def update(self, attrs):
        def payload():
            data = {k: attrs[k] for k in ("slug", "status", "facility") if k in attrs}
            if "parent" in attrs:
                data["parent"] = self.adapter.ref_id("location", self.site, attrs["parent"])
            return data

        _update(self, attrs, payload)
        return super().update(attrs)


class NetBoxRack(_DeleteMixin, models.Rack):
    nb_id: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        def build():
            site = adapter.require("site", ids["site"])
            return adapter.client.create(
                ENDPOINT["rack"],
                {
                    "name": ids["name"],
                    "site": site["id"],
                    "location": adapter.ref_id("location", ids["site"], attrs.get("location")),
                    "status": attrs.get("status") or "active",
                    "width": attrs.get("width", 19),
                    "u_height": attrs.get("u_height", 42),
                    "desc_units": attrs.get("desc_units", False),
                    "comments": attrs.get("comments", ""),
                    "tags": adapter.tags_for(attrs.get("tags")),
                },
            )

        return _create(cls, adapter, ids, attrs, build)

    def update(self, attrs):
        def payload():
            data = {k: attrs[k] for k in ("status", "width", "u_height", "desc_units", "comments") if k in attrs}
            if "location" in attrs:
                data["location"] = self.adapter.ref_id("location", self.site, attrs["location"])
            if "tags" in attrs:
                data["tags"] = self.adapter.tags_for(attrs["tags"])
            return data

        _update(self, attrs, payload)
        return super().update(attrs)


class NetBoxDevice(_DeleteMixin, models.Device):
    nb_id: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        def build():
            site = adapter.require("site", attrs["site"])
            dtype = adapter.find("device_type", attrs["manufacturer"], attrs["device_type"])
            role = adapter.find("device_role", attrs["role"])
            rack = adapter.find("rack", attrs["site"], attrs["rack"]) if attrs.get("rack") else None
            if dtype is None or role is None or (attrs.get("rack") and rack is None):
                raise NetBoxError("its device type, role or rack was not found")
            payload = {
                "name": ids["name"],
                "device_type": dtype["id"],
                "role": role["id"],
                "site": site["id"],
                "rack": rack["id"] if rack else None,
                "location": adapter.ref_id("location", attrs["site"], attrs.get("location")),
                "position": attrs.get("position"),
                "status": attrs.get("status") or "active",
                "serial": attrs.get("serial", ""),
                "comments": attrs.get("comments", ""),
                "tags": adapter.tags_for(attrs.get("tags")),
            }
            if attrs.get("face"):
                payload["face"] = attrs["face"]
            if adapter.custom_field and attrs.get("railyard_id"):
                payload["custom_fields"] = {CUSTOM_FIELD: attrs["railyard_id"]}
            return adapter.client.create(ENDPOINT["device"], payload)

        return _create(cls, adapter, ids, attrs, build)

    def update(self, attrs):
        adapter: NetBoxRESTAdapter = self.adapter
        site = attrs.get("site", self.site)

        def payload():
            data = {k: attrs[k] for k in ("position", "face", "status", "serial", "comments") if k in attrs}
            if "role" in attrs:
                data["role"] = adapter.require("device_role", attrs["role"])["id"]
            if "device_type" in attrs or "manufacturer" in attrs:
                mfr, model = attrs.get("manufacturer", self.manufacturer), attrs.get("device_type", self.device_type)
                data["device_type"] = adapter.require("device_type", mfr, model)["id"]
            if "site" in attrs:
                data["site"] = adapter.require("site", site)["id"]
            if "rack" in attrs or "site" in attrs:
                rack = attrs.get("rack", self.rack)
                data["rack"] = adapter.require("rack", site, rack)["id"] if rack else None
            if "location" in attrs or "site" in attrs:
                data["location"] = adapter.ref_id("location", site, attrs.get("location", self.location))
            if "tags" in attrs:
                data["tags"] = adapter.tags_for(attrs["tags"])
            if "railyard_id" in attrs and adapter.custom_field:
                data["custom_fields"] = {CUSTOM_FIELD: attrs["railyard_id"] or None}
            if "position" in data and data["position"] is None:
                data["face"] = ""  # a device without a U position has no face
            return data

        _update(self, attrs, payload)
        return super().update(attrs)


# Components: (device, name, type) plus a little per kind. Module functions rather than a mixin with
# an underscore class attribute, because DiffSyncModel is Pydantic and would make it private.


def _ensure_component(
    cls, adapter: NetBoxRESTAdapter, ids: dict, attrs: dict, payload: dict, adopt: dict, mapping: dict | None = None
):
    """Create a component on a device the sync owns, or adopt a same-named one already on it.

    ``payload`` is what a new component is created with, ``adopt`` what an adopted one is brought in
    line with, and ``mapping`` a front port's coupling to its rear port (set on an adopted front port
    only when it has none).
    """
    model_type = cls.get_type()
    endpoint = ENDPOINT[model_type]

    def build():
        device = adapter.get_or_none("device", ids["device"])
        if device is None or device.nb_id is None:
            raise NetBoxError(f"device {ids['device']} is not managed by this sync")
        existing = adapter.client.first(endpoint, device_id=device.nb_id, name=ids["name"])
        if existing is None:
            data = {"device": device.nb_id, "name": ids["name"], **payload, **(mapping or {})}
            return adapter.client.create(endpoint, {**data, "tags": adapter.tags_for()})
        # Adopting: tag it and bring its type in line, leaving the rest as NetBox has it.
        patch = {"tags": sorted(set(tag_ids(existing)) | {adapter.tag_id}), **adopt}
        if mapping and front_port_rear(existing)[0] is None:
            patch.update(mapping)
        obj = adapter.client.update(endpoint, existing["id"], patch)
        label = f"{model_type.replace('_', ' ')} {ids['device']}:{ids['name']}"
        if label not in adapter.report.adopted:
            adapter.report.adopted.append(label)
        return obj

    return _create(cls, adapter, ids, attrs, build)


class NetBoxInterface(_DeleteMixin, models.Interface):
    nb_id: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        kind = {"type": attrs.get("type") or "other"}
        return _ensure_component(cls, adapter, ids, attrs, dict(kind), kind)

    def update(self, attrs):
        _update(self, attrs, {k: attrs[k] for k in ("type",) if k in attrs})
        return super().update(attrs)


class NetBoxRearPort(_DeleteMixin, models.RearPort):
    nb_id: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        kind = {"type": attrs.get("type") or "8p8c"}
        payload = {**kind, "positions": attrs.get("positions", 1)}
        return _ensure_component(cls, adapter, ids, attrs, payload, kind)

    def update(self, attrs):
        _update(self, attrs, {k: attrs[k] for k in ("type", "positions") if k in attrs})
        return super().update(attrs)


class NetBoxFrontPort(_DeleteMixin, models.FrontPort):
    nb_id: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        kind = {"type": attrs.get("type") or "8p8c"}
        payload = {**kind, "positions": 1} if adapter.port_mappings else dict(kind)
        what = f"create front port {ids['device']}:{ids['name']}"
        try:
            rear = adapter.rear_port_id(ids["device"], attrs.get("rear_port", ""))
        except NetBoxError as exc:
            raise _fail(adapter, ObjectNotCreated, what, str(exc)) from None
        if rear is None and not adapter.port_mappings:  # up to 4.4 a front port needs its rear port
            raise _fail(adapter, ObjectNotCreated, what, f"its rear port {attrs.get('rear_port')!r} is not synced")
        mapping = adapter.front_mapping(rear, attrs.get("rear_port_position", 1)) if rear is not None else None
        return _ensure_component(cls, adapter, ids, attrs, payload, kind, mapping)

    def update(self, attrs):
        adapter: NetBoxRESTAdapter = self.adapter

        def payload():
            data = {k: attrs[k] for k in ("type",) if k in attrs}
            if "rear_port" in attrs or "rear_port_position" in attrs:
                name = attrs.get("rear_port", self.rear_port)
                rear = adapter.rear_port_id(self.device, name)
                if rear is None:
                    raise NetBoxError(f"its rear port {name!r} is not synced")
                position = attrs.get("rear_port_position", self.rear_port_position)
                adapter.free_rear_slot(self.nb_id, rear, position)  # ports swapped in Railyard: step one
                data.update(adapter.front_mapping(rear, position))
            return data

        _update(self, attrs, payload)
        return super().update(attrs)


class NetBoxPowerOutlet(_DeleteMixin, models.PowerOutlet):
    nb_id: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        kind = {"type": attrs.get("type") or "iec-60320-c13"}
        return _ensure_component(cls, adapter, ids, attrs, dict(kind), kind)

    def update(self, attrs):
        _update(self, attrs, {k: attrs[k] for k in ("type",) if k in attrs})
        return super().update(attrs)


class NetBoxPowerPort(_DeleteMixin, models.PowerPort):
    nb_id: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        kind = {"type": attrs.get("type") or "iec-60320-c14"}
        return _ensure_component(cls, adapter, ids, attrs, dict(kind), kind)

    def update(self, attrs):
        _update(self, attrs, {k: attrs[k] for k in ("type",) if k in attrs})
        return super().update(attrs)


class NetBoxCable(_DeleteMixin, models.Cable):
    nb_id: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        def build():
            ends = []
            for side in ("a", "b"):
                term = adapter.owned_termination(ids[f"{side}_device"], ids[f"{side}_type"], ids[f"{side}_name"])
                if term is None:
                    raise NetBoxError(f"{ids[f'{side}_device']}:{ids[f'{side}_name']} is not a port this sync manages")
                if _ref_id(term.get("cable")) is not None:
                    raise NetBoxError(f"{ids[f'{side}_device']}:{ids[f'{side}_name']} is already cabled")
                ends.append([{"object_type": ids[f"{side}_type"], "object_id": term["id"]}])
            payload = {
                "a_terminations": ends[0],
                "b_terminations": ends[1],
                "status": attrs.get("status") or "connected",
                "label": attrs.get("label", ""),
                "tags": adapter.tags_for(),
            }
            cable_type = attrs.get("type") or ("power" if attrs.get("is_power") else "")
            if cable_type:
                payload["type"] = cable_type
            if attrs.get("color"):
                payload["color"] = attrs["color"]
            return adapter.client.create(ENDPOINT["cable"], payload)

        return _create(cls, adapter, ids, attrs, build)

    def update(self, attrs):
        payload = {k: attrs[k] for k in ("label", "status", "color", "type") if k in attrs}
        _update(self, attrs, payload)
        return super().update(attrs)


# ---- deletion guard ------------------------------------------------------------------------------

_PROTECT = "still in use by {n} {what} this sync doesn't delete"
_CASCADE = "would also delete {n} {what} not managed by this sync"
_MODIFY = "would modify {n} {what} not managed by this sync"
_CONNECTED = "connected by {n} {what} not managed by this sync"

#: What NetBox does to other objects when one of these is deleted: (endpoint, filter, the field to
#: re-check on each result — ``None`` when the filter is trusted — and how it is affected). Dependents
#: the sync owns don't block a delete; NetBox protecting the object, or a cascade, modification or
#: disconnection reaching objects it doesn't own, does.
_DEPENDENTS: dict[str, list[tuple[str, str, str | None, str]]] = {
    "manufacturer": [
        ("dcim/device-types", "manufacturer_id", "manufacturer", _PROTECT),
        ("dcim/module-types", "manufacturer_id", "manufacturer", _PROTECT),
        ("dcim/platforms", "manufacturer_id", "manufacturer", _MODIFY),
    ],
    "device_type": [("dcim/devices", "device_type_id", "device_type", _PROTECT)],
    "device_role": [
        ("dcim/devices", "role_id", "role", _PROTECT),
        ("virtualization/virtual-machines", "role_id", "role", _PROTECT),
    ],
    "site": [
        ("dcim/locations", "site_id", "site", _CASCADE),
        ("dcim/racks", "site_id", "site", _PROTECT),
        ("dcim/devices", "site_id", "site", _PROTECT),
        ("dcim/power-panels", "site_id", "site", _PROTECT),
    ],
    "location": [
        ("dcim/locations", "parent_id", "parent", _CASCADE),
        ("dcim/racks", "location_id", "location", _PROTECT),
        ("dcim/devices", "location_id", "location", _PROTECT),
        ("dcim/power-panels", "location_id", "location", _PROTECT),
    ],
    "rack": [
        ("dcim/devices", "rack_id", "rack", _PROTECT),
        ("dcim/power-feeds", "rack_id", "rack", _PROTECT),
        ("dcim/rack-reservations", "rack_id", "rack", _CASCADE),
    ],
    "device": [
        ("dcim/cables", "device_id", None, _CONNECTED),
        ("ipam/ip-addresses", "device_id", None, _CASCADE),
        ("ipam/services", "device_id", "device", _CASCADE),
        ("dcim/modules", "device_id", "device", _CASCADE),
        ("dcim/devices", "parent_device_id", "parent_device", _MODIFY),
    ],
    "interface": [
        ("ipam/ip-addresses", "interface_id", "assigned_object_id", _CASCADE),
        ("dcim/interfaces", "parent_id", "parent", _MODIFY),
        ("dcim/interfaces", "lag_id", "lag", _MODIFY),
        ("dcim/interfaces", "bridge_id", "bridge", _MODIFY),
    ],
    "power_port": [("dcim/power-outlets", "power_port_id", "power_port", _MODIFY)],
}

_NOUNS = {"ip-addresses": "IP addresses", "virtual-machines": "virtual machines"}


def _noun(endpoint: str, n: int) -> str:
    name = endpoint.split("/")[-1]
    noun = _NOUNS.get(name, name.replace("-", " "))
    if n != 1:
        return noun
    return noun[:-2] if noun.endswith("sses") else noun[:-1] if noun.endswith("s") else noun


# ---- adapter -------------------------------------------------------------------------------------


class NetBoxRESTAdapter(OwnershipMixin, Adapter):
    manufacturer = NetBoxManufacturer
    device_type = NetBoxDeviceType
    device_role = NetBoxDeviceRole
    site = NetBoxSite
    location = NetBoxLocation
    rack = NetBoxRack
    device = NetBoxDevice
    interface = NetBoxInterface
    rear_port = NetBoxRearPort
    front_port = NetBoxFrontPort
    power_outlet = NetBoxPowerOutlet
    power_port = NetBoxPowerPort
    cable = NetBoxCable

    top_level = models.TOP_LEVEL

    def __init__(
        self,
        client: NetBoxClient,
        *,
        tag: dict | None,
        tag_slug: str,
        user_tags: dict[str, int] | None = None,
        custom_field: bool = True,
        port_mappings: bool | None = None,
        import_components: bool = False,
        devicetype_library: DeviceTypeLibrary | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.client = client
        self.tag = tag  # None before the first real sync: nothing is owned yet
        self.tag_id: int | None = tag.get("id") if tag else None
        self.tag_slug = tag_slug
        self.user_tags = dict(user_tags or {})  # user tag slug -> NetBox id
        self.custom_field = custom_field  # the railyard_id custom field exists on devices
        self.port_mappings = front_port_mappings(client.version) if port_mappings is None else port_mappings
        self.import_components = import_components
        self.dtl = devicetype_library
        self.source: Adapter | None = None
        self.report = OwnershipReport()
        self.counts: dict[str, Counter] = {"create": Counter(), "update": Counter(), "delete": Counter()}
        self.renames: list[tuple[str, str, int]] = []  # (old name, new name, NetBox id)
        #: rear port id -> (its positions before free_rear_slot added one, its device id)
        self.parked: dict[int, tuple[int, int | None]] = {}
        self._cache: dict[tuple, dict] = {}

    # -- lookups -------------------------------------------------------------------------------

    def tags_for(self, user_slugs: Iterable[str] | None = None) -> list[int]:
        """The tag ids an owned object carries: the ownership tag plus its user tags that exist."""
        ids = [self.tag_id] if self.tag_id is not None else []
        ids += [self.user_tags[s] for s in user_slugs or [] if s in self.user_tags]
        return ids

    def _cached(self, key: tuple, endpoint: str, attempts: list[dict]) -> dict | None:
        if key in self._cache:
            return self._cache[key]
        for filters in attempts:
            found = self.client.first(endpoint, **filters)
            if found is not None:
                self._cache[key] = found
                return found
        return None

    def find(self, model_type: str, *key: str) -> dict | None:
        """The NetBox object a model's natural key names, owned or not (``None`` when absent)."""
        endpoint = ENDPOINT[model_type]
        if model_type in ("manufacturer", "device_role", "site"):
            (name,) = key
            return self._cached((model_type, name), endpoint, [{"name": name}, {"slug": slugify(name)}])
        if model_type == "device_type":
            mfr = self.find("manufacturer", key[0])
            if mfr is None:
                return None
            return self._cached((model_type, *key), endpoint, [{"manufacturer_id": mfr["id"], "model": key[1]}])
        if model_type in ("location", "rack"):
            site = self.find("site", key[0])
            if site is None or not key[1]:
                return None
            return self._cached((model_type, *key), endpoint, [{"site_id": site["id"], "name": key[1]}])
        raise ValueError(model_type)

    def require(self, model_type: str, *key: str) -> dict:
        found = self.find(model_type, *key)
        if found is None:
            raise NetBoxError(f"{model_type.replace('_', ' ')} {' / '.join(key)} not found")
        return found

    def ref_id(self, model_type: str, *key: str | None) -> int | None:
        """The id of an optional reference: ``None`` for a blank name, an error when it is missing."""
        if not key[-1]:
            return None
        return self.require(model_type, *key)["id"]

    def rear_port_id(self, device: str, name: str) -> int | None:
        if not name:
            return None
        rear = self.get_or_none("rear_port", {"device": device, "name": name})
        if rear is not None and rear.nb_id is not None:
            return rear.nb_id
        owner = self.get_or_none("device", device)
        if owner is None or owner.nb_id is None:
            return None
        found = self.client.first(ENDPOINT["rear_port"], device_id=owner.nb_id, name=name)
        return found["id"] if found else None

    def free_rear_slot(self, front_id: int | None, rear_id: int, position: int) -> None:
        """Make a rear port position free for the front port ``front_id`` to map onto it (step one of two).

        When two owned front ports swap rear ports in Railyard, the first update would map onto a position the
        other still holds, which NetBox refuses (one front port per rear port position). The holder, which this
        run re-maps too, is moved aside first: from 4.5 it is unmapped; up to 4.4, where a front port must have
        a rear port, it is parked on an extra position of the rear port, which :meth:`after_sync` removes once
        the holder has moved. A holder the sync doesn't own is never touched: the update fails instead."""
        rear = self.client.get(ENDPOINT["rear_port"], rear_id) or {}
        device_id = _ref_id(rear.get("device"))
        found = self.client.list(ENDPOINT["front_port"], device_id=device_id) if device_id is not None else []
        holder = next(
            (o for o in found if o.get("id") != front_id and front_port_rear(o) == (rear_id, position)),
            None,
        )
        if holder is None:
            return
        if self.tag_slug not in tag_slugs(holder):
            raise NetBoxError(
                f"rear port position {position} is mapped to front port {holder.get('name')!r}, which this sync "
                "doesn't own"
            )
        if self.port_mappings:
            mappings = holder.get("rear_ports") or holder.get("mappings") or []
            keep = [
                {
                    "position": m.get("position"),
                    "rear_port": _ref_id(m.get("rear_port")),
                    "rear_port_position": m.get("rear_port_position"),
                }
                for m in mappings
                if isinstance(m, dict)
                and (_ref_id(m.get("rear_port")), _int(m.get("rear_port_position"), 1)) != (rear_id, position)
            ]
            self.client.update(ENDPOINT["front_port"], holder["id"], {"rear_ports": keep})
            return
        if self.tag_slug not in tag_slugs(rear):
            raise NetBoxError(f"its rear port is held by front port {holder.get('name')!r} and isn't this sync's")
        positions = _int(rear.get("positions"), 1)
        self.parked.setdefault(rear_id, (positions, device_id))
        self.client.update(ENDPOINT["rear_port"], rear_id, {"positions": positions + 1})
        self.client.update(ENDPOINT["front_port"], holder["id"], {"rear_port_position": positions + 1})

    def after_sync(self) -> None:
        """Remove the extra rear port positions :meth:`free_rear_slot` added, once nothing is parked on them."""
        for rear_id, (positions, device_id) in self.parked.items():
            try:
                fronts = self.client.list(ENDPOINT["front_port"], device_id=device_id)
                if any(front_port_rear(o)[1] > positions for o in fronts if front_port_rear(o)[0] == rear_id):
                    self.report.warnings.append(
                        f"rear port {rear_id} keeps an extra position: a front port moved aside to free its position "
                        "is no longer in Railyard (allow deletes to remove it)"
                    )
                    continue
                self.client.update(ENDPOINT["rear_port"], rear_id, {"positions": positions})
            except NetBoxError as exc:
                self.report.errors.append(self.client.scrub(f"restore rear port {rear_id} positions: {exc}"))
        self.parked.clear()

    def front_mapping(self, rear_id: int, rear_pos: int) -> dict:
        """A front port's coupling to its rear port, in the connected NetBox's API shape."""
        if self.port_mappings:
            return {"rear_ports": [{"position": 1, "rear_port": rear_id, "rear_port_position": rear_pos}]}
        return {"rear_port": rear_id, "rear_port_position": rear_pos}

    def owned_termination(self, device: str, ct: str, name: str) -> dict | None:
        """A cable end on a device the sync owns (``None`` if the device or component isn't there)."""
        model_type = CT_MODEL.get(ct)
        owner = self.get_or_none("device", device)
        if model_type is None or owner is None or owner.nb_id is None:
            return None
        known = self.get_or_none(model_type, {"device": device, "name": name})
        if known is not None and known.nb_id is not None:
            return self.client.get(ENDPOINT[model_type], known.nb_id)
        return self.client.first(ENDPOINT[model_type], device_id=owner.nb_id, name=name)

    # -- load ----------------------------------------------------------------------------------

    def _add(self, model) -> None:
        try:
            self.add(model)
        except ObjectAlreadyExists:
            self.report.warnings.append(
                f"two Railyard-owned objects share the identity {model.get_type()} {model.get_unique_id()}; "
                f"NetBox #{model.nb_id} is ignored (neither updated nor deleted)"
            )

    def _user_tags(self, obj: dict) -> list[str]:
        return sorted(s for s in tag_slugs(obj) if s != self.tag_slug)

    def load(self) -> None:
        """Load only the objects this project owns (carry its tag), each with its NetBox id, so a re-sync
        diffs to no change and writes go to exactly these objects."""
        if self.tag is None:
            return

        def owned(model_type: str, **extra) -> list[dict]:
            return self.client.list(ENDPOINT[model_type], tag=self.tag_slug, **extra)

        for o in owned("manufacturer"):
            self._add(self.manufacturer(name=o["name"], slug=o.get("slug") or "", nb_id=o["id"]))
        for o in owned("device_type"):
            self._add(
                self.device_type(
                    manufacturer=_ref_name(o.get("manufacturer")),
                    model=o.get("model") or "",
                    slug=o.get("slug") or "",
                    u_height=_int(o.get("u_height"), 0),
                    is_full_depth=bool(o.get("is_full_depth")),
                    part_number=o.get("part_number") or "",
                    nb_id=o["id"],
                )
            )
        for o in owned("device_role"):
            self._add(
                self.device_role(name=o["name"], slug=o.get("slug") or "", color=o.get("color") or "", nb_id=o["id"])
            )
        for o in owned("site"):
            self._add(
                self.site(
                    name=o["name"],
                    slug=o.get("slug") or "",
                    status=_choice(o.get("status")),
                    facility=o.get("facility") or "",
                    nb_id=o["id"],
                )
            )
        for o in owned("location"):
            self._add(
                self.location(
                    site=_ref_name(o.get("site")),
                    name=o["name"],
                    slug=o.get("slug") or "",
                    parent=_ref_name(o.get("parent")),
                    status=_choice(o.get("status")),
                    facility=o.get("facility") or "",
                    nb_id=o["id"],
                )
            )
        for o in owned("rack"):
            self._add(
                self.rack(
                    site=_ref_name(o.get("site")),
                    name=o["name"],
                    status=_choice(o.get("status")),
                    width=_int(_choice(o.get("width")), 19),
                    u_height=_int(o.get("u_height"), 42),
                    desc_units=bool(o.get("desc_units")),
                    location=_ref_name(o.get("location")),
                    comments=o.get("comments") or "",
                    tags=self._user_tags(o),
                    nb_id=o["id"],
                )
            )
        for o in owned("device", exclude="config_context"):
            dtype = o.get("device_type") or {}
            position = o.get("position")
            self._add(
                self.device(
                    name=o.get("name") or "",
                    device_type=dtype.get("model") or "",
                    manufacturer=_ref_name(dtype.get("manufacturer")),
                    role=_ref_name(o.get("role") or o.get("device_role")),
                    site=_ref_name(o.get("site")),
                    rack=_ref_name(o.get("rack")) or None,
                    position=_int(position) if position is not None else None,
                    face=_choice(o.get("face")),
                    status=_choice(o.get("status")),
                    railyard_id=str((o.get("custom_fields") or {}).get(CUSTOM_FIELD) or "")
                    if self.custom_field
                    else "",
                    serial=o.get("serial") or "",
                    location=_ref_name(o.get("location")),
                    comments=o.get("comments") or "",
                    tags=self._user_tags(o),
                    nb_id=o["id"],
                )
            )

        rear_names: dict[int, str] = {}
        for o in owned("rear_port"):
            rear_names[o["id"]] = o["name"]
            self._add(
                self.rear_port(
                    device=_ref_name(o.get("device")),
                    name=o["name"],
                    type=_choice(o.get("type")),
                    positions=_int(o.get("positions"), 1),
                    nb_id=o["id"],
                )
            )
        for o in owned("front_port"):
            rear_id, rear_pos = front_port_rear(o)
            rear_name = _ref_name(o.get("rear_port")) if isinstance(o.get("rear_port"), dict) else ""
            if rear_id is not None and not rear_name:
                rear_name = rear_names.get(rear_id) or _ref_name(self.client.get(ENDPOINT["rear_port"], rear_id))
            self._add(
                self.front_port(
                    device=_ref_name(o.get("device")),
                    name=o["name"],
                    type=_choice(o.get("type")),
                    rear_port=rear_name,
                    rear_port_position=rear_pos if rear_id is not None else 1,
                    nb_id=o["id"],
                )
            )
        for model_type in ("interface", "power_outlet", "power_port"):
            for o in owned(model_type):
                model = getattr(self, model_type)
                self._add(
                    model(device=_ref_name(o.get("device")), name=o["name"], type=_choice(o.get("type")), nb_id=o["id"])
                )
        for o in owned("cable"):
            ends = [self._termination(o.get(f"{side}_terminations")) for side in ("a", "b")]
            if None in ends:
                continue  # not a shape this sync creates (several terminations a side, a circuit, …)
            (a_dev, a_type, a_name), (b_dev, b_type, b_name) = ends
            cable_type = _choice(o.get("type"))
            self._add(
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
                    status=_choice(o.get("status")),
                    color=(o.get("color") or "").lower(),
                    nb_id=o["id"],
                )
            )

    def _termination(self, terms: Any) -> tuple[str, str, str] | None:
        if not isinstance(terms, list) or len(terms) != 1 or not isinstance(terms[0], dict):
            return None
        term = terms[0]
        ct = str(term.get("object_type") or term.get("termination_type") or "")
        model_type = CT_MODEL.get(ct)
        if model_type is None:
            return None
        obj = term.get("object") if isinstance(term.get("object"), dict) else None
        if obj is None or not _ref_name(obj.get("device")):
            obj = self.client.get(ENDPOINT[model_type], term.get("object_id"))
        if not obj or not _ref_name(obj.get("device")):
            return None
        return _ref_name(obj.get("device")), ct, str(obj.get("name") or "")

    # -- ownership pre-pass --------------------------------------------------------------------

    def _owns(self, model) -> bool:
        return self.get_or_none(model.get_type(), model.get_unique_id()) is not None

    def reconcile(self, source: Adapter, *, allow_deletes: bool = False) -> OwnershipReport:
        """Decide, before anything is written, what to do about Railyard objects the sync doesn't own yet
        but that already exist in NetBox, and drop from ``source`` everything it must not create.

        Read-only, so a dry run reports exactly what a real run will skip, rename or use as-is.
        """
        self.source = source
        report = self.report
        self.detect_renames(source)
        drop: list = []
        skipped_types: set[tuple[str, str]] = set()
        skipped_devices: set[str] = set()
        skipped_ends: set[tuple[str, str, str]] = set()

        def reference(model, what: str) -> None:
            report.referenced.append(f"{what} (exists; used as-is, not modified)")
            drop.append(model)

        def conflict(model, why: str) -> None:
            report.conflicts.append(why)
            drop.append(model)

        def new(type_name: str) -> list:
            return [m for m in source.get_all(type_name) if not self._owns(m)]

        for m in new("manufacturer"):
            if self.find("manufacturer", m.name) is not None:
                reference(m, f"manufacturer {m.name}")
        for dt in new("device_type"):
            mfr = self.find("manufacturer", dt.manufacturer)
            if mfr is None:
                continue
            if self.find("device_type", dt.manufacturer, dt.model) is not None:
                reference(dt, f"device type {dt.manufacturer} {dt.model}")
            elif dt.slug and self.client.first(ENDPOINT["device_type"], manufacturer_id=mfr["id"], slug=dt.slug):
                skipped_types.add((dt.manufacturer, dt.model))
                conflict(dt, f"device type {dt.manufacturer} {dt.model}: slug {dt.slug!r} is used by another type")
        for role in new("device_role"):
            if self.find("device_role", role.name) is not None:
                reference(role, f"device role {role.name}")
        for site in new("site"):
            if self.find("site", site.name) is not None:
                reference(site, f"site {site.name}")
        for loc in new("location"):
            if self.find("location", loc.site, loc.name) is not None:
                reference(loc, f"location {loc.site}/{loc.name}")
        for rack in new("rack"):
            if self.find("rack", rack.site, rack.name) is not None:
                reference(rack, f"rack {rack.site}/{rack.name}")
        # NetBox puts a racked device in its rack's location; in a rack used as-is that is the rack's
        # real location, which need not be the one Railyard drew.
        used_as_is = {(r.site, r.name) for r in drop if r.get_type() == "rack"}
        for dev in source.get_all("device"):
            if dev.rack and (dev.site, dev.rack) in used_as_is:
                dev.location = _ref_name(self.find("rack", dev.site, dev.rack).get("location"))

        for dev in new("device"):
            if (dev.manufacturer, dev.device_type) in skipped_types:
                skipped_devices.add(dev.name)
                conflict(dev, f"device {dev.name}: skipped because its device type was skipped")
                continue
            existing = self._device_in_site(dev.site, dev.name)
            if existing is not None:
                skipped_devices.add(dev.name)
                why = (
                    "carries this sync's tag but was not read back"
                    if self.tag_slug in tag_slugs(existing)
                    else "already exists there and is not managed by this sync"
                )
                conflict(dev, f"device {dev.name} in site {dev.site} {why}")

        for type_name in COMPONENT_TYPES:
            ct = next(k for k, v in CT_MODEL.items() if v == type_name)
            for comp in new(type_name):
                if comp.device in skipped_devices:
                    report.dependents_skipped += 1
                    skipped_ends.add((comp.device, ct, comp.name))
                    drop.append(comp)
                    continue
                owner = self.get_or_none("device", comp.device)
                if owner is None or owner.nb_id is None:
                    continue  # its device is created by this run, so the component can't exist yet
                if self.client.first(ENDPOINT[type_name], device_id=owner.nb_id, name=comp.name) is not None:
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
            for dev_name, ct, name in ends:
                term = self.owned_termination(dev_name, ct, name)
                cable_id = _ref_id(term.get("cable")) if term else None
                if cable_id is not None and cable_id not in doomed_cables:
                    conflict(cab, f"cable {cab.label or cab.get_unique_id()}: {dev_name}:{name} is already cabled")
                    break

        for model in drop:
            source.remove(model)
        return report

    def device_named(self, site: str, name: str) -> int | None:
        found = self._device_in_site(site, name)
        return found.get("id") if found else None

    def _device_in_site(self, site_name: str, name: str) -> dict | None:
        site = self.find("site", site_name)
        if site is None:
            return None
        return self.client.first(ENDPOINT["device"], site_id=site["id"], name__ie=name, exclude="config_context")

    def apply_renames(self) -> None:
        """Rename the NetBox devices ``reconcile`` found renamed in Railyard (a real run only)."""
        for old, new, nb_id in self.renames:
            try:
                obj = self.client.get(ENDPOINT["device"], nb_id)
                if obj is None or self.tag_slug not in tag_slugs(obj):
                    raise NetBoxError("it is no longer a Railyard-owned object in NetBox")
                self.client.update(ENDPOINT["device"], nb_id, {"name": new})
            except NetBoxError as exc:
                self.report.errors.append(self.client.scrub(f"rename device {old} → {new}: {exc}"))
                continue
            self.counts["update"]["device"] += 1

    # -- device-type templates -----------------------------------------------------------------

    _TEMPLATE_ENDPOINTS = (
        ("rear-ports", "dcim/rear-port-templates"),  # before front ports, which name them
        ("front-ports", "dcim/front-port-templates"),
        ("interfaces", "dcim/interface-templates"),
        ("power-ports", "dcim/power-port-templates"),  # before power outlets, which name them
        ("power-outlets", "dcim/power-outlet-templates"),
        ("console-ports", "dcim/console-port-templates"),
        ("console-server-ports", "dcim/console-server-port-templates"),
    )
    _TEMPLATE_FIELDS = ("name", "label", "type", "mgmt_only", "positions", "maximum_draw", "allocated_draw", "feed_leg")

    def import_templates(self, device_type: dict, ids: dict, attrs: dict) -> None:
        """Create a new device type's component templates from the devicetype-library (when enabled).

        Like Railyard's own export the type is otherwise created lean; the library's templates are an
        opt-in, and a template NetBox refuses is a warning, not a failed sync.
        """
        if not self.import_components or self.dtl is None:
            return
        src = self.source.get_or_none("device_type", ids) if self.source is not None else None
        spec = self.dtl.resolve(
            {
                "manufacturer": ids["manufacturer"],
                "model": ids["model"],
                "uHeight": attrs.get("u_height", 1),
                "fullDepth": attrs.get("is_full_depth", True),
                "key": getattr(src, "library_slug", "") or attrs.get("slug", ""),
            }
        )
        made: dict[str, dict[str, int]] = {}
        for lib_key, endpoint in self._TEMPLATE_ENDPOINTS:
            made[lib_key] = {}
            for entry in spec.components.get(lib_key) or []:
                if not isinstance(entry, dict) or not entry.get("name"):
                    continue
                data = {k: entry[k] for k in self._TEMPLATE_FIELDS if k in entry}
                data["device_type"] = device_type["id"]
                if lib_key == "front-ports" and entry.get("rear_port") in made["rear-ports"]:
                    rear = made["rear-ports"][entry["rear_port"]]
                    data.update(self.front_mapping(rear, _int(entry.get("rear_port_position"), 1)))
                    if self.port_mappings:
                        data["positions"] = 1
                if lib_key == "power-outlets" and entry.get("power_port") in made["power-ports"]:
                    data["power_port"] = made["power-ports"][entry["power_port"]]
                try:
                    made[lib_key][entry["name"]] = self.client.create(endpoint, data)["id"]
                except NetBoxError as exc:
                    self.report.warnings.append(
                        self.client.scrub(f"device type {ids['model']}: template {lib_key} {entry['name']!r}: {exc}")
                    )

    # -- deletes -------------------------------------------------------------------------------

    def preview_delete(self, model, scheduled: set[tuple[str, int]]) -> list[str]:
        """Why deleting ``model`` would be refused (empty if it wouldn't), ignoring blockers that this run
        deletes first. Read-only; used by the dry run."""
        obj = self.client.get(ENDPOINT[model.get_type()], model.nb_id) if model.nb_id else None
        if obj is None or self.tag_slug not in tag_slugs(obj):
            return ["no longer a Railyard-owned object"]
        return self.deletion_blockers(model.get_type(), obj, scheduled)

    def deletion_blockers(self, model_type: str, obj: dict, scheduled: set[tuple[str, int]] = frozenset()) -> list[str]:
        """Reasons deleting ``obj`` would reach beyond what this sync owns. ``scheduled`` lists
        (endpoint, id) this run deletes first, which therefore don't block."""
        reasons: list[str] = []

        def foreign(endpoint: str, found: list[dict]) -> int:
            return sum(
                1 for o in found if self.tag_slug not in tag_slugs(o) and (endpoint, o.get("id")) not in scheduled
            )

        def query(endpoint: str, **filters) -> list[dict] | None:
            try:
                return self.client.list(endpoint, **filters)
            except NetBoxNotFoundError:
                return []  # an endpoint this NetBox release doesn't have
            except NetBoxError as exc:
                reasons.append(self.client.scrub(f"could not check its {_noun(endpoint, 2)}: {exc}"))
                return None

        for endpoint, param, check, template in _DEPENDENTS.get(model_type, []):
            found = query(endpoint, **{param: obj["id"]})
            if not found:
                continue
            if check is not None:
                found = [
                    o for o in found if (o.get(check) if check.endswith("_id") else _ref_id(o.get(check))) == obj["id"]
                ]
            if n := foreign(endpoint, found):
                reasons.append(template.format(n=n, what=_noun(endpoint, n)))

        if model_type == "rear_port":  # up to 4.4 its front ports are deleted with it; later, unmapped
            found = query(ENDPOINT["front_port"], device_id=_ref_id(obj.get("device")))
            mapped = [o for o in found or [] if front_port_rear(o)[0] == obj["id"]]
            if n := foreign(ENDPOINT["front_port"], mapped):
                reasons.append(_MODIFY.format(n=n, what=_noun(ENDPOINT["front_port"], n)))
        if model_type in COMPONENT_TYPES and (cable_id := _ref_id(obj.get("cable"))) is not None:
            cable = self.client.get(ENDPOINT["cable"], cable_id)
            if cable is not None and foreign(ENDPOINT["cable"], [cable]):
                reasons.append(_CONNECTED.format(n=1, what="cable"))
        return reasons
