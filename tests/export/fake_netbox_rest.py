"""An in-memory NetBox REST API, just enough of it for the export's tests.

It speaks the request side of ``requests.Session`` (``request(method, url, headers=, params=, json=,
timeout=, verify=)``) and answers like NetBox 4.x: paginated lists with ``count``/``next``/``results``,
nested references (``{"id", "name", …}``), choice fields as ``{"value", "label"}``, tags as nested
objects, ``?tag=<slug>`` / ``?<field>_id=`` / ``?name__ie=`` filters, 400 for invalid or duplicate
objects, 409 when a delete is protected, and the cascades NetBox applies on delete. Front ports take
the API shape of the configured version: ``rear_port``/``rear_port_position`` up to 4.4, a
``rear_ports`` list from 4.5 (the other shape is refused, so tests prove which one was sent).

Objects are stored flat, references as bare ids (``device["site"] == 3``); ``add()`` seeds them
directly, as an operator would have created them.
"""

from __future__ import annotations

import itertools
import json as jsonlib
import re
from collections import defaultdict
from urllib.parse import urlsplit

CHOICES = {"status", "type", "face", "width"}

#: endpoint -> {field: referenced endpoint}
FK = {
    "dcim/device-types": {"manufacturer": "dcim/manufacturers"},
    "dcim/locations": {"site": "dcim/sites", "parent": "dcim/locations"},
    "dcim/racks": {"site": "dcim/sites", "location": "dcim/locations"},
    "dcim/devices": {
        "device_type": "dcim/device-types",
        "role": "dcim/device-roles",
        "site": "dcim/sites",
        "rack": "dcim/racks",
        "location": "dcim/locations",
        "parent_device": "dcim/devices",
    },
    "dcim/interfaces": {
        "device": "dcim/devices",
        "cable": "dcim/cables",
        "parent": "dcim/interfaces",
        "lag": "dcim/interfaces",
        "bridge": "dcim/interfaces",
    },
    "dcim/rear-ports": {"device": "dcim/devices", "cable": "dcim/cables"},
    "dcim/front-ports": {"device": "dcim/devices", "cable": "dcim/cables", "rear_port": "dcim/rear-ports"},
    "dcim/power-ports": {"device": "dcim/devices", "cable": "dcim/cables"},
    "dcim/power-outlets": {"device": "dcim/devices", "cable": "dcim/cables", "power_port": "dcim/power-ports"},
    "dcim/interface-templates": {"device_type": "dcim/device-types"},
    "dcim/rear-port-templates": {"device_type": "dcim/device-types"},
    "dcim/front-port-templates": {"device_type": "dcim/device-types", "rear_port": "dcim/rear-port-templates"},
    "dcim/power-port-templates": {"device_type": "dcim/device-types"},
    "dcim/power-outlet-templates": {"device_type": "dcim/device-types", "power_port": "dcim/power-port-templates"},
    "dcim/console-port-templates": {"device_type": "dcim/device-types"},
    "dcim/console-server-port-templates": {"device_type": "dcim/device-types"},
    "dcim/power-panels": {"site": "dcim/sites", "location": "dcim/locations"},
    "dcim/power-feeds": {"rack": "dcim/racks"},
    "dcim/rack-reservations": {"rack": "dcim/racks"},
    "dcim/modules": {"device": "dcim/devices"},
    "dcim/module-types": {"manufacturer": "dcim/manufacturers"},
    "dcim/platforms": {"manufacturer": "dcim/manufacturers"},
    "ipam/services": {"device": "dcim/devices"},
    "ipam/ip-addresses": {},
    "dcim/cables": {},
    "dcim/manufacturers": {},
    "dcim/device-roles": {},
    "dcim/sites": {},
    "extras/tags": {},
    "extras/custom-fields": {},
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
UNIQUE = {
    "extras/tags": [("name",), ("slug",)],
    "extras/custom-fields": [("name",)],
    "dcim/manufacturers": [("name",), ("slug",)],
    "dcim/device-roles": [("name",), ("slug",)],
    "dcim/sites": [("name",), ("slug",)],
    "dcim/device-types": [("manufacturer", "model"), ("manufacturer", "slug")],
    "dcim/locations": [("site", "parent", "name")],
    "dcim/racks": [("site", "location", "name")],
    **{ep: [("device", "name")] for ep in COMPONENTS.values()},
}
#: endpoint -> [(dependent endpoint, field)] whose existence makes a delete fail (Django PROTECT).
PROTECT = {
    "dcim/manufacturers": [("dcim/device-types", "manufacturer")],
    "dcim/device-types": [("dcim/devices", "device_type")],
    "dcim/device-roles": [("dcim/devices", "role")],
    "dcim/sites": [("dcim/racks", "site"), ("dcim/devices", "site")],
    "dcim/locations": [("dcim/racks", "location"), ("dcim/devices", "location")],
    "dcim/racks": [("dcim/devices", "rack")],
}


class Response:
    def __init__(self, status: int, payload=None):
        self.status_code = status
        self._payload = payload
        self.text = "" if payload is None else jsonlib.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class Invalid(Exception):
    def __init__(self, status: int, detail):
        super().__init__(str(detail))
        self.status = status
        self.detail = detail


class FakeNetBox:
    URL = "https://netbox.example.com"

    def __init__(self, version: str = "4.6.0", token: str = "nbt_abc123.s3cr3t-t0ken", *, page_size_cap: int = 3):
        self.version = version
        self.token = token
        self.objects: dict[str, dict[int, dict]] = defaultdict(dict)
        self.calls: list[dict] = []
        self.forbid: set[tuple[str, str]] = set()  # (method, endpoint) answered 403
        self.page_size_cap = page_size_cap  # small, so pagination is always exercised
        self._ids = itertools.count(1)

    # -- seeding and inspection ------------------------------------------------------------------

    @property
    def mappings(self) -> bool:
        major, minor = (int(x) for x in self.version.split(".")[:2])
        return (major, minor) >= (4, 5)

    def add(self, endpoint: str, **fields) -> int:
        obj_id = next(self._ids)
        fields.setdefault("tags", [])
        self.objects[endpoint][obj_id] = {"id": obj_id, **fields}
        return obj_id

    def all(self, endpoint: str, **match) -> list[dict]:
        return [o for o in self.objects[endpoint].values() if all(o.get(k) == v for k, v in match.items())]

    def one(self, endpoint: str, **match) -> dict:
        found = self.all(endpoint, **match)
        assert len(found) == 1, (endpoint, match, found)
        return found[0]

    def tag_id(self, slug: str) -> int | None:
        return next((t["id"] for t in self.objects["extras/tags"].values() if t["slug"] == slug), None)

    def tagged(self, endpoint: str, slug: str) -> list[dict]:
        tid = self.tag_id(slug)
        return [o for o in self.objects[endpoint].values() if tid in o.get("tags", [])]

    @property
    def writes(self) -> list[dict]:
        return [c for c in self.calls if c["method"] != "GET"]

    # -- the session interface -------------------------------------------------------------------

    def request(self, method, url, headers=None, params=None, json=None, timeout=None, verify=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json, "headers": headers})
        expected = f"Bearer {self.token}" if self.token.startswith("nbt_") else f"Token {self.token}"
        if (headers or {}).get("Authorization") != expected:
            return Response(401, {"detail": "Invalid v2 token"})
        parts = urlsplit(url)
        if f"{parts.scheme}://{parts.netloc}" != self.URL:
            raise AssertionError(f"request sent to another host: {url}")
        path = parts.path
        if path == "/api/status/":
            return Response(200, {"netbox-version": self.version, "python-version": "3.12.0"})
        m = re.fullmatch(r"/api/([a-z]+/[a-z-]+)/(?:(\d+)/)?", path)
        if not m or m.group(1) not in FK:
            return Response(404, {"detail": "Not found."})
        endpoint, obj_id = m.group(1), int(m.group(2)) if m.group(2) else None
        if (method, endpoint) in self.forbid:
            return Response(403, {"detail": "You do not have permission to perform this action."})
        try:
            if method == "GET" and obj_id is None:
                return Response(200, self._list(endpoint, list(params or [])))
            if obj_id is not None and obj_id not in self.objects[endpoint]:
                return Response(404, {"detail": "No object matches the given query."})
            if method == "GET":
                return Response(200, self._render(endpoint, self.objects[endpoint][obj_id]))
            if method == "POST":
                return Response(201, self._render(endpoint, self._create(endpoint, dict(json or {}))))
            if method == "PATCH":
                return Response(200, self._render(endpoint, self._patch(endpoint, obj_id, dict(json or {}))))
            if method == "DELETE":
                self._delete(endpoint, obj_id)
                return Response(204)
        except Invalid as exc:
            return Response(exc.status, exc.detail)
        return Response(405, {"detail": f"Method {method} not allowed."})

    # -- reads -----------------------------------------------------------------------------------

    def _matches(self, endpoint: str, obj: dict, key: str, value: str) -> bool:
        if key == "tag":
            return self.tag_id(value) in obj.get("tags", [])
        if key == "name__ie":
            return str(obj.get("name") or "").lower() == value.lower()
        if endpoint == "ipam/ip-addresses" and key in ("interface_id", "device_id"):
            iface = self.objects["dcim/interfaces"].get(obj.get("assigned_object_id"))
            if key == "interface_id":
                return obj.get("assigned_object_id") == int(value)
            return iface is not None and iface.get("device") == int(value)
        if endpoint == "dcim/cables" and key == "device_id":
            return any(
                self.objects[COMPONENTS[ct]].get(oid, {}).get("device") == int(value)
                for ct, oid in obj["a_terminations"] + obj["b_terminations"]
            )
        if key.endswith("_id"):
            return obj.get(key[:-3]) == int(value)
        return str(obj.get(key) if obj.get(key) is not None else "") == str(value)

    def _list(self, endpoint: str, params: list[tuple[str, str]]) -> dict:
        limit, offset, filters = 50, 0, []
        for key, value in params:
            if key == "limit":
                limit = int(value)
            elif key == "offset":
                offset = int(value)
            elif key != "exclude":
                filters.append((key, str(value)))
        found = [
            o
            for o in sorted(self.objects[endpoint].values(), key=lambda o: o["id"])
            if all(self._matches(endpoint, o, k, v) for k, v in filters)
        ]
        size = min(limit, self.page_size_cap)
        page = found[offset : offset + size]
        more = offset + size < len(found)
        # NetBox behind a proxy often builds ``next`` with the wrong host: it must not be followed.
        nxt = f"http://internal:8080/api/{endpoint}/?limit={size}&offset={offset + size}" if more else None
        return {
            "count": len(found),
            "next": nxt,
            "previous": None,
            "results": [self._render(endpoint, o) for o in page],
        }

    def _nested(self, endpoint: str, obj_id) -> dict | None:
        obj = self.objects[endpoint].get(obj_id) if obj_id is not None else None
        if obj is None:
            return None
        out = {"id": obj_id, "url": f"{self.URL}/api/{endpoint}/{obj_id}/", "display": str(obj.get("name"))}
        for key in ("name", "slug", "model", "label"):
            if key in obj:
                out[key] = obj[key]
        if endpoint == "dcim/device-types":
            out["manufacturer"] = self._nested("dcim/manufacturers", obj["manufacturer"])
        if "device" in obj and endpoint in COMPONENTS.values():
            out["device"] = self._nested("dcim/devices", obj["device"])
            out["cable"] = obj.get("cable")
        return out

    def _render(self, endpoint: str, obj: dict) -> dict:
        out = {}
        for key, value in obj.items():
            if key in FK.get(endpoint, {}):
                out[key] = self._nested(FK[endpoint][key], value)
            elif key == "tags":
                out[key] = [
                    {k: self.objects["extras/tags"][t][k] for k in ("id", "name", "slug", "color")} for t in value
                ]
            elif key in CHOICES and endpoint not in ("extras/custom-fields",):
                out[key] = {"value": value, "label": str(value).title()} if value not in (None, "") else None
            elif key in ("a_terminations", "b_terminations"):
                out[key] = [
                    {"object_type": ct, "object_id": oid, "object": self._nested(COMPONENTS[ct], oid)}
                    for ct, oid in value
                ]
            elif key == "rear_ports":
                out[key] = [dict(m) for m in value]
            else:
                out[key] = value
        if endpoint == "dcim/devices":
            fields = [
                cf["name"]
                for cf in self.objects["extras/custom-fields"].values()
                if "dcim.device" in cf["object_types"]
            ]
            out["custom_fields"] = {name: (obj.get("custom_fields") or {}).get(name) for name in fields}
        return out

    # -- writes ----------------------------------------------------------------------------------

    def _tags(self, value) -> list[int]:
        ids = []
        for tag in value or []:
            tid = tag.get("id") if isinstance(tag, dict) else tag
            if isinstance(tag, dict) and "slug" in tag:
                tid = self.tag_id(tag["slug"])
            if tid not in self.objects["extras/tags"]:
                raise Invalid(400, {"tags": [f"Related object not found using the provided attributes: {tag}"]})
            ids.append(tid)
        return ids

    def _check(self, endpoint: str, obj: dict) -> None:
        for fk, target in FK.get(endpoint, {}).items():
            if obj.get(fk) is not None and obj[fk] not in self.objects[target]:
                raise Invalid(400, {fk: [f"Related object not found: {obj[fk]}"]})
        for fields in UNIQUE.get(endpoint, []):
            key = tuple(obj.get(f) for f in fields)
            for other in self.objects[endpoint].values():
                if other["id"] != obj.get("id") and tuple(other.get(f) for f in fields) == key:
                    raise Invalid(400, {"__all__": [f"{endpoint} with this {', '.join(fields)} already exists."]})
        if endpoint == "dcim/devices" and obj.get("name"):
            for other in self.objects[endpoint].values():
                same = other["id"] != obj.get("id") and other.get("site") == obj.get("site")
                if same and str(other.get("name") or "").lower() == obj["name"].lower():
                    raise Invalid(400, {"__all__": ["Device name must be unique per site."]})
        if endpoint in ("dcim/front-ports", "dcim/front-port-templates"):
            if self.mappings and "rear_port" in obj:
                raise Invalid(400, {"rear_port": ["NetBox 4.5+: map front ports with rear_ports"]})
            if not self.mappings and ("rear_ports" in obj or obj.get("rear_port") is None):
                raise Invalid(400, {"rear_port": ["This field is required."]})
            for m in obj.get("rear_ports") or []:
                if m.get("rear_port") not in self.objects[endpoint.replace("front", "rear")]:
                    raise Invalid(400, {"rear_ports": ["Related rear port not found"]})
            # One front port per rear port position, which must exist on the rear port (as NetBox enforces).
            slots = [(m.get("rear_port"), m.get("rear_port_position")) for m in obj.get("rear_ports") or []]
            if obj.get("rear_port") is not None:
                slots.append((obj["rear_port"], obj.get("rear_port_position", 1)))
            rears = self.objects[endpoint.replace("front", "rear")]
            for rear_id, position in slots:
                if rear_id in rears and int(position or 1) > int(rears[rear_id].get("positions") or 1):
                    raise Invalid(400, {"rear_port_position": [f"Invalid rear port position ({position})"]})
                for other in self.objects[endpoint].values():
                    if other["id"] == obj.get("id"):
                        continue
                    theirs = [(m.get("rear_port"), m.get("rear_port_position")) for m in other.get("rear_ports") or []]
                    if other.get("rear_port") is not None:
                        theirs.append((other["rear_port"], other.get("rear_port_position", 1)))
                    if (rear_id, position) in theirs:
                        raise Invalid(400, {"__all__": ["A front port is already mapped to this rear port position."]})

    def _create(self, endpoint: str, data: dict) -> dict:
        data["tags"] = self._tags(data.get("tags"))
        if endpoint == "dcim/cables":
            return self._create_cable(data)
        if endpoint == "dcim/devices" and data.get("custom_fields"):
            self._check_custom_fields(data["custom_fields"])
        obj = {"id": None, **data}
        self._check(endpoint, obj)
        obj["id"] = next(self._ids)
        self.objects[endpoint][obj["id"]] = obj
        if endpoint == "dcim/devices":
            self._instantiate_templates(obj)
        return obj

    def _check_custom_fields(self, values: dict) -> None:
        known = {
            cf["name"] for cf in self.objects["extras/custom-fields"].values() if "dcim.device" in cf["object_types"]
        }
        for name in values:
            if name not in known:
                raise Invalid(400, {"custom_fields": [f"Unknown field name '{name}' in custom field data."]})

    def _instantiate_templates(self, device: dict) -> None:
        """NetBox creates a device's components from its type's templates (untagged)."""
        for tmpl_endpoint, endpoint in TEMPLATES.items():
            for tmpl in self.all(tmpl_endpoint, device_type=device["device_type"]):
                fields = {
                    k: v for k, v in tmpl.items() if k not in ("id", "device_type", "tags", "rear_port", "rear_ports")
                }
                self.add(endpoint, device=device["id"], cable=None, **fields)

    def _create_cable(self, data: dict) -> dict:
        ends = []
        for side in ("a_terminations", "b_terminations"):
            terms = []
            for term in data.get(side) or []:
                ct, oid = term.get("object_type"), term.get("object_id")
                comp = self.objects[COMPONENTS.get(ct, "")].get(oid) if ct in COMPONENTS else None
                if comp is None:
                    raise Invalid(400, {side: [f"Termination {ct} {oid} not found"]})
                if comp.get("cable"):
                    raise Invalid(400, {side: [f"{comp['name']} already has a cable attached"]})
                terms.append((ct, oid))
            if not terms:
                raise Invalid(400, {side: ["This field is required."]})
            ends.append(terms)
        cable_id = next(self._ids)
        obj = {
            "id": cable_id,
            "a_terminations": ends[0],
            "b_terminations": ends[1],
            "status": data.get("status", "connected"),
            "type": data.get("type", ""),
            "label": data.get("label", ""),
            "color": data.get("color", ""),
            "tags": data["tags"],
        }
        self.objects["dcim/cables"][cable_id] = obj
        for ct, oid in ends[0] + ends[1]:
            self.objects[COMPONENTS[ct]][oid]["cable"] = cable_id
        return obj

    def _patch(self, endpoint: str, obj_id: int, data: dict) -> dict:
        obj = dict(self.objects[endpoint][obj_id])
        if "tags" in data:
            data["tags"] = self._tags(data["tags"])
        if "custom_fields" in data:
            self._check_custom_fields(data["custom_fields"])
            data["custom_fields"] = {**(obj.get("custom_fields") or {}), **data["custom_fields"]}
        if endpoint == "dcim/front-ports" and ("rear_ports" in data or "rear_port" in data):
            obj.pop("rear_ports", None), obj.pop("rear_port", None)
        obj.update(data)
        self._check(endpoint, obj)
        self.objects[endpoint][obj_id] = obj
        return obj

    def _delete(self, endpoint: str, obj_id: int) -> None:
        for dep_endpoint, fk in PROTECT.get(endpoint, []):
            if self.all(dep_endpoint, **{fk: obj_id}):
                raise Invalid(409, {"detail": "Unable to delete object: dependent objects were found."})
        obj = self.objects[endpoint].pop(obj_id)
        if endpoint == "dcim/cables":
            for ct, oid in obj["a_terminations"] + obj["b_terminations"]:
                comp = self.objects[COMPONENTS[ct]].get(oid)
                if comp is not None:
                    comp["cable"] = None
        elif endpoint in COMPONENTS.values():
            if obj.get("cable") in self.objects["dcim/cables"]:
                self._delete("dcim/cables", obj["cable"])
            if endpoint == "dcim/interfaces":
                for ip in self.all("ipam/ip-addresses", assigned_object_id=obj_id):
                    self.objects["ipam/ip-addresses"].pop(ip["id"])
            if endpoint == "dcim/rear-ports":
                for fp in list(self.objects["dcim/front-ports"].values()):
                    if self.mappings:
                        fp["rear_ports"] = [m for m in fp.get("rear_ports") or [] if m["rear_port"] != obj_id]
                    elif fp.get("rear_port") == obj_id:
                        self._delete("dcim/front-ports", fp["id"])
        elif endpoint == "dcim/devices":
            for comp_endpoint in COMPONENTS.values():
                for comp in self.all(comp_endpoint, device=obj_id):
                    if comp["id"] in self.objects[comp_endpoint]:
                        self._delete(comp_endpoint, comp["id"])
        elif endpoint == "dcim/sites":
            for loc in self.all("dcim/locations", site=obj_id):
                self.objects["dcim/locations"].pop(loc["id"], None)
        elif endpoint == "dcim/locations":
            for child in self.all("dcim/locations", parent=obj_id):
                self._delete("dcim/locations", child["id"])
