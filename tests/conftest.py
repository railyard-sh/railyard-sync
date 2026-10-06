"""Shared fixtures for the core suite. No NetBox needed — the client gets an injected fake session,
and the device-type library gets an injected fetcher, so nothing touches the network."""

from __future__ import annotations

import json
import pathlib
from collections.abc import Callable

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

# tests/integration needs a live NetBox (it runs under NetBox's own test runner; see its README).
collect_ignore = ["integration"]


# ---- fake HTTP session for RailyardClient ---------------------------------


class FakeResponse:
    def __init__(self, status: int, payload=None, text: str = ""):
        self.status_code = status
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        return self._payload


class FakeSession:
    """Records requests and returns whatever ``handler(method, url, headers, params)`` yields."""

    def __init__(self, handler: Callable[[str, str, dict, dict | None], FakeResponse]):
        self.handler = handler
        self.calls: list[dict] = []

    def request(self, method, url, headers=None, params=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": headers or {}, "params": params})
        return self.handler(method, url, headers or {}, params)


@pytest.fixture
def fake_session_factory():
    return lambda handler: FakeSession(handler)


# ---- project fixtures -----------------------------------------------------


@pytest.fixture
def example_project() -> dict:
    return json.loads((FIXTURES / "example-project.json").read_text())


@pytest.fixture
def cabled_project() -> dict:
    """A small project that exercises the cabling planner: a switch with real ports, a port-less
    server (synthesized interface), and a 0U PDU fed by a power link."""
    return {
        "schemaVersion": "1",
        "id": "prj_cable",
        "name": "Cabling fixture",
        "dataCentres": [{"id": "dc1", "name": "DC1", "status": "Active"}],
        "rows": [{"id": "row1", "name": "A", "dcId": "dc1", "rackIds": ["r1"]}],
        "racks": [
            {
                "id": "r1",
                "name": "R1",
                "uHeight": 42,
                "widthMm": 800,  # -> 23"
                "rowId": "row1",
                "dcId": "dc1",
                "status": "Active",
                "placements": [
                    {
                        "id": "p_sw",
                        "startU": 10,
                        "heightU": 1,
                        "face": "front",
                        "label": "SW-1",
                        "deviceTypeRef": "sw",
                        "role": "leaf",
                    },
                    {
                        "id": "p_srv",
                        "startU": 1,
                        "heightU": 2,
                        "face": "front",
                        "label": "SRV-1",
                        "deviceTypeRef": "srv",
                        "role": "compute",
                    },
                    {
                        "id": "p_pdu",
                        "startU": 0,
                        "heightU": 0,
                        "face": "front",
                        "mount": "zeroU",
                        "side": "left",
                        "label": "PDU-1",
                        "deviceTypeRef": "pdu",
                        "role": "power",
                    },
                ],
            }
        ],
        "catalogue": [
            {
                "key": "sw",
                "manufacturer": "Acme",
                "model": "SW1",
                "uHeight": 1,
                "fullDepth": True,
                "ports": {
                    "front": 2,
                    "passThrough": False,
                    "media": "SFP+",
                    "frontPorts": [{"name": "Eth1", "type": "SFP+"}, {"name": "Eth2", "type": "SFP+"}],
                },
            },
            {"key": "srv", "manufacturer": "Acme", "model": "SRV1", "uHeight": 2, "fullDepth": True},
            {
                "key": "pdu",
                "manufacturer": "Acme",
                "model": "PDU1",
                "uHeight": 0,
                "fullDepth": False,
                "outlets": {"count": 2, "type": "C13", "feeds": 1},
            },
        ],
        "cables": [
            {
                "id": "c1",
                "a": {"rackId": "r1", "placementId": "p_sw", "side": "front", "portIndex": 1},
                "b": {"rackId": "r1", "placementId": "p_srv"},
                "media": "SFP+",
                "label": "L1",
            }
        ],
        "powerLinks": [
            {
                "id": "pw1",
                "device": {"rackId": "r1", "placementId": "p_srv"},
                "pdu": {"rackId": "r1", "placementId": "p_pdu", "outlet": 1},
            }
        ],
    }
