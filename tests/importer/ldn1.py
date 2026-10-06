"""A small but realistic NetBox site as a :class:`Snapshot`, built in code.

LDN1 sits in the UK region (under Europe), with Building A → Hall 1 below it and three racks in the
hall, one of which repeats another's name in a different case. It holds a leaf switch (a module
slot included), a pass-through LC patch panel, servers, two 0U PDUs, a 1.5U shelf, a blade chassis
with a blade in a bay, an unracked spare and a server that overlaps another. Cables cover the
importable cases (data, patch-panel, module port, power to PDU outlets) and the ones the importer
must refuse (circuit, power feed, console, multi-termination, another site, a skipped device).
"""

from __future__ import annotations

from railyard_sync.dcim.snapshot import (
    Cable,
    Component,
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

LEAF_SLUG = "cisco-nexus-93180yc-fx"


def _leaf_type() -> DeviceType:
    interfaces = [ComponentTemplate(f"Ethernet1/{n}", "interface", "25gbase-x-sfp28") for n in (1, 2, 3, 4)]
    interfaces.append(ComponentTemplate("Ethernet1/49", "interface", "100gbase-x-qsfp28"))
    interfaces.append(ComponentTemplate("mgmt0", "interface", "1000base-t", mgmt_only=True))
    power = [ComponentTemplate(name, "power-port", "iec-60320-c14", maximum_draw_w=650) for name in ("PSU1", "PSU2")]
    return DeviceType(
        id="1",
        manufacturer="Cisco",
        model="Nexus 93180YC-FX",
        slug=LEAF_SLUG,
        u_height=1,
        is_full_depth=True,
        part_number="N9K-C93180YC-FX",
        components=interfaces + power,
    )


def _panel_type() -> DeviceType:
    fronts = [
        ComponentTemplate(f"Front {n}", "front-port", "lc-apc", rear_port_name="Rear 1", rear_port_position=n)
        for n in (1, 2, 3, 4)
    ]
    return DeviceType(
        id="2",
        manufacturer="Panduit",
        model="LC 4-port cassette",
        slug="panduit-lc-4",
        u_height=1,
        is_full_depth=False,
        components=[*fronts, ComponentTemplate("Rear 1", "rear-port", "mpo", positions=4)],
    )


def _server_type() -> DeviceType:
    return DeviceType(
        id="3",
        manufacturer="Dell",
        model="PowerEdge R650",
        slug="dell-poweredge-r650",
        u_height=1,
        is_full_depth=True,
        weight_kg=19.9,
        airflow="front-to-rear",
        components=[
            ComponentTemplate("NIC1", "interface", "10gbase-x-sfpp"),
            ComponentTemplate("NIC2", "interface", "10gbase-x-sfpp"),
            ComponentTemplate("iDRAC", "interface", "1000base-t", mgmt_only=True),
            ComponentTemplate("bond0", "interface", "lag"),
            ComponentTemplate("PSU1", "power-port", "iec-60320-c14", maximum_draw_w=800, allocated_draw_w=400),
            ComponentTemplate("PSU2", "power-port", "iec-60320-c14", maximum_draw_w=800, allocated_draw_w=400),
        ],
    )


def _pdu_type() -> DeviceType:
    outlets = [ComponentTemplate(f"Outlet {n}", "power-outlet", "iec-60320-c13") for n in range(1, 11)]
    outlets += [ComponentTemplate(f"Outlet {n}", "power-outlet", "iec-60320-c19") for n in (11, 12)]
    return DeviceType(
        id="4",
        manufacturer="APC",
        model="AP8868",
        slug="apc-ap8868",
        u_height=0,
        is_full_depth=False,
        components=[ComponentTemplate("Input", "power-port", "iec-60309-p-n-e-6h", maximum_draw_w=7400), *outlets],
    )


def device_types() -> list[DeviceType]:
    return [
        _leaf_type(),
        _panel_type(),
        _server_type(),
        _pdu_type(),
        DeviceType(
            id="5", manufacturer="Generic", model="Pi shelf", slug="pi-shelf", u_height=1.5, is_full_depth=False
        ),
        DeviceType(id="6", manufacturer="HPE", model="c7000", slug="hpe-c7000", u_height=10, subdevice_role="parent"),
        DeviceType(id="7", manufacturer="HPE", model="BL460c", slug="hpe-bl460c", u_height=0, subdevice_role="child"),
    ]


def _device_components() -> list[Component]:
    comps: list[Component] = []

    def iface(cid, device, name, type_, module=""):
        comps.append(Component(id=cid, device_id=device, name=name, kind="interface", type=type_, module=module))

    # Leaf 1000: four SFP28, one QSFP28 uplink, mgmt, and a module slot interface.
    for n, cid in zip((1, 2, 3, 4), ("101", "102", "103", "104"), strict=True):
        iface(cid, "1000", f"Ethernet1/{n}", "25gbase-x-sfp28")
    iface("149", "1000", "Ethernet1/49", "100gbase-x-qsfp28")
    iface("150", "1000", "mgmt0", "1000base-t")
    iface("151", "1000", "Ethernet2/1", "10gbase-t", module="Slot 2")
    comps += [
        Component(id="31", device_id="1000", name="PSU1", kind="power-port", type="iec-60320-c14"),
        Component(id="32", device_id="1000", name="PSU2", kind="power-port", type="iec-60320-c14"),
        Component(id="9", device_id="1000", name="con0", kind="console-port", type="rj-45"),
    ]
    # Patch panel 1001: four LC-APC fronts onto one MPO rear.
    comps.append(Component(id="71", device_id="1001", name="Rear 1", kind="rear-port", type="mpo", positions=4))
    for n in (1, 2, 3, 4):
        comps.append(
            Component(
                id=str(60 + n),
                device_id="1001",
                name=f"Front {n}",
                kind="front-port",
                type="lc-apc",
                rear_port_id="71",
                rear_port_position=n,
            )
        )
    # Servers 1002 (imported), 1009 (overlaps 1002), 1010 (in the duplicate-name rack).
    for device, base in (("1002", 200), ("1009", 300), ("1010", 400)):
        iface(str(base + 1), device, "NIC1", "10gbase-x-sfpp")
        iface(str(base + 2), device, "NIC2", "10gbase-x-sfpp")
        iface(str(base + 3), device, "iDRAC", "1000base-t")
        iface(str(base + 4), device, "bond0", "lag")
        comps.append(
            Component(id=str(base + 5), device_id=device, name="PSU1", kind="power-port", type="iec-60320-c14")
        )
        comps.append(
            Component(id=str(base + 6), device_id=device, name="PSU2", kind="power-port", type="iec-60320-c14")
        )
    # PDUs 1003 and 1004: outlet ids deliberately out of name order.
    for device, base in (("1003", 500), ("1004", 600)):
        comps.append(
            Component(id=str(base), device_id=device, name="Input", kind="power-port", type="iec-60309-p-n-e-6h")
        )
        for n in range(12, 0, -1):
            outlet_type = "iec-60320-c19" if n > 10 else "iec-60320-c13"
            comps.append(
                Component(
                    id=str(base + 13 - n), device_id=device, name=f"Outlet {n}", kind="power-outlet", type=outlet_type
                )
            )
    # The console server port the leaf's console cable lands on.
    comps.append(Component(id="900", device_id="1006", name="Bay 1", kind="other"))
    return comps


def snapshot() -> Snapshot:
    """The LDN1 fixture."""
    devices = [
        Device(
            id="1000",
            name="ldn1-leaf-01",
            device_type_id="1",
            site_id="10",
            role="Leaf switch",
            location_id="21",
            rack_id="100",
            position=40,
            face="front",
            serial="FDO12345678",
            tags=["fabric", "fabric", " "],
        ),
        Device(
            id="1001",
            name="ldn1-pp-01",
            device_type_id="2",
            site_id="10",
            role="Patch panel",
            rack_id="100",
            position=42,
            face="front",
        ),
        Device(
            id="1002",
            name="ldn1-srv-01",
            device_type_id="3",
            site_id="10",
            role="Server",
            rack_id="100",
            position=10,
            face="rear",
            status="planned",
            serial="X" * 60,
            comments="Rebuilt after RMA.",
        ),
        Device(id="1003", name="ldn1-pdu-a", device_type_id="4", site_id="10", role="PDU", rack_id="100"),
        Device(id="1004", name="ldn1-pdu-b", device_type_id="4", site_id="10", role="PDU", rack_id="100"),
        Device(
            id="1005",
            name="ldn1-pi-shelf",
            device_type_id="5",
            site_id="10",
            rack_id="101",
            position=20.5,
            face="front",
        ),
        Device(
            id="1006", name="ldn1-chassis-01", device_type_id="6", site_id="10", rack_id="101", position=1, face="front"
        ),
        Device(id="1007", name="blade-01", device_type_id="7", site_id="10", rack_id="101", parent_device_id="1006"),
        Device(id="1008", name="spare-srv", device_type_id="3", site_id="10", location_id="21", status="inventory"),
        Device(
            id="1009", name="ldn1-srv-02", device_type_id="3", site_id="10", rack_id="100", position=10, face="front"
        ),
        Device(
            id="1010", name="ldn1-srv-03", device_type_id="3", site_id="10", rack_id="102", position=41, face="front"
        ),
    ]

    def end(object_type, object_id):
        return [Termination(object_type, object_id)]

    cables = [
        Cable("5000", end("dcim.interface", "101"), end("dcim.interface", "201"), type="dac-passive", color="ff0000"),
        Cable("5001", end("dcim.interface", "149"), end("dcim.frontport", "61"), type="smf-os2", label="LDN-0001"),
        Cable("5002", end("dcim.rearport", "71"), end("circuits.circuittermination", "7")),
        Cable("5003", end("dcim.powerport", "205"), end("dcim.poweroutlet", "503"), type="power"),  # Outlet 10
        Cable("5004", end("dcim.poweroutlet", "612"), end("dcim.powerport", "206"), type="power"),  # Outlet 1, reversed
        Cable("5005", end("dcim.powerport", "500"), end("dcim.powerfeed", "1")),
        Cable("5006", end("dcim.interface", "301"), end("dcim.interface", "102")),
        Cable(
            "5007",
            [Termination("dcim.interface", "103"), Termination("dcim.interface", "104")],
            [Termination("dcim.interface", "401"), Termination("dcim.interface", "402")],
        ),
        Cable("5008", end("dcim.interface", "151"), end("dcim.interface", "403"), type="cat6a", status="planned"),
        Cable("5009", end("dcim.consoleport", "9"), end("dcim.consoleserverport", "77")),
        Cable("5010", end("dcim.interface", "150"), end("dcim.interface", "99999")),
    ]
    return Snapshot(
        source="netbox",
        source_url="https://netbox.example.com",
        source_version="4.3.7",
        regions=[Region("1", "Europe", "europe"), Region("2", "UK", "uk", parent_id="1"), Region("3", "APAC", "apac")],
        sites=[Site("10", "LDN1", "ldn1", facility="Equinix LD5", region_id="2", tags=["prod"])],
        locations=[
            Location("21", "Hall 1", "hall-1", site_id="10", parent_id="20"),
            Location("20", "Building A", "building-a", site_id="10", facility="LD5-A"),
        ],
        rack_types=[
            RackType(
                "1",
                "APC",
                "NetShelter SX 42U",
                "apc-ar3100",
                u_height=42,
                outer_width_mm=600,
                outer_depth_mm=1070,
                form_factor="4-post-cabinet",
            )
        ],
        racks=[
            Rack(
                "100",
                "A01",
                site_id="10",
                location_id="21",
                role="Network",
                rack_type_id="1",
                outer_width_mm=600,
                outer_depth_mm=1070,
                max_weight_kg=1000,
                comments="Fabric rack",
                tags=["core"],
            ),
            Rack("101", "A02", site_id="10", location_id="21", status="planned", width_in=23, u_height=47),
            Rack("102", "a01", site_id="10", location_id="21", desc_units=True),
        ],
        device_types=device_types(),
        devices=devices,
        components=_device_components(),
        cables=cables,
        power_panels=[PowerPanel("1", "PP-A", site_id="10", location_id="20")],
        power_feeds=[
            PowerFeed("1", "Feed A", "1", rack_id="100", voltage=230, amperage=32),
            PowerFeed("2", "Feed B", "1", rack_id="100", type="redundant", voltage=230, amperage=32),
            PowerFeed("3", "Feed C", "1", rack_id="101", phase="three-phase", voltage=400, amperage=16),
        ],
        warnings=["cable 5010: far end interface 99999 is outside the loaded sites"],
    )


def leaf_catalogue_entry() -> dict:
    """What Railyard's catalogue serves for the leaf switch (the projection, before source stamping)."""
    front = [{"name": f"Ethernet1/{n}", "type": "SFP28"} for n in (1, 2, 3, 4)]
    front.append({"name": "Ethernet1/49", "type": "QSFP28"})
    front.append({"name": "mgmt0", "type": "RJ45"})
    return {
        "key": LEAF_SLUG,
        "manufacturer": "Cisco",
        "model": "Nexus 93180YC-FX",
        "uHeight": 1,
        "fullDepth": True,
        "powerInlets": [{"name": "PSU1", "connector": "C14"}, {"name": "PSU2", "connector": "C14"}],
        "ports": {"front": 6, "passThrough": False, "media": "SFP28", "frontPorts": front},
        "partNumber": "N9K-C93180YC-FX",
    }


class FakeCatalogue:
    """A :class:`CatalogueLookup` that knows only the leaf switch."""

    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    def device_type(self, slug: str, manufacturer: str, model: str) -> dict | None:
        self.calls.append((slug, manufacturer, model))
        if slug != LEAF_SLUG:
            return None
        entry = leaf_catalogue_entry()
        entry["source"] = {"kind": "catalogue", "ref": "a" * 64, "revision": "2026.10.1"}
        return entry
