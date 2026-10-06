"""Canonical DiffSync models — the shared diff surface.

These mirror Railyard's Go export plan one-for-one (``export/netbox.go`` objects +
``export/cabling.go`` component/cable plan), so a sync produces the same objects Railyard's own
NetBox CSV export would. Each DCIM target (NetBox now, Nautobot later) subclasses these and adds
``create``/``update``/``delete`` — the ``_identifiers``/``_attributes`` stay identical so the diff
lines up across both.

``TOP_LEVEL`` encodes create-ordering: prerequisites (manufacturers, device types, roles, sites)
before the things that reference them (racks, devices), and ports before the cables that terminate on
them (a front port must exist before its cable; a front port references its rear port, so rear ports
come first).
"""

from __future__ import annotations

from diffsync import DiffSyncModel
from pydantic import Field


class Manufacturer(DiffSyncModel):
    _modelname = "manufacturer"
    _identifiers = ("name",)
    _attributes = ("slug",)
    name: str
    slug: str


class DeviceType(DiffSyncModel):
    _modelname = "device_type"
    _identifiers = ("manufacturer", "model")
    _attributes = ("slug", "u_height", "is_full_depth", "part_number")
    manufacturer: str
    model: str
    slug: str
    u_height: int = 1
    is_full_depth: bool = True
    part_number: str = ""
    # Non-diffed resolution metadata: the netbox-community devicetype-library slug (Railyard `key`)
    # and, when fetched, the full component-template spec for an optional future full-import mode.
    library_slug: str = ""
    components: dict = Field(default_factory=dict)


class DeviceRole(DiffSyncModel):
    _modelname = "device_role"
    _identifiers = ("name",)
    _attributes = ("slug", "color")
    name: str
    slug: str
    color: str = "9e9e9e"


class Site(DiffSyncModel):
    _modelname = "site"
    _identifiers = ("name",)
    _attributes = ("slug", "status")
    name: str
    slug: str
    status: str = "active"


class Rack(DiffSyncModel):
    _modelname = "rack"
    _identifiers = ("site", "name")
    _attributes = ("status", "width", "u_height", "desc_units")
    site: str
    name: str
    status: str = "active"
    width: int = 19
    u_height: int = 42
    desc_units: bool = False


class Device(DiffSyncModel):
    _modelname = "device"
    _identifiers = ("name",)
    _attributes = (
        "device_type",
        "manufacturer",
        "role",
        "site",
        "rack",
        "position",
        "face",
        "status",
        "railyard_id",
    )
    name: str
    device_type: str  # the device-type *model* (its natural key on a device)
    manufacturer: str
    role: str
    site: str
    rack: str | None = None
    position: int | None = None  # None for a 0U side-mount
    face: str = "front"
    status: str = "active"
    railyard_id: str = ""  # Railyard placement id, stamped as a custom field for stable identity


class Interface(DiffSyncModel):
    _modelname = "interface"
    _identifiers = ("device", "name")
    _attributes = ("type",)
    device: str
    name: str
    type: str = "other"


class RearPort(DiffSyncModel):
    _modelname = "rear_port"
    _identifiers = ("device", "name")
    _attributes = ("type", "positions")
    device: str
    name: str
    type: str = "8p8c"
    positions: int = 1


class FrontPort(DiffSyncModel):
    _modelname = "front_port"
    _identifiers = ("device", "name")
    _attributes = ("type", "rear_port", "rear_port_position")
    device: str
    name: str
    type: str = "8p8c"
    rear_port: str = ""
    rear_port_position: int = 1


class PowerOutlet(DiffSyncModel):
    _modelname = "power_outlet"
    _identifiers = ("device", "name")
    _attributes = ("type",)
    device: str
    name: str
    type: str = "iec-60320-c13"


class PowerPort(DiffSyncModel):
    _modelname = "power_port"
    _identifiers = ("device", "name")
    _attributes = ("type",)
    device: str
    name: str
    type: str = "iec-60320-c14"


class Cable(DiffSyncModel):
    _modelname = "cable"
    # A cable's identity is its two terminations (device + component + name), in the order the
    # planner emits them. ``a_type``/``b_type`` are DCIM content types (dcim.interface, dcim.frontport,
    # dcim.rearport, dcim.powerport, dcim.poweroutlet).
    _identifiers = ("a_device", "a_type", "a_name", "b_device", "b_type", "b_name")
    _attributes = ("is_power", "label")
    a_device: str
    a_type: str
    a_name: str
    b_device: str
    b_type: str
    b_name: str
    is_power: bool = False
    label: str = ""


# Create-dependency order for the adapters' ``top_level``.
TOP_LEVEL = [
    "manufacturer",
    "device_type",
    "device_role",
    "site",
    "rack",
    "device",
    "rear_port",
    "front_port",
    "interface",
    "power_outlet",
    "power_port",
    "cable",
]

__all__ = [
    "Manufacturer",
    "DeviceType",
    "DeviceRole",
    "Site",
    "Rack",
    "Device",
    "Interface",
    "RearPort",
    "FrontPort",
    "PowerOutlet",
    "PowerPort",
    "Cable",
    "TOP_LEVEL",
]
