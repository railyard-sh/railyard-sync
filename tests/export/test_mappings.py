"""The value maps must match Railyard's Go export (netbox.go / cabling.go)."""

from railyard_sync.export import mappings as m


def test_slugify():
    assert m.slugify("LDN1-A01") == "ldn1-a01"
    assert m.slugify("Cisco Nexus 93180YC-FX") == "cisco-nexus-93180yc-fx"
    assert m.slugify("  Data  Centre!!  ") == "data-centre"
    assert m.slugify("") == ""


def test_width_inches():
    assert m.width_inches(600) == 19
    assert m.width_inches(699) == 19
    assert m.width_inches(700) == 23
    assert m.width_inches(800) == 23
    assert m.width_inches(0) == 19  # falls back to the 600mm default
    assert m.width_inches(None) == 19


def test_face_for_netbox():
    assert m.face_for_netbox("rear") == "rear"
    assert m.face_for_netbox("front") == "front"
    assert m.face_for_netbox("full") == "front"  # full depth mounts on the front
    assert m.face_for_netbox(None) == "front"


def test_interface_type():
    assert m.interface_type("RJ45") == "1000base-t"
    assert m.interface_type("SFP+") == "10gbase-x-sfpp"
    assert m.interface_type("QSFP28") == "100gbase-x-qsfp28"
    assert m.interface_type("weird") == "other"


def test_port_connector():
    assert m.port_connector("LC") == "lc"
    assert m.port_connector("MPO") == "mpo"
    assert m.port_connector("RJ45") == "8p8c"
    assert m.port_connector("") == "8p8c"


def test_outlet_type():
    assert m.outlet_type("C13") == "iec-60320-c13"
    assert m.outlet_type("C19") == "iec-60320-c19"
    assert m.outlet_type("C13/C19") == "iec-60320-c19"  # C19 wins (checked first)
    assert m.outlet_type("NEMA 5-15") == "nema-5-15r"
    assert m.outlet_type("mystery") == "iec-60320-c13"


def test_inlet_type():
    assert m.inlet_type() == "iec-60320-c14"


def test_status_slug():
    assert m.status_slug("Active") == "active"
    assert m.status_slug("") == "active"  # default
    assert m.status_slug("Decommissioning") == "decommissioning"
