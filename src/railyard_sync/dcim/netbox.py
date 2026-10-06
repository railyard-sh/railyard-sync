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
import re
from collections.abc import Iterable, Iterator
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlsplit

from .errors import DCIMAuthError, DCIMConnectionError, DCIMError, DCIMNotFoundError, DCIMVersionError
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

log = logging.getLogger(__name__)

MIN_VERSION = (4, 0)
MAX_TESTED_VERSION = (4, 6)
V2_TOKEN_PREFIX = "nbt_"
# Ids per request when filtering by a list of ids (``id=`` / ``device_type_id=``), keeping URLs short.
ID_CHUNK = 100

_MM_PER = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "ft": 304.8}
_KG_PER = {"kg": 1.0, "g": 0.001, "lb": 0.45359237, "oz": 0.028349523125}
_M_PER = {"km": 1000.0, "m": 1.0, "cm": 0.01, "mi": 1609.344, "ft": 0.3048, "in": 0.0254}

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


class _Response(Protocol):  # the subset of requests.Response we rely on
    status_code: int

    @property
    def text(self) -> str: ...

    def json(self) -> Any: ...


class _Session(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> _Response: ...


def _default_session() -> _Session:
    import requests  # imported lazily so the module imports without requests when a session is injected

    return requests.Session()


def _connection_errors() -> tuple[type[BaseException], ...]:
    try:
        import requests
    except ImportError:  # pragma: no cover - requests is a dependency
        return (OSError,)
    return (requests.exceptions.RequestException, OSError)


# ---- small readers for NetBox's JSON shapes ---------------------------------------------------


def _value(field: Any, default: str = "") -> str:
    """An enumeration's slug: NetBox returns choice fields as ``{"value", "label"}``."""
    if isinstance(field, dict):
        field = field.get("value")
    if field is None:
        return default
    return str(field)


def _ref_id(field: Any) -> str | None:
    """The id of a nested object (``{"id": 7, …}``) or a bare id, as a string."""
    if isinstance(field, dict):
        field = field.get("id")
    if field is None or field == "":
        return None
    return str(field)


def _name(field: Any) -> str:
    """The name of a nested object (role, tenant, platform, manufacturer…), or ""."""
    if isinstance(field, dict):
        return str(field.get("name") or field.get("display") or "")
    return "" if field is None else str(field)


def _tags(item: dict) -> list[str]:
    return [str(t.get("name") if isinstance(t, dict) else t) for t in item.get("tags") or []]


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any, default: int) -> int:
    number = _number(value)
    return default if number is None else int(number)


def _convert(value: Any, unit: Any, table: dict[str, float], default_unit: str) -> float | None:
    number = _number(value)
    if number is None:
        return None
    factor = table.get(_value(unit, default_unit).lower())
    if factor is None:
        raise DCIMError(f"Unknown unit {_value(unit)!r} (expected one of {', '.join(table)}).")
    return round(number * factor, 6)


def _mm(value: Any, unit: Any) -> float | None:
    return _convert(value, unit, _MM_PER, "mm")


def _kg(value: Any, unit: Any) -> float | None:
    return _convert(value, unit, _KG_PER, "kg")


def _metres(value: Any, unit: Any) -> float | None:
    return _convert(value, unit, _M_PER, "m")


def _parse_version(text: str) -> tuple[int, int, int]:
    match = re.match(r"\s*v?(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not match:
        raise DCIMVersionError(f"Could not read the NetBox version from /api/status/ ({text!r}).")
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def _chunks(items: list[str], size: int | None = None) -> Iterator[list[str]]:
    size = size or ID_CHUNK
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _unique(items: Iterable[str | None]) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        if item is not None:
            seen.setdefault(item, None)
    return list(seen)


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


class NetBoxLoader:
    """Reads NetBox sites into a :class:`Snapshot`. One instance can serve several loads."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        session: _Session | None = None,
        verify: bool | str = True,
        timeout: float = 30,
        page_size: int = 1000,
    ) -> None:
        if not url:
            raise ValueError("NetBox URL is required")
        parts = urlsplit(url.strip())
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("NetBox URL must be an http:// or https:// URL")
        if parts.username or parts.password:
            raise ValueError("NetBox URL must not contain credentials; pass the token separately")
        if not token:
            raise ValueError("NetBox API token is required")
        if page_size < 1:
            raise ValueError("page_size must be at least 1")
        base = url.strip().rstrip("/")
        if base.endswith("/api"):  # a common slip: the API root rather than NetBox's own URL
            base = base[: -len("/api")]
        self.url = base
        self._token = token
        self._auth = f"Bearer {token}" if token.startswith(V2_TOKEN_PREFIX) else f"Token {token}"
        self._session = session or _default_session()
        self._verify = verify
        self._timeout = timeout
        self._page_size = page_size
        self.version = ""

    def __repr__(self) -> str:
        return f"NetBoxLoader(url={self.url!r})"

    # -- HTTP ------------------------------------------------------------------------------------

    def _scrub(self, text: str) -> str:
        """Remove the token from text that may echo it: the whole token, and a v2 token's secret half."""
        for secret in (self._token, self._token.partition(".")[2]):
            if len(secret) >= 8:
                text = text.replace(secret, "***")
        return text

    def _get(self, path: str, params: list[tuple[str, Any]] | None = None) -> Any:
        url = f"{self.url}{path}"
        headers = {"Authorization": self._auth, "Accept": "application/json"}
        log.debug("GET %s %s", path, params or "")
        try:
            resp = self._session.request(
                "GET", url, headers=headers, params=params, timeout=self._timeout, verify=self._verify
            )
        except _connection_errors() as exc:
            raise DCIMConnectionError(
                self._scrub(f"Could not reach NetBox at {self.url}: {type(exc).__name__}: {exc}")
            ) from None
        status = resp.status_code
        if status in (401, 403):
            raise DCIMAuthError(
                f"NetBox refused the API token (HTTP {status}) for {path}: it is wrong, expired, or lacks "
                "read permission. A read-only token is enough.",
                status=status,
            )
        if status == 404:
            raise DCIMNotFoundError(f"Not found in NetBox (HTTP 404): {path}", status=status)
        if status < 200 or status >= 300:
            body = ""
            try:
                body = self._scrub(resp.text[:400])
            except Exception:  # pragma: no cover - defensive
                pass
            raise DCIMError(f"NetBox API error (HTTP {status}) for {path}: {body}", status=status)
        try:
            return resp.json()
        except ValueError:
            raise DCIMError(
                f"NetBox returned a response that is not JSON for {path}: is {self.url} a NetBox server?",
                status=status,
            ) from None

    def _list(self, path: str, params: list[tuple[str, Any]] | None = None) -> Iterator[dict]:
        """Every object a list endpoint returns, following ``next`` page by page.

        Only the query of ``next`` is used; requests always go to the configured URL. NetBox behind
        a proxy often builds ``next`` with the wrong scheme or host, and the token must never be sent
        anywhere else.
        """
        query: list[tuple[str, Any]] = list(params or []) + [("limit", self._page_size), ("offset", 0)]
        while True:
            page = self._get(path, query)
            if not isinstance(page, dict) or "results" not in page:
                raise DCIMError(f"NetBox returned an unexpected response for {path} (no 'results').")
            yield from page["results"]
            nxt = page.get("next")
            if not nxt or not page["results"]:
                return
            query = parse_qsl(urlsplit(nxt).query, keep_blank_values=True)

    def _list_by_ids(self, path: str, key: str, ids: list[str]) -> Iterator[dict]:
        for chunk in _chunks(ids):
            yield from self._list(path, [(key, i) for i in chunk])

    # -- load ------------------------------------------------------------------------------------

    def check_version(self) -> str:
        """Read ``/api/status/`` and return NetBox's version; refuse anything below 4.0 or from 5.0."""
        status = self._get("/api/status/")
        text = str((status or {}).get("netbox-version") or "")
        major_minor = _parse_version(text)[:2]
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
        major_minor = _parse_version(version)[:2]
        snap = Snapshot(source="netbox", source_url=self.url, source_version=version)
        if major_minor > MAX_TESTED_VERSION:
            snap.warnings.append(
                f"NetBox {version} is newer than railyard-sync has been tested with "
                f"({MAX_TESTED_VERSION[0]}.{MAX_TESTED_VERSION[1]}); check the import report carefully."
            )

        raw_sites = self._find_sites(sites) if sites is not None else self._all_sites()
        site_ids = [str(s["id"]) for s in raw_sites]
        by_site = [("site_id", i) for i in site_ids]

        snap.regions = self._load_regions(raw_sites)
        snap.sites = [self._site(s) for s in raw_sites]
        snap.locations = [self._location(x) for x in self._list("/api/dcim/locations/", by_site)]

        raw_racks = list(self._list("/api/dcim/racks/", by_site))
        snap.racks = [self._rack(r) for r in raw_racks]
        rack_type_ids = _unique(r.rack_type_id for r in snap.racks)
        if rack_type_ids and major_minor >= (4, 1):
            raw_types = self._list_by_ids("/api/dcim/rack-types/", "id", rack_type_ids)
            snap.rack_types = [self._rack_type(t) for t in raw_types]

        devices_params = by_site + [("exclude", "config_context")]
        snap.devices = [self._device(d) for d in self._list("/api/dcim/devices/", devices_params)]
        snap.device_types = self._load_device_types(_unique(d.device_type_id for d in snap.devices), snap.warnings)

        loaded_devices = {d.id for d in snap.devices}
        for kind, endpoint, _ in _COMPONENT_ENDPOINTS:
            for item in self._list(f"/api/dcim/{endpoint}/", by_site):
                component = self._component(kind, item, snap.warnings)
                if component.device_id in loaded_devices:
                    snap.components.append(component)

        snap.power_panels = [self._power_panel(p) for p in self._list("/api/dcim/power-panels/", by_site)]
        snap.power_feeds = [self._power_feed(f) for f in self._list("/api/dcim/power-feeds/", by_site)]
        snap.cables = self._load_cables(by_site, snap)
        return snap

    # -- sites and regions -----------------------------------------------------------------------

    def _all_sites(self) -> list[dict]:
        """Every site the token can see, in NetBox's order."""
        sites = list(self._list("/api/dcim/sites/"))
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
                results = list(self._list("/api/dcim/sites/", [(key, value)]))
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
                item = self._get(f"/api/dcim/regions/{region_id}/")
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
        for item in self._list("/api/dcim/cables/", by_site):
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


def load_netbox_snapshot(
    url: str,
    token: str,
    sites: list[str] | None,
    *,
    session: _Session | None = None,
    verify: bool | str = True,
) -> Snapshot:
    """Read ``sites`` (slugs, names or numeric ids; ``None`` for every site) from the NetBox at ``url``."""
    return NetBoxLoader(url, token, session=session, verify=verify).load(sites)
