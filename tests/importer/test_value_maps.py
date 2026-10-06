"""The importer's value maps reverse Railyard's export: an imported value exports back to its slug."""

from __future__ import annotations

import pytest

from railyard_sync.importer import mappings as m


@pytest.mark.parametrize(
    ("slug", "expected"),
    [
        ("10gbase-x-sfpp", ("SFP+", None)),
        ("25gbase-x-sfp28", ("SFP28", None)),
        ("100gbase-x-qsfp28", ("QSFP28", None)),
        ("800gbase-x-osfp", ("OSFP", "800gbase-x-osfp")),
        ("400gbase-x-qsfpdd", ("QSFP-DD", None)),
        ("1000base-t", ("RJ45", None)),
        ("10gbase-t", ("RJ45", "10gbase-t")),
        ("100base-fx", ("100base-fx", None)),  # no family: kept as it is
        ("lag", (None, None)),
        ("other", (None, None)),
        ("", (None, None)),
    ],
)
def test_interface_connector(slug, expected):
    assert m.interface_connector(slug) == expected


def test_port_connector_keeps_the_polish_as_netbox_type():
    assert m.port_connector("lc") == ("LC", None)
    assert m.port_connector("lc-apc") == ("LC", "lc-apc")
    assert m.port_connector("mpo") == ("MPO", None)
    assert m.port_connector("8p8c") == ("RJ45", None)
    assert m.port_connector("110-punch") == ("110-punch", None)


def test_instance_connector_keeps_a_precise_slug():
    assert m.instance_connector("10gbase-x-sfpp", "interface") == "SFP+"
    assert m.instance_connector("10gbase-t", "interface") == "10gbase-t"
    assert m.instance_connector("lc-apc", "front-port") == "lc-apc"


def test_cableable_interfaces():
    assert m.cableable_interface("10gbase-x-sfpp")
    for slug in ("lag", "virtual", "bridge", "ieee802.11ax", "lte", "other-wireless"):
        assert not m.cableable_interface(slug)


def test_power_connectors_and_outlets():
    assert m.power_connector("iec-60320-c14") == "C14"
    assert m.power_connector("nema-5-15p") == "NEMA 5-15P"
    assert m.power_connector("dc-terminal") == "dc-terminal"
    assert m.power_connector("other") is None
    assert m.outlet_type_of(["iec-60320-c13"] * 3 + ["iec-60320-c19", "nema-5-20r", None]) == "C13/C19/NEMA 5-20R"


def test_cable_media_round_trips_through_railyards_cable_type():
    # Each media string must map back to its slug under Railyard's cableType (cabling.go).
    assert m.cable_media("cat6a") == "Cat6A"
    assert m.cable_media("mmf-om4") == "OM4"
    assert m.cable_media("smf-os2") == "OS2"
    assert m.cable_media("dac-active") == "Active DAC"
    assert m.cable_media("power") == "power"
    assert m.cable_media("") == ""


def test_cable_colour():
    assert m.cable_colour("FF0000") == "#ff0000"
    assert m.cable_colour("f00") == "#ff0000"
    assert m.cable_colour("red") == ""


def test_rack_width_and_status():
    assert m.rack_width_mm(19, None) == 600
    assert m.rack_width_mm(23, None) == 800
    assert m.rack_width_mm(19, 750.4) == 750
    assert m.width_inches(800) == 23
    assert m.status_label("decommissioning") == "Decommissioning"
    assert m.status_label("") == ""


def test_natural_order_and_fit():
    names = ["Outlet 10", "Outlet 2", "Outlet 1"]
    assert sorted(names, key=m.natural_key) == ["Outlet 1", "Outlet 2", "Outlet 10"]
    long = "x" * 300
    assert len(m.fit(long, 100)) == 100
    assert m.fit(long, 100) != m.fit(long + "y", 100)
    assert m.fit("short", 100) == "short"
