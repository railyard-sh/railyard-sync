"""``sync_serials_to_dctrack`` against the in-memory dcTrack (``fake_dctrack_rest.py``): matching by cabinet and U,
the serial plan, the bulk apply and the File Import CSV, and the dcTrack client's auth and wording."""

from __future__ import annotations

import base64
import csv
import logging

import pytest
from export.fake_dctrack_rest import FakeDcTrack
from export.sunbird_docs import cabinet, device, document

from railyard_sync.export import dctrack_serials as ds
from railyard_sync.export.dctrack_rest import DcTrackAuthError, DcTrackClient, DcTrackError, import_object


@pytest.fixture
def dctrack() -> FakeDcTrack:
    fake = FakeDcTrack()
    fake.add_location("LON1")
    return fake


def client(fake: FakeDcTrack, **kwargs) -> DcTrackClient:
    return DcTrackClient(fake.URL, user=fake.USER, password=fake.PASSWORD, session=fake, **kwargs)


def sync(fake: FakeDcTrack, doc: dict, **kwargs) -> ds.SerialSyncResult:
    kwargs.setdefault("location", "LON1")
    return ds.sync_serials_to_dctrack(doc, client(fake), **kwargs)


def by_name(result: ds.SerialSyncResult) -> dict[str, ds.PlannedDevice]:
    return {d.name: d for d in result.devices}


def outcomes(result: ds.SerialSyncResult) -> dict[str, tuple[str, str]]:
    return {d.name: (d.outcome, d.reason) for d in result.devices}


# ---- matching ----------------------------------------------------------------------------------------------------


def test_bottom_u_is_matched_by_default_and_the_racks_own_u_with_match_u_own(dctrack):
    cab = dctrack.add_cabinet("LON1", "R01")
    bottom = dctrack.add_item(cab, "srv-at-12", 12)
    own = dctrack.add_item(cab, "srv-at-30", 30)
    doc = document([device("SRV-01", 12, own=30, serial="ABC123")])  # a top-down rack: U30 is 12th from the bottom

    result = sync(dctrack, doc)
    assert by_name(result)["SRV-01"].item_id == str(bottom)
    assert by_name(result)["SRV-01"].outcome == ds.UPDATE

    result = sync(dctrack, doc, match_u=ds.MATCH_OWN)
    assert by_name(result)["SRV-01"].item_id == str(own) and by_name(result)["SRV-01"].u == 30


def test_the_face_must_suit_the_items_rails(dctrack):
    cab = dctrack.add_cabinet("LON1", "R01")
    front = dctrack.add_item(cab, "front-10", 10, rails="Front")
    back = dctrack.add_item(cab, "back-10", 10, rails="Back")
    dctrack.add_item(cab, "back-20", 20, rails="Back")
    dctrack.add_item(cab, "back-30", 30, rails="Back")
    result = sync(
        dctrack,
        document(
            [
                device("F", 10, serial="S1", face="front"),
                device("R", 10, serial="S2", face="rear"),
                device("FULL", 20, serial="S3", face="full"),
                device("F30", 30, serial="S4", face="front"),
            ]
        ),
    )
    devices = by_name(result)
    assert devices["F"].item_id == str(front) and devices["R"].item_id == str(back)
    assert devices["FULL"].outcome == ds.UPDATE
    assert (devices["F30"].outcome, devices["F30"].note) == (ds.NO_ITEM, "no dcTrack item at U30 (front) in R01")


def test_several_items_at_the_position_are_ambiguous(dctrack):
    cab = dctrack.add_cabinet("LON1", "R01")
    dctrack.add_item(cab, "a", 5)
    dctrack.add_item(cab, "b", 5)
    dctrack.add_item(cab, "front-9", 9, rails="Front")
    dctrack.add_item(cab, "back-9", 9, rails="Back")
    result = sync(dctrack, document([device("X", 5, serial="S1"), device("FULL", 9, serial="S2", face="full")]))
    assert outcomes(result) == {"X": (ds.AMBIGUOUS, ds.SEVERAL_ITEMS), "FULL": (ds.AMBIGUOUS, ds.SEVERAL_ITEMS)}
    assert "2 dcTrack items at U5 (front): A, B" in by_name(result)["X"].note
    assert result.failed and dctrack.writes == []


def test_two_railyard_devices_on_one_item_are_both_ambiguous(dctrack):
    cab = dctrack.add_cabinet("LON1", "R01")
    dctrack.add_item(cab, "shared", 7)
    dctrack.add_item(cab, "other", 8)
    result = sync(
        dctrack,
        document(
            [
                device("FRONT", 7, serial="S1", face="front"),
                device("REAR", 7, serial="S2", face="rear"),
                device("BLANK", 8, serial=""),
                device("ALSO-8", 8, serial="S3", face="full"),
            ]
        ),
        dry_run=False,
    )
    assert outcomes(result) == {
        "FRONT": (ds.AMBIGUOUS, ds.SAME_ITEM),
        "REAR": (ds.AMBIGUOUS, ds.SAME_ITEM),
        "BLANK": (ds.NO_SERIAL, ""),  # it still makes the item ambiguous for the device that has a serial
        "ALSO-8": (ds.AMBIGUOUS, ds.SAME_ITEM),
    }
    assert dctrack.writes == [] and result.failed


def test_a_height_mismatch_is_a_conflict_that_is_never_written(dctrack):
    cab = dctrack.add_cabinet("LON1", "R01")
    item = dctrack.add_item(cab, "srv", 3, height=1)
    result = sync(dctrack, document([device("SRV", 3, serial="S1", height=2)]), dry_run=False, overwrite=True)
    assert outcomes(result) == {"SRV": (ds.CONFLICT, ds.HEIGHT_MISMATCH)}
    assert "2U in Railyard, 1U in dcTrack" in by_name(result)["SRV"].line()
    assert dctrack.serial(item) == "" and result.failed


def test_a_cabinet_dctrack_does_not_have_and_a_device_without_a_position(dctrack):
    cab = dctrack.add_cabinet("LON1", "R010")  # a quick search's "contains" match must not take it for R01
    dctrack.add_item(cab, "srv", 1)
    result = sync(dctrack, document([device("A", 1, serial="S1"), device("B", None, serial="S2")]))
    assert outcomes(result)["A"] == (ds.CABINET_NOT_FOUND, "")
    assert by_name(result)["A"].note == "no cabinet named 'R01' in dcTrack location LON1"
    assert not result.failed  # reported, but not a conflict

    dctrack.add_cabinet("LON1", "r01")
    result = sync(dctrack, document([device("B", None, serial="S2")]))
    assert outcomes(result)["B"] == (ds.NO_POSITION, "")


def test_the_deepest_mapped_space_wins_then_location_then_unmapped(dctrack):
    for code in ("ROW1", "HALLA"):
        dctrack.add_location(code)
    row1 = dctrack.add_cabinet("ROW1", "R01")
    halla = dctrack.add_cabinet("HALLA", "R02")
    lon1 = dctrack.add_cabinet("LON1", "R03")
    for cab in (row1, halla, lon1):
        dctrack.add_item(cab, f"srv-{cab}", 1)
    cabinets = [
        cabinet("R01", "r1", ["London", "LON1", "Hall A", "Row 1"]),
        cabinet("R02", "r2", ["London", "LON1", "Hall A", "Row 2"]),
        cabinet("R03", "r3", ["London", "LON1", "Hall B"]),
        cabinet("R04", "r4", ["Paris", "PAR1"]),
    ]
    devices = [device(f"D{n}", 1, serial=f"S{n}", cabinet=f"R0{n}", rack=f"r{n}") for n in range(1, 5)]
    location_map = ds.parse_location_map(["Hall A=HALLA", "row 1=ROW1", "PAR1=NOPE"])

    result = sync(dctrack, document(devices, cabinets), location="LON1", location_map=location_map)
    found = by_name(result)
    assert [found[f"D{n}"].location for n in (1, 2, 3)] == ["ROW1", "HALLA", "LON1"]
    assert all(found[f"D{n}"].outcome == ds.UPDATE for n in (1, 2, 3))
    assert found["D4"].outcome == ds.LOCATION_NOT_FOUND
    assert any("no location with code 'NOPE'" in w for w in result.warnings)

    # Without the Row 1 mapping, R01 is looked for in HALLA (dcTrack has it in ROW1); without --location, the
    # cabinets under no mapped space are unmapped.
    hall_only = ds.parse_location_map(["Hall A=HALLA"])
    result = sync(dctrack, document(devices, cabinets), location="", location_map=hall_only)
    assert [d.outcome for d in result.devices] == [ds.CABINET_NOT_FOUND, ds.UPDATE, ds.UNMAPPED, ds.UNMAPPED]
    assert "no --location-map entry for its spaces (London / LON1 / Hall B)" in result.devices[2].note


def test_a_location_map_entry_needs_a_name_and_a_code():
    with pytest.raises(ValueError, match="must be 'Railyard space name=dcTrack location code'"):
        ds.parse_location_map(["Hall A"])
    assert ds.parse_location_map([" Hall=A = H-A "]) == {"hall=a": "H-A"}


# ---- planning ----------------------------------------------------------------------------------------------------


@pytest.fixture
def rack(dctrack):
    """R01 with an item at U1..U5 holding the serials '', 'SAME', 'OLD', 'OLD', 'KEEP'."""
    cab = dctrack.add_cabinet("LON1", "R01")
    return [
        dctrack.add_item(cab, f"item-{u}", u, serial=s) for u, s in enumerate(["", "SAME", "OLD", "OLD", "KEEP"], 1)
    ]


PLAN = [
    device("NEW", 1, serial="N1"),
    device("SAME", 2, serial=" SAME "),
    device("CASE", 3, serial="old"),
    device("DIFF", 4, serial="D4"),
    device("NONE", 5, serial=""),
]


def test_the_plan_unchanged_update_conflict_and_no_serial(dctrack, rack):
    result = sync(dctrack, document(PLAN))
    assert outcomes(result) == {
        "NEW": (ds.UPDATE, ""),
        "SAME": (ds.UNCHANGED, ""),
        "CASE": (ds.CONFLICT, ds.SERIAL_DIFFERS),  # compared case-sensitively
        "DIFF": (ds.CONFLICT, ds.SERIAL_DIFFERS),
        "NONE": (ds.NO_SERIAL, ""),
    }
    assert result.counts == {
        **dict.fromkeys(ds.OUTCOMES, 0),
        "update": 1,
        "unchanged": 1,
        "conflict": 2,
        "no-serial": 1,
    }
    assert by_name(result)["NEW"].line() == "R01 U1 NEW → dcTrack ITEM-1: (blank) → N1"
    assert by_name(result)["DIFF"].line() == (
        "R01 U4 DIFF → dcTrack ITEM-4: OLD → D4 [conflict, serial-differs: kept; --overwrite replaces it]"
    )
    assert result.dry_run and dctrack.writes == [] and result.failed


def test_overwrite_updates_a_differing_serial_and_never_clears_one(dctrack, rack):
    result = sync(dctrack, document(PLAN), overwrite=True, dry_run=False)
    assert [d.outcome for d in result.devices] == [ds.UPDATE, ds.UNCHANGED, ds.UPDATE, ds.UPDATE, ds.NO_SERIAL]
    assert [dctrack.serial(i) for i in rack] == ["N1", "SAME", "old", "D4", "KEEP"]
    assert result.updated == 3 and not result.failed
    assert "(replaces dcTrack's serial: --overwrite)" in by_name(result)["DIFF"].line()


def test_cabinets_without_a_serial_to_write_are_not_read(dctrack):
    dctrack.add_cabinet("LON1", "R01")
    result = sync(dctrack, document([device("A", 1), device("B", 2)]))
    assert [d.outcome for d in result.devices] == [ds.NO_SERIAL, ds.NO_SERIAL]
    assert dctrack.calls == []


def test_a_serial_the_search_leaves_out_is_read_from_the_item(dctrack, rack):
    dctrack.serials_in_search = False
    result = sync(dctrack, document(PLAN))
    assert outcomes(result)["SAME"] == (ds.UNCHANGED, "")
    reads = [c["url"] for c in dctrack.calls if "/dcimoperations/items/" in c["url"]]
    assert len(reads) == 4  # one per matched device with a serial


# ---- apply ---------------------------------------------------------------------------------------------------------


def test_apply_writes_in_batches_and_reports_each_row_dctrack_refuses(dctrack):
    cab = dctrack.add_cabinet("LON1", "R01")
    items = [dctrack.add_item(cab, f"srv-{u}", u) for u in (1, 2, 3)]
    dctrack.refuse[items[1]] = "Serial number SN2 is not valid"
    doc = document([device(f"D{u}", u, serial=f"SN{u}") for u in (1, 2, 3)])
    result = sync(dctrack, doc, dry_run=False, batch_size=2)

    bulk = [c for c in dctrack.calls if c["url"].endswith("/items/bulk")]
    assert len(bulk) == 2
    assert bulk[0]["json"] == {
        "bodies": [{"id": items[0], "tiSerialNumber": "SN1"}, {"id": items[1], "tiSerialNumber": "SN2"}],
        "method": "PUT",
        "proceedOnWarning": True,
    }
    assert [dctrack.serial(i) for i in items] == ["SN1", "", "SN3"]
    assert result.updated == 2 and not result.ok and result.failed
    (error,) = result.errors
    assert error == "R01 U2 D2 → dcTrack SRV-2: (blank) → SN2 — dcTrack refused it: Serial number SN2 is not valid"
    assert [d.applied for d in result.devices] == [True, False, True]


def test_a_batch_dctrack_fails_as_a_whole_fails_each_of_its_rows(dctrack, rack, monkeypatch):
    def refuse(self, rows):
        raise DcTrackError("dcTrack failed (HTTP 500) for POST /api/v2/dcimoperations/items/bulk", status=500)

    monkeypatch.setattr(DcTrackClient, "bulk_update", refuse)
    result = sync(dctrack, document(PLAN), dry_run=False)
    assert result.updated == 0 and len(result.errors) == 1 and "HTTP 500" in result.errors[0]


# ---- the File Import CSV --------------------------------------------------------------------------------------------


def test_the_import_file_holds_the_updates_with_formulas_escaped(dctrack, tmp_path):
    cab = dctrack.add_cabinet("LON1", "R01")
    dctrack.add_item(cab, "sw-1", 1, item_class="Network")
    dctrack.add_item(cab, "pdu-1", 2, item_class="Rack PDU")
    dctrack.add_item(cab, "srv-3", 3)
    dctrack.add_item(cab, "srv-4", 4, serial="SAME")
    path = tmp_path / "import.csv"
    doc = document(
        [
            device("SW", 1, serial="=HYPERLINK(1)"),
            device("PDU", 2, serial="-12"),
            device("SRV", 3, serial="Ünïcode-1"),
            device("UNCHANGED", 4, serial="SAME"),
        ]
    )
    result = sync(dctrack, doc, import_file=str(path))
    assert result.import_rows == 3 and dctrack.writes == []
    with open(path, encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows == [
        ["# Operation", "Object", "Location", "Name", "Serial Number"],
        ["EDIT", "Network", "LON1", "SW-1", "'=HYPERLINK(1)"],
        ["EDIT", "Rack PDU", "LON1", "PDU-1", "'-12"],
        ["EDIT", "Device", "LON1", "SRV-3", "Ünïcode-1"],
    ]
    assert any("2 serials" in w and "leading '" in w for w in result.warnings)


@pytest.mark.parametrize(
    ("text", "cell"),
    [("=1+1", "'=1+1"), ("+1", "'+1"), ("-1", "'-1"), ("@SUM(A1)", "'@SUM(A1)"), ("A=1", "A=1"), ("", "")],
)
def test_safe_cell(text, cell):
    assert ds.safe_cell(text) == cell


def test_import_object_from_the_item_class():
    assert [import_object(c) for c in ("Device / Standard", "network", "Data Panel", "Odd Class", "")] == [
        "Device",
        "Network",
        "Data Panel",
        "Odd Class",
        "",
    ]


# ---- the client ------------------------------------------------------------------------------------------------------


def test_http_is_refused_unless_allowed_and_a_trailing_slash_is_dropped(dctrack):
    with pytest.raises(ValueError, match="must be https"):
        DcTrackClient("http://dctrack.example.com", user="u", password="p")
    assert DcTrackClient("http://dctrack.example.com", user="u", password="p", allow_http=True).url.startswith("http:")
    assert client(dctrack).url == FakeDcTrack.URL
    assert DcTrackClient(FakeDcTrack.URL + "/", user="u", password="p").url == FakeDcTrack.URL


def test_basic_auth_on_every_call_and_tls_options_passed_through(dctrack):
    cab = dctrack.add_cabinet("LON1", "R01")
    dctrack.add_item(cab, "srv", 1)
    ds.sync_serials_to_dctrack(
        document([device("A", 1, serial="S")]),
        client(dctrack, verify="/etc/ssl/private-ca.pem"),
        location="LON1",
    )
    basic = "Basic " + base64.b64encode(f"{dctrack.USER}:{dctrack.PASSWORD}".encode()).decode()
    assert dctrack.calls and all(c["headers"]["Authorization"] == basic for c in dctrack.calls)
    assert all(c["verify"] == "/etc/ssl/private-ca.pem" for c in dctrack.calls)
    assert all(not c["url"].endswith("/") for c in dctrack.calls)


def test_oauth2_client_credentials_get_a_bearer_token(dctrack, caplog):
    caplog.set_level(logging.DEBUG, logger="railyard_sync")
    oauth = DcTrackClient(
        dctrack.URL, client_id=dctrack.CLIENT_ID, client_secret=dctrack.CLIENT_SECRET, session=dctrack
    )
    assert [loc.code for loc in oauth.locations()] == ["LON1"]
    token_call, read = dctrack.calls
    assert (
        token_call["url"] == f"{dctrack.URL}/oauth/token" and token_call["data"]["grant_type"] == "client_credentials"
    )
    assert read["headers"]["Authorization"] == f"Bearer {dctrack.ACCESS_TOKEN}"
    assert "dcTrack POST /oauth/token -> 200" in caplog.text
    for secret in (dctrack.CLIENT_SECRET, dctrack.ACCESS_TOKEN):
        assert secret not in caplog.text


def test_an_expired_oauth2_token_is_renewed_once(dctrack):
    oauth = DcTrackClient(
        dctrack.URL, client_id=dctrack.CLIENT_ID, client_secret=dctrack.CLIENT_SECRET, session=dctrack
    )
    oauth.locations()
    dctrack.tokens.clear()  # expired
    oauth.locations()
    assert [c["url"].rsplit("/", 1)[-1] for c in dctrack.calls] == [
        "token",
        "locations",
        "locations",
        "token",
        "locations",
    ]


def test_refused_credentials_are_explained_without_dctracks_stack_trace(dctrack):
    bad = DcTrackClient(dctrack.URL, user=dctrack.USER, password="wrong-pass", session=dctrack)
    with pytest.raises(DcTrackAuthError) as e:
        bad.locations()
    message = str(e.value)
    assert (
        "dcTrack did not accept the credentials (HTTP 401) for GET /api/v1/locations: Authentication failed" in message
    )
    assert "--dctrack-user" in message and "java.lang" not in message
    assert "wrong-pass" not in message

    oauth = DcTrackClient(dctrack.URL, client_id="nobody", client_secret="not-it-1234", session=dctrack)
    with pytest.raises(DcTrackAuthError, match="did not give an OAuth2 token to client 'nobody' \\(HTTP 401\\)"):
        oauth.locations()


def test_one_of_user_or_client_and_its_secret_are_required():
    with pytest.raises(ValueError, match="one of them"):
        DcTrackClient(FakeDcTrack.URL, user="u", password="p", client_id="c", client_secret="s")
    with pytest.raises(ValueError, match="DCTRACK_PASSWORD"):
        DcTrackClient(FakeDcTrack.URL, user="u")
