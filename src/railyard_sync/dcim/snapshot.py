"""A DCIM site as a source-neutral snapshot.

This is the contract between the loaders and the importer. A loader (``railyard_sync.dcim.netbox``
reading NetBox's REST API, a future Nautobot loader, or a plugin reading its own ORM) fills a
:class:`Snapshot`; :func:`railyard_sync.importer.build_project` turns it into a Railyard project.
Nothing here knows about Railyard's model, and nothing in the importer knows which DCIM the data came
from.

Conventions:

- ``id`` is the source object's primary key as a string (NetBox and Nautobot use ints and UUIDs).
  Railyard identities are derived from it, so a re-import finds the same objects again.
- Lengths are millimetres, masses kilograms, power watts, regardless of the source's units.
- ``status``, ``type`` and similar enumerations keep the source's slug (``active``, ``cat6a``,
  ``iec-60320-c14``) so nothing is lost before the importer maps it.
- ``tags`` are tag names. ``custom_fields`` keeps the source's custom field data verbatim.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

ComponentKind = Literal["interface", "front-port", "rear-port", "power-port", "power-outlet", "console-port", "other"]


@dataclass
class Region:
    id: str
    name: str
    slug: str
    parent_id: str | None = None


@dataclass
class Site:
    id: str
    name: str
    slug: str
    status: str = "active"
    facility: str = ""
    region_id: str | None = None
    description: str = ""
    comments: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class Location:
    """A NetBox location / Nautobot location below the site (building, floor, room, cage…)."""

    id: str
    name: str
    slug: str
    site_id: str
    parent_id: str | None = None
    status: str = "active"
    facility: str = ""
    location_type: str = ""  # Nautobot's location type name; NetBox has none
    description: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class RackType:
    id: str
    manufacturer: str
    model: str
    slug: str
    u_height: int = 42
    width_in: int = 19
    outer_width_mm: float | None = None
    outer_depth_mm: float | None = None
    form_factor: str = ""


@dataclass
class Rack:
    id: str
    name: str
    site_id: str
    location_id: str | None = None
    status: str = "active"
    role: str = ""
    u_height: int = 42
    starting_unit: int = 1
    desc_units: bool = False
    width_in: int = 19  # rail width
    outer_width_mm: float | None = None
    outer_depth_mm: float | None = None
    rack_type_id: str | None = None
    form_factor: str = ""
    max_weight_kg: float | None = None
    serial: str = ""
    asset_tag: str = ""
    facility_id: str = ""
    comments: str = ""
    tags: list[str] = field(default_factory=list)
    custom_fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class ComponentTemplate:
    """One component on a device type: an interface, port, power port or outlet template."""

    name: str
    kind: ComponentKind
    type: str = ""  # the source's type slug, e.g. "10gbase-x-sfpp", "lc", "iec-60320-c14"
    positions: int = 1  # rear ports
    rear_port_name: str = ""  # front ports: the rear port this maps to
    rear_port_position: int = 1
    mgmt_only: bool = False
    maximum_draw_w: float | None = None  # power ports
    allocated_draw_w: float | None = None
    feed_leg: str = ""  # power outlets


@dataclass
class DeviceType:
    id: str
    manufacturer: str
    model: str
    slug: str
    u_height: float = 1
    is_full_depth: bool = True
    part_number: str = ""
    weight_kg: float | None = None
    airflow: str = ""
    subdevice_role: str = ""  # "parent" / "child" / ""
    components: list[ComponentTemplate] = field(default_factory=list)


@dataclass
class Device:
    id: str
    name: str
    device_type_id: str
    site_id: str
    role: str = ""
    location_id: str | None = None
    rack_id: str | None = None
    position: float | None = None  # lowest U occupied; None when not racked (0U or unplaced)
    face: str = ""  # "front" / "rear" / ""
    status: str = "active"
    serial: str = ""
    asset_tag: str = ""
    airflow: str = ""
    parent_device_id: str | None = None  # installed in another device's device bay
    platform: str = ""
    tenant: str = ""
    comments: str = ""
    tags: list[str] = field(default_factory=list)
    custom_fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class Component:
    """A component on a device: what a cable terminates on."""

    id: str
    device_id: str
    name: str
    kind: ComponentKind
    type: str = ""
    label: str = ""
    description: str = ""
    mgmt_only: bool = False
    enabled: bool = True
    module: str = ""  # the module bay/module it sits in, when any
    positions: int = 1  # rear ports
    rear_port_id: str | None = None  # front ports
    rear_port_position: int = 1
    maximum_draw_w: float | None = None  # power ports
    allocated_draw_w: float | None = None
    power_port_id: str | None = None  # power outlets: the PDU inlet that feeds it
    feed_leg: str = ""


@dataclass
class Termination:
    """One end of a cable: a component on a device, or something the importer cannot model."""

    object_type: str  # "dcim.interface", "dcim.frontport", "dcim.powerfeed", "circuits.circuittermination"…
    object_id: str


@dataclass
class Cable:
    id: str
    a: list[Termination]
    b: list[Termination]
    type: str = ""
    status: str = "connected"
    label: str = ""
    color: str = ""  # hex without "#", as NetBox stores it
    length_m: float | None = None
    description: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class PowerPanel:
    id: str
    name: str
    site_id: str
    location_id: str | None = None


@dataclass
class PowerFeed:
    id: str
    name: str
    power_panel_id: str
    rack_id: str | None = None
    status: str = "active"
    type: str = "primary"  # "primary" / "redundant"
    supply: str = "ac"  # "ac" / "dc"
    phase: str = "single-phase"  # "single-phase" / "three-phase"
    voltage: float = 230
    amperage: float = 16
    max_utilization: float = 80  # percent

    @property
    def available_power_w(self) -> float:
        """Usable power as NetBox computes it: volts x amps (x sqrt 3 for three-phase) x max utilisation."""
        watts = self.voltage * self.amperage * (3**0.5 if self.phase == "three-phase" else 1)
        return watts * self.max_utilization / 100


@dataclass
class Snapshot:
    """Everything one import reads from a DCIM: one or more sites and everything inside them."""

    source: str  # "netbox" / "nautobot"
    source_url: str = ""
    source_version: str = ""
    regions: list[Region] = field(default_factory=list)
    sites: list[Site] = field(default_factory=list)
    locations: list[Location] = field(default_factory=list)
    rack_types: list[RackType] = field(default_factory=list)
    racks: list[Rack] = field(default_factory=list)
    device_types: list[DeviceType] = field(default_factory=list)
    devices: list[Device] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)
    cables: list[Cable] = field(default_factory=list)
    power_panels: list[PowerPanel] = field(default_factory=list)
    power_feeds: list[PowerFeed] = field(default_factory=list)
    # A cable's far end may be outside the imported sites; loaders record what they could resolve
    # so the importer reports it rather than guessing.
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Snapshot:
        """Rebuild a snapshot from :meth:`to_dict` output (the ``--snapshot-out`` file)."""

        def many(kind, key):
            return [kind(**item) for item in data.get(key, [])]

        device_types = []
        for item in data.get("device_types", []):
            item = dict(item)
            item["components"] = [ComponentTemplate(**c) for c in item.get("components", [])]
            device_types.append(DeviceType(**item))
        cables = []
        for item in data.get("cables", []):
            item = dict(item)
            item["a"] = [Termination(**t) for t in item.get("a", [])]
            item["b"] = [Termination(**t) for t in item.get("b", [])]
            cables.append(Cable(**item))
        return cls(
            source=data["source"],
            source_url=data.get("source_url", ""),
            source_version=data.get("source_version", ""),
            regions=many(Region, "regions"),
            sites=many(Site, "sites"),
            locations=many(Location, "locations"),
            rack_types=many(RackType, "rack_types"),
            racks=many(Rack, "racks"),
            device_types=device_types,
            devices=many(Device, "devices"),
            components=many(Component, "components"),
            cables=cables,
            power_panels=many(PowerPanel, "power_panels"),
            power_feeds=many(PowerFeed, "power_feeds"),
            warnings=list(data.get("warnings", [])),
        )
