"""SyncDocumentAdapter loads the canonical models from Railyard's NetBox sync document.

The fixtures are the documents Railyard's own exporter writes for two projects
(``railyard export --format netbox-sync``, via ``tests/fixtures/sync/make_documents.py``).
"""

import copy
import json
import pathlib

import pytest

from railyard_sync.export import models
from railyard_sync.export.sync_document import SyncDocumentAdapter, SyncDocumentError, parse_document

SYNC = pathlib.Path(__file__).parent.parent / "fixtures" / "sync"


def document(name: str = "cabled") -> dict:
    return json.loads((SYNC / f"netbox-sync-{name}.json").read_text())


def load(doc: dict) -> SyncDocumentAdapter:
    adapter = SyncDocumentAdapter(doc)
    adapter.load()
    return adapter


def ids(adapter, model) -> set:
    return {tuple(getattr(o, k) for k in model._identifiers) for o in adapter.get_all(model)}


# ---- the example project -------------------------------------------------------------------------


def test_example_project_matches_the_bundle():
    a = load(document("example"))
    assert a.project_id == "prj_replace-with-a-unique-id"
    assert {s.name for s in a.get_all(models.Site)} == {"LDN1", "FRA1"}
    assert a.get(models.Site, "LDN1").slug == "ldn1-2334f5e693"  # the bundle's slug, not re-derived
    assert ids(a, models.Location) == {("LDN1", "A [a559f3df2a]"), ("FRA1", "A [ea30999eaa]")}
    assert ids(a, models.Rack) == {("LDN1", "LDN1-A01"), ("LDN1", "LDN1-A02"), ("FRA1", "FRA1-A01")}
    rack = a.get(models.Rack, {"site": "LDN1", "name": "LDN1-A01"})
    assert (rack.width, rack.u_height, rack.desc_units, rack.location) == (19, 42, False, "A [a559f3df2a]")

    dev = a.get(models.Device, "LDN1-A01-LEAF-01")
    assert (dev.manufacturer, dev.device_type, dev.role) == ("Cisco", "Nexus 93180YC-FX", "leaf")
    assert (dev.rack, dev.position, dev.face, dev.location) == ("LDN1-A01", 41, "front", "A [a559f3df2a]")
    assert dev.railyard_id == "pl_ldn_a01_leaf1"
    dt = a.get_all(models.DeviceType)[0]
    assert (dt.slug, dt.u_height, dt.is_full_depth, dt.library_slug) == (
        "cisco-nexus-93180yc-fx",
        1,
        True,
        "cisco-nexus-93180yc-fx",
    )
    # the document's own warnings are carried through for the report
    assert any("cannot retain its parent container" in w for w in a.warnings)


# ---- the cabled project ---------------------------------------------------------------------------


def test_cabled_project_devices_and_attributes():
    a = load(document())
    assert ids(a, models.Location) == {("DC1", "Hall 1"), ("DC1", "A")}
    assert a.get(models.Location, {"site": "DC1", "name": "A"}).parent == "Hall 1"
    rack = a.get(models.Rack, {"site": "DC1", "name": "R1"})
    assert (rack.status, rack.width, rack.comments, rack.tags) == ("planned", 23, "Cold aisle A", ["build-1"])
    srv = a.get(models.Device, "SRV-1")
    assert (srv.serial, srv.comments, srv.face, srv.position) == ("SN-0001", "Database host", "rear", 1)
    assert a.get(models.Device, "SW-1").tags == ["build-1"]
    pdu = a.get(models.Device, "PDU-1")
    assert (pdu.position, pdu.face) == (None, "")  # 0U: no position and a blank face, as NetBox stores it
    assert [t.slug for t in a.tags] == ["build-1"]


def test_cabled_project_ports_and_cables():
    a = load(document())
    fronts = {(f.device, f.name): (f.rear_port, f.rear_port_position, f.type) for f in a.get_all(models.FrontPort)}
    assert fronts == {("PP-1", "1"): ("1", 1, "lc"), ("PP-1", "2"): ("2", 1, "lc")}
    assert {(r.device, r.name, r.positions) for r in a.get_all(models.RearPort)} == {("PP-1", "1", 1), ("PP-1", "2", 1)}
    assert ("SRV-1", "iface1") in ids(a, models.Interface)
    cables = {(c.a_device, c.a_name, c.b_device, c.b_name): c for c in a.get_all(models.Cable)}
    l1 = cables[("SW-1", "Eth1", "PP-1", "1")]
    assert (l1.label, l1.color, l1.is_power, l1.status) == ("L1", "ff0000", False, "connected")
    power = cables[("SRV-1", "PSU1", "PDU-1", "OUT1")]
    assert (power.is_power, power.type) == (True, "power")


def test_netbox_44_bundle_reads_the_same():
    # 4.4 front-port rows have no ``positions`` column; the models must not differ.
    a, b = load(document()), load(document("cabled-4.4"))
    for model in (models.FrontPort, models.RearPort, models.Cable, models.Device):
        assert {m.get_unique_id(): m.get_attrs() for m in a.get_all(model)} == {
            m.get_unique_id(): m.get_attrs() for m in b.get_all(model)
        }


def test_json_native_values_read_like_csv_strings():
    doc = document()
    for row in doc["objects"]["racks"]:
        row.update(width=23, u_height=42, desc_units=False, tags=["build-1"])
    for row in doc["objects"]["devices"]:
        row["position"] = int(row["position"]) if row["position"] else None
    a, b = load(document()), load(doc)
    for model in (models.Rack, models.Device):
        assert [m.get_attrs() for m in a.get_all(model)] == [m.get_attrs() for m in b.get_all(model)]


# ---- envelope -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d.update(format="netbox-csv"), "Not a Railyard NetBox sync document"),
        (lambda d: d.update(version=2), "upgrade railyard-sync"),
        (lambda d: d["project"].pop("id"), "no project id"),
        (lambda d: d["objects"].update(racks={"name": "x"}), "objects.racks"),
    ],
)
def test_bad_envelopes_are_refused(mutate, message):
    doc = document()
    mutate(doc)
    with pytest.raises(SyncDocumentError, match=message):
        parse_document(doc)


def test_unknown_kinds_and_bad_rows_are_warnings_not_failures():
    doc = copy.deepcopy(document())
    doc["objects"]["console-ports"] = [{"device": "SW-1", "name": "con0"}]
    doc["objects"]["devices"].append({"name": "", "site": "DC1"})
    a = load(doc)
    assert any("console-ports" in w for w in a.warnings)
    assert any("devices row 5 skipped: no name" in w for w in a.warnings)
    assert len(a.get_all(models.Device)) == 4


# ---- the contract's identities and extras ---------------------------------------------------------


def test_identities_are_kept_and_the_device_id_is_its_placement():
    a = load(document())
    assert a.identities[("device", "SRV-1")] == ("device", "p_srv")
    assert a.get(models.Device, "SRV-1").railyard_id == "p_srv"
    assert a.get(models.DeviceType, {"manufacturer": "Acme", "model": "PP24"}).library_slug == "pp"
    power = next(c for c in a.get_all(models.Cable) if c.is_power)
    assert a.identities[("cable", power.get_unique_id())] == ("power-link", "pw1")


def test_a_device_without_a_device_identity_gets_no_railyard_id():
    doc = document()
    doc["objects"]["devices"][0]["railyard"] = {"kind": "container", "id": "dc1"}
    assert load(doc).get(models.Device, "SW-1").railyard_id == ""


def test_unresolved_placements_are_reported():
    a = load(document("example"))
    assert a.document.unresolved[0]["placementId"] == "pl_ldn_a01_srv1"
    assert any(
        "placement LDN1-A01-SRV-01 in rack LDN1-A01: device type 'some 2U server' is not in the catalogue; not synced"
        in w
        for w in a.warnings
    )


def test_placeholder_device_types_have_no_library_slug():
    doc = document("example")
    doc["objects"]["device-types"].append(
        {
            "manufacturer": "Placeholder",
            "model": "some 2U server",
            "slug": "placeholder-some-2u-server",
            "u_height": "2",
            "is_full_depth": "true",
            "railyard": {"kind": "device-type", "placeholder": True},
        }
    )
    doc["unresolved"][0]["placeheld"] = True
    a = load(doc)
    assert a.get(models.DeviceType, {"manufacturer": "Placeholder", "model": "some 2U server"}).library_slug == ""
    assert any(w.endswith("synced as a placeholder") for w in a.warnings)


def test_a_merge_request_draft_says_so():
    doc = document()
    doc["project"]["changeRequestId"] = "cr_42"
    assert parse_document(doc).change_request_id == "cr_42"
    assert parse_document(document()).change_request_id == ""
