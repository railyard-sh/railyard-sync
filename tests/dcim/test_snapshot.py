import json

from railyard_sync.dcim.snapshot import (
    Cable,
    Component,
    ComponentTemplate,
    Device,
    DeviceType,
    PowerFeed,
    Rack,
    Site,
    Snapshot,
    Termination,
)


def test_snapshot_round_trips_through_json():
    snap = Snapshot(
        source="netbox",
        source_url="https://netbox.example",
        sites=[Site(id="1", name="LDN1", slug="ldn1")],
        racks=[Rack(id="7", name="A01", site_id="1")],
        device_types=[
            DeviceType(
                id="3",
                manufacturer="Arista",
                model="DCS-7050SX3-48YC8",
                slug="arista-dcs-7050sx3-48yc8",
                components=[ComponentTemplate(name="Ethernet1", kind="interface", type="25gbase-x-sfp28")],
            )
        ],
        devices=[
            Device(id="11", name="leaf1", device_type_id="3", site_id="1", rack_id="7", position=40, face="front")
        ],
        components=[Component(id="101", device_id="11", name="Ethernet1", kind="interface", type="25gbase-x-sfp28")],
        cables=[Cable(id="9", a=[Termination("dcim.interface", "101")], b=[Termination("dcim.interface", "102")])],
    )
    again = Snapshot.from_dict(json.loads(json.dumps(snap.to_dict())))
    assert again == snap


def test_power_feed_available_power_matches_netbox():
    single = PowerFeed(id="1", name="A", power_panel_id="p", voltage=230, amperage=32, max_utilization=80)
    assert round(single.available_power_w) == 5888
    three = PowerFeed(id="2", name="B", power_panel_id="p", phase="three-phase", voltage=400, amperage=32)
    assert round(three.available_power_w) == round(400 * 32 * 3**0.5 * 0.8)
