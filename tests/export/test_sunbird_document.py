"""The ``sunbird-sync`` document's envelope and rows (``export/sunbird_document.py``)."""

from __future__ import annotations

import pytest
from export.sunbird_docs import cabinet, device, document

from railyard_sync.export.sunbird_document import SunbirdDocumentError, parse_document


def test_reads_cabinets_devices_skipped_and_warnings():
    doc = parse_document(
        document(
            [device("SRV-01", 12, serial=" ABC123 ", face="full", height=2)],
            skipped=[
                {"cabinet": "R01", "name": "PDU-A", "reason": "zero-u", "railyard": {"kind": "device", "id": "p9"}}
            ],
            warnings=["2 devices have no device type"],
        )
    )
    assert (doc.project_id, doc.project_name, doc.revision) == ("p1", "LON1 build", 42)
    (cab,) = doc.cabinets
    assert (cab.name, cab.where, cab.rack_id) == ("R01", "London / LON1 / Hall A / Row 1", "r1")
    (dev,) = doc.devices
    assert dev.cabinet is cab
    assert (dev.u_from_bottom, dev.u_position, dev.u_height, dev.face, dev.serial) == (12, 12, 2, "full", "ABC123")
    assert doc.skipped[0].reason == "zero-u" and doc.warnings == ["2 devices have no device type"]


def test_a_device_is_tied_to_its_cabinet_by_rack_id_when_two_cabinets_share_a_name():
    doc = parse_document(
        document(
            [device("A", 1, rack="r1"), device("B", 1, rack="r2")],
            cabinets=[cabinet("R01", "r1"), cabinet("R01", "r2", location=["London", "LON1", "Hall B"])],
        )
    )
    assert [d.cabinet.where for d in doc.devices] == ["London / LON1 / Hall A / Row 1", "London / LON1 / Hall B"]


def test_a_cabinet_the_document_does_not_list_is_warned_about_once():
    doc = parse_document(document([device("A", 1, cabinet="R09", rack="r9"), device("B", 2, cabinet="R09", rack="r9")]))
    assert doc.devices[0].cabinet is doc.devices[1].cabinet
    assert doc.devices[0].cabinet.location == ()
    assert len(doc.warnings) == 1 and "'R09', which the document does not list" in doc.warnings[0]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"format": "railyard-netbox-sync"}, "Not a Railyard Sunbird sync document: format is 'railyard-netbox-sync'"),
        ({"format": None}, "format is None"),
        ({"version": 2}, "version 2 is not supported"),
        ({"version": "1"}, "version '1' is not supported"),
        ({"version": True}, "version True is not supported"),
        ({"project": {"name": "x"}}, "names no project id"),
        ({"devices": {"a": 1}}, "'devices' must be a list of objects"),
    ],
)
def test_a_document_this_version_cannot_read_is_refused(change, message):
    with pytest.raises(SunbirdDocumentError) as e:
        parse_document({**document([]), **change})
    assert message in str(e.value)


def test_an_unknown_face_is_refused():
    with pytest.raises(SunbirdDocumentError, match="face 'left' is not one of front, rear, full"):
        parse_document(document([device("A", 1, face="left")]))


def test_not_an_object_is_refused():
    with pytest.raises(SunbirdDocumentError, match="must be a JSON object"):
        parse_document([])
