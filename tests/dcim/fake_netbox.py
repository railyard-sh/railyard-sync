"""A fake NetBox REST API over the JSON fixtures in ``tests/fixtures/netbox/``.

It serves ``/api/status/``, region details and the list endpoints the loader reads, applying the
filters the loader sends (``site_id``, ``id``, ``device_type_id``, ``slug``, ``name``) and paginating
with ``limit``/``offset`` the way NetBox does, including a ``next`` link on another host (as NetBox
behind a proxy often produces) so the tests can prove the loader never follows it there.
"""

from __future__ import annotations

import json
import pathlib
import re
from collections import Counter
from urllib.parse import urlencode, urlsplit

from conftest import FakeResponse, FakeSession

FIXTURES = pathlib.Path(__file__).parent.parent / "fixtures" / "netbox"
BASE = "https://netbox.example.com"
NEXT_HOST = "http://netbox-internal:8080"  # what a misconfigured proxy puts in ``next``
V2_TOKEN = "nbt_4Kq9zX1bR7.Jf8sP2wL0vM3nC6tY5hA9eD1uG4iO7kQ"
V1_TOKEN = "0123456789abcdef0123456789abcdef01234567"

LIST_ENDPOINTS = [
    "sites",
    "locations",
    "rack-types",
    "racks",
    "device-types",
    "devices",
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
COMPONENT_TYPES = {
    "dcim.interface": "interfaces",
    "dcim.frontport": "front-ports",
    "dcim.rearport": "rear-ports",
    "dcim.powerport": "power-ports",
    "dcim.poweroutlet": "power-outlets",
    "dcim.consoleport": "console-ports",
}
FILTERS = {"site_id", "id", "device_type_id", "slug", "name"}
IGNORED = {"limit", "offset", "exclude"}


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


class FakeNetBox:
    def __init__(self, *, token: str = V2_TOKEN, version: str = "4.6.2", legacy_ports: bool = False):
        self.token = token
        self.status = fixture("status.json") | {"netbox-version": version}
        self.regions = {str(r["id"]): r for r in fixture("regions.json")}
        self.data = {name: fixture(f"{name}.json") for name in LIST_ENDPOINTS}
        if legacy_ports:  # NetBox up to 4.4: front ports carry rear_port + rear_port_position
            for name in ("front-ports", "rear-ports", "front-port-templates", "rear-port-templates"):
                self.data[name] = fixture(f"legacy/{name}.json")
        self.requests: Counter[str] = Counter()
        self.session = FakeSession(self.handle)

    # -- helpers for tests ------------------------------------------------------------------------

    def item(self, endpoint: str, item_id: int) -> dict:
        return next(i for i in self.data[endpoint] if i["id"] == item_id)

    def list_calls(self, endpoint: str) -> list[dict]:
        return [c for c in self.session.calls if urlsplit(c["url"]).path == f"/api/dcim/{endpoint}/"]

    # -- the server -------------------------------------------------------------------------------

    def expected_auth(self) -> str:
        scheme = "Bearer" if self.token.startswith("nbt_") else "Token"
        return f"{scheme} {self.token}"

    def handle(self, method, url, headers, params):
        assert method == "GET"
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}" == BASE, f"request left the configured NetBox: {url}"
        if headers.get("Authorization") != self.expected_auth():
            return FakeResponse(403, {"detail": "Invalid v2 token"})
        path = parts.path
        self.requests[path] += 1
        if path == "/api/status/":
            return FakeResponse(200, self.status)
        if m := re.fullmatch(r"/api/dcim/regions/(\d+)/", path):
            region = self.regions.get(m.group(1))
            return FakeResponse(200, region) if region else FakeResponse(404, {"detail": "No Region matches."})
        if m := re.fullmatch(r"/api/dcim/([a-z-]+)/", path):
            if m.group(1) in self.data:
                return self.list(m.group(1), path, params)
        return FakeResponse(404, {"detail": "Not found."})

    def list(self, endpoint: str, path: str, params) -> FakeResponse:
        pairs = [(k, str(v)) for k, v in (params.items() if isinstance(params, dict) else params or [])]
        filters: dict[str, set[str]] = {}
        for key, value in pairs:
            assert key in FILTERS | IGNORED, f"unexpected query parameter {key!r} on {endpoint}"
            if key in FILTERS:
                filters.setdefault(key, set()).add(value)
        items = [i for i in self.data[endpoint] if all(self.matches(endpoint, i, k, v) for k, v in filters.items())]
        query = dict(pairs)
        limit, offset = int(query.get("limit", 50)), int(query.get("offset", 0))
        results = items[offset : offset + limit]
        nxt = None
        if offset + limit < len(items):
            kept = [(k, v) for k, v in pairs if k not in ("limit", "offset")]
            nxt = f"{NEXT_HOST}{path}?{urlencode(kept + [('limit', limit), ('offset', offset + limit)])}"
        return FakeResponse(200, {"count": len(items), "next": nxt, "previous": None, "results": results})

    def matches(self, endpoint: str, item: dict, key: str, values: set[str]) -> bool:
        if key == "id":
            return str(item["id"]) in values
        if key in ("slug", "name"):
            return str(item.get(key)) in values
        if key == "device_type_id":
            return item.get("device_type") is not None and str(item["device_type"]["id"]) in values
        if key == "site_id":
            return bool(self.sites_of(endpoint, item) & values)
        raise AssertionError(key)

    def sites_of(self, endpoint: str, item: dict) -> set[str]:
        if endpoint == "sites":
            return {str(item["id"])}
        if endpoint in ("locations", "racks", "devices", "power-panels"):
            return {str(item["site"]["id"])}
        if endpoint == "power-feeds":
            return self.sites_of("power-panels", self.item("power-panels", item["power_panel"]["id"]))
        if endpoint in COMPONENT_TYPES.values():
            return self.sites_of("devices", self.item("devices", item["device"]["id"]))
        if endpoint == "cables":
            sites: set[str] = set()
            for end in item["a_terminations"] + item["b_terminations"]:
                if end["object_type"] in COMPONENT_TYPES:
                    sites |= self.sites_of(COMPONENT_TYPES[end["object_type"]], end["object"])
                elif end["object_type"] == "dcim.powerfeed":
                    sites |= self.sites_of("power-feeds", self.item("power-feeds", end["object_id"]))
            return sites
        raise AssertionError(endpoint)
