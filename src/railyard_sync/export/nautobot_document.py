"""``NautobotSyncDocumentAdapter`` — the DiffSync *source* adapter over Railyard's Nautobot sync document.

Railyard generates it server-side, as a deliverable: ``POST /api/projects/{id}/deliverables/nautobot-sync``
(``RailyardClient.nautobot_sync_document``). It holds the objects of Railyard's Nautobot 2.x CSV bundle
(``backend/internal/export/nautobot.go``), one JSON row per CSV row with the bundle's column names, and the cables
as the bundle's ``13-cables.json`` lists them:

    {"format": "railyard-nautobot-sync", "version": 1,
     "project": {"id", "name", "revision", "changeRequestId"?},
     "objects": {"location-types", "statuses", "tags", "locations", "manufacturers", "device-types", "roles",
                 "racks", "devices", "interfaces", "rear-ports", "front-ports", "power-outlets", "power-ports",
                 "cables"},
     "unresolved": [...], "warnings": [...]}

Cells are the CSV's: ``"42"``, ``"True"``/``"False"``, comma-joined content types and tag names, and ``NoObject``
(Nautobot's marker for "no related object") in an empty natural-key lookup such as a root location's
``parent__name``, which is read as blank. Each row also has ``"railyard"``, the Railyard object it came from
(the same identities as the NetBox document: a location's container id, a device's placement id, a device type's
catalogue keys). Statuses are Nautobot's names (``Active``); the ``statuses`` section lists only the custom ones.

The envelope is checked by :func:`.sync_document.parse_document`; everything that knows the rows' shape is here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from diffsync import Adapter
from diffsync.exceptions import ObjectAlreadyExists

from . import nautobot_models as models
from .cabling import CT_POWER_OUTLET, CT_POWER_PORT
from .sync_document import SyncDocumentError, _bool, _identity, _int, _text, parse_document

FORMAT = "railyard-nautobot-sync"

#: Sections in the order the document lists them (and a target creates them).
OBJECT_KINDS = (
    "location-types",
    "statuses",
    "tags",
    "locations",
    "manufacturers",
    "device-types",
    "roles",
    "racks",
    "devices",
    "interfaces",
    "rear-ports",
    "front-ports",
    "power-outlets",
    "power-ports",
    "cables",
)

#: Nautobot's CSV marker for an empty natural-key lookup.
NO_OBJECT = "NoObject"

__all__ = ["FORMAT", "NautobotSyncDocumentAdapter", "NautobotTag", "OBJECT_KINDS", "SyncDocumentError"]


@dataclass(frozen=True)
class NautobotTag:
    """A tag the design puts on racks or devices (``objects.tags``), with the content types that use it."""

    name: str
    content_types: tuple[str, ...] = ()


def _lookup(value: Any) -> str:
    """A natural-key lookup cell: blank for Nautobot's ``NoObject``."""
    text = _text(value)
    return "" if text == NO_OBJECT else text


def _list(value: Any) -> list[str]:
    """A comma-joined cell (content types, tag names) or a JSON list, sorted and de-duplicated."""
    items = value if isinstance(value, list) else _text(value).split(",")
    return sorted({_text(item) for item in items if _text(item) and _text(item) != NO_OBJECT})


def _required(row: dict, column: str) -> str:
    value = _lookup(row.get(column))
    if not value:
        raise ValueError(f"no {column}")
    return value


class NautobotSyncDocumentAdapter(Adapter):
    """Loads the canonical Nautobot models from a Railyard Nautobot sync document (see the module docstring)."""

    location_type = models.LocationType
    status = models.Status
    manufacturer = models.Manufacturer
    device_type = models.DeviceType
    role = models.Role
    location = models.Location
    rack = models.Rack
    device = models.Device
    interface = models.Interface
    rear_port = models.RearPort
    front_port = models.FrontPort
    power_outlet = models.PowerOutlet
    power_port = models.PowerPort
    cable = models.Cable

    top_level = models.TOP_LEVEL

    def __init__(self, document: dict, **kwargs):
        super().__init__(**kwargs)
        self.document = parse_document(document, fmt=FORMAT, kinds=OBJECT_KINDS, product="Nautobot")
        self.warnings: list[str] = list(self.document.warnings)
        self.tags: list[NautobotTag] = []
        #: (model type, unique id) -> the Railyard (kind, id) the row came from, when it says.
        self.identities: dict[tuple[str, str], tuple[str, str]] = {}

    @property
    def project_id(self) -> str:
        return self.document.project_id

    @property
    def project_name(self) -> str:
        return self.document.project_name

    def load(self) -> None:
        for kind in OBJECT_KINDS:
            reader = _READERS[kind]
            for index, row in enumerate(self.document.objects[kind], start=1):
                try:
                    reader(self, row)
                except (KeyError, ValueError, TypeError, AttributeError) as exc:
                    self.warnings.append(f"{kind} row {index} skipped: {exc}")

    def _add(self, model, row: dict) -> None:
        try:
            self.add(model)
        except ObjectAlreadyExists:
            self.warnings.append(f"{model.get_type()} {model.get_unique_id()} appears twice; the first is synced")
            return
        kind, rid = _identity(row)
        if kind or rid:
            self.identities[(model.get_type(), model.get_unique_id())] = (kind, rid)


# One reader per section. Column names are the Nautobot bundle's (nautobot.go).


def _read_location_type(a: NautobotSyncDocumentAdapter, row: dict) -> None:
    a._add(
        a.location_type(
            name=_required(row, "name"),
            parent=_lookup(row.get("parent__name")),
            content_types=_list(row.get("content_types")),
            nestable=_bool(row.get("nestable")),
        ),
        row,
    )


def _read_status(a: NautobotSyncDocumentAdapter, row: dict) -> None:
    a._add(a.status(name=_required(row, "name"), content_types=_list(row.get("content_types"))), row)


def _read_tag(a: NautobotSyncDocumentAdapter, row: dict) -> None:
    a.tags.append(NautobotTag(name=_required(row, "name"), content_types=tuple(_list(row.get("content_types")))))


def _read_location(a: NautobotSyncDocumentAdapter, row: dict) -> None:
    a._add(
        a.location(
            name=_required(row, "name"),
            location_type=_required(row, "location_type__name"),
            parent=_lookup(row.get("parent__name")),
            status=_lookup(row.get("status__name")) or "Active",
            facility=_text(row.get("facility")),
        ),
        row,
    )


def _read_manufacturer(a: NautobotSyncDocumentAdapter, row: dict) -> None:
    a._add(a.manufacturer(name=_required(row, "name")), row)


def _read_device_type(a: NautobotSyncDocumentAdapter, row: dict) -> None:
    kind, rid = _identity(row)
    a._add(
        a.device_type(
            manufacturer=_required(row, "manufacturer__name"),
            model=_required(row, "model"),
            u_height=_int(row.get("u_height"), 1),
            is_full_depth=_bool(row.get("is_full_depth"), True),
            library_slug=rid if kind == "device-type" else "",
        ),
        row,
    )


def _read_role(a: NautobotSyncDocumentAdapter, row: dict) -> None:
    a._add(a.role(name=_required(row, "name"), content_types=_list(row.get("content_types"))), row)


def _read_rack(a: NautobotSyncDocumentAdapter, row: dict) -> None:
    a._add(
        a.rack(
            location=_required(row, "location__name"),
            name=_required(row, "name"),
            status=_lookup(row.get("status__name")) or "Active",
            role=_lookup(row.get("role__name")),
            type=_text(row.get("type")),
            width=_int(row.get("width"), 19),
            u_height=_int(row.get("u_height"), 42),
            desc_units=_bool(row.get("desc_units")),
            comments=_text(row.get("comments")),
            tags=_list(row.get("tags")),
        ),
        row,
    )


def _read_device(a: NautobotSyncDocumentAdapter, row: dict) -> None:
    position = _int(row.get("position"), None)
    kind, rid = _identity(row)
    a._add(
        a.device(
            name=_required(row, "name"),
            device_type=_required(row, "device_type__model"),
            manufacturer=_required(row, "device_type__manufacturer__name"),
            role=_required(row, "role__name"),
            status=_lookup(row.get("status__name")) or "Active",
            location=_lookup(row.get("location__name")),
            rack=_lookup(row.get("rack__name")) or None,
            position=position or None,
            # A device with no U position (a 0U side-mount) has no face in Nautobot; the bundle writes "front".
            face=(_text(row.get("face")) or "front") if position else "",
            railyard_id=rid if kind == "device" else "",
            comments=_text(row.get("comments")),
            tags=_list(row.get("tags")),
        ),
        row,
    )


def _component_reader(model_attr: str, default_type: str, **extra: Callable[[dict], Any]):
    def read(a: NautobotSyncDocumentAdapter, row: dict) -> None:
        attrs = {name: fn(row) for name, fn in extra.items()}
        a._add(
            getattr(a, model_attr)(
                device=_required(row, "device__name"),
                name=_required(row, "name"),
                type=_text(row.get("type")) or default_type,
                **attrs,
            ),
            row,
        )

    return read


def _end(cable: dict, side: str) -> tuple[str, str, str]:
    end = cable.get(side)
    if not isinstance(end, dict):
        raise ValueError(f"no {side} end")
    device, ctype, name = _text(end.get("device")), _text(end.get("type")), _text(end.get("name"))
    if not (device and ctype and name):
        raise ValueError(f"an incomplete {side} end")
    return device, ctype, name


def _read_cable(a: NautobotSyncDocumentAdapter, row: dict) -> None:
    (a_dev, a_type, a_name), (b_dev, b_type, b_name) = _end(row, "a"), _end(row, "b")
    cable_type = _text(row.get("type"))
    a._add(
        a.cable(
            a_device=a_dev,
            a_type=a_type,
            a_name=a_name,
            b_device=b_dev,
            b_type=b_type,
            b_name=b_name,
            is_power=cable_type == "power" or bool({a_type, b_type} & {CT_POWER_PORT, CT_POWER_OUTLET}),
            label=_text(row.get("label")),
            type=cable_type,
            status=_text(row.get("status")) or "Connected",
            color=_text(row.get("color")).lstrip("#").lower(),
        ),
        row,
    )


_READERS: dict[str, Callable[[NautobotSyncDocumentAdapter, dict], None]] = {
    "location-types": _read_location_type,
    "statuses": _read_status,
    "tags": _read_tag,
    "locations": _read_location,
    "manufacturers": _read_manufacturer,
    "device-types": _read_device_type,
    "roles": _read_role,
    "racks": _read_rack,
    "devices": _read_device,
    "interfaces": _component_reader("interface", "other", status=lambda r: _lookup(r.get("status__name")) or "Active"),
    "rear-ports": _component_reader("rear_port", "8p8c", positions=lambda r: _int(r.get("positions"), 1) or 1),
    "front-ports": _component_reader(
        "front_port",
        "8p8c",
        rear_port=lambda r: _lookup(r.get("rear_port__name")),
        rear_port_position=lambda r: _int(r.get("rear_port_position"), 1) or 1,
    ),
    "power-outlets": _component_reader("power_outlet", "iec-60320-c13"),
    "power-ports": _component_reader("power_port", "iec-60320-c14"),
    "cables": _read_cable,
}
