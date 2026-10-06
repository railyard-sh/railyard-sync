"""Build a Railyard project from a DCIM snapshot.

:func:`build_project` turns a :class:`~railyard_sync.dcim.snapshot.Snapshot` into a Railyard Project
JSON document, ready to ``PUT``, plus an :class:`~railyard_sync.importer.report.ImportReport`. The
mapping and identity scheme are set out in this repository's ``CLAUDE.md``; in short:

- regions are ``group`` containers, a site a ``floor`` container (with its ``dataCentres`` record) and
  its locations ``group`` containers below it; every ``group`` container has a ``locations`` record;
- racks keep their size, numbering, status, role and load rating, and their primary power feeds
  become ``powerCapacityW``;
- racked devices become placements with their real ports and power inputs; 0U devices are side
  mounted; everything Railyard cannot hold is skipped and reported, never guessed at;
- data cables between imported ports become ``cables``, and power cables to PDU outlets
  ``powerLinks``.

Every id is derived from the source object's primary key (``nb-rack-12``), so a re-import finds the
same objects. The output is deterministic: collections are sorted by source id (natural order), and
nothing depends on the order the snapshot lists things in.

Railyard's server refuses documents that break its limits, while NetBox accepts things Railyard does
not (half-U positions, overlapping devices after rounding, names longer than Railyard allows). The
builder keeps the document within those limits (``railyard/backend/internal/model``): identifiers
and names of at most 256 characters, port and power-input names of 100, serials of 50, notes of
10,000, at most 64 tags of 100 characters, unique rack names per space, unique port names per side
and kind, and every placement inside its rack without overlapping another.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..dcim.snapshot import Component, Device, DeviceType, Rack, Snapshot
from . import mappings as m
from .catalogue import CatalogueLookup
from .devicetypes import custom_device_type, normalise_inlet_name
from .report import ImportReport

SCHEMA_VERSION = "1"

MAX_IDENTIFIER = 256  # ids, names, labels, roles (model.MaxIdentifierLength)
MAX_PORT_ID = 200  # port and power-input ids
MAX_PORT_NAME = 100  # port and power-input names and connectors
MAX_SERIAL = 50  # model.MaxDeviceSerialLength
MAX_NOTES = 10_000
MAX_TAGS = 64
MAX_TAG = 100
MAX_CABLE_MEDIA = 100
MAX_RACK_U = 100
MAX_PORTS = 4096
MAX_INLETS = 256
PDU_ROLE_OUTLETS = 24  # model.OutletSpecFor: a 0U PDU-role device with an untyped strip

_PREFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
_PASSIVE_ROLES = {"patch-panel", "cable-management", "blanking-panel", "passive"}
_BLANKING_ROLE = re.compile(r"(?i)^(blanking[-_ ]?(panel|plate)|blank)$")
_BLANKING_TYPE = re.compile(r"(?i)(^|[-_\s])blanking[-_\s]+(panel|plate)($|[-_\s])")

# Cable termination object types (NetBox and Nautobot share the dcim.* content types).
_TERMINATION_KINDS = {
    "dcim.interface": "interface",
    "dcim.frontport": "front-port",
    "dcim.rearport": "rear-port",
    "dcim.powerport": "power-port",
    "dcim.poweroutlet": "power-outlet",
    "dcim.consoleport": "console-port",
    "dcim.consoleserverport": "console-port",
}
_UNMODELLED_ENDS = {
    "dcim.powerfeed": "a power feed (power feeds are not modelled; they set the rack's power capacity)",
    "circuits.circuittermination": "a circuit (circuits are not modelled)",
    "dcim.consoleport": "a console port (console cabling is not modelled)",
    "dcim.consoleserverport": "a console server port (console cabling is not modelled)",
}
_DATA_KINDS = {"interface", "front-port", "rear-port"}
_POWER_ENDS = {"dcim.powerport", "dcim.poweroutlet", "dcim.powerfeed"}
_PORT_ID_KIND = {"interface": "if", "front-port": "fp", "rear-port": "rp"}


@dataclass
class BuildResult:
    """A built project and what the import did."""

    project: dict  # Railyard Project JSON, ready to PUT
    report: ImportReport


def build_project(
    snapshot: Snapshot,
    *,
    project_id: str,
    name: str,
    catalogue: CatalogueLookup | None = None,
    prefix: str = "nb",
    imported_at: datetime | None = None,
) -> BuildResult:
    """Build the Railyard project for ``snapshot``.

    ``catalogue`` resolves device types to Railyard catalogue entries (by slug); without it, or when it
    has no match, a custom type ``<prefix>-dt-<slug>`` is built from the component templates.
    ``prefix`` starts every derived id (``nb`` for NetBox, ``nbt`` for Nautobot). ``imported_at``
    defaults to now; tests pass a fixed time.
    """
    if not _PREFIX.fullmatch(prefix or ""):
        raise ValueError("prefix must be 1-32 letters, digits, '-' or '_', starting with a letter or digit")
    if not (project_id or "").strip():
        raise ValueError("project_id is required")
    if not (name or "").strip():
        raise ValueError("name is required")
    return _Builder(snapshot, project_id, name, catalogue, prefix, imported_at).build()


def _by_id(item) -> tuple:
    return m.natural_key(str(item.id))


@dataclass
class _Placed:
    """An imported device: where it is and what the cable and power passes need to know."""

    rack_id: str
    placement: dict
    device_type: dict
    ports: dict[tuple[str, str], str] = field(default_factory=dict)  # (kind, component id) -> port id
    inlets: dict[str, str] = field(default_factory=dict)  # power-port id -> inlet id
    outlets: dict[str, int] = field(default_factory=dict)  # power-outlet id -> 1-based outlet number


class _Builder:
    def __init__(self, snapshot: Snapshot, project_id: str, name: str, catalogue, prefix: str, imported_at):
        self.s = snapshot
        self.project_id = project_id
        self.name = name
        self.catalogue = catalogue
        self.prefix = prefix
        self.imported_at = imported_at or datetime.now(UTC)
        self.report = ImportReport()
        self.report.warnings.extend(snapshot.warnings)

        self.sites = {s.id: s for s in snapshot.sites}
        self.regions = {r.id: r for r in snapshot.regions}
        self.locations = {loc.id: loc for loc in snapshot.locations}
        self.device_types = {dt.id: dt for dt in snapshot.device_types}
        self.devices = {d.id: d for d in snapshot.devices}
        # Railyard names devices uniquely across an estate (case and surrounding spaces ignored); NetBox
        # only within a site. Keys are casefolded names already given; renamed maps placement id to the
        # DCIM's own name.
        self.device_names: dict[str, str] = {}
        self.renamed_devices: dict[str, str] = {}
        self.components: dict[tuple[str, str], Component] = {}
        self.components_by_device: dict[str, list[Component]] = {}
        for comp in snapshot.components:
            self.components[(comp.kind, comp.id)] = comp
            self.components_by_device.setdefault(comp.device_id, []).append(comp)

        self.containers: list[dict] = []
        self.location_records: list[dict] = []
        self.data_centres: list[dict] = []
        self.container_ids: dict[tuple[str, str], str] = {}  # ("site"|"loc", source id) -> container id
        self.racks: list[dict] = []
        self.rack_by_source: dict[str, dict] = {}
        self.rack_types: list[dict] = []
        self.rack_type_keys: dict[str, str] = {}
        self.catalogue_entries: dict[str, dict] = {}
        self.type_keys: dict[str, str] = {}  # snapshot device type id -> Railyard key
        self.placed: dict[str, _Placed] = {}  # device id -> placement
        self.cables: list[dict] = []
        self.power_links: list[dict] = []
        self.device_status: dict[str, str] = {}
        self.cable_status: dict[str, str] = {}
        self.module_components: dict[str, int] = {}

    # ---- ids and text ------------------------------------------------------------------------

    def ident(self, kind: str, source_id: str, limit: int = MAX_IDENTIFIER) -> str:
        """``<prefix>-<kind>-<source id>``; a source id that would break Railyard's id rules (too long,
        a ':' — never valid in a rack id — or blank) is replaced by its digest, still deterministic."""
        source = str(source_id)
        value = f"{self.prefix}-{kind}-{source}"
        if len(value) > limit or ":" in value or not source.strip() or source != source.strip():
            value = f"{self.prefix}-{kind}-x{m.digest(source)}"
        return value

    def fit(self, value: str, limit: int, what: str) -> str:
        fitted = m.fit(value, limit)
        if fitted != value:
            self.report.warn(f"{what}: shortened to {limit} characters ({fitted!r})")
        return fitted

    def unique_device_name(self, device: Device, name: str) -> str:
        """``name``, or ``name (<site slug>)`` / ``name #<id>`` when another imported device already has it.

        Railyard refuses two devices with one name in an estate, ignoring case and surrounding spaces; NetBox
        allows it across sites. Devices are taken in source-id order, so the older device keeps its name and
        a refresh gives the same names again. The rename is reported and recorded in
        ``meta.railyardSync.renamedDevices`` (placement id -> the DCIM's name).
        """
        key = name.strip().casefold()
        if key not in self.device_names:
            self.device_names[key] = device.id
            return name
        site = self.sites.get(device.site_id)
        candidates = [f"{name} ({site.slug})"] if site and site.slug else []
        candidates.append(f"{name} #{device.id}")
        for candidate in candidates:
            candidate = m.fit(candidate, MAX_IDENTIFIER)
            if candidate.strip().casefold() not in self.device_names:
                break
        self.device_names[candidate.strip().casefold()] = device.id
        placement_id = self.ident("dev", device.id)
        self.renamed_devices[placement_id] = name
        self.report.warn(
            f"device {device.id} is named {name!r}, like another device in this import; Railyard needs device "
            f"names to be unique across an estate, so it is imported as {candidate!r}"
        )
        return candidate

    def notes(self, value: str, what: str) -> str:
        value = (value or "").strip()
        if len(value) > MAX_NOTES:
            self.report.warn(f"{what}: comments shortened to {MAX_NOTES} characters")
            value = value[: MAX_NOTES - 1].rstrip() + "…"
        return value

    def tags(self, tags: list[str], what: str) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for tag in tags:
            tag = (tag or "").strip()
            if not tag:
                continue
            tag = self.fit(tag, MAX_TAG, f"{what} tag")
            if tag not in seen:
                seen.add(tag)
                out.append(tag)
        if len(out) > MAX_TAGS:
            self.report.warn(f"{what}: only the first {MAX_TAGS} of {len(out)} tags are kept")
            out = out[:MAX_TAGS]
        return out

    # ---- build -------------------------------------------------------------------------------

    def build(self) -> BuildResult:
        self.build_spaces()
        self.build_rack_types()
        self.build_racks()
        self.build_devices()
        self.build_cables()
        catalogue = [self.catalogue_entries[key] for key in sorted(self.catalogue_entries)]
        project = {
            "schemaVersion": SCHEMA_VERSION,
            "id": self.project_id,
            "name": self.name,
            "containers": self.containers,
            "locations": self.location_records,
            "dataCentres": self.data_centres,
            "rows": [],
            "racks": self.racks,
            "podPatterns": [],
            "namingRules": [],
            "catalogue": catalogue,
            "rackTypes": self.rack_types,
            "cables": self.cables,
            "powerLinks": self.power_links,
            "meta": {"railyardSync": self.meta()},
        }
        return BuildResult(project=project, report=self.report)

    # ---- spaces ------------------------------------------------------------------------------

    def add_group(self, container_id: str, name: str, type_: str, parent: str | None, status="", facility=""):
        container: dict[str, Any] = {"id": container_id, "name": name, "type": type_}
        if parent:
            container["parentId"] = parent
        container["layout"] = "group"
        if status:
            container["status"] = status
        if facility:
            container["facility"] = facility
        self.containers.append(container)
        record: dict[str, Any] = {"id": container_id, "name": name}
        if facility:
            record["facility"] = facility
        if status:
            record["status"] = status
        self.location_records.append(record)

    def build_spaces(self) -> None:
        sites = sorted(self.sites.values(), key=_by_id)
        # The region chain above each site, emitted root first.
        emitted: dict[str, str] = {}

        def emit_region(region_id: str, trail: tuple[str, ...] = ()) -> str | None:
            if region_id in emitted:
                return emitted[region_id]
            region = self.regions.get(region_id)
            if region is None or region_id in trail:
                if region_id in trail:
                    self.report.warn(f"region {region_id}: its parent chain loops; it is placed at the top")
                return None
            parent = emit_region(region.parent_id, (*trail, region_id)) if region.parent_id else None
            container_id = self.ident("region", region.id)
            name = self.fit((region.name or region.slug or region.id).strip(), MAX_IDENTIFIER, f"region {region.id}")
            self.add_group(container_id, name, "Region", parent)
            emitted[region_id] = container_id
            self.report.count("regions")
            return container_id

        for site in sites:
            region = None
            if site.region_id:
                region = emit_region(site.region_id)
                if region is None and site.region_id not in self.regions:
                    self.report.warn(f"site {site.name}: its region {site.region_id} is not in the snapshot")
            site_id = self.ident("site", site.id)
            name = self.fit((site.name or site.slug or site.id).strip(), MAX_IDENTIFIER, f"site {site.id}")
            status = m.status_label(site.status)
            container: dict[str, Any] = {"id": site_id, "name": name, "type": "Site"}
            if region:
                container["parentId"] = region
            container["layout"] = "floor"
            if status:
                container["status"] = status
            if site.facility:
                container["facility"] = self.fit(site.facility, MAX_IDENTIFIER, f"site {name} facility")
            container["exportSite"] = True
            self.containers.append(container)
            dc: dict[str, Any] = {"id": site_id, "name": name}
            if region:
                dc["locationId"] = region  # the nearest location record above the floor
            if status:
                dc["status"] = status
            self.data_centres.append(dc)
            self.container_ids[("site", site.id)] = site_id
            self.report.count("sites")

        # Locations, parents first, under their site.
        done: set[str] = set()

        def emit_location(location_id: str, trail: tuple[str, ...] = ()) -> None:
            if location_id in done:
                return
            loc = self.locations[location_id]
            done.add(location_id)
            site_container = self.container_ids[("site", loc.site_id)]
            parent = site_container
            if loc.parent_id:
                parent_loc = self.locations.get(loc.parent_id)
                if parent_loc is None or parent_loc.site_id != loc.site_id or loc.parent_id in trail:
                    self.report.warn(
                        f"location {loc.name}: its parent {loc.parent_id} is not imported; placed in its site"
                    )
                else:
                    emit_location(loc.parent_id, (*trail, location_id))
                    parent = self.container_ids.get(("loc", loc.parent_id), site_container)
            container_id = self.ident("loc", loc.id)
            name = self.fit((loc.name or loc.slug or loc.id).strip(), MAX_IDENTIFIER, f"location {loc.id}")
            type_ = self.fit((loc.location_type or "Location").strip() or "Location", MAX_IDENTIFIER, "location type")
            facility = self.fit(loc.facility or "", MAX_IDENTIFIER, f"location {name} facility")
            self.add_group(container_id, name, type_, parent, m.status_label(loc.status), facility)
            self.container_ids[("loc", loc.id)] = container_id
            self.report.count("locations")

        for loc in sorted(self.locations.values(), key=_by_id):
            if ("site", loc.site_id) not in self.container_ids:
                self.report.skip("locations", loc.id, loc.name, "its site is not imported")
                done.add(loc.id)
                continue
            emit_location(loc.id)

    # ---- rack types and racks ------------------------------------------------------------------

    def build_rack_types(self) -> None:
        for rt in sorted(self.s.rack_types, key=_by_id):
            key = self.ident("rt", rt.slug or rt.id)
            if key in self.rack_type_keys.values():
                key = self.ident("rt", f"{rt.slug}-{rt.id}")
            u_height = min(max(int(rt.u_height or 42), 1), MAX_RACK_U)
            entry: dict[str, Any] = {
                "key": key,
                "manufacturer": self.fit(rt.manufacturer or "", MAX_IDENTIFIER, f"rack type {rt.slug} manufacturer"),
                "model": self.fit(rt.model or rt.slug or key, MAX_IDENTIFIER, f"rack type {rt.slug} model"),
                "uHeight": u_height,
                "widthMm": m.rack_width_mm(rt.width_in, rt.outer_width_mm),
            }
            if rt.outer_depth_mm:
                entry["depthMm"] = max(1, round(rt.outer_depth_mm))
            if rt.form_factor:
                entry["formFactor"] = self.fit(rt.form_factor, MAX_IDENTIFIER, f"rack type {rt.slug} form factor")
            self.rack_types.append(entry)
            self.rack_type_keys[rt.id] = key
            self.report.count("rackTypes")

    def rack_power_capacity(self) -> dict[str, float]:
        capacity: dict[str, float] = {}
        for feed in self.s.power_feeds:
            if feed.rack_id and (feed.type or "primary") == "primary":
                capacity[feed.rack_id] = capacity.get(feed.rack_id, 0) + feed.available_power_w
        return capacity

    def build_racks(self) -> None:
        capacity = self.rack_power_capacity()
        names: dict[str, set[str]] = {}  # container id -> rack name identities
        for rack in sorted(self.s.racks, key=_by_id):
            site_container = self.container_ids.get(("site", rack.site_id))
            if site_container is None:
                self.report.skip("racks", rack.id, rack.name, "its site is not imported")
                continue
            container = site_container
            if rack.location_id:
                container = self.container_ids.get(("loc", rack.location_id), site_container)
                if container == site_container:
                    self.report.warn(
                        f"rack {rack.name}: its location {rack.location_id} is not imported; placed in its site"
                    )
            rack_id = self.ident("rack", rack.id)
            name = self.rack_name(rack, names.setdefault(container, set()))
            u_height = int(rack.u_height or 42)
            if not 1 <= u_height <= MAX_RACK_U:
                self.report.warn(
                    f"rack {name}: {u_height}U is outside Railyard's 1-{MAX_RACK_U}U; "
                    f"set to {min(max(u_height, 1), MAX_RACK_U)}U"
                )
                u_height = min(max(u_height, 1), MAX_RACK_U)
            width = m.rack_width_mm(rack.width_in, rack.outer_width_mm)
            rail = int(rack.width_in or 19)
            if rail not in (19, 23):
                self.report.warn(
                    f'rack {name}: Railyard models 19" and 23" rails only; '
                    f'its {rail}" rail exports as {m.width_inches(width)}"'
                )
            elif m.width_inches(width) != rail:
                self.report.warn(
                    f"rack {name}: its {width} mm outer width makes Railyard export it as a "
                    f'{m.width_inches(width)}" rack, not {rail}"'
                )
            out: dict[str, Any] = {"id": rack_id, "name": name, "uHeight": u_height, "widthMm": width}
            if rack.outer_depth_mm:
                out["depthMm"] = max(1, round(rack.outer_depth_mm))
            out["startingUnit"] = max(1, int(rack.starting_unit or 1))
            if rack.desc_units:
                out["descendingUnits"] = True
            out["containerId"] = container
            out["dcId"] = site_container  # the nearest floor ancestor: the site
            if rack.rack_type_id in self.rack_type_keys:
                out["rackTypeKey"] = self.rack_type_keys[rack.rack_type_id]
            out["indexInRow"] = 0
            status = m.status_label(rack.status)
            if status:
                out["status"] = self.fit(status, MAX_IDENTIFIER, f"rack {name} status")
            if rack.role:
                out["role"] = self.fit(rack.role, MAX_IDENTIFIER, f"rack {name} role")
            if rack.id in capacity:
                out["powerCapacityW"] = int(round(capacity[rack.id]))
            if rack.max_weight_kg and rack.max_weight_kg > 0:
                out["maxLoadKg"] = rack.max_weight_kg
            notes = self.notes(rack.comments, f"rack {name}")
            if notes:
                out["notes"] = notes
            tags = self.tags(rack.tags, f"rack {name}")
            if tags:
                out["tags"] = tags
            out["placements"] = []
            self.racks.append(out)
            self.rack_by_source[rack.id] = out
            self.report.count("racks")

    def rack_name(self, rack: Rack, taken: set[str]) -> str:
        name = (rack.name or "").strip() or f"Rack {rack.id}"
        name = self.fit(name, MAX_IDENTIFIER, f"rack {rack.id} name")
        if m.identity_key(name) in taken:
            base, n = name, 1
            while m.identity_key(name) in taken:
                suffix = f" ({rack.id})" if n == 1 else f" ({rack.id}-{n})"
                name = m.fit(base, MAX_IDENTIFIER - len(suffix)) + suffix
                n += 1
            self.report.warn(f"rack {base!r} (id {rack.id}): another rack in its space has that name; renamed {name!r}")
        taken.add(m.identity_key(name))
        return name

    # ---- device types ------------------------------------------------------------------------

    def device_type_entry(self, dt: DeviceType) -> dict:
        if dt.id in self.type_keys:
            return self.catalogue_entries[self.type_keys[dt.id]]
        height = max(0, math.ceil(dt.u_height or 0))
        entry = None
        if self.catalogue is not None and dt.slug:
            entry = self.catalogue.device_type(dt.slug, dt.manufacturer, dt.model)
        if entry is not None:
            key = entry["key"]
            if key not in self.catalogue_entries:
                self.catalogue_entries[key] = entry
                self.report.count("catalogueDeviceTypes")
            entry = self.catalogue_entries[key]
            if int(entry.get("uHeight") or 0) != height:
                self.report.warn(
                    f"device type {dt.slug}: Railyard's catalogue entry is {entry.get('uHeight')}U, "
                    f"the DCIM's {dt.u_height}U; "
                    "devices are placed at the DCIM's height"
                )
        else:
            key = self.ident("dt", dt.slug or dt.id)
            if key in self.catalogue_entries:
                key = self.ident("dt", f"{dt.slug}-{dt.id}")
            device_outlets = max(
                (
                    [c.type for c in self.components_by_device.get(d.id, []) if c.kind == "power-outlet"]
                    for d in sorted(self.s.devices, key=_by_id)
                    if d.device_type_id == dt.id
                ),
                key=len,
                default=[],
            )
            warnings: list[str] = []
            entry = custom_device_type(dt, key, device_outlets, warnings)
            for warning in warnings:
                self.report.warn(warning)
            if dt.u_height and dt.u_height != math.floor(dt.u_height):
                self.report.warn(
                    f"device type {dt.slug}: {dt.u_height}U is not a whole number of units; rounded up to {height}U"
                )
            self.catalogue_entries[key] = entry
            self.report.count("customDeviceTypes")
        self.type_keys[dt.id] = key
        return entry

    # ---- devices -----------------------------------------------------------------------------

    def build_devices(self) -> None:
        occupied: dict[str, dict[tuple[int, str], str]] = {}  # rack id -> (unit, face) -> label
        sides: dict[str, int] = {}
        for device in sorted(self.s.devices, key=_by_id):
            label = (device.name or "").strip()
            what = label or f"device {device.id}"
            if device.parent_device_id:
                parent = self.devices.get(device.parent_device_id)
                parent_name = (parent.name if parent else "") or device.parent_device_id
                self.report.skip(
                    "devices",
                    device.id,
                    label,
                    f"installed in a device bay of {parent_name}; child devices are not modelled",
                )
                continue
            if device.site_id not in self.sites:
                self.report.skip("devices", device.id, label, "its site is not imported")
                continue
            if not device.rack_id:
                self.report.skip("devices", device.id, label, "not in a rack; unracked devices are not modelled")
                continue
            rack = self.rack_by_source.get(device.rack_id)
            if rack is None:
                self.report.skip("devices", device.id, label, f"its rack {device.rack_id} is not imported")
                continue
            dt = self.device_types.get(device.device_type_id)
            if dt is None:
                self.report.skip(
                    "devices", device.id, label, f"its device type {device.device_type_id} is not in the snapshot"
                )
                continue

            placement: dict[str, Any] = {"id": self.ident("dev", device.id)}
            if (dt.u_height or 0) <= 0:
                side = "left" if sides.get(rack["id"], 0) % 2 == 0 else "right"
                sides[rack["id"]] = sides.get(rack["id"], 0) + 1
                placement.update({"startU": 1, "heightU": 1, "face": "front", "mount": "zeroU", "side": side})
            else:
                if device.position is None:
                    self.report.skip("devices", device.id, label, f"in rack {rack['name']} without a position")
                    continue
                start = math.floor(device.position)
                top = math.ceil(device.position + dt.u_height)
                height = top - start
                if start != device.position or height != dt.u_height:
                    self.report.warn(
                        f"{what}: occupies U{device.position} for {dt.u_height}U; "
                        f"Railyard places whole units, so it takes U{start}-U{top - 1}"
                    )
                low = rack["startingUnit"]
                high = low + rack["uHeight"] - 1
                if start < low or top - 1 > high:
                    self.report.skip(
                        "devices",
                        device.id,
                        label,
                        f"U{start}-U{top - 1} does not fit rack {rack['name']} (U{low}-U{high})",
                    )
                    continue
                face = "full" if dt.is_full_depth else (device.face if device.face in ("front", "rear") else "front")
                if not dt.is_full_depth and device.face not in ("front", "rear"):
                    self.report.warn(f"{what}: no mounting face; placed on the front")
                faces = ("front", "rear") if face == "full" else (face,)
                cells = occupied.setdefault(rack["id"], {})
                clash = next((cells[(u, f)] for u in range(start, top) for f in faces if (u, f) in cells), None)
                if clash is not None:
                    self.report.skip(
                        "devices", device.id, label, f"overlaps {clash} in rack {rack['name']} at U{start}-U{top - 1}"
                    )
                    continue
                for u in range(start, top):
                    for f in faces:
                        cells[(u, f)] = what
                placement.update({"startU": start, "heightU": height, "face": face})

            entry = self.device_type_entry(dt)
            if label:
                placement["label"] = self.unique_device_name(
                    device, self.fit(label, MAX_IDENTIFIER, f"device {device.id} name")
                )
            placement["namingMode"] = "manual"
            if device.serial:
                serial = device.serial.strip()
                if len(serial) > MAX_SERIAL:
                    self.report.warn(f"{what}: serial {serial!r} is longer than {MAX_SERIAL} characters; truncated")
                    serial = serial[:MAX_SERIAL]
                if serial:
                    placement["serial"] = serial
            placement["deviceTypeRef"] = entry["key"]
            if device.role:
                placement["role"] = self.fit(device.role, MAX_IDENTIFIER, f"{what} role")
            notes = self.notes(device.comments, what)
            if notes:
                placement["notes"] = notes
            tags = self.tags(device.tags, what)
            if tags:
                placement["tags"] = tags
            placed = _Placed(rack_id=rack["id"], placement=placement, device_type=entry)
            placement["ports"] = self.device_ports(device, entry, placed, what)
            placement["powerInlets"] = self.device_inlets(device, entry, placed, what)
            self.device_outlets(device, placed)
            rack["placements"].append(placement)
            self.placed[device.id] = placed
            if (device.status or "active") != "active":
                self.device_status[placement["id"]] = device.status
            self.report.count("devices")

    def device_ports(self, device: Device, entry: dict, placed: _Placed, what: str) -> list[dict]:
        comps = self.components_by_device.get(device.id, [])
        order = {"interface": 0, "front-port": 1, "rear-port": 2}
        data = sorted(
            (c for c in comps if c.kind in order),
            key=lambda c: (order[c.kind], m.natural_key(c.name), m.natural_key(c.id)),
        )
        for comp in comps:
            if comp.kind == "console-port":
                self.report.skip("consolePorts", comp.id, f"{what} {comp.name}", "console ports are not modelled")
            elif comp.kind == "other":
                self.report.skip("components", comp.id, f"{what} {comp.name}", "this component type is not modelled")
        spec = entry.get("ports") or {}
        pass_through = bool(spec.get("passThrough"))
        front_defs = {d["name"]: i for i, d in enumerate(spec.get("frontPorts") or []) if d.get("name")}
        rear_defs = {d["name"]: i for i, d in enumerate(spec.get("rearPorts") or []) if d.get("name")}
        rear_ids = {c.id for c in data if c.kind == "rear-port"}

        ports: list[dict] = []
        names: set[tuple[str, str, str]] = set()
        modules = 0
        for comp in data:
            name_what = f"{what} {comp.name}"
            if comp.kind == "interface" and not m.cableable_interface(comp.type):
                self.report.skip("ports", comp.id, name_what, "virtual or wireless interface; it takes no cable")
                continue
            if comp.kind == "front-port" and comp.rear_port_id not in rear_ids:
                self.report.skip("ports", comp.id, name_what, "front port without a rear port on its device")
                continue
            if len(ports) >= MAX_PORTS:
                self.report.skip("ports", comp.id, name_what, f"the device has more than {MAX_PORTS} ports")
                continue
            side = "rear" if comp.kind == "rear-port" else "front"
            port_id = self.ident(_PORT_ID_KIND[comp.kind], comp.id, MAX_PORT_ID)
            name = self.port_name(comp, side, names, what)
            port: dict[str, Any] = {"id": port_id, "name": name, "side": side, "kind": comp.kind}
            connector = m.instance_connector(comp.type, comp.kind)
            defs, template_names = (
                (spec.get("frontPorts") or [], front_defs)
                if side == "front"
                else (spec.get("rearPorts") or [], rear_defs)
            )
            # A switch's template lists its interfaces, a patch panel's its front and rear ports.
            from_template = pass_through if comp.kind != "interface" else not pass_through
            template = template_names.get(comp.name) if from_template and not comp.module else None
            if comp.module:
                port["origin"] = "module"
                modules += 1
            elif template is not None:
                port["origin"] = "template"
                definition = defs[template]
                classified = (
                    m.interface_connector(comp.type) if comp.kind == "interface" else m.port_connector(comp.type)
                )
                if classified == (definition.get("type"), definition.get("netboxType")):
                    # The template's type: keep its connector, so the export finds its netboxType again.
                    connector = definition.get("type") or spec.get("media")
            else:
                port["origin"] = "custom"
            if connector:
                port["connector"] = m.fit(connector, MAX_PORT_NAME)
            if comp.module:
                port["module"] = m.fit(comp.module.strip(), MAX_PORT_NAME)
            if template is not None and not comp.module:
                port["templateIndex"] = template + 1
            if comp.kind == "front-port":
                port["peerId"] = self.ident("rp", comp.rear_port_id, MAX_PORT_ID)
                if (comp.rear_port_position or 1) > 1:
                    port["peerPosition"] = int(comp.rear_port_position)
            ports.append(port)
            placed.ports[(comp.kind, comp.id)] = port_id
        self.report.count("ports", len(ports))
        if modules:
            self.module_components[placed.placement["id"]] = modules
            self.report.warn(
                f"{what}: {modules} port(s) sit in modules; imported as module ports, "
                "the modules themselves are not modelled"
            )
        return ports

    def port_name(self, comp: Component, side: str, names: set, what: str) -> str:
        name = m.fit((comp.name or "").strip() or f"{comp.kind} {comp.id}", MAX_PORT_NAME)
        key = (side, comp.kind, m.identity_key(name))
        if key in names:
            original = name
            suffix = f" ({comp.id})"
            name = m.fit(original, MAX_PORT_NAME - len(suffix)) + suffix
            key = (side, comp.kind, m.identity_key(name))
            self.report.warn(f"{what}: two {comp.kind}s are named {original!r}; one is renamed {name!r}")
        names.add(key)
        return name

    def device_inlets(self, device: Device, entry: dict, placed: _Placed, what: str) -> list[dict]:
        template_names = {normalise_inlet_name(i["name"]) for i in entry.get("powerInlets") or [] if i.get("name")}
        comps = sorted(
            (c for c in self.components_by_device.get(device.id, []) if c.kind == "power-port"),
            key=lambda c: (m.natural_key(c.name), m.natural_key(c.id)),
        )
        inlets: list[dict] = []
        seen: set[str] = set()
        for comp in comps:
            if len(inlets) >= MAX_INLETS:
                self.report.skip(
                    "powerInlets", comp.id, f"{what} {comp.name}", f"the device has more than {MAX_INLETS} power ports"
                )
                continue
            name = m.fit((comp.name or "").strip() or f"PSU {comp.id}", MAX_PORT_NAME)
            if normalise_inlet_name(name) in seen:
                original, suffix = name, f" ({comp.id})"
                name = m.fit(original, MAX_PORT_NAME - len(suffix)) + suffix
                self.report.warn(f"{what}: two power ports are named {original!r}; one is renamed {name!r}")
            seen.add(normalise_inlet_name(name))
            inlet_id = self.ident("pp", comp.id, MAX_PORT_ID)
            inlet: dict[str, Any] = {"id": inlet_id, "name": name}
            connector = m.power_connector(comp.type)
            if connector:
                inlet["connector"] = m.fit(connector, MAX_PORT_NAME)
            template = not comp.module and normalise_inlet_name(comp.name or "") in template_names
            inlet["origin"] = "template" if template else "custom"
            inlets.append(inlet)
            placed.inlets[comp.id] = inlet_id
        self.report.count("powerInlets", len(inlets))
        return inlets

    def device_outlets(self, device: Device, placed: _Placed) -> None:
        outlets = sorted(
            (c for c in self.components_by_device.get(device.id, []) if c.kind == "power-outlet"),
            key=lambda c: (m.natural_key(c.name), m.natural_key(c.id)),
        )
        for number, comp in enumerate(outlets, start=1):
            placed.outlets[comp.id] = number

    # ---- cables ------------------------------------------------------------------------------

    def resolve_end(self, terminations) -> tuple[Component | None, _Placed | None, str]:
        """The component and imported device at one cable end, or the reason it cannot be imported."""
        if len(terminations) != 1:
            return None, None, f"{len(terminations)} terminations on one end; Railyard cables are point to point"
        term = terminations[0]
        if term.object_type in _UNMODELLED_ENDS:
            return None, None, f"one end is {_UNMODELLED_ENDS[term.object_type]}"
        kind = _TERMINATION_KINDS.get(term.object_type)
        if kind is None:
            return None, None, f"one end is a {term.object_type}, which Railyard does not model"
        comp = self.components.get((kind, str(term.object_id)))
        if comp is None:
            return None, None, "one end is outside the imported sites"
        placed = self.placed.get(comp.device_id)
        if placed is None:
            device = self.devices.get(comp.device_id)
            return comp, None, f"its device {(device.name if device else '') or comp.device_id} is not imported"
        return comp, placed, ""

    def build_cables(self) -> None:
        used_ports: set[tuple[str, str]] = set()
        used_inlets: set[tuple[str, str]] = set()
        used_outlets: set[tuple[str, int]] = set()
        for cable in sorted(self.s.cables, key=_by_id):
            label = cable.label or f"cable {cable.id}"
            a, a_placed, a_reason = self.resolve_end(cable.a)
            b, b_placed, b_reason = self.resolve_end(cable.b)
            reason = a_reason or b_reason
            # A cable on any power termination is a power cable, reported with the power links.
            kind = "powerLinks" if any(t.object_type in _POWER_ENDS for t in [*cable.a, *cable.b]) else "cables"
            if reason:
                self.report.skip(kind, cable.id, label, reason)
                continue
            assert a and b and a_placed and b_placed
            if {a.kind, b.kind} <= {"power-port", "power-outlet"}:
                self.add_power_link(cable, a, a_placed, b, b_placed, used_inlets, used_outlets)
            elif a.kind in _DATA_KINDS and b.kind in _DATA_KINDS:
                self.add_cable(cable, a, a_placed, b, b_placed, used_ports)
            else:
                self.report.skip(kind, cable.id, label, f"connects a {a.kind} to a {b.kind}")

    def add_cable(self, cable, a: Component, a_placed: _Placed, b: Component, b_placed: _Placed, used: set) -> None:
        label = cable.label or f"cable {cable.id}"
        a_port = a_placed.ports.get((a.kind, a.id))
        b_port = b_placed.ports.get((b.kind, b.id))
        if a_port is None or b_port is None:
            missing = a if a_port is None else b
            self.report.skip("cables", cable.id, label, f"its {missing.kind} {missing.name!r} was not imported")
            return
        a_id, b_id = a_placed.placement["id"], b_placed.placement["id"]
        if a_id == b_id:
            self.report.skip("cables", cable.id, label, "both ends are on one device, which Railyard does not allow")
            return
        if (a_id, a_port) in used or (b_id, b_port) in used:
            self.report.skip("cables", cable.id, label, "a port it uses already has a cable")
            return
        used.update({(a_id, a_port), (b_id, b_port)})
        out: dict[str, Any] = {
            "id": self.ident("cable", cable.id),
            "a": {"rackId": a_placed.rack_id, "placementId": a_id, "portId": a_port},
            "b": {"rackId": b_placed.rack_id, "placementId": b_id, "portId": b_port},
        }
        media = m.cable_media(cable.type)
        if media:
            out["media"] = self.fit(media, MAX_CABLE_MEDIA, f"cable {cable.id} type")
        if cable.label:
            out["label"] = self.fit(cable.label, MAX_IDENTIFIER, f"cable {cable.id} label")
        colour = m.cable_colour(cable.color)
        if colour:
            out["colour"] = colour
        out["kind"] = "patch"
        self.cables.append(out)
        if (cable.status or "connected") != "connected":
            self.cable_status[out["id"]] = cable.status
        self.report.count("cables")

    def add_power_link(self, cable, a, a_placed, b, b_placed, used_inlets: set, used_outlets: set) -> None:
        label = cable.label or f"cable {cable.id}"
        if a.kind == b.kind:
            self.report.skip(
                "powerLinks", cable.id, label, f"connects two {a.kind}s; Railyard links a device input to a PDU outlet"
            )
            return
        (inlet_comp, device), (outlet_comp, pdu) = (
            ((a, a_placed), (b, b_placed)) if a.kind == "power-port" else ((b, b_placed), (a, a_placed))
        )
        device_id, pdu_id = device.placement["id"], pdu.placement["id"]
        inlet = device.inlets.get(inlet_comp.id)
        outlet = pdu.outlets.get(outlet_comp.id)
        if inlet is None:
            self.report.skip("powerLinks", cable.id, label, f"its power port {inlet_comp.name!r} was not imported")
            return
        if device_id == pdu_id:
            self.report.skip("powerLinks", cable.id, label, "a PDU cannot power itself")
            return
        if _outlet_count(device) is not None or _passive(device):
            self.report.skip(
                "powerLinks",
                cable.id,
                label,
                "the powered device is itself a PDU or passive; Railyard powers only end devices from PDUs",
            )
            return
        count = _outlet_count(pdu)
        if count is None:
            self.report.skip("powerLinks", cable.id, label, "the device it draws from has no PDU outlets in Railyard")
            return
        if outlet is None or outlet > count:
            self.report.skip(
                "powerLinks", cable.id, label, f"outlet {outlet_comp.name!r} is beyond the PDU type's {count} outlets"
            )
            return
        if (device_id, inlet) in used_inlets or (pdu_id, outlet) in used_outlets:
            self.report.skip("powerLinks", cable.id, label, "its power input or outlet already has a link")
            return
        used_inlets.add((device_id, inlet))
        used_outlets.add((pdu_id, outlet))
        self.power_links.append(
            {
                "id": self.ident("power", cable.id),
                "device": {"rackId": device.rack_id, "placementId": device_id, "inletId": inlet},
                "pdu": {"rackId": pdu.rack_id, "placementId": pdu_id, "outlet": outlet},
            }
        )
        if (cable.status or "connected") != "connected":
            self.cable_status[self.ident("power", cable.id)] = cable.status
        self.report.count("powerLinks")

    # ---- meta --------------------------------------------------------------------------------

    def meta(self) -> dict[str, Any]:
        site_ids = set(self.sites)
        panels = [p for p in sorted(self.s.power_panels, key=_by_id) if p.site_id in site_ids]
        panel_ids = {p.id for p in panels}
        feeds = []
        for feed in sorted(self.s.power_feeds, key=_by_id):
            if feed.power_panel_id not in panel_ids:
                continue
            record = asdict(feed)
            record["availablePowerW"] = round(feed.available_power_w, 1)
            if feed.rack_id in self.rack_by_source:
                record["railyardRackId"] = self.rack_by_source[feed.rack_id]["id"]
            feeds.append(record)
        unmodelled: dict[str, Any] = {
            "powerPanels": [asdict(p) for p in panels],
            "powerFeeds": feeds,
            "skipped": [asdict(item) for item in self.report.skipped],
        }
        if self.cable_status:
            unmodelled["cableStatus"] = self.cable_status
        if self.module_components:
            unmodelled["moduleComponents"] = self.module_components
        self.report.unmodelled = unmodelled
        return {
            "source": self.s.source,
            "url": self.s.source_url,
            "version": self.s.source_version,
            "sites": [{"id": s.id, "slug": s.slug, "name": s.name} for s in sorted(self.sites.values(), key=_by_id)],
            "importedAt": self.imported_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "prefix": self.prefix,
            "unmodelled": unmodelled,
            "deviceStatus": self.device_status,
            "renamedDevices": self.renamed_devices,
        }


def _outlet_count(placed: _Placed) -> int | None:
    """The outlets Railyard gives a placement (model.OutletSpecFor), or None when it is not a PDU."""
    spec = placed.device_type.get("outlets")
    if spec is not None:
        return int(spec.get("count") or 0)
    role = normalise_inlet_name(placed.placement.get("role", ""))
    if placed.placement.get("mount") == "zeroU" and role in ("pdu", "power"):
        return PDU_ROLE_OUTLETS
    return None


def _passive(placed: _Placed) -> bool:
    """Whether Railyard treats a placement as passive gear that cannot draw power (model.passivePowerDevice)."""
    role = placed.placement.get("role", "").strip()
    dt = placed.device_type
    if _BLANKING_ROLE.match(role) or any(_BLANKING_TYPE.search(str(dt.get(k, ""))) for k in ("key", "model")):
        return True
    if (dt.get("ports") or {}).get("passThrough"):
        return True
    return role.lower() in _PASSIVE_ROLES
