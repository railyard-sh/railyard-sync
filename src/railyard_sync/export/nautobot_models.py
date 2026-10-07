"""Canonical DiffSync models for Nautobot 2.x — the diff surface of a Railyard → Nautobot export.

They mirror the rows of Railyard's Nautobot bundle (``backend/internal/export/nautobot.go``), which the
``nautobot-sync`` document carries, one model per bundle file. Where NetBox's shape fits (rear and front ports,
power ports and outlets, cables) the NetBox models in :mod:`.models` are used as they are; the rest differ:

- **Locations instead of sites.** Nautobot has one location tree, each location typed by a location type.
  Railyard's export makes location names unique across the bundle (Nautobot looks them up by name), so a
  location's identity is its name, and its parent and type are attributes.
- **Statuses and roles are objects**, each enabled for the content types (``dcim.rack``, ``dcim.device``…) that
  may use it, and so are location types. The bundle lists only the custom statuses; Nautobot's built-in ones
  (Active, Planned…) are used as they are.
- **No slugs** (Nautobot 2 dropped them), so manufacturers and device types are keyed by their names.
- A rack is identified by its location and name, as Railyard's export names racks uniquely per space.

Targets subclass these and add ``create``/``update``/``delete`` (``nautobot_rest.py``); ``TOP_LEVEL`` is the
create order: what a row looks up by name before the rows that look it up.
"""

from __future__ import annotations

from diffsync import DiffSyncModel
from pydantic import Field

from .models import Cable, FrontPort, PowerOutlet, PowerPort, RearPort


class LocationType(DiffSyncModel):
    _modelname = "location_type"
    _identifiers = ("name",)
    _attributes = ("parent", "content_types", "nestable")
    name: str
    parent: str = ""  # the parent location type's name; blank at the top
    content_types: list[str] = Field(default_factory=list)  # sorted
    nestable: bool = False


class Status(DiffSyncModel):
    """A custom status the design uses (Nautobot's built-in statuses are not in the document)."""

    _modelname = "status"
    _identifiers = ("name",)
    _attributes = ("content_types",)
    name: str
    content_types: list[str] = Field(default_factory=list)  # sorted


class Manufacturer(DiffSyncModel):
    _modelname = "manufacturer"
    _identifiers = ("name",)
    _attributes = ()
    name: str


class DeviceType(DiffSyncModel):
    _modelname = "device_type"
    _identifiers = ("manufacturer", "model")
    _attributes = ("u_height", "is_full_depth")
    manufacturer: str
    model: str
    u_height: int = 1
    is_full_depth: bool = True
    # Not diffed: the Railyard catalogue key the type was exported for (the devicetype-library slug).
    library_slug: str = ""


class Role(DiffSyncModel):
    _modelname = "role"
    _identifiers = ("name",)
    _attributes = ("content_types",)
    name: str
    content_types: list[str] = Field(default_factory=list)  # sorted


class Location(DiffSyncModel):
    _modelname = "location"
    _identifiers = ("name",)
    _attributes = ("location_type", "parent", "status", "facility")
    name: str
    location_type: str
    parent: str = ""  # the parent location's name; blank at the top of the tree
    status: str = "Active"
    facility: str = ""


class Rack(DiffSyncModel):
    _modelname = "rack"
    _identifiers = ("location", "name")
    _attributes = ("status", "role", "type", "width", "u_height", "desc_units", "comments", "tags")
    location: str
    name: str
    status: str = "Active"
    role: str = ""
    type: str = ""  # the rack's form factor ("4-post-cabinet"); blank when unset
    width: int = 19
    u_height: int = 42
    desc_units: bool = False
    comments: str = ""
    tags: list[str] = Field(default_factory=list)  # user tag names, sorted (never the ownership tag)


class Device(DiffSyncModel):
    _modelname = "device"
    _identifiers = ("name",)
    _attributes = (
        "device_type",
        "manufacturer",
        "role",
        "status",
        "location",
        "rack",
        "position",
        "face",
        "railyard_id",
        "comments",
        "tags",
    )
    name: str
    device_type: str  # the device type's model
    manufacturer: str
    role: str
    status: str = "Active"
    location: str = ""
    rack: str | None = None
    position: int | None = None  # None for a 0U side-mount
    face: str = "front"  # blank when there is no position
    railyard_id: str = ""  # the Railyard placement id, stamped as the railyard_id custom field
    comments: str = ""
    tags: list[str] = Field(default_factory=list)  # user tag names, sorted (never the ownership tag)


class Interface(DiffSyncModel):
    _modelname = "interface"
    _identifiers = ("device", "name")
    _attributes = ("type", "status")
    device: str
    name: str
    type: str = "other"
    status: str = "Active"  # Nautobot 2 requires an interface status


#: Create-dependency order: location types, statuses, manufacturers and roles before what names them.
TOP_LEVEL = [
    "location_type",
    "status",
    "manufacturer",
    "device_type",
    "role",
    "location",
    "rack",
    "device",
    "rear_port",
    "front_port",
    "interface",
    "power_outlet",
    "power_port",
    "cable",
]

#: Models without tags in Nautobot (organisational models): their ownership is a custom field, not the tag.
UNTAGGED = ("location_type", "status", "manufacturer", "role")

__all__ = [
    "Cable",
    "Device",
    "DeviceType",
    "FrontPort",
    "Interface",
    "Location",
    "LocationType",
    "Manufacturer",
    "PowerOutlet",
    "PowerPort",
    "Rack",
    "RearPort",
    "Role",
    "Status",
    "TOP_LEVEL",
    "UNTAGGED",
]
