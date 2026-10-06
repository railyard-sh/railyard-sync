"""``SyncDocumentAdapter`` — the DiffSync *source* adapter over Railyard's NetBox sync document.

Railyard generates the document server-side, as a deliverable:
``POST /api/projects/{id}/deliverables/netbox-sync`` (see ``railyard_sync.deliverables``). It holds
the same objects as Railyard's NetBox CSV bundle (``backend/internal/export/netbox.go``), one JSON
row per CSV row with the bundle's column names, so a sync creates exactly what an import of the bundle
would, without porting Railyard's mapping (naming, slugs, statuses, cabling plan) to Python:

    {"format": "railyard-netbox-sync", "version": 1, "netboxVersion": "4.5",
     "project": {"id", "name", "revision"},
     "objects": {"tags", "manufacturers", "device-types", "device-roles", "sites", "locations",
                 "racks", "devices", "interfaces", "rear-ports", "front-ports", "power-outlets",
                 "power-ports", "cables"},
     "warnings": [...]}

Each row also carries ``"railyard": {"kind", "id"}``, the Railyard object it came from. A device's id
(its placement id) is stamped on the NetBox device as the ``railyard_id`` custom field.

The document's contract is new, so **everything that knows its shape is in this module**: the
format/version check in :func:`parse_document` and one row reader per object kind (``_READERS``).
Values may arrive as CSV strings (``"42"``, ``"true"``, ``""``) or as JSON numbers, booleans and
nulls; the readers accept either.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from diffsync import Adapter
from diffsync.exceptions import ObjectAlreadyExists

from . import models
from .cabling import CT_POWER_OUTLET, CT_POWER_PORT
from .mappings import slugify

FORMAT = "railyard-netbox-sync"
SUPPORTED_VERSIONS = (1,)

#: Object kinds in the order the document lists them (and a target creates them).
OBJECT_KINDS = (
    "tags",
    "manufacturers",
    "device-types",
    "device-roles",
    "sites",
    "locations",
    "racks",
    "devices",
    "interfaces",
    "rear-ports",
    "front-ports",
    "power-outlets",
    "power-ports",
    "cables",
)

#: Upper bound on the rows read for one kind, so a malformed document can't make a sync create an
#: unbounded number of objects. Far above any real estate (Railyard caps racks per estate).
MAX_ROWS_PER_KIND = 200_000


class SyncDocumentError(ValueError):
    """The document is not a Railyard NetBox sync document this version can read."""


@dataclass(frozen=True)
class UserTag:
    """A tag the design puts on racks or devices (``objects.tags``). Not the ownership tag."""

    name: str
    slug: str
    color: str = "9e9e9e"


@dataclass
class SyncDocument:
    """The parsed envelope: everything but the object rows, which the adapter reads."""

    project_id: str
    project_name: str
    revision: Any
    netbox_version: str
    objects: dict[str, list[dict]]
    warnings: list[str] = field(default_factory=list)


# ---- value readers -----------------------------------------------------------------------------


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _int(value: Any, default: int | None = 0) -> int | None:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(round(float(text)))
    except ValueError:
        return default


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = _text(value).lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    return default


def _slugs(value: Any) -> list[str]:
    """A tags cell: comma-separated slugs (the CSV form) or a list of slugs, sorted and de-duplicated."""
    items = value if isinstance(value, list) else _text(value).split(",")
    return sorted({_text(item) for item in items if _text(item)})


def _identity(row: dict) -> tuple[str, str]:
    ident = row.get("railyard")
    if isinstance(ident, dict):
        return _text(ident.get("kind")), _text(ident.get("id"))
    return "", ""


# ---- envelope ----------------------------------------------------------------------------------


def parse_document(document: Any) -> SyncDocument:
    """Check the envelope and return it with each kind's rows (``[]`` when a kind is absent)."""
    if not isinstance(document, dict):
        raise SyncDocumentError("The sync document must be a JSON object.")
    if document.get("format") != FORMAT:
        raise SyncDocumentError(f"Not a Railyard NetBox sync document: format is {document.get('format')!r}.")
    version = document.get("version")
    if version not in SUPPORTED_VERSIONS:
        raise SyncDocumentError(
            f"Sync document version {version!r} is not supported by this railyard-sync (it reads version "
            f"{', '.join(map(str, SUPPORTED_VERSIONS))}): upgrade railyard-sync."
        )
    project = document.get("project")
    if not isinstance(project, dict) or not _text(project.get("id")):
        raise SyncDocumentError("The sync document names no project id, so its objects can't be tracked safely.")
    objects = document.get("objects") or {}
    if not isinstance(objects, dict):
        raise SyncDocumentError("The sync document's 'objects' must be a JSON object.")

    warnings = [_text(w) for w in document.get("warnings") or [] if _text(w)]
    rows: dict[str, list[dict]] = {}
    for kind in OBJECT_KINDS:
        value = objects.get(kind) or []
        if not isinstance(value, list) or not all(isinstance(r, dict) for r in value):
            raise SyncDocumentError(f"The sync document's objects.{kind} must be a list of objects.")
        if len(value) > MAX_ROWS_PER_KIND:
            raise SyncDocumentError(f"The sync document has {len(value)} {kind}; at most {MAX_ROWS_PER_KIND}.")
        rows[kind] = value
    for kind in sorted(set(objects) - set(OBJECT_KINDS)):
        warnings.append(f"The sync document's {kind!r} objects are not synced by this railyard-sync; ignored.")
    return SyncDocument(
        project_id=_text(project.get("id")),
        project_name=_text(project.get("name")),
        revision=project.get("revision"),
        netbox_version=_text(document.get("netboxVersion")),
        objects=rows,
        warnings=warnings,
    )


# ---- the adapter -------------------------------------------------------------------------------


class SyncDocumentAdapter(Adapter):
    """Loads the canonical models from a Railyard NetBox sync document (see the module docstring)."""

    manufacturer = models.Manufacturer
    device_type = models.DeviceType
    device_role = models.DeviceRole
    site = models.Site
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
        self.document = parse_document(document)
        self.warnings: list[str] = list(self.document.warnings)
        self.tags: list[UserTag] = []
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
                except (KeyError, ValueError) as exc:
                    self.warnings.append(f"{kind} row {index} skipped: {exc}")

    # -- helpers used by the row readers --------------------------------------------------------

    def _add(self, model, row: dict) -> None:
        try:
            self.add(model)
        except ObjectAlreadyExists:
            self.warnings.append(f"{model.get_type()} {model.get_unique_id()} appears twice; the first is synced")
            return
        kind, rid = _identity(row)
        if kind or rid:
            self.identities[(model.get_type(), model.get_unique_id())] = (kind, rid)


def _required(row: dict, column: str) -> str:
    value = _text(row.get(column))
    if not value:
        raise ValueError(f"no {column}")
    return value


# One reader per object kind. Column names are the NetBox bundle's (netbox.go, containers.go).


def _read_tag(a: SyncDocumentAdapter, row: dict) -> None:
    name = _required(row, "name")
    a.tags.append(UserTag(name=name, slug=_text(row.get("slug")) or slugify(name), color=_color(row) or "9e9e9e"))


def _read_manufacturer(a: SyncDocumentAdapter, row: dict) -> None:
    name = _required(row, "name")
    a._add(a.manufacturer(name=name, slug=_text(row.get("slug")) or slugify(name)), row)


def _read_device_type(a: SyncDocumentAdapter, row: dict) -> None:
    manufacturer, model = _required(row, "manufacturer"), _required(row, "model")
    kind, rid = _identity(row)
    a._add(
        a.device_type(
            manufacturer=manufacturer,
            model=model,
            slug=_text(row.get("slug")) or slugify(f"{manufacturer}-{model}"),
            u_height=_int(row.get("u_height"), 1),
            is_full_depth=_bool(row.get("is_full_depth"), True),
            part_number=_text(row.get("part_number")),
            # A catalogue entry's key is its devicetype-library slug (see devicetype_library.py).
            library_slug=rid if kind in ("catalogue", "device-type", "deviceType") else "",
        ),
        row,
    )


def _read_device_role(a: SyncDocumentAdapter, row: dict) -> None:
    name = _required(row, "name")
    a._add(a.device_role(name=name, slug=_text(row.get("slug")) or slugify(name), color=_color(row) or "9e9e9e"), row)


def _read_site(a: SyncDocumentAdapter, row: dict) -> None:
    name = _required(row, "name")
    a._add(
        a.site(
            name=name,
            slug=_text(row.get("slug")) or slugify(name),
            status=_text(row.get("status")) or "active",
            facility=_text(row.get("facility")),
        ),
        row,
    )


def _read_location(a: SyncDocumentAdapter, row: dict) -> None:
    name = _required(row, "name")
    a._add(
        a.location(
            site=_required(row, "site"),
            name=name,
            slug=_text(row.get("slug")) or slugify(name),
            parent=_text(row.get("parent")),
            status=_text(row.get("status")) or "active",
            facility=_text(row.get("facility")),
        ),
        row,
    )


def _read_rack(a: SyncDocumentAdapter, row: dict) -> None:
    a._add(
        a.rack(
            site=_required(row, "site"),
            name=_required(row, "name"),
            status=_text(row.get("status")) or "active",
            width=_int(row.get("width"), 19),
            u_height=_int(row.get("u_height"), 42),
            desc_units=_bool(row.get("desc_units")),
            location=_text(row.get("location")),
            comments=_text(row.get("comments")),
            tags=_slugs(row.get("tags")),
        ),
        row,
    )


def _read_device(a: SyncDocumentAdapter, row: dict) -> None:
    position = _int(row.get("position"), None)
    kind, rid = _identity(row)
    a._add(
        a.device(
            name=_required(row, "name"),
            device_type=_required(row, "device_type"),
            manufacturer=_required(row, "manufacturer"),
            role=_required(row, "role"),
            site=_required(row, "site"),
            rack=_text(row.get("rack")) or None,
            position=position or None,
            # A device with no U position (a 0U side-mount) has a blank face in NetBox; the bundle
            # writes "front" for it, which NetBox would keep but a re-read could not tell apart.
            face=(_text(row.get("face")) or "front") if position else "",
            status=_text(row.get("status")) or "active",
            railyard_id=rid if kind in ("", "placement", "device") else "",
            serial=_text(row.get("serial")),
            location=_text(row.get("location")),
            comments=_text(row.get("comments")),
            tags=_slugs(row.get("tags")),
        ),
        row,
    )


def _component_reader(model_attr: str, default_type: str, **extra: Callable[[dict], Any]):
    def read(a: SyncDocumentAdapter, row: dict) -> None:
        attrs = {name: fn(row) for name, fn in extra.items()}
        model = getattr(a, model_attr)
        a._add(
            model(
                device=_required(row, "device"),
                name=_required(row, "name"),
                type=_text(row.get("type")) or default_type,
                **attrs,
            ),
            row,
        )

    return read


def _read_cable(a: SyncDocumentAdapter, row: dict) -> None:
    a_type, b_type = _required(row, "side_a_type"), _required(row, "side_b_type")
    cable_type = _text(row.get("type"))
    a._add(
        a.cable(
            a_device=_required(row, "side_a_device"),
            a_type=a_type,
            a_name=_required(row, "side_a_name"),
            b_device=_required(row, "side_b_device"),
            b_type=b_type,
            b_name=_required(row, "side_b_name"),
            is_power=cable_type == "power" or bool({a_type, b_type} & {CT_POWER_PORT, CT_POWER_OUTLET}),
            label=_text(row.get("label")),
            type=cable_type,
            status=_text(row.get("status")) or "connected",
            color=_color(row),
        ),
        row,
    )


def _color(row: dict) -> str:
    return _text(row.get("color")).lstrip("#").lower()


_READERS: dict[str, Callable[[SyncDocumentAdapter, dict], None]] = {
    "tags": _read_tag,
    "manufacturers": _read_manufacturer,
    "device-types": _read_device_type,
    "device-roles": _read_device_role,
    "sites": _read_site,
    "locations": _read_location,
    "racks": _read_rack,
    "devices": _read_device,
    "interfaces": _component_reader("interface", "other"),
    "rear-ports": _component_reader("rear_port", "8p8c", positions=lambda r: _int(r.get("positions"), 1) or 1),
    "front-ports": _component_reader(
        "front_port",
        "8p8c",
        rear_port=lambda r: _text(r.get("rear_port")),
        rear_port_position=lambda r: _int(r.get("rear_port_position"), 1) or 1,
    ),
    "power-outlets": _component_reader("power_outlet", "iec-60320-c13"),
    "power-ports": _component_reader("power_port", "iec-60320-c14"),
    "cables": _read_cable,
}
