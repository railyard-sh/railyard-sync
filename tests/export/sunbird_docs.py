"""Builders for Railyard ``sunbird-sync`` documents (the contract in ``export/sunbird_document.py``)."""

from __future__ import annotations

LOCATION = ["London", "LON1", "Hall A", "Row 1"]


def cabinet(name: str = "R01", rack_id: str = "r1", location: list[str] | None = None, **extra) -> dict:
    return {
        "name": name,
        "location": list(LOCATION if location is None else location),
        "uHeight": 42,
        "startingUnit": 1,
        "descendingUnits": False,
        "railyard": {"kind": "rack", "id": rack_id},
        **extra,
    }


def device(
    name: str,
    u: int | None,
    *,
    serial: str = "",
    face: str = "front",
    height: int = 1,
    cabinet: str = "R01",
    rack: str = "r1",
    own: int | None = None,
    make: str = "Dell",
    model: str = "PowerEdge R750",
    kind: str | None = "device",
    role: str = "Server",
) -> dict:
    """A racked device whose bottom U (from the cabinet's bottom) is ``u``; ``own`` is its U in the rack's own
    numbering (``u`` unless given)."""
    return {
        "cabinet": cabinet,
        "name": name,
        "uPosition": u if own is None else own,
        "uFromBottom": u,
        "uHeight": height,
        "face": face,
        "make": make,
        "model": model,
        "partNumber": "",
        "serial": serial,
        "railyard": {"kind": "device", "id": f"pl-{name}", "rackId": rack},
        **({} if kind is None else {"kind": kind, "role": role}),
    }


def zero_u(
    name: str,
    side: str = "left",
    *,
    serial: str = "",
    cabinet: str = "R01",
    rack: str = "r1",
    make: str = "APC",
    model: str = "AP8941",
    kind: str = "pdu",
) -> dict:
    """A side-mounted device, for ``zeroUDevices``."""
    return {
        "cabinet": cabinet,
        "name": name,
        "side": side,
        "make": make,
        "model": model,
        "partNumber": "",
        "serial": serial,
        "role": "PDU",
        "kind": kind,
        "railyard": {"kind": "device", "id": f"pl-{name}", "rackId": rack},
    }


def with_zero_u(doc: dict, *devices: dict) -> dict:
    """``doc`` with these 0U devices, also listed in ``skipped`` as Railyard lists them."""
    doc["zeroUDevices"] = list(devices)
    doc["skipped"] = [
        *doc.get("skipped", []),
        *({"cabinet": d["cabinet"], "name": d["name"], "reason": "zero-u", "railyard": d["railyard"]} for d in devices),
    ]
    return doc


def document(devices: list[dict], cabinets: list[dict] | None = None, **extra) -> dict:
    return {
        "format": "railyard-sunbird-sync",
        "version": 1,
        "project": {"id": "p1", "name": "LON1 build", "revision": 42, "scope": {"rackIds": ["r1"]}},
        "cabinets": [cabinet()] if cabinets is None else cabinets,
        "devices": devices,
        "skipped": [],
        "warnings": [],
        **extra,
    }
