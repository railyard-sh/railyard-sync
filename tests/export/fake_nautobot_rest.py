"""An in-memory Nautobot 2.x REST API, just enough of it for the export's tests.

It speaks the request side of ``requests.Session`` and answers like Nautobot 2.4 (checked against a real one):
UUID ids, paginated lists with ``count``/``next``/``results``, nested references as ``{"id", "object_type", "url"}``
at ``depth=0`` and named objects at ``depth=1``, choice fields as ``{"value", "label"}``, tags as nested objects,
``custom_fields`` holding every custom field enabled for the model, and the filters the sync sends — anything else
is refused with 400, as Nautobot's strict filtering does, and so is a filter naming an object that does not exist.

Writes are validated as Nautobot validates them: references must exist, statuses, roles and tags must be enabled
for the model, a location's type must fit its parent's (location types form a tree), a rack or device may only go
in a location whose type is enabled for it, a device in a rack is in the rack's location, an interface needs a
status, a front port its rear port. Deletes follow Nautobot's ``on_delete``: PROTECT answers 409, cascades and
SET_NULLs happen. A new device gets its device type's components (untagged), as Nautobot instantiates templates.

Objects are stored flat, references as bare ids; ``add()`` seeds them directly, as an operator would have.
"""

from __future__ import annotations

import json as jsonlib
import re
import uuid
from collections import defaultdict
from urllib.parse import urlsplit

URL = "https://nautobot.example.com"
TOKEN = "0f" * 20  # a fake Nautobot API token

#: endpoint -> {field: referenced endpoint}
FK = {
    "dcim/location-types": {"parent": "dcim/location-types"},
    "dcim/locations": {"parent": "dcim/locations", "location_type": "dcim/location-types", "status": "extras/statuses"},
    "dcim/manufacturers": {},
    "dcim/device-types": {"manufacturer": "dcim/manufacturers"},
    "dcim/platforms": {"manufacturer": "dcim/manufacturers"},
    "dcim/racks": {"location": "dcim/locations", "status": "extras/statuses", "role": "extras/roles"},
    "dcim/devices": {
        "device_type": "dcim/device-types",
        "role": "extras/roles",
        "status": "extras/statuses",
        "location": "dcim/locations",
        "rack": "dcim/racks",
    },
    "dcim/interfaces": {
        "device": "dcim/devices",
        "status": "extras/statuses",
        "cable": "dcim/cables",
        "parent_interface": "dcim/interfaces",
        "lag": "dcim/interfaces",
        "bridge": "dcim/interfaces",
    },
    "dcim/rear-ports": {"device": "dcim/devices", "cable": "dcim/cables"},
    "dcim/front-ports": {"device": "dcim/devices", "cable": "dcim/cables", "rear_port": "dcim/rear-ports"},
    "dcim/power-ports": {"device": "dcim/devices", "cable": "dcim/cables"},
    "dcim/power-outlets": {"device": "dcim/devices", "cable": "dcim/cables", "power_port": "dcim/power-ports"},
    "dcim/interface-templates": {"device_type": "dcim/device-types"},
    "dcim/rear-port-templates": {"device_type": "dcim/device-types"},
    "dcim/front-port-templates": {"device_type": "dcim/device-types", "rear_port_template": "dcim/rear-port-templates"},
    "dcim/power-port-templates": {"device_type": "dcim/device-types"},
    "dcim/power-outlet-templates": {"device_type": "dcim/device-types"},
    "dcim/device-bays": {"device": "dcim/devices", "installed_device": "dcim/devices"},
    "dcim/power-panels": {"location": "dcim/locations"},
    "dcim/power-feeds": {"power_panel": "dcim/power-panels", "rack": "dcim/racks", "status": "extras/statuses"},
    "dcim/rack-reservations": {"rack": "dcim/racks"},
    "dcim/cables": {"status": "extras/statuses"},
    "ipam/ip-addresses": {},
    "ipam/ip-address-to-interface": {"ip_address": "ipam/ip-addresses", "interface": "dcim/interfaces"},
    "ipam/services": {"device": "dcim/devices"},
    "extras/statuses": {},
    "extras/roles": {},
    "extras/tags": {},
    "extras/custom-fields": {},
}
CONTENT_TYPE = {
    "dcim/location-types": "dcim.locationtype",
    "dcim/locations": "dcim.location",
    "dcim/manufacturers": "dcim.manufacturer",
    "dcim/device-types": "dcim.devicetype",
    "dcim/racks": "dcim.rack",
    "dcim/devices": "dcim.device",
    "dcim/interfaces": "dcim.interface",
    "dcim/rear-ports": "dcim.rearport",
    "dcim/front-ports": "dcim.frontport",
    "dcim/power-ports": "dcim.powerport",
    "dcim/power-outlets": "dcim.poweroutlet",
    "dcim/cables": "dcim.cable",
    "extras/statuses": "extras.status",
    "extras/roles": "extras.role",
    "dcim/platforms": "dcim.platform",
}
TAGGABLE = {
    "dcim/locations",
    "dcim/device-types",
    "dcim/racks",
    "dcim/devices",
    "dcim/interfaces",
    "dcim/rear-ports",
    "dcim/front-ports",
    "dcim/power-ports",
    "dcim/power-outlets",
    "dcim/cables",
}
COMPONENTS = {
    "dcim.interface": "dcim/interfaces",
    "dcim.rearport": "dcim/rear-ports",
    "dcim.frontport": "dcim/front-ports",
    "dcim.powerport": "dcim/power-ports",
    "dcim.poweroutlet": "dcim/power-outlets",
}
TEMPLATES = {
    "dcim/interface-templates": "dcim/interfaces",
    "dcim/rear-port-templates": "dcim/rear-ports",
    "dcim/front-port-templates": "dcim/front-ports",
    "dcim/power-port-templates": "dcim/power-ports",
    "dcim/power-outlet-templates": "dcim/power-outlets",
}
CHOICES = {"type", "width", "face", "feed_leg", "outer_unit"}
UNIQUE = {
    "dcim/location-types": [("name",)],
    "dcim/locations": [("parent", "name")],
    "dcim/manufacturers": [("name",)],
    "dcim/device-types": [("manufacturer", "model")],
    "dcim/racks": [("location", "name")],
    "dcim/devices": [("location", "name")],
    "extras/statuses": [("name",)],
    "extras/roles": [("name",)],
    "extras/tags": [("name",)],
    "extras/custom-fields": [("key",)],
    **{ep: [("device", "name")] for ep in COMPONENTS.values()},
}
#: endpoint -> [(dependent endpoint, field)] whose existence makes a delete fail (Django PROTECT).
PROTECT = {
    "dcim/location-types": [("dcim/locations", "location_type"), ("dcim/location-types", "parent")],
    "dcim/manufacturers": [("dcim/device-types", "manufacturer")],
    "dcim/device-types": [("dcim/devices", "device_type")],
    "extras/roles": [("dcim/devices", "role"), ("dcim/racks", "role")],
    "extras/statuses": [(ep, "status") for ep in ("dcim/locations", "dcim/racks", "dcim/devices", "dcim/interfaces")],
    "dcim/locations": [("dcim/racks", "location"), ("dcim/devices", "location"), ("dcim/power-panels", "location")],
    "dcim/racks": [("dcim/devices", "rack"), ("dcim/power-feeds", "rack")],
}
#: Filters each endpoint accepts beyond name/id/tags/depth/limit/offset and cf_<key> (Nautobot refuses others).
FILTERS = {
    "dcim/location-types": {"parent"},
    "dcim/locations": {"parent", "location_type", "status"},
    "dcim/device-types": {"manufacturer", "model"},
    "dcim/platforms": {"manufacturer"},
    "dcim/racks": {"location", "role", "status"},
    "dcim/devices": {"location", "rack", "role", "status", "device_type", "name__ie"},
    "dcim/interfaces": {"device", "parent_interface", "lag", "bridge", "status"},
    "dcim/rear-ports": {"device"},
    "dcim/front-ports": {"device", "rear_port"},  # rear_port: an exact filter
    "dcim/power-ports": {"device"},
    "dcim/power-outlets": {"device", "power_port"},
    "dcim/device-bays": {"device"},
    "dcim/power-panels": {"location"},
    "dcim/power-feeds": {"rack"},
    "dcim/rack-reservations": {"rack"},
    "dcim/cables": {"device_id", "status"},
    "ipam/ip-addresses": {"device_id"},
    "ipam/ip-address-to-interface": {"interface"},
    "ipam/services": {"device"},
    "extras/tags": {"q"},
}

BUILTIN_STATUSES = {
    "Active": [
        "dcim.location",
        "dcim.rack",
        "dcim.device",
        "dcim.interface",
        "dcim.powerfeed",
        "ipam.ipaddress",
    ],
    "Planned": ["dcim.location", "dcim.rack", "dcim.device", "dcim.interface", "dcim.cable", "dcim.powerfeed"],
    "Connected": ["dcim.cable"],
    "Decommissioning": ["dcim.location", "dcim.device", "dcim.interface", "dcim.cable"],
    "Retired": ["dcim.location"],
}


class Response:
    def __init__(self, status: int, payload=None):
        self.status_code = status
        self._payload = payload
        self.text = "" if payload is None else jsonlib.dumps(payload)
        self.headers: dict = {}

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class Invalid(Exception):
    def __init__(self, status: int, detail):
        super().__init__(str(detail))
        self.status = status
        self.detail = detail


class FakeNautobot:
    URL = URL

    def __init__(self, version: str = "2.4.43", token: str = TOKEN, *, page_size_cap: int = 3):
        self.version = version
        self.token = token
        self.objects: dict[str, dict[str, dict]] = defaultdict(dict)
        self.calls: list[dict] = []
        self.forbid: set[tuple[str, str]] = set()  # (method, endpoint) answered 403
        self.page_size_cap = page_size_cap  # small, so pagination is always exercised
        for name, cts in BUILTIN_STATUSES.items():
            self.add("extras/statuses", name=name, content_types=list(cts), color="4caf50")

    # -- seeding and inspection ------------------------------------------------------------------

    def add(self, endpoint: str, **fields) -> str:
        obj_id = str(uuid.uuid4())
        if endpoint in TAGGABLE:
            fields.setdefault("tags", [])
        fields.setdefault("custom_fields", {})
        self.objects[endpoint][obj_id] = {"id": obj_id, **fields}
        return obj_id

    def all(self, endpoint: str, **match) -> list[dict]:
        return [o for o in self.objects[endpoint].values() if all(o.get(k) == v for k, v in match.items())]

    def one(self, endpoint: str, **match) -> dict:
        found = self.all(endpoint, **match)
        assert len(found) == 1, (endpoint, match, found)
        return found[0]

    def id_of(self, endpoint: str, **match) -> str:
        return self.one(endpoint, **match)["id"]

    def tag(self, name: str) -> dict | None:
        return next((t for t in self.objects["extras/tags"].values() if t["name"] == name), None)

    def tagged(self, endpoint: str, tag_id: str) -> list[dict]:
        return [o for o in self.objects[endpoint].values() if tag_id in o.get("tags", [])]

    def status_id(self, name: str) -> str:
        return self.id_of("extras/statuses", name=name)

    @property
    def writes(self) -> list[dict]:
        return [c for c in self.calls if c["method"] != "GET"]

    # -- the session interface -------------------------------------------------------------------

    def request(self, method, url, headers=None, params=None, json=None, timeout=None, verify=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json})
        if (headers or {}).get("Authorization") != f"Token {self.token}":
            return Response(403, {"detail": "Invalid token"})
        parts = urlsplit(url)
        if f"{parts.scheme}://{parts.netloc}" != self.URL:
            raise AssertionError(f"request sent to another host: {url}")
        path = parts.path
        if path == "/api/status/":
            return Response(200, {"nautobot-version": self.version, "python-version": "3.12.0"})
        m = re.fullmatch(r"/api/([a-z]+/[a-z-]+)/(?:([0-9a-f-]{36})/)?", path)
        if not m or m.group(1) not in FK:
            return Response(404, {"detail": "Not found."})
        endpoint, obj_id = m.group(1), m.group(2)
        if (method, endpoint) in self.forbid:
            return Response(403, {"detail": "You do not have permission to perform this action."})
        query = list(params or [])
        depth = next((int(v) for k, v in query if k == "depth"), 0)
        try:
            if method == "GET" and obj_id is None:
                return Response(200, self._list(endpoint, query, depth))
            if obj_id is not None and obj_id not in self.objects[endpoint]:
                return Response(404, {"detail": "Not found."})
            if method == "GET":
                return Response(200, self._render(endpoint, self.objects[endpoint][obj_id], depth))
            if method == "POST":
                return Response(201, self._render(endpoint, self._create(endpoint, dict(json or {})), 0))
            if method == "PATCH":
                return Response(200, self._render(endpoint, self._patch(endpoint, obj_id, dict(json or {})), 0))
            if method == "DELETE":
                self._delete(endpoint, obj_id)
                return Response(204)
        except Invalid as exc:
            return Response(exc.status, exc.detail)
        return Response(405, {"detail": f'Method "{method}" not allowed.'})

    # -- reads -----------------------------------------------------------------------------------

    def _resolve(self, endpoint: str, value: str) -> str:
        """A filter value naming an object (an id, or a name/model): its id, or 400 as Nautobot answers."""
        objs = self.objects[endpoint]
        if value in objs:
            return value
        found = [o["id"] for o in objs.values() if value in (o.get("name"), o.get("model"))]
        if not found:
            raise Invalid(400, {"filter": [f"Select a valid choice. {value} is not one of the available choices."]})
        return found[0]

    def _matches(self, endpoint: str, obj: dict, key: str, value: str) -> bool:
        if key == "tags":
            return self._resolve("extras/tags", value) in obj.get("tags", [])
        if key.startswith("cf_"):
            return str((obj.get("custom_fields") or {}).get(key[3:]) or "") == value
        if key == "name__ie":
            return str(obj.get("name") or "").lower() == value.lower()
        if key == "q":
            return value.lower() in str(obj.get("name") or "").lower()
        if key == "device_id" and endpoint == "dcim/cables":
            ends = [
                (obj["termination_a_type"], obj["termination_a_id"]),
                (obj["termination_b_type"], obj["termination_b_id"]),
            ]
            return any(
                self.objects[COMPONENTS[ct]].get(oid, {}).get("device") == value for ct, oid in ends if ct in COMPONENTS
            )
        if key == "device_id" and endpoint == "ipam/ip-addresses":
            links = self.all("ipam/ip-address-to-interface", ip_address=obj["id"])
            return any(
                self.objects["dcim/interfaces"].get(link["interface"], {}).get("device") == value for link in links
            )
        if key in FK.get(endpoint, {}):
            target = FK[endpoint][key]
            wanted = self._resolve(target, value)
            if endpoint in ("dcim/racks", "dcim/devices", "dcim/power-panels") and key == "location":
                return obj.get(key) in self._subtree(wanted)  # a tree filter: the location and below it
            return obj.get(key) == wanted
        if key in ("id",):
            return obj["id"] == value
        return str(obj.get(key) if obj.get(key) is not None else "") == value

    def _subtree(self, loc_id: str) -> set[str]:
        out, changed = {loc_id}, True
        while changed:
            changed = False
            for loc in self.objects["dcim/locations"].values():
                if loc.get("parent") in out and loc["id"] not in out:
                    out.add(loc["id"])
                    changed = True
        return out

    def _list(self, endpoint: str, params: list[tuple[str, str]], depth: int) -> dict:
        limit, offset, filters = 50, 0, []
        allowed = {"name", "id", "depth", "limit", "offset"} | FILTERS.get(endpoint, set())
        if endpoint in TAGGABLE:
            allowed.add("tags")
        ct = CONTENT_TYPE.get(endpoint)
        fields = {f["key"] for f in self.objects["extras/custom-fields"].values() if ct in f["content_types"]}
        for key, value in params:
            if key not in allowed and not (key.startswith("cf_") and key[3:] in fields):
                raise Invalid(400, {key: ["Unknown filter field"]})
            if key == "limit":
                limit = int(value)
            elif key == "offset":
                offset = int(value)
            elif key != "depth":
                filters.append((key, str(value)))
        found = [
            o for o in self.objects[endpoint].values() if all(self._matches(endpoint, o, k, v) for k, v in filters)
        ]
        size = min(limit, self.page_size_cap)
        page = found[offset : offset + size]
        more = offset + size < len(found)
        nxt = f"http://nautobot-internal:8080/api/{endpoint}/?limit={size}&offset={offset + size}" if more else None
        return {
            "count": len(found),
            "next": nxt,
            "previous": None,
            "results": [self._render(endpoint, o, depth) for o in page],
        }

    def _ref(self, endpoint: str, obj_id, depth: int) -> dict | None:
        obj = self.objects[endpoint].get(obj_id) if obj_id is not None else None
        if obj is None:
            return None
        if depth <= 0:
            return {
                "id": obj_id,
                "object_type": CONTENT_TYPE.get(endpoint, endpoint),
                "url": f"{URL}/api/{endpoint}/{obj_id}/",
            }
        return self._render(endpoint, obj, depth - 1)

    def _render(self, endpoint: str, obj: dict, depth: int) -> dict:
        out: dict = {"object_type": CONTENT_TYPE.get(endpoint, endpoint), "url": f"{URL}/api/{endpoint}/{obj['id']}/"}
        for key, value in obj.items():
            if key in FK.get(endpoint, {}):
                out[key] = self._ref(FK[endpoint][key], value, depth)
            elif key == "tags":
                out[key] = [self._ref("extras/tags", t, depth) for t in value]
            elif key in CHOICES and endpoint not in ("extras/custom-fields",):
                out[key] = {"value": value, "label": str(value).title()} if value not in (None, "") else None
            elif key in ("termination_a_id", "termination_b_id"):
                side = key[len("termination_") : len("termination_") + 1]
                ct = obj[f"termination_{side}_type"]
                out[key] = value
                out[f"termination_{side}"] = self._ref(COMPONENTS[ct], value, depth) if ct in COMPONENTS else None
            else:
                out[key] = value
        ct = CONTENT_TYPE.get(endpoint)
        if ct is not None:
            fields = [f["key"] for f in self.objects["extras/custom-fields"].values() if ct in f["content_types"]]
            out["custom_fields"] = {key: (obj.get("custom_fields") or {}).get(key) for key in fields}
        if endpoint == "dcim/devices" and "position" not in out:
            out["position"] = None
        return out

    # -- writes ----------------------------------------------------------------------------------

    def _ids(self, endpoint: str, value) -> str | None:
        if value is None or value == "":
            return None
        ref = value.get("id") if isinstance(value, dict) else value
        if ref not in self.objects[endpoint]:
            raise Invalid(400, {"detail": f"Related object not found using the provided attributes: {value}"})
        return ref

    def _check(self, endpoint: str, obj: dict) -> None:
        for fk, target in FK.get(endpoint, {}).items():
            if obj.get(fk) is not None:
                obj[fk] = self._ids(target, obj[fk])
        if "tags" in obj:
            obj["tags"] = [self._ids("extras/tags", t) for t in obj["tags"]]
            ct = CONTENT_TYPE[endpoint]
            for tag_id in obj["tags"]:
                if ct not in self.objects["extras/tags"][tag_id]["content_types"]:
                    raise Invalid(400, {"tags": [f"Related object not found using the provided attributes: {tag_id}"]})
        for fields in UNIQUE.get(endpoint, []):
            key = tuple(obj.get(f) for f in fields)
            for other in self.objects[endpoint].values():
                if other["id"] != obj.get("id") and tuple(other.get(f) for f in fields) == key:
                    raise Invalid(400, {"__all__": [f"{endpoint} with this {', '.join(fields)} already exists."]})
        ct = CONTENT_TYPE.get(endpoint)
        for fk, target in (("status", "extras/statuses"), ("role", "extras/roles")):
            if fk in FK.get(endpoint, {}) and obj.get(fk) is not None:
                if ct not in self.objects[target][obj[fk]]["content_types"]:
                    raise Invalid(400, {fk: [f"{fk} is not enabled for {ct}"]})
        if (
            endpoint in ("dcim/locations", "dcim/racks", "dcim/devices", "dcim/interfaces")
            and obj.get("status") is None
        ):
            raise Invalid(400, {"status": ["This field is required."]})
        if endpoint == "dcim/locations":
            self._check_location(obj)
        if endpoint in ("dcim/racks", "dcim/devices"):
            loc = self.objects["dcim/locations"].get(obj.get("location"))
            if loc is None:
                raise Invalid(400, {"location": ["This field is required."]})
            loc_type = self.objects["dcim/location-types"][loc["location_type"]]
            if ct not in loc_type["content_types"]:
                raise Invalid(400, {"location": [f'{ct} may not associate to locations of type "{loc_type["name"]}".']})
        if endpoint == "dcim/devices" and obj.get("rack"):
            rack = self.objects["dcim/racks"][obj["rack"]]
            if rack["location"] != obj.get("location"):
                raise Invalid(400, {"rack": ["Rack does not belong to the device's location."]})
        if endpoint == "dcim/front-ports":
            rear = self.objects["dcim/rear-ports"].get(obj.get("rear_port"))
            if rear is None or rear["device"] != obj.get("device"):
                raise Invalid(400, {"rear_port": ["This field is required (a rear port on the same device)."]})
            position = int(obj.get("rear_port_position") or 1)
            if position > int(rear.get("positions") or 1):
                raise Invalid(400, {"rear_port_position": [f"Invalid rear port position ({position})"]})
            for other in self.objects[endpoint].values():
                if other["id"] != obj.get("id") and (
                    other.get("rear_port"),
                    int(other.get("rear_port_position") or 1),
                ) == (
                    obj["rear_port"],
                    position,
                ):
                    raise Invalid(
                        400, {"__all__": ["Front port with this Rear port and Rear port position already exists."]}
                    )
        if endpoint == "dcim/rear-ports" and obj.get("id"):
            mapped = [int(f.get("rear_port_position") or 1) for f in self.all("dcim/front-ports", rear_port=obj["id"])]
            if mapped and max(mapped) > int(obj.get("positions") or 1):
                raise Invalid(
                    400, {"positions": ["The number of positions cannot be less than the mapped front ports."]}
                )
        if obj.get("custom_fields"):
            known = {f["key"] for f in self.objects["extras/custom-fields"].values() if ct in f["content_types"]}
            for key in obj["custom_fields"]:
                if key not in known:
                    raise Invalid(400, {"custom_fields": [f"Unknown field name '{key}' in custom field data."]})

    def _check_location(self, obj: dict) -> None:
        loc_type = self.objects["dcim/location-types"][obj["location_type"]]
        parent = self.objects["dcim/locations"].get(obj.get("parent"))
        parent_type = parent["location_type"] if parent else None
        allowed = {loc_type.get("parent")} | ({loc_type["id"]} if loc_type.get("nestable") else set())
        if parent_type not in allowed:
            raise Invalid(400, {"parent": [f'A location of type "{loc_type["name"]}" can\'t have this parent.']})

    def _create(self, endpoint: str, data: dict) -> dict:
        if endpoint in TAGGABLE:
            data.setdefault("tags", [])
        data.setdefault("custom_fields", {})
        if endpoint == "dcim/cables":
            return self._create_cable(data)
        obj = {"id": None, **data}
        if endpoint == "dcim/devices":
            obj.setdefault("position", None)
        self._check(endpoint, obj)
        obj["id"] = str(uuid.uuid4())
        self.objects[endpoint][obj["id"]] = obj
        if endpoint == "dcim/devices":
            self._instantiate_templates(obj)
        return obj

    def _instantiate_templates(self, device: dict) -> None:
        """Nautobot creates a device's components from its type's templates (untagged)."""
        rears: dict[str, str] = {}
        for tmpl_endpoint, endpoint in TEMPLATES.items():
            for tmpl in self.all(tmpl_endpoint, device_type=device["device_type"]):
                fields = {k: v for k, v in tmpl.items() if k not in ("id", "device_type", "rear_port_template")}
                if endpoint == "dcim/interfaces":
                    fields["status"] = self.status_id("Active")
                if endpoint == "dcim/front-ports":
                    fields["rear_port"] = rears.get(tmpl.get("rear_port_template"))
                new_id = self.add(endpoint, device=device["id"], cable=None, **fields)
                if endpoint == "dcim/rear-ports":
                    rears[tmpl["id"]] = new_id

    def _create_cable(self, data: dict) -> dict:
        obj = {"id": str(uuid.uuid4())}
        for side in ("a", "b"):
            ct, oid = data.get(f"termination_{side}_type"), data.get(f"termination_{side}_id")
            comp = self.objects[COMPONENTS[ct]].get(oid) if ct in COMPONENTS else None
            if comp is None:
                raise Invalid(400, {f"termination_{side}_id": [f"Termination {ct} {oid} not found"]})
            if comp.get("cable"):
                raise Invalid(400, {f"termination_{side}_id": [f"{comp['name']} already has a cable attached"]})
            obj[f"termination_{side}_type"], obj[f"termination_{side}_id"] = ct, oid
        status = self._ids("extras/statuses", data.get("status"))
        if status is None or "dcim.cable" not in self.objects["extras/statuses"][status]["content_types"]:
            raise Invalid(400, {"status": ["A cable status is required (Connected, Planned or Decommissioning)."]})
        obj.update(
            {
                "status": status,
                "type": data.get("type", ""),
                "label": data.get("label", ""),
                "color": data.get("color", ""),
                "tags": [self._ids("extras/tags", t) for t in data.get("tags") or []],
                "custom_fields": {},
            }
        )
        self.objects["dcim/cables"][obj["id"]] = obj
        for side in ("a", "b"):
            self.objects[COMPONENTS[obj[f"termination_{side}_type"]]][obj[f"termination_{side}_id"]]["cable"] = obj[
                "id"
            ]
        return obj

    def _patch(self, endpoint: str, obj_id: str, data: dict) -> dict:
        obj = dict(self.objects[endpoint][obj_id])
        if "custom_fields" in data:
            data["custom_fields"] = {**(obj.get("custom_fields") or {}), **data["custom_fields"]}
        obj.update(data)
        self._check(endpoint, obj)
        self.objects[endpoint][obj_id] = obj
        return obj

    def _delete(self, endpoint: str, obj_id: str) -> None:
        for dep_endpoint, fk in PROTECT.get(endpoint, []):
            if self.all(dep_endpoint, **{fk: obj_id}):
                raise Invalid(409, {"detail": "Unable to delete object. Dependent objects were found."})
        obj = self.objects[endpoint].pop(obj_id)
        if endpoint == "dcim/cables":
            for side in ("a", "b"):
                comp = self.objects[COMPONENTS[obj[f"termination_{side}_type"]]].get(obj[f"termination_{side}_id"])
                if comp is not None:
                    comp["cable"] = None
        elif endpoint in COMPONENTS.values():
            if obj.get("cable") in self.objects["dcim/cables"]:
                self._delete("dcim/cables", obj["cable"])
            if endpoint == "dcim/interfaces":
                for link in self.all("ipam/ip-address-to-interface", interface=obj_id):
                    self.objects["ipam/ip-address-to-interface"].pop(link["id"])
            if endpoint == "dcim/rear-ports":
                for fp in self.all("dcim/front-ports", rear_port=obj_id):
                    if fp["id"] in self.objects["dcim/front-ports"]:
                        self._delete("dcim/front-ports", fp["id"])
            if endpoint == "dcim/power-ports":
                for outlet in self.all("dcim/power-outlets", power_port=obj_id):
                    outlet["power_port"] = None
        elif endpoint == "dcim/devices":
            for comp_endpoint in COMPONENTS.values():
                for comp in self.all(comp_endpoint, device=obj_id):
                    if comp["id"] in self.objects[comp_endpoint]:
                        self._delete(comp_endpoint, comp["id"])
            for bay in self.all("dcim/device-bays", device=obj_id):
                self.objects["dcim/device-bays"].pop(bay["id"])
            for service in self.all("ipam/services", device=obj_id):
                self.objects["ipam/services"].pop(service["id"])
        elif endpoint == "dcim/locations":
            for child in self.all("dcim/locations", parent=obj_id):
                self._delete("dcim/locations", child["id"])
        elif endpoint == "dcim/racks":
            for reservation in self.all("dcim/rack-reservations", rack=obj_id):
                self.objects["dcim/rack-reservations"].pop(reservation["id"])
