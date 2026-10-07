"""A fake Nautobot 2.4 REST API over the JSON fixtures in ``tests/fixtures/nautobot/``.

The fixtures are what a real Nautobot 2.4 returned for a small estate (``/api/dcim/<endpoint>/?depth=…``, results
only): Europe → United Kingdom → LDN1 (a Site) → Building A → Data Hall 1 (a Room), and MAN1, another site whose
objects must not be read. The fake serves ``/api/status/`` and the list endpoints the loader reads, applying the
filters it sends the way Nautobot does — ``location`` on racks, devices, power panels and feeds includes the
location's descendants (a tree filter), on components it is the device's own location, ``location_id`` on cables
is either end's device's location — and paginates with ``limit``/``offset``, with a ``next`` link on another host
(as Nautobot behind a proxy often produces) so the tests can prove the loader never follows it there.
"""

from __future__ import annotations

import json
import pathlib
from collections import Counter
from urllib.parse import urlencode, urlsplit

from conftest import FakeResponse, FakeSession

FIXTURES = pathlib.Path(__file__).parent.parent / "fixtures" / "nautobot"
BASE = "https://nautobot.example.com"
NEXT_HOST = "http://nautobot-internal:8080"
TOKEN = "0f" * 20  # a fake Nautobot API token (40 hex characters)

LIST_ENDPOINTS = [
    "locations",
    "location-types",
    "racks",
    "devices",
    "device-types",
    "interface-templates",
    "front-port-templates",
    "rear-port-templates",
    "power-port-templates",
    "power-outlet-templates",
    "console-port-templates",
    "interfaces",
    "front-ports",
    "rear-ports",
    "power-ports",
    "power-outlets",
    "console-ports",
    "cables",
    "power-panels",
    "power-feeds",
]
COMPONENTS = {"interfaces", "front-ports", "rear-ports", "power-ports", "power-outlets", "console-ports"}
TEMPLATES = {e for e in LIST_ENDPOINTS if e.endswith("-templates")}
TREE_FILTERED = {"racks", "devices", "power-panels", "power-feeds"}
FILTERS = {"location", "location_id", "id", "device_type"}
IGNORED = {"limit", "offset", "depth"}

# Ids in the fixtures, for the tests.
EUROPE = "11851469-e125-4045-963a-5736d1948c79"
UK = "6936d14e-0b64-45bc-976a-cb0d64bf7401"
LDN1 = "7e3d2ecd-a86a-4482-8644-83d05ef339ed"
BUILDING_A = "3d404b8b-1431-4623-8527-c4f3bcefb488"
HALL_1 = "c024d50f-198f-45cc-b060-f16a1068591d"
MAN1 = "bde7e159-2008-45ca-82a7-29ba1fdb9ec0"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def _id(ref) -> str | None:
    return ref.get("id") if isinstance(ref, dict) else ref


class FakeNautobot:
    def __init__(self, *, token: str = TOKEN, version: str = "2.4.43"):
        self.token = token
        self.status = fixture("status.json") | {"nautobot-version": version}
        self.data = {name: fixture(f"{name}.json") for name in LIST_ENDPOINTS}
        self.requests: Counter[str] = Counter()
        self.session = FakeSession(self.handle)

    # -- helpers for tests ------------------------------------------------------------------------

    def item(self, endpoint: str, item_id: str) -> dict:
        return next(i for i in self.data[endpoint] if i["id"] == item_id)

    def named(self, endpoint: str, name: str) -> dict:
        return next(i for i in self.data[endpoint] if i.get("name") == name)

    def list_calls(self, endpoint: str) -> list[dict]:
        return [c for c in self.session.calls if urlsplit(c["url"]).path == f"/api/dcim/{endpoint}/"]

    # -- the server -------------------------------------------------------------------------------

    def handle(self, method, url, headers, params):
        assert method == "GET"
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}" == BASE, f"request left the configured Nautobot: {url}"
        if headers.get("Authorization") != f"Token {self.token}":
            return FakeResponse(403, {"detail": "Invalid token"})
        path = parts.path
        self.requests[path] += 1
        if path == "/api/status/":
            return FakeResponse(200, self.status)
        endpoint = path.removeprefix("/api/dcim/").rstrip("/")
        if path.startswith("/api/dcim/") and endpoint in self.data:
            return self.list(endpoint, path, params)
        return FakeResponse(404, {"detail": "Not found."})

    def list(self, endpoint: str, path: str, params) -> FakeResponse:
        pairs = [(k, str(v)) for k, v in (params.items() if isinstance(params, dict) else params or [])]
        filters: dict[str, set[str]] = {}
        for key, value in pairs:
            assert key in FILTERS | IGNORED, f"unexpected query parameter {key!r} on {endpoint}"
            if key in FILTERS:
                filters.setdefault(key, set()).add(value)
        depth = dict(pairs).get("depth", "0")
        expected = "0" if endpoint in COMPONENTS | TEMPLATES else "1"
        assert depth == expected, f"{endpoint} read at depth {depth}, expected {expected}"
        items = [i for i in self.data[endpoint] if all(self.matches(endpoint, i, k, v) for k, v in filters.items())]
        query = dict(pairs)
        limit, offset = int(query.get("limit", 50)), int(query.get("offset", 0))
        results = items[offset : offset + limit]
        nxt = None
        if offset + limit < len(items):
            kept = [(k, v) for k, v in pairs if k not in ("limit", "offset")]
            nxt = f"{NEXT_HOST}{path}?{urlencode(kept + [('limit', limit), ('offset', offset + limit)])}"
        return FakeResponse(200, {"count": len(items), "next": nxt, "previous": None, "results": results})

    # -- filters ----------------------------------------------------------------------------------

    def subtree(self, loc_id: str) -> set[str]:
        out = {loc_id}
        changed = True
        while changed:
            changed = False
            for loc in self.data["locations"]:
                if _id(loc.get("parent")) in out and loc["id"] not in out:
                    out.add(loc["id"])
                    changed = True
        return out

    def device_location(self, device_id: str | None) -> str | None:
        device = next((d for d in self.data["devices"] if d["id"] == device_id), None)
        return _id(device["location"]) if device else None

    def matches(self, endpoint: str, item: dict, key: str, values: set[str]) -> bool:
        if key == "id":
            return item["id"] in values
        if key == "device_type":
            return _id(item.get("device_type")) in values
        if key == "location" and endpoint in TREE_FILTERED:
            wanted = set().union(*(self.subtree(v) for v in values))
            if endpoint == "power-feeds":
                panel = self.item("power-panels", _id(item["power_panel"]))
                return _id(panel["location"]) in wanted
            return _id(item.get("location")) in wanted
        if key == "location" and endpoint in COMPONENTS:
            return self.device_location(_id(item.get("device"))) in values
        if key == "location_id" and endpoint == "cables":
            ends = [item.get("termination_a") or {}, item.get("termination_b") or {}]
            return any(self.device_location(_id(end.get("device"))) in values for end in ends)
        raise AssertionError(f"{key} on {endpoint}")
