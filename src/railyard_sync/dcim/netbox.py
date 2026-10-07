"""``NetBoxLoader`` — read one or more NetBox sites into a :class:`~railyard_sync.dcim.snapshot.Snapshot`.

Supports NetBox 4.0 to 4.6 over the REST API. Auth is ``Authorization: Token <token>`` for v1 tokens
and ``Authorization: Bearer <token>`` for the v2 tokens NetBox 4.5 introduced (they start ``nbt_``).

What one load reads, all filtered server-side so a site with thousands of devices costs a few dozen
paginated requests rather than one per device:

    GET /api/status/                                      the version (refused below 4.0)
    GET /api/dcim/sites/?id= | ?slug= | ?name=             each requested site
    GET /api/dcim/regions/{id}/                            the region chain above each site
    GET /api/dcim/{locations,racks,devices}/?site_id=…
    GET /api/dcim/rack-types/?id=…                         4.1+, only the types the racks use
    GET /api/dcim/device-types/?id=…                       only the types the devices use
    GET /api/dcim/*-port-templates/?device_type_id=…       interface, front/rear port, power port,
        /api/dcim/power-outlet-templates/                  power outlet and console port templates,
        /api/dcim/console-port-templates/                  several device types per request
    GET /api/dcim/{interfaces,front-ports,rear-ports,power-ports,power-outlets,console-ports}/?site_id=…
    GET /api/dcim/cables/?site_id=…
    GET /api/dcim/{power-panels,power-feeds}/?site_id=…

Full representations are requested (no ``brief`` and no ``fields=``: brief shapes changed between
4.0 and 4.6, and field selection is not available on every endpoint across that range).
``exclude=config_context`` keeps device lists cheap.

Version differences handled here:

- 4.0 calls a rack's form factor ``type``; 4.1 renamed it ``form_factor`` and added rack types.
- Up to 4.4 a front port (and front port template) has ``rear_port`` + ``rear_port_position``. From
  4.5 the mapping is a list, ``rear_ports: [{position, rear_port, rear_port_position}]`` (the rear
  port is a bare id there), and front ports gained ``positions``. Both are read; a front port mapped
  to more than one rear port keeps its first mapping and records a warning.

A ``session`` (anything with ``request(method, url, headers=, params=, timeout=, verify=)`` returning
an object with ``status_code``/``json()``/``text``, i.e. a ``requests.Session``) can be injected,
which is how the tests avoid the network. The token is never logged, and never appears in an
exception message or the loader's ``repr``.
"""

from __future__ import annotations

import logging
from typing import Any

from ..dcim_http import NETBOX, Session
from ..log import count
from .errors import DCIMNotFoundError, DCIMVersionError
from .rest import DCIMReader, RESTReader, unique
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
    RackType,
    Region,
    Site,
    Snapshot,
    Termination,
)
from .values import (
    convert as _convert,  # noqa: F401 - kept for callers of the old private name
    integer as _int,
    kg as _kg,
    metres as _metres,
    mm as _mm,
    name_of as _name,
    number as _number,
    parse_version,
    ref_id as _ref_id,
    tag_names as _tags,
    value as _value,
)

log = logging.getLogger(__name__)

MIN_VERSION = (4, 0)
MAX_TESTED_VERSION = (4, 6)
V2_TOKEN_PREFIX = "nbt_"


# Device component endpoints, and the cable termination type for each kind.
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
    ("rear-port", "rear-port-templates"),  # before front ports: 4.5+ front port mappings name them by id
    ("front-port", "front-port-templates"),
    ("power-port", "power-port-templates"),
    ("power-outlet", "power-outlet-templates"),
    ("console-port", "console-port-templates"),
]
# The order component templates are listed in on a device type.
_TEMPLATE_ORDER: list[ComponentKind] = [
    "interface",
    "front-port",
    "rear-port",
    "power-port",
    "power-outlet",
    "console-port",
]


def _first_mapping(port: dict) -> tuple[Any, int, int]:
    """A front port's (rear port, rear port position, number of mappings) in either API shape."""
    mappings = port.get("rear_ports")
    if isinstance(mappings, list):  # 4.5+: an explicit mapping list
        if not mappings:
            return None, 1, 0
        first = min(mappings, key=lambda m: _int(m.get("position"), 1))
        return first.get("rear_port"), _int(first.get("rear_port_position"), 1), len(mappings)
    rear = port.get("rear_port")  # up to 4.4: one rear port and a position
    return rear, _int(port.get("rear_port_position"), 1), 0 if rear is None else 1


# ---- the loader -------------------------------------------------------------------------------


class NetBoxReader(DCIMReader):
    """Turns NetBox's answers, in its REST API's shape, into a :class:`Snapshot`: everything one load
    asks for and how each answer maps onto the snapshot. It asks only through ``fetch`` and ``fetch_all``
    (``DCIMReader``): :class:`NetBoxLoader` answers them over HTTP, and a plugin may answer them from its own
    database in the same shape, so both read the same snapshot."""

    product = NETBOX

    # -- load ------------------------------------------------------------------------------------

    def check_version(self) -> str:
        """Read ``/api/status/`` and return NetBox's version; refuse anything below 4.0 or from 5.0."""
        status = self.fetch("/api/status/")
        text = str((status or {}).get("netbox-version") or "")
        major_minor = parse_version(text, "NetBox")[:2]
        if major_minor < MIN_VERSION:
            raise DCIMVersionError(f"NetBox {text} is not supported: railyard-sync needs NetBox 4.0 or later.")
        if major_minor[0] > MAX_TESTED_VERSION[0]:
            raise DCIMVersionError(
                f"NetBox {text} is not supported yet: railyard-sync is tested with NetBox 4.0 to "
                f"{MAX_TESTED_VERSION[0]}.{MAX_TESTED_VERSION[1]}."
            )
        self.version = text
        return text

    def load(self, sites: list[str] | None) -> Snapshot:
        """Read the given sites (each a slug, name or numeric id) and everything inside them.

        ``None`` reads every site the token can see."""
        if isinstance(sites, str):
            sites = [sites]
        if sites is not None and not sites:
            raise ValueError("at least one site is required")
        version = self.check_version()
        major_minor = parse_version(version, "NetBox")[:2]
        snap = Snapshot(source="netbox", source_url=self.url, source_version=version)
        if major_minor > MAX_TESTED_VERSION:
            snap.warnings.append(
                f"NetBox {version} is newer than railyard-sync has been tested with "
                f"({MAX_TESTED_VERSION[0]}.{MAX_TESTED_VERSION[1]}); check the import report carefully."
            )

        raw_sites = self._find_sites(sites) if sites is not None else self._all_sites()
        site_ids = [str(s["id"]) for s in raw_sites]
        slugs = [str(s.get("slug") or s.get("name") or s["id"]) for s in raw_sites]
        shown = ", ".join(slugs[:8]) + (f" and {len(slugs) - 8} more" if len(slugs) > 8 else "")
        log.info("Reading NetBox %s (%s): %s (%s)…", self.url, version, count(len(raw_sites), "site"), shown)
        by_site = [("site_id", i) for i in site_ids]

        snap.regions = self._load_regions(raw_sites)
        snap.sites = [self._site(s) for s in raw_sites]
        snap.locations = [self._location(x) for x in self.fetch_all("/api/dcim/locations/", by_site)]
        log.debug("Read %s and %s", count(len(snap.regions), "region"), count(len(snap.locations), "location"))

        raw_racks = list(self.fetch_all("/api/dcim/racks/", by_site))
        snap.racks = [self._rack(r) for r in raw_racks]
        rack_type_ids = unique(r.rack_type_id for r in snap.racks)
        if rack_type_ids and major_minor >= (4, 1):
            raw_types = self._list_by_ids("/api/dcim/rack-types/", "id", rack_type_ids)
            snap.rack_types = [self._rack_type(t) for t in raw_types]

        devices_params = by_site + [("exclude", "config_context")]
        snap.devices = [self._device(d) for d in self.fetch_all("/api/dcim/devices/", devices_params)]
        log.debug("Read %s and %s", count(len(snap.racks), "rack"), count(len(snap.devices), "device"))
        snap.device_types = self._load_device_types(unique(d.device_type_id for d in snap.devices), snap.warnings)
        log.debug("Read %s with their component templates", count(len(snap.device_types), "device type"))

        loaded_devices = {d.id for d in snap.devices}
        for kind, endpoint, _ in _COMPONENT_ENDPOINTS:
            before = len(snap.components)
            for item in self.fetch_all(f"/api/dcim/{endpoint}/", by_site):
                component = self._component(kind, item, snap.warnings)
                if component.device_id in loaded_devices:
                    snap.components.append(component)
            log.debug("Read %s", count(len(snap.components) - before, kind.replace("-", " ")))

        snap.power_panels = [self._power_panel(p) for p in self.fetch_all("/api/dcim/power-panels/", by_site)]
        snap.power_feeds = [self._power_feed(f) for f in self.fetch_all("/api/dcim/power-feeds/", by_site)]
        snap.cables = self._load_cables(by_site, snap)
        log.debug(
            "Read %s, %s and %s",
            count(len(snap.power_panels), "power panel"),
            count(len(snap.power_feeds), "power feed"),
            count(len(snap.cables), "cable"),
        )
        return snap

    # -- sites and regions -----------------------------------------------------------------------

    def _all_sites(self) -> list[dict]:
        """Every site the token can see, in NetBox's order."""
        sites = list(self.fetch_all("/api/dcim/sites/"))
        if not sites:
            raise DCIMNotFoundError("This NetBox has no sites the token can see.")
        return sites

    def _find_sites(self, refs: list[str]) -> list[dict]:
        found: dict[str, dict] = {}
        for ref in refs:
            needle = str(ref).strip()
            if not needle:
                raise ValueError("site reference must not be empty")
            lookups = [("id", needle)] if needle.isdigit() else []
            lookups += [("slug", needle), ("name", needle)]
            match = None
            for key, value in lookups:
                results = list(self.fetch_all("/api/dcim/sites/", [(key, value)]))
                if results:
                    match = results[0]
                    break
            if match is None:
                raise DCIMNotFoundError(f"No NetBox site matched {needle!r} (by id, slug or name).")
            found.setdefault(str(match["id"]), match)
        return list(found.values())

    def _load_regions(self, raw_sites: list[dict]) -> list[Region]:
        """The region chain above each site, outermost first, each region once."""
        regions: dict[str, Region] = {}
        for raw in raw_sites:
            chain: list[Region] = []
            region_id = _ref_id(raw.get("region"))
            while region_id is not None and region_id not in regions and all(r.id != region_id for r in chain):
                item = self.fetch(f"/api/dcim/regions/{region_id}/")
                region = Region(
                    id=str(item["id"]),
                    name=str(item.get("name") or ""),
                    slug=str(item.get("slug") or ""),
                    parent_id=_ref_id(item.get("parent")),
                )
                chain.append(region)
                region_id = region.parent_id
            for region in reversed(chain):
                regions[region.id] = region
        return list(regions.values())

    @staticmethod
    def _site(item: dict) -> Site:
        return Site(
            id=str(item["id"]),
            name=str(item.get("name") or ""),
            slug=str(item.get("slug") or ""),
            status=_value(item.get("status"), "active"),
            facility=str(item.get("facility") or ""),
            region_id=_ref_id(item.get("region")),
            description=str(item.get("description") or ""),
            comments=str(item.get("comments") or ""),
            tags=_tags(item),
        )

    @staticmethod
    def _location(item: dict) -> Location:
        return Location(
            id=str(item["id"]),
            name=str(item.get("name") or ""),
            slug=str(item.get("slug") or ""),
            site_id=_ref_id(item.get("site")) or "",
            parent_id=_ref_id(item.get("parent")),
            status=_value(item.get("status"), "active"),
            facility=str(item.get("facility") or ""),  # 4.3+
            description=str(item.get("description") or ""),
            tags=_tags(item),
        )

    # -- racks ---------------------------------------------------------------------------------

    @staticmethod
    def _rack_type(item: dict) -> RackType:
        return RackType(
            id=str(item["id"]),
            manufacturer=_name(item.get("manufacturer")),
            model=str(item.get("model") or ""),
            slug=str(item.get("slug") or ""),
            u_height=_int(item.get("u_height"), 42),
            width_in=_int(_value(item.get("width")), 19),
            outer_width_mm=_mm(item.get("outer_width"), item.get("outer_unit")),
            outer_depth_mm=_mm(item.get("outer_depth"), item.get("outer_unit")),
            form_factor=_value(item.get("form_factor")),
        )

    @staticmethod
    def _rack(item: dict) -> Rack:
        # 4.0 names the form factor "type"; 4.1 renamed it "form_factor".
        form_factor = item.get("form_factor") if "form_factor" in item else item.get("type")
        return Rack(
            id=str(item["id"]),
            name=str(item.get("name") or ""),
            site_id=_ref_id(item.get("site")) or "",
            location_id=_ref_id(item.get("location")),
            status=_value(item.get("status"), "active"),
            role=_name(item.get("role")),
            u_height=_int(item.get("u_height"), 42),
            starting_unit=_int(item.get("starting_unit"), 1),
            desc_units=bool(item.get("desc_units")),
            width_in=_int(_value(item.get("width")), 19),
            outer_width_mm=_mm(item.get("outer_width"), item.get("outer_unit")),
            outer_depth_mm=_mm(item.get("outer_depth"), item.get("outer_unit")),
            rack_type_id=_ref_id(item.get("rack_type")),
            form_factor=_value(form_factor),
            max_weight_kg=_kg(item.get("max_weight"), item.get("weight_unit")),
            serial=str(item.get("serial") or ""),
            asset_tag=str(item.get("asset_tag") or ""),
            facility_id=str(item.get("facility_id") or ""),
            comments=str(item.get("comments") or ""),
            tags=_tags(item),
            custom_fields=dict(item.get("custom_fields") or {}),
        )

    # -- device types --------------------------------------------------------------------------

    def _load_device_types(self, ids: list[str], warnings: list[str]) -> list[DeviceType]:
        types: dict[str, DeviceType] = {}
        for item in self._list_by_ids("/api/dcim/device-types/", "id", ids):
            u_height = _number(item.get("u_height"))  # a decimal: 0.5 U and 1.5 U types exist
            types[str(item["id"])] = DeviceType(
                id=str(item["id"]),
                manufacturer=_name(item.get("manufacturer")),
                model=str(item.get("model") or ""),
                slug=str(item.get("slug") or ""),
                u_height=1 if u_height is None else u_height,
                is_full_depth=bool(item.get("is_full_depth", True)),
                part_number=str(item.get("part_number") or ""),
                weight_kg=_kg(item.get("weight"), item.get("weight_unit")),
                airflow=_value(item.get("airflow")),
                subdevice_role=_value(item.get("subdevice_role")),
            )
        missing = [i for i in ids if i not in types]
        if missing:
            warnings.append(
                f"NetBox did not return device type(s) {', '.join(missing)} used by the loaded devices; "
                "does the token lack permission to read device types?"
            )
        if not types:
            return []

        templates: dict[str, dict[ComponentKind, list[ComponentTemplate]]] = {
            dt: {kind: [] for kind in _TEMPLATE_ORDER} for dt in types
        }
        rear_names: dict[str, str] = {}  # rear port template id -> name (for 4.5+ mappings)
        for kind, endpoint in _TEMPLATE_ENDPOINTS:
            for item in self._list_by_ids(f"/api/dcim/{endpoint}/", "device_type_id", list(types)):
                dt_id = _ref_id(item.get("device_type"))
                if dt_id not in templates:  # module type templates, or a filter NetBox ignored
                    continue
                template = self._template(kind, item, rear_names, types[dt_id], warnings)
                if kind == "rear-port":
                    rear_names[str(item["id"])] = template.name
                templates[dt_id][kind].append(template)
        for dt_id, device_type in types.items():
            device_type.components = [t for kind in _TEMPLATE_ORDER for t in templates[dt_id][kind]]
        return [types[i] for i in ids if i in types]

    @staticmethod
    def _template(
        kind: ComponentKind, item: dict, rear_names: dict[str, str], device_type: DeviceType, warnings: list[str]
    ) -> ComponentTemplate:
        template = ComponentTemplate(name=str(item.get("name") or ""), kind=kind, type=_value(item.get("type")))
        if kind == "interface":
            template.mgmt_only = bool(item.get("mgmt_only"))
        elif kind == "rear-port":
            template.positions = _int(item.get("positions"), 1)
        elif kind == "front-port":
            template.positions = _int(item.get("positions"), 1)
            rear, position, count = _first_mapping(item)
            if isinstance(rear, dict):  # up to 4.4: a nested rear port template
                template.rear_port_name = str(rear.get("name") or "")
            elif rear is not None:  # 4.5+: the rear port template's id
                template.rear_port_name = rear_names.get(str(rear), "")
            template.rear_port_position = position
            if count > 1:
                warnings.append(
                    f"Device type {device_type.slug or device_type.id}: front port template {template.name!r} maps "
                    f"to {count} rear port positions; only the first is kept."
                )
        elif kind == "power-port":
            template.maximum_draw_w = _number(item.get("maximum_draw"))
            template.allocated_draw_w = _number(item.get("allocated_draw"))
        elif kind == "power-outlet":
            template.feed_leg = _value(item.get("feed_leg"))
        return template

    # -- devices and components ----------------------------------------------------------------

    @staticmethod
    def _device(item: dict) -> Device:
        return Device(
            id=str(item["id"]),
            name=str(item.get("name") or ""),
            device_type_id=_ref_id(item.get("device_type")) or "",
            site_id=_ref_id(item.get("site")) or "",
            role=_name(item.get("role") or item.get("device_role")),
            location_id=_ref_id(item.get("location")),
            rack_id=_ref_id(item.get("rack")),
            position=_number(item.get("position")),
            face=_value(item.get("face")),
            status=_value(item.get("status"), "active"),
            serial=str(item.get("serial") or ""),
            asset_tag=str(item.get("asset_tag") or ""),
            airflow=_value(item.get("airflow")),
            parent_device_id=_ref_id(item.get("parent_device")),
            platform=_name(item.get("platform")),
            tenant=_name(item.get("tenant")),
            comments=str(item.get("comments") or ""),
            tags=_tags(item),
            custom_fields=dict(item.get("custom_fields") or {}),
        )

    @staticmethod
    def _component(kind: ComponentKind, item: dict, warnings: list[str]) -> Component:
        module = item.get("module")
        module_name = ""
        if isinstance(module, dict):
            module_name = _name(module.get("module_bay")) or str(module.get("display") or module.get("id") or "")
        component = Component(
            id=str(item["id"]),
            device_id=_ref_id(item.get("device")) or "",
            name=str(item.get("name") or ""),
            kind=kind,
            type=_value(item.get("type")),
            label=str(item.get("label") or ""),
            description=str(item.get("description") or ""),
            module=module_name,
        )
        if kind == "interface":
            component.mgmt_only = bool(item.get("mgmt_only"))
            component.enabled = bool(item.get("enabled", True))
        elif kind == "rear-port":
            component.positions = _int(item.get("positions"), 1)
        elif kind == "front-port":
            component.positions = _int(item.get("positions"), 1)
            rear, position, count = _first_mapping(item)
            component.rear_port_id = _ref_id(rear)
            component.rear_port_position = position
            if count > 1:
                warnings.append(
                    f"Front port {component.id} ({component.name!r} on device {component.device_id}) maps to "
                    f"{count} rear port positions; only the first is kept."
                )
        elif kind == "power-port":
            component.maximum_draw_w = _number(item.get("maximum_draw"))
            component.allocated_draw_w = _number(item.get("allocated_draw"))
        elif kind == "power-outlet":
            component.power_port_id = _ref_id(item.get("power_port"))
            component.feed_leg = _value(item.get("feed_leg"))
        return component

    # -- power -----------------------------------------------------------------------------------

    @staticmethod
    def _power_panel(item: dict) -> PowerPanel:
        return PowerPanel(
            id=str(item["id"]),
            name=str(item.get("name") or ""),
            site_id=_ref_id(item.get("site")) or "",
            location_id=_ref_id(item.get("location")),
        )

    @staticmethod
    def _power_feed(item: dict) -> PowerFeed:
        feed = PowerFeed(
            id=str(item["id"]),
            name=str(item.get("name") or ""),
            power_panel_id=_ref_id(item.get("power_panel")) or "",
            rack_id=_ref_id(item.get("rack")),
        )
        feed.status = _value(item.get("status"), feed.status)
        feed.type = _value(item.get("type"), feed.type)
        feed.supply = _value(item.get("supply"), feed.supply)
        feed.phase = _value(item.get("phase"), feed.phase)
        for attr in ("voltage", "amperage", "max_utilization"):
            number = _number(item.get(attr))
            if number is not None:
                setattr(feed, attr, number)
        return feed

    # -- cables --------------------------------------------------------------------------------

    def _load_cables(self, by_site: list[tuple[str, Any]], snap: Snapshot) -> list[Cable]:
        loaded = {(otype, c.id) for c in snap.components for kind, _, otype in _COMPONENT_ENDPOINTS if kind == c.kind}
        loaded |= {("dcim.powerfeed", f.id) for f in snap.power_feeds}
        devices = {d.id for d in snap.devices}
        cables: dict[str, Cable] = {}
        for item in self.fetch_all("/api/dcim/cables/", by_site):
            cable_id = str(item["id"])
            if cable_id in cables:
                continue
            cable = Cable(
                id=cable_id,
                a=[self._termination(t) for t in item.get("a_terminations") or []],
                b=[self._termination(t) for t in item.get("b_terminations") or []],
                type=_value(item.get("type")),
                status=_value(item.get("status"), "connected"),
                label=str(item.get("label") or ""),
                color=str(item.get("color") or "").lstrip("#").lower(),
                length_m=_metres(item.get("length"), item.get("length_unit")),
                description=str(item.get("description") or ""),
                tags=_tags(item),
            )
            cables[cable_id] = cable
            for side, raw_ends in (("A", item.get("a_terminations") or []), ("B", item.get("b_terminations") or [])):
                for raw in raw_ends:
                    end = self._termination(raw)
                    if self._outside(end, raw, loaded, devices):
                        label = f" ({cable.label})" if cable.label else ""
                        snap.warnings.append(
                            f"Cable {cable_id}{label}: its {side} end ({end.object_type} {end.object_id}) is outside "
                            "the loaded sites; the cable is kept for the importer to decide."
                        )
        return list(cables.values())

    @staticmethod
    def _termination(raw: dict) -> Termination:
        return Termination(object_type=str(raw.get("object_type") or ""), object_id=str(raw.get("object_id") or ""))

    @staticmethod
    def _outside(end: Termination, raw: dict, loaded: set[tuple[str, str]], devices: set[str]) -> bool:
        """Whether a DCIM termination belongs to something outside the loaded sites.

        Circuit terminations and other non-DCIM objects are left to the importer, which skips and
        reports them. A DCIM object counts as inside when it was loaded, or when it sits on a loaded
        device (a console server port, say, which the snapshot does not carry).
        """
        if not end.object_type.startswith("dcim.") or (end.object_type, end.object_id) in loaded:
            return False
        obj = raw.get("object")
        device_id = _ref_id(obj.get("device")) if isinstance(obj, dict) else None
        return device_id is None or device_id not in devices


class NetBoxLoader(RESTReader, NetBoxReader):
    """Reads NetBox sites into a :class:`Snapshot`. One instance can serve several loads."""

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

    def auth_header(self) -> str:
        return f"Bearer {self._token}" if self._token.startswith(V2_TOKEN_PREFIX) else f"Token {self._token}"


def load_netbox_snapshot(
    url: str,
    token: str,
    sites: list[str] | None,
    *,
    session: Session | None = None,
    verify: bool | str = True,
) -> Snapshot:
    """Read ``sites`` (slugs, names or numeric ids; ``None`` for every site) from the NetBox at ``url``."""
    return NetBoxLoader(url, token, session=session, verify=verify).load(sites)
