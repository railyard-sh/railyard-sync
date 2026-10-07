"""``NautobotLoader`` — read one or more Nautobot locations into a :class:`~railyard_sync.dcim.snapshot.Snapshot`.

Supports Nautobot 2.x (tested with 2.4) over the REST API, with ``Authorization: Token <token>``.

**Nautobot has no sites.** Every place is a :class:`Location` in one tree, typed by a location type (Region → Site →
Building → Room, as each installation chooses). An import is built around the locations it is asked for: each one
becomes the snapshot's *site* (Railyard's floor container), the locations above it its *regions* and the locations
below it the snapshot's *locations*, every one keeping its location type's name. With ``locations=None`` (the CLI's
``--all-locations``) the import takes every location whose type is the first, going down the tree, that may hold
racks or devices (a Site under Regions, typically), so each data centre is one site.

What one load reads, filtered server-side by the imported locations and their descendants:

    GET /api/status/                                          the version (refused below 2.0)
    GET /api/dcim/locations/?depth=1                          the location tree the token can see
    GET /api/dcim/location-types/?depth=1                     --all-locations only: which types hold racks
    GET /api/dcim/{racks,devices}/?location=…&depth=1
    GET /api/dcim/device-types/?id=…&depth=1                  only the types the devices use
    GET /api/dcim/*-templates/?device_type=…                  interface, front/rear port, power port, power
                                                              outlet and console port templates
    GET /api/dcim/{interfaces,front-ports,rear-ports,power-ports,power-outlets,console-ports}/?location=…
    GET /api/dcim/cables/?location_id=…&depth=1
    GET /api/dcim/{power-panels,power-feeds}/?location=…&depth=1

``?depth=1`` is asked for where names are needed (a status, role, location type or tag is only an id at depth 0);
components and templates are read at depth 0, as ids are all they need, which keeps a large site cheap. Statuses
keep their Nautobot name as a slug (``Active`` → ``active``, ``Pre-production`` → ``pre-production``).

What Nautobot does not have, or this loader does not read: rack types (none), device-type slugs (a type's slug is
derived from its manufacturer and model, as the devicetype-library spells it), rack and device-type weights,
and components installed in modules (Nautobot files them under the module, not the device). Front ports map onto
one rear port position each, as in NetBox up to 4.4.

A ``session`` (``requests.Session``-like) can be injected, which is how the tests avoid the network and how the
Nautobot app reads its own database in-process. The token is never logged, and never appears in an exception
message or the loader's ``repr``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

from ..dcim_http import NAUTOBOT, Session
from ..export.mappings import slugify
from ..log import count
from .errors import DCIMError, DCIMNotFoundError, DCIMVersionError
from .rest import RESTReader, unique
from .snapshot import (
    Cable,
    Component,
    ComponentKind,
    ComponentTemplate,
    Device,
    DeviceType,
    Location,
    PowerFeed,
    PowerPanel,
    Rack,
    Region,
    Site,
    Snapshot,
    Termination,
)
from .values import integer, kg, metres, mm, name_of, number, parse_version, ref_id, tag_names, value

log = logging.getLogger(__name__)

MIN_VERSION = (2, 0)
MAX_TESTED_VERSION = (2, 4)
#: The newest major release read at all (with a warning past MAX_TESTED_VERSION).
MAX_MAJOR = 3
#: Ids per filtered request. Nautobot ids are UUIDs (36 characters), so fewer than NetBox's integers.
ID_CHUNK = 50

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

#: The location type content types that mark where a data centre starts: a location that may hold racks or devices.
_HOLDS = {"dcim.rack", "dcim.device"}

_COMPONENT_ENDPOINTS: list[tuple[ComponentKind, str, str]] = [
    ("interface", "interfaces", "dcim.interface"),
    ("rear-port", "rear-ports", "dcim.rearport"),
    ("front-port", "front-ports", "dcim.frontport"),
    ("power-port", "power-ports", "dcim.powerport"),
    ("power-outlet", "power-outlets", "dcim.poweroutlet"),
    ("console-port", "console-ports", "dcim.consoleport"),
]
_TEMPLATE_ENDPOINTS: list[tuple[ComponentKind, str]] = [
    ("interface", "interface-templates"),
    ("rear-port", "rear-port-templates"),  # before front ports, which name them by id
    ("front-port", "front-port-templates"),
    ("power-port", "power-port-templates"),
    ("power-outlet", "power-outlet-templates"),
    ("console-port", "console-port-templates"),
]
_TEMPLATE_ORDER: list[ComponentKind] = [
    "interface",
    "front-port",
    "rear-port",
    "power-port",
    "power-outlet",
    "console-port",
]


def status_slug(field: Any, default: str = "active") -> str:
    """A Nautobot status (an object at depth 1, its name otherwise) as a slug: ``Active`` -> ``active``."""
    name = name_of(field) if isinstance(field, dict) else ("" if field is None else str(field))
    name = "-".join(name.strip().lower().split())
    return name or default


def is_uuid(text: str) -> bool:
    return bool(_UUID.match(text or ""))


class NautobotLoader(RESTReader):
    """Reads Nautobot locations into a :class:`Snapshot`. One instance can serve several loads."""

    product = NAUTOBOT
    id_chunk = ID_CHUNK

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
        super().__init__(url, token, session=session, verify=verify, timeout=timeout, page_size=page_size, log=log)

    # -- version ---------------------------------------------------------------------------------

    def check_version(self) -> str:
        """Read ``/api/status/`` and return Nautobot's version; refuse 1.x (it has sites) and unknown majors."""
        status = self._get("/api/status/")
        text = str((status or {}).get("nautobot-version") or "")
        major_minor = parse_version(text, "Nautobot")[:2]
        if major_minor < MIN_VERSION:
            raise DCIMVersionError(f"Nautobot {text} is not supported: railyard-sync needs Nautobot 2.0 or later.")
        if major_minor[0] > MAX_MAJOR:
            raise DCIMVersionError(
                f"Nautobot {text} is not supported yet: railyard-sync is tested with Nautobot 2.0 to "
                f"{MAX_TESTED_VERSION[0]}.{MAX_TESTED_VERSION[1]}."
            )
        self.version = text
        return text

    # -- load ------------------------------------------------------------------------------------

    def load(self, locations: list[str] | None) -> Snapshot:
        """Read the given locations (each a name or a UUID) and everything inside them.

        ``None`` reads every top data-centre location the token can see (see the module docstring)."""
        if isinstance(locations, str):
            locations = [locations]
        if locations is not None and not locations:
            raise ValueError("at least one location is required")
        version = self.check_version()
        snap = Snapshot(source="nautobot", source_url=self.url, source_version=version)
        if parse_version(version, "Nautobot")[:2] > MAX_TESTED_VERSION:
            snap.warnings.append(
                f"Nautobot {version} is newer than railyard-sync has been tested with "
                f"({MAX_TESTED_VERSION[0]}.{MAX_TESTED_VERSION[1]}); check the import report carefully."
            )

        tree = _Tree(self._list("/api/dcim/locations/", [("depth", 1)]))
        chosen = self._find_locations(tree, locations) if locations is not None else self._top_locations(tree)
        chosen = self._outermost(tree, chosen, snap.warnings)
        names = [tree.items[i].get("name") or i for i in chosen]
        shown = ", ".join(names[:8]) + (f" and {len(names) - 8} more" if len(names) > 8 else "")
        log.info("Reading Nautobot %s (%s): %s (%s)…", self.url, version, count(len(chosen), "location"), shown)

        site_of: dict[str, str] = {}  # every imported location id -> the import site it is under
        regions: dict[str, Region] = {}
        for site_id in chosen:
            for ancestor in tree.ancestors(site_id):
                regions.setdefault(ancestor, self._region(tree.items[ancestor]))
            snap.sites.append(self._site(tree.items[site_id]))
            site_of[site_id] = site_id
            for loc_id in tree.descendants(site_id):
                site_of[loc_id] = site_id
                snap.locations.append(self._location(tree.items[loc_id], site_id))
        snap.regions = list(regions.values())
        subtree = list(site_of)
        log.debug("Read %s and %s", count(len(snap.regions), "region"), count(len(snap.locations), "location"))

        def located(path: str, key: str = "location", depth: int = 1) -> list[dict]:
            """Every object a list endpoint has in the imported locations, each once."""
            extra = [("depth", depth)] if depth else []
            found: dict[str, dict] = {}
            for item in self._list_by_ids(path, key, subtree, extra):
                found.setdefault(str(item["id"]), item)
            return list(found.values())

        snap.racks = [self._rack(r, site_of) for r in located("/api/dcim/racks/")]
        raw_devices = located("/api/dcim/devices/")
        snap.devices = [self._device(d, site_of) for d in raw_devices]
        log.debug("Read %s and %s", count(len(snap.racks), "rack"), count(len(snap.devices), "device"))
        snap.device_types = self._load_device_types(unique(d.device_type_id for d in snap.devices), snap.warnings)
        log.debug("Read %s with their component templates", count(len(snap.device_types), "device type"))

        loaded_devices = {d.id for d in snap.devices}
        for kind, endpoint, _ in _COMPONENT_ENDPOINTS:
            before = len(snap.components)
            for item in located(f"/api/dcim/{endpoint}/", depth=0):
                component = self._component(kind, item, snap.warnings)
                if component.device_id in loaded_devices:
                    snap.components.append(component)
            log.debug("Read %s", count(len(snap.components) - before, kind.replace("-", " ")))

        snap.power_panels = [self._power_panel(p, site_of) for p in located("/api/dcim/power-panels/")]
        snap.power_feeds = [self._power_feed(f) for f in located("/api/dcim/power-feeds/")]
        snap.cables = self._load_cables(located("/api/dcim/cables/", key="location_id"), snap)
        log.debug(
            "Read %s, %s and %s",
            count(len(snap.power_panels), "power panel"),
            count(len(snap.power_feeds), "power feed"),
            count(len(snap.cables), "cable"),
        )
        return snap

    # -- choosing the locations --------------------------------------------------------------------

    def _find_locations(self, tree: _Tree, refs: list[str]) -> list[str]:
        found: list[str] = []
        for ref in refs:
            needle = str(ref).strip()
            if not needle:
                raise ValueError("location reference must not be empty")
            if is_uuid(needle):
                match = next((i for i in tree.items if i.lower() == needle.lower()), None)
                if match is None:
                    raise DCIMNotFoundError(f"No Nautobot location has the id {needle} (or the token may not see it).")
            else:
                matches = [i for i, item in tree.items.items() if str(item.get("name") or "") == needle]
                if not matches:
                    folded = needle.casefold()
                    matches = [i for i, item in tree.items.items() if str(item.get("name") or "").casefold() == folded]
                if not matches:
                    raise DCIMNotFoundError(f"No Nautobot location is named {needle!r} (or the token may not see it).")
                if len(matches) > 1:
                    listed = "; ".join(f"{tree.path(i)} ({i})" for i in matches[:6])
                    raise DCIMNotFoundError(
                        f"{len(matches)} Nautobot locations are named {needle!r}: {listed}. Name the one to import "
                        "by its id."
                    )
                match = matches[0]
            if match not in found:
                found.append(match)
        return found

    def _top_locations(self, tree: _Tree) -> list[str]:
        """The locations where a data centre starts: of a type that holds racks or devices, below none such."""
        holds: dict[str, bool] = {}
        for item in self._list("/api/dcim/location-types/", [("depth", 1)]):
            types = {str(t) for t in item.get("content_types") or []}
            holds[str(item["id"])] = bool(types & _HOLDS)

        def holding(loc_id: str) -> bool:
            return holds.get(ref_id(tree.items[loc_id].get("location_type")) or "", False)

        top = [i for i in tree.order if holding(i) and not any(holding(a) for a in tree.ancestors(i))]
        if not top:
            raise DCIMNotFoundError(
                "This Nautobot has no location the token can see whose location type may hold racks or devices."
            )
        return top

    @staticmethod
    def _outermost(tree: _Tree, chosen: list[str], warnings: list[str]) -> list[str]:
        """``chosen`` without the locations inside another chosen one (they are imported with it)."""
        picked = set(chosen)
        out = []
        for loc_id in chosen:
            outer = next((a for a in tree.ancestors(loc_id) if a in picked), None)
            if outer is not None:
                warnings.append(
                    f"Location {tree.path(loc_id)} is inside {tree.path(outer)}, which is imported too; it is "
                    "imported as part of it."
                )
                continue
            out.append(loc_id)
        return out

    # -- locations -------------------------------------------------------------------------------

    @staticmethod
    def _region(item: dict) -> Region:
        name = str(item.get("name") or "")
        return Region(
            id=str(item["id"]),
            name=name,
            slug=slugify(name),
            parent_id=ref_id(item.get("parent")),
            location_type=name_of(item.get("location_type")),
        )

    @staticmethod
    def _site(item: dict) -> Site:
        name = str(item.get("name") or "")
        return Site(
            id=str(item["id"]),
            name=name,
            slug=slugify(name),
            status=status_slug(item.get("status")),
            facility=str(item.get("facility") or ""),
            region_id=ref_id(item.get("parent")),
            description=str(item.get("description") or ""),
            comments=str(item.get("comments") or ""),
            tags=tag_names(item),
            location_type=name_of(item.get("location_type")),
        )

    @staticmethod
    def _location(item: dict, site_id: str) -> Location:
        name = str(item.get("name") or "")
        parent = ref_id(item.get("parent"))
        return Location(
            id=str(item["id"]),
            name=name,
            slug=slugify(name),
            site_id=site_id,
            parent_id=None if parent == site_id else parent,
            status=status_slug(item.get("status")),
            facility=str(item.get("facility") or ""),
            location_type=name_of(item.get("location_type")),
            description=str(item.get("description") or ""),
            tags=tag_names(item),
        )

    # -- racks and devices -----------------------------------------------------------------------

    @staticmethod
    def _placed(location: Any, site_of: dict[str, str]) -> tuple[str, str | None]:
        """(import site id, location id below it or None) for an object's location."""
        loc_id = ref_id(location) or ""
        site_id = site_of.get(loc_id, "")
        return site_id, (loc_id if loc_id and loc_id != site_id else None)

    def _rack(self, item: dict, site_of: dict[str, str]) -> Rack:
        site_id, location_id = self._placed(item.get("location"), site_of)
        return Rack(
            id=str(item["id"]),
            name=str(item.get("name") or ""),
            site_id=site_id,
            location_id=location_id,
            status=status_slug(item.get("status")),
            role=name_of(item.get("role")),
            u_height=integer(item.get("u_height"), 42),
            starting_unit=1,
            desc_units=bool(item.get("desc_units")),
            width_in=integer(value(item.get("width")), 19),
            outer_width_mm=mm(item.get("outer_width"), item.get("outer_unit")),
            outer_depth_mm=mm(item.get("outer_depth"), item.get("outer_unit")),
            form_factor=value(item.get("type")),
            max_weight_kg=kg(item.get("max_weight"), item.get("weight_unit")),
            serial=str(item.get("serial") or ""),
            asset_tag=str(item.get("asset_tag") or ""),
            facility_id=str(item.get("facility_id") or ""),
            comments=str(item.get("comments") or ""),
            tags=tag_names(item),
            custom_fields=dict(item.get("custom_fields") or {}),
        )

    def _device(self, item: dict, site_of: dict[str, str]) -> Device:
        site_id, location_id = self._placed(item.get("location"), site_of)
        bay = item.get("parent_bay")
        parent = ref_id(bay.get("device")) if isinstance(bay, dict) else None
        if isinstance(bay, dict) and parent is None:
            parent = f"bay {name_of(bay) or ref_id(bay)}"
        return Device(
            id=str(item["id"]),
            name=str(item.get("name") or ""),
            device_type_id=ref_id(item.get("device_type")) or "",
            site_id=site_id,
            role=name_of(item.get("role")),
            location_id=location_id,
            rack_id=ref_id(item.get("rack")),
            position=number(item.get("position")),
            face=value(item.get("face")),
            status=status_slug(item.get("status")),
            serial=str(item.get("serial") or ""),
            asset_tag=str(item.get("asset_tag") or ""),
            parent_device_id=parent,
            platform=name_of(item.get("platform")),
            tenant=name_of(item.get("tenant")),
            comments=str(item.get("comments") or ""),
            tags=tag_names(item),
            custom_fields=dict(item.get("custom_fields") or {}),
        )

    # -- device types ----------------------------------------------------------------------------

    def _load_device_types(self, ids: list[str], warnings: list[str]) -> list[DeviceType]:
        types: dict[str, DeviceType] = {}
        for item in self._list_by_ids("/api/dcim/device-types/", "id", ids, [("depth", 1)]):
            manufacturer = name_of(item.get("manufacturer"))
            model = str(item.get("model") or "")
            types[str(item["id"])] = DeviceType(
                id=str(item["id"]),
                manufacturer=manufacturer,
                model=model,
                # Nautobot 2 has no device-type slug; the devicetype-library's is manufacturer-model.
                slug=slugify(f"{manufacturer}-{model}"),
                u_height=integer(item.get("u_height"), 1),
                is_full_depth=bool(item.get("is_full_depth", True)),
                part_number=str(item.get("part_number") or ""),
                weight_kg=kg(item.get("weight"), item.get("weight_unit")),
                subdevice_role=value(item.get("subdevice_role")),
            )
        missing = [i for i in ids if i not in types]
        if missing:
            warnings.append(
                f"Nautobot did not return device type(s) {', '.join(missing)} used by the loaded devices; does the "
                "token lack permission to read device types?"
            )
        if not types:
            return []
        templates: dict[str, dict[ComponentKind, list[ComponentTemplate]]] = {
            dt: {kind: [] for kind in _TEMPLATE_ORDER} for dt in types
        }
        rear_names: dict[str, str] = {}  # rear port template id -> name
        for kind, endpoint in _TEMPLATE_ENDPOINTS:
            for item in self._list_by_ids(f"/api/dcim/{endpoint}/", "device_type", list(types)):
                dt_id = ref_id(item.get("device_type"))
                if dt_id not in templates:  # a module type's template, or a filter Nautobot ignored
                    continue
                template = self._template(kind, item, rear_names)
                if kind == "rear-port":
                    rear_names[str(item["id"])] = template.name
                templates[dt_id][kind].append(template)
        for dt_id, device_type in types.items():
            device_type.components = [t for kind in _TEMPLATE_ORDER for t in templates[dt_id][kind]]
        return [types[i] for i in ids if i in types]

    @staticmethod
    def _template(kind: ComponentKind, item: dict, rear_names: dict[str, str]) -> ComponentTemplate:
        template = ComponentTemplate(name=str(item.get("name") or ""), kind=kind, type=value(item.get("type")))
        if kind == "interface":
            template.mgmt_only = bool(item.get("mgmt_only"))
        elif kind == "rear-port":
            template.positions = integer(item.get("positions"), 1)
        elif kind == "front-port":
            rear = item.get("rear_port_template") or item.get("rear_port")
            template.rear_port_name = name_of(rear) if isinstance(rear, dict) and rear.get("name") else ""
            if not template.rear_port_name and ref_id(rear):
                template.rear_port_name = rear_names.get(ref_id(rear) or "", "")
            template.rear_port_position = integer(item.get("rear_port_position"), 1)
        elif kind == "power-port":
            template.maximum_draw_w = number(item.get("maximum_draw"))
            template.allocated_draw_w = number(item.get("allocated_draw"))
        elif kind == "power-outlet":
            template.feed_leg = value(item.get("feed_leg"))
        return template

    # -- components ------------------------------------------------------------------------------

    @staticmethod
    def _component(kind: ComponentKind, item: dict, warnings: list[str]) -> Component:
        module = item.get("module")
        component = Component(
            id=str(item["id"]),
            device_id=ref_id(item.get("device")) or "",
            name=str(item.get("name") or ""),
            kind=kind,
            type=value(item.get("type")),
            label=str(item.get("label") or ""),
            description=str(item.get("description") or ""),
            module=(name_of(module) or ref_id(module) or "") if isinstance(module, dict) else "",
        )
        if kind == "interface":
            component.mgmt_only = bool(item.get("mgmt_only"))
            component.enabled = bool(item.get("enabled", True))
        elif kind == "rear-port":
            component.positions = integer(item.get("positions"), 1)
        elif kind == "front-port":
            component.rear_port_id = ref_id(item.get("rear_port"))
            component.rear_port_position = integer(item.get("rear_port_position"), 1)
        elif kind == "power-port":
            component.maximum_draw_w = number(item.get("maximum_draw"))
            component.allocated_draw_w = number(item.get("allocated_draw"))
        elif kind == "power-outlet":
            component.power_port_id = ref_id(item.get("power_port"))
            component.feed_leg = value(item.get("feed_leg"))
        return component

    # -- power -----------------------------------------------------------------------------------

    def _power_panel(self, item: dict, site_of: dict[str, str]) -> PowerPanel:
        site_id, location_id = self._placed(item.get("location"), site_of)
        return PowerPanel(
            id=str(item["id"]), name=str(item.get("name") or ""), site_id=site_id, location_id=location_id
        )

    @staticmethod
    def _power_feed(item: dict) -> PowerFeed:
        feed = PowerFeed(
            id=str(item["id"]),
            name=str(item.get("name") or ""),
            power_panel_id=ref_id(item.get("power_panel")) or "",
            rack_id=ref_id(item.get("rack")),
        )
        feed.status = status_slug(item.get("status"), feed.status)
        feed.type = value(item.get("type"), feed.type)
        feed.supply = value(item.get("supply"), feed.supply)
        feed.phase = value(item.get("phase"), feed.phase)
        for attr in ("voltage", "amperage", "max_utilization"):
            parsed = number(item.get(attr))
            if parsed is not None:
                setattr(feed, attr, parsed)
        return feed

    # -- cables ----------------------------------------------------------------------------------

    def _load_cables(self, raw_cables: Iterable[dict], snap: Snapshot) -> list[Cable]:
        kinds = {kind: otype for kind, _, otype in _COMPONENT_ENDPOINTS}
        loaded = {(kinds[c.kind], c.id) for c in snap.components if c.kind in kinds}
        loaded |= {("dcim.powerfeed", f.id) for f in snap.power_feeds}
        devices = {d.id for d in snap.devices}
        cables: list[Cable] = []
        for item in raw_cables:
            cable_id = str(item["id"])
            ends = {side: self._termination(item, side) for side in ("a", "b")}
            cable = Cable(
                id=cable_id,
                a=[ends["a"]] if ends["a"] else [],
                b=[ends["b"]] if ends["b"] else [],
                type=value(item.get("type")),
                status=status_slug(item.get("status"), "connected"),
                label=str(item.get("label") or ""),
                color=str(item.get("color") or "").lstrip("#").lower(),
                length_m=metres(item.get("length"), item.get("length_unit")),
                tags=tag_names(item),
            )
            cables.append(cable)
            for side, end in ends.items():
                if end is not None and self._outside(end, item.get(f"termination_{side}"), loaded, devices):
                    label = f" ({cable.label})" if cable.label else ""
                    snap.warnings.append(
                        f"Cable {cable_id}{label}: its {side.upper()} end ({end.object_type} {end.object_id}) is "
                        "outside the loaded locations; the cable is kept for the importer to decide."
                    )
        return cables

    @staticmethod
    def _termination(item: dict, side: str) -> Termination | None:
        object_type = str(item.get(f"termination_{side}_type") or "")
        object_id = ref_id(item.get(f"termination_{side}_id")) or ref_id(item.get(f"termination_{side}"))
        if not object_type or not object_id:
            return None
        return Termination(object_type=object_type, object_id=object_id)

    @staticmethod
    def _outside(end: Termination, obj: Any, loaded: set[tuple[str, str]], devices: set[str]) -> bool:
        """Whether a DCIM termination belongs to something outside the loaded locations (as the NetBox loader)."""
        if not end.object_type.startswith("dcim.") or (end.object_type, end.object_id) in loaded:
            return False
        device_id = ref_id(obj.get("device")) if isinstance(obj, dict) else None
        return device_id is None or device_id not in devices


class _Tree:
    """The location tree the token can see: items by id, children by parent, in Nautobot's order."""

    def __init__(self, items: Iterable[dict]) -> None:
        self.items: dict[str, dict] = {}
        self.order: list[str] = []
        self.children: dict[str | None, list[str]] = {}
        for item in items:
            loc_id = str(item["id"])
            if loc_id in self.items:
                continue
            self.items[loc_id] = item
            self.order.append(loc_id)
        for loc_id in self.order:
            self.children.setdefault(self.parent(loc_id), []).append(loc_id)

    def parent(self, loc_id: str) -> str | None:
        parent = ref_id(self.items[loc_id].get("parent"))
        return parent if parent in self.items else None

    def ancestors(self, loc_id: str) -> list[str]:
        """The locations above ``loc_id``, outermost first (a loop in the data stops the walk)."""
        chain: list[str] = []
        parent = self.parent(loc_id)
        while parent is not None and parent not in chain and parent != loc_id:
            chain.append(parent)
            parent = self.parent(parent)
        return list(reversed(chain))

    def descendants(self, loc_id: str) -> list[str]:
        """The locations below ``loc_id``, parents before children."""
        out: list[str] = []
        queue = list(self.children.get(loc_id, []))
        seen = {loc_id}
        while queue:
            child = queue.pop(0)
            if child in seen:
                continue
            seen.add(child)
            out.append(child)
            queue.extend(self.children.get(child, []))
        return out

    def path(self, loc_id: str) -> str:
        return " → ".join(str(self.items[i].get("name") or i) for i in [*self.ancestors(loc_id), loc_id])


def load_nautobot_snapshot(
    url: str,
    token: str,
    locations: list[str] | None,
    *,
    session: Session | None = None,
    verify: bool | str = True,
) -> Snapshot:
    """Read ``locations`` (names or UUIDs; ``None`` for every top data-centre location) from the Nautobot at
    ``url``."""
    return NautobotLoader(url, token, session=session, verify=verify).load(locations)


__all__ = ["DCIMError", "NautobotLoader", "load_nautobot_snapshot", "status_slug"]
