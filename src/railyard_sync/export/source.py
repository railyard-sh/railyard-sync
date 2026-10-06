"""``RailyardAdapter`` — the DiffSync *source* adapter.

``load(project_doc)`` walks a Railyard Project document and populates every canonical model. It is the
Python analogue of ``netbox.go``'s builder (sites/racks/manufacturers/device-types/roles/devices) plus
``cabling.go``'s plan (ports + cables + power), so the resulting object graph matches Railyard's own
NetBox export. Pure Python — no NetBox imports — so it is unit-tested directly against fixtures.
"""

from __future__ import annotations

from diffsync import Adapter

from ..project import Project
from . import models
from .cabling import (
    CT_FRONT_PORT,
    CT_INTERFACE,
    CT_POWER_OUTLET,
    CT_POWER_PORT,
    CT_REAR_PORT,
    build_cabling_plan,
)
from .devicetype_library import DeviceTypeLibrary
from .mappings import (
    DEFAULT_DEVICE_ROLE,
    DEFAULT_ROLE_COLOR,
    FALLBACK_SITE,
    face_for_netbox,
    slugify,
    status_slug,
    width_inches,
)


class RailyardAdapter(Adapter):
    manufacturer = models.Manufacturer
    device_type = models.DeviceType
    device_role = models.DeviceRole
    site = models.Site
    location = models.Location  # never loaded from Project JSON; declared because TOP_LEVEL names it
    rack = models.Rack
    device = models.Device
    interface = models.Interface
    rear_port = models.RearPort
    front_port = models.FrontPort
    power_outlet = models.PowerOutlet
    power_port = models.PowerPort
    cable = models.Cable

    top_level = models.TOP_LEVEL

    def __init__(self, project_doc: dict, *, devicetype_library: DeviceTypeLibrary | None = None, **kwargs):
        super().__init__(**kwargs)
        self.project = Project(project_doc)
        self.dtl = devicetype_library or DeviceTypeLibrary()
        self.warnings: list[str] = []
        self.unresolved: list[dict] = []
        self._device_names: dict[str, str] = {}  # placement id -> exported device name
        self._used_names: dict[str, int] = {}

    # -- entry point --------------------------------------------------------

    def load(self) -> None:
        for rack in self.project.racks:
            self._load_rack(rack)
        self._load_cabling()

    # -- containers + devices ----------------------------------------------

    def _load_rack(self, rack: dict) -> None:
        site_name = FALLBACK_SITE
        site_status = status_slug("")
        dc = self.project.resolve_dc(rack)
        if dc:
            if (dc.get("name") or "").strip():
                site_name = dc["name"]
            if slugify(dc.get("status", "")):
                site_status = slugify(dc["status"])
        self.get_or_instantiate(
            self.site, ids={"name": site_name}, attrs={"slug": slugify(site_name), "status": site_status}
        )

        self.get_or_instantiate(
            self.rack,
            ids={"site": site_name, "name": rack.get("name", "")},
            attrs={
                "status": status_slug(rack.get("status", "")),
                "width": width_inches(self.project.rack_width_mm(rack)),
                "u_height": self.project.rack_u_height(rack),
                "desc_units": self.project.rack_descending(rack),
            },
        )

        for pl in self.project.placements(rack):
            self._load_device(rack, pl, site_name)

    def _load_device(self, rack: dict, pl: dict, site_name: str) -> None:
        ref = (pl.get("deviceTypeRef") or "").strip()
        dt = self.project.device_type_by_key(ref) if ref else None
        if dt is None:
            # Unresolved device-type ref: reported and skipped (mirrors the export with placeholders off).
            self.unresolved.append(
                {"rack": rack.get("name", ""), "placement": pl.get("id", ""), "ref": pl.get("deviceTypeRef", "")}
            )
            self.warnings.append(
                f"placement {pl.get('label') or pl.get('id')} in rack {rack.get('name')}: "
                f"device-type ref {pl.get('deviceTypeRef')!r} did not resolve — skipped"
            )
            return

        spec = self.dtl.resolve(dt)
        self.get_or_instantiate(
            self.manufacturer, ids={"name": spec.manufacturer}, attrs={"slug": slugify(spec.manufacturer)}
        )
        self.get_or_instantiate(
            self.device_type,
            ids={"manufacturer": spec.manufacturer, "model": spec.model},
            attrs={
                "slug": spec.slug,
                "u_height": spec.u_height,
                "is_full_depth": spec.is_full_depth,
                "part_number": spec.part_number,
            },
        )

        role = (pl.get("role") or "").strip() or DEFAULT_DEVICE_ROLE
        self.get_or_instantiate(
            self.device_role, ids={"name": role}, attrs={"slug": slugify(role), "color": DEFAULT_ROLE_COLOR}
        )

        name = self._device_name(rack, pl)
        self._device_names[pl.get("id", "")] = name
        zero_u = self.project.is_zero_u(pl)
        position = None if zero_u else int(pl.get("startU") or 0) or None
        # NetBox stores a blank face for a 0U device; match that so re-syncs don't churn.
        face = "" if zero_u else face_for_netbox(pl.get("face"))

        self.get_or_instantiate(
            self.device,
            ids={"name": name},
            attrs={
                "device_type": spec.model,
                "manufacturer": spec.manufacturer,
                "role": role,
                "site": site_name,
                "rack": rack.get("name", ""),
                "position": position,
                "face": face,
                "status": status_slug(""),
                "railyard_id": pl.get("id", ""),
            },
        )

    def _device_name(self, rack: dict, pl: dict) -> str:
        """Exported device name with global de-duplication. Port of ``deviceName`` (netbox.go:272-282)."""
        base = (pl.get("label") or "").strip() or f"{rack.get('name', '')}-U{pl.get('startU', 0)}"
        self._used_names[base] = self._used_names.get(base, 0) + 1
        if self._used_names[base] > 1:
            return f"{base}-{self._used_names[base]}"
        return base

    # -- cabling + power ----------------------------------------------------

    def _name_of(self, placement_id: str) -> tuple[str, bool]:
        name = self._device_names.get(placement_id)
        return (name, True) if name is not None else ("", False)

    def _load_cabling(self) -> None:
        plan = build_cabling_plan(self.project, self._name_of)
        self.warnings.extend(plan.warnings)

        for c in plan.rear_ports:
            self.get_or_instantiate(
                self.rear_port,
                ids={"device": c.device, "name": c.name},
                attrs={"type": c.ctype, "positions": c.positions},
            )
        for c in plan.front_ports:
            self.get_or_instantiate(
                self.front_port,
                ids={"device": c.device, "name": c.name},
                attrs={"type": c.ctype, "rear_port": c.rear_port, "rear_port_position": c.rear_pos},
            )
        for c in plan.interfaces:
            self.get_or_instantiate(self.interface, ids={"device": c.device, "name": c.name}, attrs={"type": c.ctype})
        for c in plan.power_outlets:
            self.get_or_instantiate(
                self.power_outlet, ids={"device": c.device, "name": c.name}, attrs={"type": c.ctype}
            )
        for c in plan.power_ports:
            self.get_or_instantiate(self.power_port, ids={"device": c.device, "name": c.name}, attrs={"type": c.ctype})
        for link in plan.cables:
            self.get_or_instantiate(
                self.cable,
                ids={
                    "a_device": link.a_dev,
                    "a_type": link.a_type,
                    "a_name": link.a_name,
                    "b_device": link.b_dev,
                    "b_type": link.b_type,
                    "b_name": link.b_name,
                },
                attrs={"is_power": link.power, "label": link.label},
            )

    # Re-exported termination content types, so a target adapter can switch on them without importing
    # the cabling module directly.
    CT_INTERFACE = CT_INTERFACE
    CT_FRONT_PORT = CT_FRONT_PORT
    CT_REAR_PORT = CT_REAR_PORT
    CT_POWER_PORT = CT_POWER_PORT
    CT_POWER_OUTLET = CT_POWER_OUTLET
