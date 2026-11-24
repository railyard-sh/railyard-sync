"""``sync_placements_to_dctrack`` (``export sunbird --devices``) against the in-memory dcTrack: creates on mapped,
library and placeholder models, moves of Planned items only, occupied units and taken names, missing cabinets, 0U
devices, and an older document without the placement keys."""

from __future__ import annotations

import pytest
from export.fake_dctrack_rest import FakeDcTrack
from export.sunbird_docs import cabinet, device, document, with_zero_u, zero_u

from railyard_sync.export import dctrack_devices as dd, dctrack_serials as ds
from railyard_sync.export.dctrack_rest import DcTrackClient


@pytest.fixture
def dctrack() -> FakeDcTrack:
    fake = FakeDcTrack()
    fake.add_location("LON1")
    fake.r01 = fake.add_cabinet("LON1", "R01")
    fake.r02 = fake.add_cabinet("LON1", "R02")
    return fake


def place(fake: FakeDcTrack, doc: dict, **kwargs) -> dd.PlacementResult:
    kwargs.setdefault("location", "LON1")
    client = DcTrackClient(fake.URL, user=fake.USER, password=fake.PASSWORD, session=fake)
    return dd.sync_placements_to_dctrack(doc, client, **kwargs)


def by_name(result: dd.PlacementResult) -> dict[str, dd.PlannedPlacement]:
    return {d.name: d for d in result.devices}


def placed(result: dd.PlacementResult) -> dict[str, tuple[str, str, str]]:
    """name -> (placement, outcome, reason)."""
    return {d.name: (d.placement, d.outcome, d.reason) for d in result.devices}


# ---- creates and models ------------------------------------------------------------------------------------------


def test_a_device_dctrack_lacks_is_created_planned_on_a_placeholder(dctrack):
    doc = document([device("srv-01", 12, serial="ABC123", face="front", height=2)])
    result = place(dctrack, doc)
    row = by_name(result)["srv-01"]
    assert (row.placement, row.outcome, row.create_name) == (ds.CREATE, ds.CREATE, "SRV-01")
    assert (row.model_make, row.model_name, row.model_source) == ("Railyard", "Generic 2U Device", dd.FROM_PLACEHOLDER)
    assert (result.makes, result.models, result.placeholders) == (["Railyard"], ["Railyard / Generic 2U Device"], 1)
    assert row.line() == "R01 U12 srv-01: create SRV-01 as Railyard / Generic 2U Device (placeholder), serial ABC123"
    assert dctrack.created == [] and dctrack.created_models == []

    result = place(dctrack, doc, dry_run=False)
    assert dctrack.created_makes == ["Railyard"]
    (model,) = dctrack.created_models
    assert model["make"] == "Railyard" and model["model"] == "Generic 2U Device"
    assert (model["class"], model["subclass"], model["mounting"], model["formFactor"], model["ruHeight"]) == (
        "Device",
        "Standard",
        "Rackable",
        "Fixed",
        2,
    )
    (created,) = dctrack.created
    assert created == {
        "tiName": "SRV-01",
        "cmbLocation": "LON1",
        "cmbCabinet": "R01",
        "cmbMake": "Railyard",
        "cmbModel": "Generic 2U Device",
        "cmbStatus": "Planned",
        "cmbUPosition": 12,
        "radioRailsUsed": "Front",
        "tiSerialNumber": "ABC123",
    }
    assert (result.created, result.models_created, result.ok) == (1, 1, True)
    assert by_name(result)["srv-01"].item_id == str(dctrack.item_named("SRV-01")["id"])


def test_a_mapped_model_then_a_library_match_then_an_alias(dctrack, tmp_path):
    dctrack.add_model("Dell", "PowerEdge R750", ru=2)
    dctrack.add_model("Cisco", "Nexus 93180YC-FX", item_class="Network")
    dctrack.add_model("Dell", "PE-R640", aliases=("PowerEdge R640",))
    mapping = tmp_path / "models.csv"
    mapping.write_text(
        "railyard_make,railyard_model,dctrack_make,dctrack_model\n"
        "Acme,Leaf-48,cisco,nexus 93180yc-fx\n"
        "Acme,Gone,Cisco,Nexus 1000\n",
        encoding="utf-8",
    )
    doc = document(
        [
            device("SRV-1", 1, face="full", height=2, make="dell", model="poweredge r750"),
            device("LEAF-1", 5, kind="network", make="ACME", model="leaf-48"),
            device("SRV-2", 7, make="Dell", model="PowerEdge R640"),
            device("OLD-1", 9, make="Acme", model="Gone"),
        ]
    )
    result = place(dctrack, doc, model_map=dd.read_model_map(str(mapping)), dry_run=False)
    rows = by_name(result)
    assert (rows["SRV-1"].model_make, rows["SRV-1"].model_name, rows["SRV-1"].model_source) == (
        "Dell",
        "PowerEdge R750",
        dd.FROM_LIBRARY,
    )
    assert (rows["LEAF-1"].model_make, rows["LEAF-1"].model_name, rows["LEAF-1"].model_source) == (
        "Cisco",
        "Nexus 93180YC-FX",
        dd.FROM_MAP,
    )
    assert (rows["SRV-2"].model_name, rows["SRV-2"].model_source) == ("PE-R640", dd.FROM_LIBRARY)
    assert rows["OLD-1"].outcome == ds.NO_MODEL and "--model-map gives Cisco / Nexus 1000" in rows["OLD-1"].note
    assert result.makes == [] and result.models == [] and result.placeholders == 0
    assert [c["tiName"] for c in dctrack.created] == ["SRV-1", "LEAF-1", "SRV-2"]
    assert dctrack.item_named("SRV-1")["radioRailsUsed"] == "Both"
    assert not result.failed  # no-model is reported, not a conflict


def test_placeholders_are_created_once_and_reused_and_a_second_run_is_a_no_op(dctrack):
    doc = document(
        [
            device("A", 1, serial="SA"),
            device("B", 2),
            device("SW", 3, kind="network"),
            device("PP", 4, kind="patch-panel", make="", model=""),
        ]
    )
    result = place(dctrack, doc, dry_run=False)
    assert result.models == [
        "Railyard / Generic 1U Device",
        "Railyard / Generic 1U Network",
        "Railyard / Generic 1U Data Panel",
    ]
    assert [m["model"] for m in dctrack.created_models] == [
        "Generic 1U Device",
        "Generic 1U Network",
        "Generic 1U Data Panel",
    ]
    assert dctrack.created_models[2]["subclass"] == "Patch Panel" and result.placeholders == 4
    writes = (len(dctrack.created), len(dctrack.created_models), len(dctrack.writes))

    again = place(dctrack, doc, dry_run=False)
    assert placed(again) == {
        "A": ("", ds.UNCHANGED, ""),
        "B": ("", ds.NO_SERIAL, ""),
        "SW": ("", ds.NO_SERIAL, ""),
        "PP": ("", ds.NO_SERIAL, ""),
    }
    assert (len(dctrack.created), len(dctrack.created_models), len(dctrack.writes)) == writes
    assert again.makes == again.models == again.cabinets == again.only_in_dctrack == []

    # A new device needing a placeholder that now exists reuses it.
    more = place(dctrack, document([*doc["devices"], device("C", 10)]))
    assert more.models == [] and by_name(more)["C"].model_name == "Generic 1U Device"


# ---- moves -------------------------------------------------------------------------------------------------------


def test_a_planned_item_elsewhere_is_moved_and_an_installed_one_never(dctrack):
    planned = dctrack.add_item(dctrack.r02, "srv-01", 5, status="Planned", serial="")
    dctrack.add_item(dctrack.r02, "srv-02", 6, status="Installed")
    doc = document([device("SRV-01", 12, serial="ABC"), device("SRV-02", 14, serial="DEF")])
    result = place(dctrack, doc)
    assert placed(result) == {
        "SRV-01": (ds.MOVE, ds.UPDATE, ""),
        "SRV-02": ("", ds.CONFLICT, ds.INSTALLED_ELSEWHERE),
    }
    assert by_name(result)["SRV-01"].line() == (
        "R01 U12 SRV-01 → dcTrack SRV-01: move from R02 U5 to R01, serial (blank) → ABC"
    )
    assert by_name(result)["SRV-02"].note == "Installed in R02 U6: never moved"
    assert result.counts[ds.MOVE] == 1 and result.failed

    result = place(dctrack, doc, dry_run=False)
    item = dctrack.items[planned]
    assert (item["cmbCabinet"], item["cmbUPosition"], item["radioRailsUsed"], item["tiSerialNumber"]) == (
        "R01",
        12,
        "Front",
        "ABC",
    )
    assert dctrack.item_named("SRV-02")["cmbCabinet"] == "R02"
    assert (result.moved, result.updated, result.errors) == (1, 1, [])
    move = next(w for w in dctrack.writes if "cmbCabinet" in w)
    assert set(move) == {"id", "cmbCabinet", "cmbUPosition", "radioRailsUsed"}


def test_a_name_that_differs_at_the_position_is_reported_not_renamed(dctrack):
    dctrack.add_item(dctrack.r01, "lon1-web-01", 3)
    dctrack.add_item(dctrack.r01, "kvm", 40)
    result = place(dctrack, document([device("WEB-01", 3)]), dry_run=False)
    row = by_name(result)["WEB-01"]
    assert (row.found_by, row.name_differs, row.item_name, row.outcome) == (
        "position",
        True,
        "LON1-WEB-01",
        ds.NO_SERIAL,
    )
    assert dctrack.item_named("LON1-WEB-01") is not None and dctrack.writes == []
    assert result.only_in_dctrack == ["R01 U40 KVM (Device)"]


# ---- refusals before writing ---------------------------------------------------------------------------------------


def test_occupied_units_and_taken_names_are_conflicts(dctrack):
    dctrack.add_item(dctrack.r01, "big", 10, height=2)  # U10-U11, both rails
    dctrack.add_item(dctrack.r01, "rear-only", 20, rails="Back")
    dctrack.add_item(dctrack.r01, "web-1", 30)
    doc = document(
        [
            device("X", 11, face="front"),  # inside BIG
            device("Y", 19, face="front", height=2),  # U19-U20 front: REAR-ONLY uses the back rails only
            device("Z", 21, face="full", height=1),
            device("WEB-1", 30),  # matched at the position
            device("WEB-1-COPY", 31, make="", model=""),
            device("R02", 33),  # a cabinet's name
        ]
    )
    doc["devices"][4]["name"] = "web-1"  # a second Railyard device named like the item WEB-1 at U30
    doc["devices"][4]["railyard"]["id"] = "pl-copy"
    result = place(dctrack, doc)
    rows = result.devices
    assert [(r.name, r.placement, r.outcome, r.reason) for r in rows] == [
        ("X", "", ds.CONFLICT, ds.POSITION_OCCUPIED),
        ("Y", ds.CREATE, ds.CREATE, ""),
        ("Z", ds.CREATE, ds.CREATE, ""),
        ("WEB-1", "", ds.NO_SERIAL, ""),
        ("web-1", "", ds.CONFLICT, ds.NAME_TAKEN),
        ("R02", "", ds.CONFLICT, ds.NAME_TAKEN),
    ]
    assert rows[0].note == "U11 is taken by BIG (U10–U11, Both)"
    assert "matched to another device" in rows[4].note and "is a cabinet" in rows[5].note
    assert result.failed


def test_two_unnamed_devices_get_their_own_names(dctrack):
    result = place(dctrack, document([device("", 5), device("", 6)]))
    assert [r.create_name for r in result.devices] == ["R01-U5", "R01-U6"]


# ---- cabinets ------------------------------------------------------------------------------------------------------


def test_a_missing_cabinet_is_skipped_without_cabinet_model_and_created_with_it(dctrack):
    cabinets = [cabinet("R09", "r9", row="Row A", rowPosition=3, make="APC", model="AR3100")]
    doc = document([device("SRV-9", 4, cabinet="R09", rack="r9")], cabinets=cabinets)
    result = place(dctrack, doc)
    assert placed(result) == {"SRV-9": ("", ds.CABINET_NOT_FOUND, "")}
    assert "--cabinet-model creates it" in by_name(result)["SRV-9"].note

    with pytest.raises(dd.PlacementRefused, match="--cabinet-model APC/Missing is not in dcTrack's Models Library"):
        place(dctrack, doc, cabinet_model=("APC", "Missing"))
    dctrack.add_model("APC", "NetShelter SX 42U", item_class="Cabinet", ru=42)
    dctrack.add_model("Dell", "PowerEdge R750", ru=2)
    with pytest.raises(dd.PlacementRefused, match="is a Device model, not a cabinet model"):
        place(dctrack, doc, cabinet_model=("Dell", "PowerEdge R750"))

    result = place(dctrack, doc, cabinet_model=("apc", "netshelter sx 42u"))
    assert result.cabinets == ["R09 in LON1 as APC / NetShelter SX 42U, row Row A position 3"]
    assert by_name(result)["SRV-9"].line().endswith("in new cabinet R09")

    result = place(dctrack, doc, cabinet_model=("APC", "NetShelter SX 42U"), dry_run=False)
    cabinet_body, item_body = dctrack.created
    assert cabinet_body == {
        "tiName": "R09",
        "cmbLocation": "LON1",
        "cmbMake": "APC",
        "cmbModel": "NetShelter SX 42U",
        "cmbStatus": "Planned",
        "cmbRowLabel": "Row A",
        "cmbRowPosition": 3,
    }
    assert item_body["cmbCabinet"] == "R09" and dctrack.item_named("SRV-9")["cmbCabinet"] == "R09"
    assert (result.cabinets_created, result.created, result.errors) == (1, 1, [])


def test_a_refused_cabinet_or_model_fails_the_devices_that_need_it(dctrack, monkeypatch):
    dctrack.add_model("APC", "NetShelter SX 42U", item_class="Cabinet", ru=42)
    doc = document(
        [device("SRV-9", 4, cabinet="R09", rack="r9"), device("SRV-1", 4)],
        cabinets=[cabinet(), cabinet("R09", "r9")],
    )
    monkeypatch.setattr(
        DcTrackClient, "create_model", lambda self, body: (_ for _ in ()).throw(dd.DcTrackError("model refused"))
    )
    result = place(dctrack, doc, cabinet_model=("APC", "NetShelter SX 42U"), dry_run=False)
    assert dctrack.item_named("R09") is not None  # the cabinet was created
    assert result.errors[0] == "model Railyard / Generic 1U Device: dcTrack refused it: model refused"
    assert any("not created: model Railyard / Generic 1U Device was not created" in e for e in result.errors)
    assert result.created == 0 and result.failed


# ---- 0U devices ----------------------------------------------------------------------------------------------------


def test_zero_u_devices_are_created_on_their_side_or_matched_by_name(dctrack):
    dctrack.add_zero_u_item(dctrack.r01, "pdu-b", "Right")
    doc = with_zero_u(
        document([device("SRV-1", 1)]),
        zero_u("PDU-A", "left", serial="PA1"),
        zero_u("PDU-B", "right", serial="PB1"),
    )
    result = place(dctrack, doc)
    rows = by_name(result)
    assert (rows["PDU-A"].placement, rows["PDU-A"].model_name) == (ds.CREATE, "Generic 0U Rack PDU")
    assert rows["PDU-A"].line().startswith("R01 0U left PDU-A: create PDU-A as Railyard / Generic 0U Rack PDU")
    assert (rows["PDU-B"].found_by, rows["PDU-B"].placement, rows["PDU-B"].outcome) == ("name", "", ds.UPDATE)
    assert by_name(result)["SRV-1"].placement == ds.CREATE  # the 0U PDU takes no rack unit
    assert result.skipped == [] and result.only_in_dctrack == []

    result = place(dctrack, doc, dry_run=False)
    pdu = next(c for c in dctrack.created if c["tiName"] == "PDU-A")
    assert (pdu["radioCabinetSide"], pdu["radioDepthPosition"], pdu["cmbUPosition"]) == ("Left", "Back", 1)
    assert "radioRailsUsed" not in pdu
    model = next(m for m in dctrack.created_models if m["model"] == "Generic 0U Rack PDU")
    assert (model["mounting"], model["ruHeight"], model["class"], model["subclass"]) == (
        "ZeroU",
        0,
        "Rack PDU",
        "AC Power",
    )
    assert dctrack.item_named("PDU-B")["tiSerialNumber"] == "PB1" and result.ok


# ---- options and older documents ------------------------------------------------------------------------------


def test_no_placeholders_makes_an_unmatched_model_a_problem(dctrack):
    result = place(dctrack, document([device("SRV-1", 1)]), placeholders=False, dry_run=False)
    assert placed(result) == {"SRV-1": ("", ds.NO_MODEL, "")}
    assert by_name(result)["SRV-1"].note == "Dell / PowerEdge R750: not in dcTrack's Models Library (--no-placeholders)"
    assert dctrack.created == [] and dctrack.created_models == [] and not result.failed


def test_a_custom_placeholder_make(dctrack):
    dctrack.add_make("Acme Generic")
    result = place(dctrack, document([device("SRV-1", 1)]), placeholder_make="Acme Generic")
    assert result.makes == [] and result.models == ["Acme Generic / Generic 1U Device"]


def test_an_older_document_without_the_placement_keys(dctrack):
    doc = document([device("SRV-1", 1, kind=None, serial="S1")])
    assert "zeroUDevices" not in doc and "kind" not in doc["devices"][0] and "row" not in doc["cabinets"][0]
    result = place(dctrack, doc, dry_run=False)
    assert by_name(result)["SRV-1"].model_name == "Generic 1U Device"
    assert dctrack.item_named("SRV-1")["tiSerialNumber"] == "S1" and result.ok


def test_the_model_map_must_have_its_columns(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("make,model\nA,B\n", encoding="utf-8")
    with pytest.raises(dd.PlacementRefused, match="must have the columns railyard_make,railyard_model"):
        dd.read_model_map(str(bad))
    gaps = tmp_path / "gaps.csv"
    gaps.write_text("﻿Railyard_Make,railyard_model,dctrack_make,dctrack_model\nA,B,,D\n", encoding="utf-8")
    with pytest.raises(dd.PlacementRefused, match="line 2: every column needs a value"):
        dd.read_model_map(str(gaps))
    with pytest.raises(dd.PlacementRefused, match="cannot read the model map"):
        dd.read_model_map(str(tmp_path / "missing.csv"))
