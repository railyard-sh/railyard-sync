"""An in-memory Sunbird dcTrack REST API, just enough of it for ``export sunbird``'s tests.

It speaks the request side of ``requests.Session`` and answers as dcTrack 9.3's API guide documents: https only, a
trailing slash is a 404, basic auth or an OAuth2 bearer token on every call (401 otherwise), ``/api/v1/locations``,
quick search with ``eq`` as a case-insensitive "contains" match and ``pageNumber``/``pageSize`` paging, cabinetItems
without serials and with ``cmbUPosition`` as an int, the item itself under ``{"item": …}`` with ``tiClass`` as
"Device / Standard", and the bulk endpoint's per-row ``children`` (207 when some rows fail). Failures carry
``errorList``, ``message`` and a ``trace`` that must never be shown.

For placement (``--devices``): the Models Library (``quicksearch/models`` with ``contains``, aliases), makes
(``GET``/``POST /api/v2/makes``), model creates (``POST /api/v2/models``: the make must exist, make and model are
unique), item creates (single and bulk ``"method": "POST"``: the model must exist, the cabinet must be in the location,
names are upper-cased and unique per location, the units must be free on the rails used) and moves (a bulk PUT of
``cmbCabinet``/``cmbUPosition``, refused for an item that is not Planned, so tests prove one is never sent).

Items are stored flat by id; ``add_location``, ``add_cabinet``, ``add_item``, ``add_zero_u_item`` and ``add_model``
seed them as an operator would have.
"""

from __future__ import annotations

import base64
import itertools
from typing import Any
from urllib.parse import urlsplit

from conftest import FakeResponse

#: Fields an update may set; anything else in a bulk body is refused, so tests prove nothing else is sent.
UPDATABLE = {"tiSerialNumber", "cmbCabinet", "cmbUPosition", "radioRailsUsed", "radioCabinetSide", "radioDepthPosition"}
PLACEMENT = {"cmbCabinet", "cmbUPosition", "radioRailsUsed", "radioCabinetSide", "radioDepthPosition"}


def _error(status: int, message: str) -> FakeResponse:
    return FakeResponse(
        status, {"errorList": [message], "warningList": [], "message": message, "trace": "java.lang.Exception: at …"}
    )


class FakeDcTrack:
    URL = "https://dctrack.example.com"
    USER = "railyard"
    PASSWORD = "Pa55-dcTrk"
    CLIENT_ID = "railyard-sync"
    CLIENT_SECRET = "oauth-secret-0123456789"
    ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiJ9.fake-access-token"

    def __init__(self) -> None:
        self.locations: list[dict] = []
        self.items: dict[int, dict] = {}
        self.calls: list[dict] = []
        self.writes: list[dict] = []  # each bulk body dcTrack applied
        self.refuse: dict[int, str] = {}  # item id -> the error a bulk update of it gets
        self.serials_in_search = True  # False: quick search leaves tiSerialNumber out (the item GET has it)
        self.tokens: set[str] = set()
        self.makes: list[dict] = []
        self.models: list[dict] = []
        self.created: list[dict] = []  # each item create dcTrack applied (cabinets too)
        self.created_models: list[dict] = []
        self.created_makes: list[str] = []
        self._ids = itertools.count(100)

    # -- seeding ----------------------------------------------------------------------------------

    def add_location(self, code: str, name: str = "") -> int:
        loc_id = next(self._ids)
        self.locations.append({"id": loc_id, "tiLocationCode": code, "tiLocationName": name or code})
        return loc_id

    def add_cabinet(self, location: str, name: str) -> int:
        cab_id = next(self._ids)
        self.items[cab_id] = {
            "id": cab_id,
            "tiName": name.upper(),
            "tiClass": "Cabinet",
            "cmbLocation": location,
            "cmbCabinet": "",
            "cmbUPosition": "-9",
        }
        return cab_id

    def add_item(
        self,
        cabinet: int,
        name: str,
        u: int,
        *,
        rails: str = "Both",
        height: int = 1,
        item_class: str = "Device",
        serial: str = "",
        status: str = "Installed",
    ) -> int:
        item_id = next(self._ids)
        cab = self.items[cabinet]
        self.items[item_id] = {
            "id": item_id,
            "tiName": name.upper(),
            "tiClass": item_class,
            "cmbLocation": cab["cmbLocation"],
            "cmbCabinet": cab["tiName"],
            "cmbUPosition": u,
            "radioRailsUsed": rails,
            "tiRUs": height,
            "tiSerialNumber": serial,
            "cmbStatus": status,
            "mounting": "Rackable",
        }
        return item_id

    def add_zero_u_item(
        self, cabinet: int, name: str, side: str, *, item_class: str = "Rack PDU", status: str = "Installed"
    ) -> int:
        item_id = self.add_item(cabinet, name, 1, item_class=item_class, height=0, status=status)
        self.items[item_id].update({"mounting": "ZeroU", "radioCabinetSide": side, "radioRailsUsed": ""})
        return item_id

    def add_make(self, name: str) -> None:
        if not self._make(name):
            self.makes.append({"makeId": next(self._ids), "makeName": name, "aliases": []})

    def add_model(
        self, make: str, model: str, *, item_class: str = "Device", ru: int = 1, aliases: tuple[str, ...] = ()
    ) -> None:
        self.add_make(make)
        self.models.append(
            {
                "modelId": next(self._ids),
                "make": self._make(make)["makeName"],
                "model": model,
                "class": item_class,
                "mounting": "ZeroU" if ru == 0 else "Rackable",
                "formFactor": "Fixed",
                "ruHeight": ru,
                "modelAliases": list(aliases),
            }
        )

    def _make(self, name: str) -> dict | None:
        return next((m for m in self.makes if m["makeName"].casefold() == name.strip().casefold()), None)

    def _model(self, make: str, model: str) -> dict | None:
        return next(
            (
                m
                for m in self.models
                if m["make"].casefold() == str(make).strip().casefold()
                and m["model"].casefold() == str(model).strip().casefold()
            ),
            None,
        )

    def item_named(self, name: str) -> dict | None:
        return next((i for i in self.items.values() if i["tiName"] == name.upper()), None)

    def serial(self, item_id: int) -> str:
        return self.items[item_id]["tiSerialNumber"]

    # -- the session --------------------------------------------------------------------------------

    def request(self, method, url, headers=None, params=None, json=None, data=None, timeout=None, **kwargs):
        headers = headers or {}
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "params": params, "json": json, "data": data, **kwargs}
        )
        parts = urlsplit(url)
        if f"{parts.scheme}://{parts.netloc}" != self.URL:
            raise ConnectionError(f"no route to {parts.scheme}://{parts.netloc}")
        path = parts.path
        if path.endswith("/"):
            return _error(404, "Not Found")
        if path == "/oauth/token" and method == "POST":
            return self._oauth(data or {})
        if not self._authorised(headers.get("Authorization", "")):
            return _error(401, "Authentication failed")
        if path == "/api/v1/locations" and method == "GET":
            return FakeResponse(200, {"locations": self.locations})
        if path == "/api/v2/quicksearch/items" and method == "POST":
            return self._search(json or {}, dict(params or {}))
        if path.startswith("/api/v2/items/cabinetItems/") and method == "GET":
            return self._cabinet_items(path.rsplit("/", 1)[-1])
        if path.startswith("/api/v2/dcimoperations/items/") and method == "GET":
            item = self.items.get(int(path.rsplit("/", 1)[-1]))
            if item is None:
                return _error(404, "Item not found")
            return FakeResponse(200, {"item": {**item, "tiClass": f"{item['tiClass']} / Standard"}})
        if path == "/api/v2/dcimoperations/items/bulk" and method == "POST":
            return self._bulk(json or {})
        if path == "/api/v2/dcimoperations/items" and method == "POST":
            status, answer = self._create(json or {})
            return (
                FakeResponse(status, {"id": answer["id"], "tiName": answer["tiName"]})
                if status == 200
                else _error(status, answer)
            )
        if path == "/api/v2/quicksearch/models" and method == "POST":
            return self._search_models(json or {})
        if path == "/api/v2/makes" and method == "GET":
            return FakeResponse(200, self.makes)
        if path == "/api/v2/makes" and method == "POST":
            name = str((json or {}).get("makeName") or "").strip()
            if not name or self._make(name):
                return _error(400, f"Make {name} already exists" if name else "makeName is required")
            self.add_make(name)
            self.created_makes.append(name)
            return FakeResponse(200, self._make(name))
        if path == "/api/v2/models" and method == "POST":
            return self._create_model(json or {})
        return _error(404, "Not Found")

    def _authorised(self, value: str) -> bool:
        basic = base64.b64encode(f"{self.USER}:{self.PASSWORD}".encode()).decode()
        return value == f"Basic {basic}" or (value.startswith("Bearer ") and value[7:] in self.tokens)

    def _oauth(self, form: dict) -> FakeResponse:
        if (
            form.get("grant_type") != "client_credentials"
            or form.get("client_id") != self.CLIENT_ID
            or form.get("client_secret") != self.CLIENT_SECRET
        ):
            return FakeResponse(401, {"error": "invalid_client", "error_description": "Client authentication failed"})
        self.tokens.add(self.ACCESS_TOKEN)
        return FakeResponse(
            200,
            {
                "access_token": self.ACCESS_TOKEN,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "user_permissions",
            },
        )

    def _search(self, body: dict, params: dict) -> FakeResponse:
        rows = list(self.items.values())
        for column in body.get("columns") or []:
            want = _wanted(column)
            rows = [r for r in rows if want in str(r.get(column["name"], "")).casefold()]  # "contains", as dcTrack
        selected = [c["name"] for c in body.get("selectedColumns") or []]
        if not self.serials_in_search:
            selected = [c for c in selected if c != "tiSerialNumber"]
        size = int(params.get("pageSize") or 50)
        page = int(params.get("pageNumber") or 1)
        chunk = rows[(page - 1) * size : page * size] if size else rows
        rows_out = [{**r, "tiMounting": f"{r.get('mounting', 'Rackable')} / Fixed"} for r in chunk]
        items = [{name: (str(r.get(name, "")) if name != "id" else r["id"]) for name in selected} for r in rows_out]
        return FakeResponse(200, {"totalRows": len(rows), "pageNumber": page, "searchResults": {"items": items}})

    def _cabinet_items(self, cabinet_id: str) -> FakeResponse:
        cab = self.items.get(int(cabinet_id))
        if cab is None or cab["tiClass"] != "Cabinet":
            return _error(404, "Cabinet not found")
        rows = [
            {
                "id": r["id"],
                "tiName": r["tiName"],
                "className": r["tiClass"],
                "tiRackUnits": r["tiRUs"],
                "cmbUPosition": int(r["cmbUPosition"]),
                "radioRailsUsed": r["radioRailsUsed"],
                "cmbStatus": r.get("cmbStatus", "Installed"),
                "mounting": r.get("mounting", "Rackable"),
            }
            for r in self.items.values()
            if r["tiClass"] != "Cabinet" and r["cmbCabinet"] == cab["tiName"] and r["cmbLocation"] == cab["cmbLocation"]
        ]
        return FakeResponse(200, {"cabinetItems": rows})

    def _bulk(self, body: dict) -> FakeResponse:
        if body.get("method") not in ("PUT", "POST") or not isinstance(body.get("bodies"), list):
            return _error(400, "Invalid bulk request")
        children = []
        if body["method"] == "POST":
            for row in body["bodies"]:
                status, answer = self._create(row)
                children.append(
                    {"httpCode": 200, "errorList": [], "request": row, "response": dict(answer)}
                    if status == 200
                    else {"httpCode": status, "errorList": [answer], "request": row}
                )
            status = 200 if all(c["httpCode"] == 200 for c in children) else 207
            return FakeResponse(status, {"httpCode": status, "errorList": [], "children": children})
        for row in body["bodies"]:
            item = self.items.get(row.get("id")) if isinstance(row.get("id"), int) else None
            extra = set(row) - {"id"} - UPDATABLE
            if item is None:
                children.append(
                    {"httpCode": 400, "errorList": ["Item cannot be updated. It does not exist in database."]}
                )
            elif extra:
                children.append({"httpCode": 400, "errorList": [f"Unexpected fields {sorted(extra)}"]})
            elif row["id"] in self.refuse:
                children.append({"httpCode": 400, "errorList": [self.refuse[row["id"]]], "request": row})
            elif set(row) & PLACEMENT and (refusal := self._move_refusal(item, row)):
                children.append({"httpCode": 400, "errorList": [refusal], "request": row})
            else:
                item.update({k: v for k, v in row.items() if k != "id"})
                self.writes.append(dict(row))
                children.append({"httpCode": 200, "errorList": [], "request": row, "response": dict(item)})
        status = 200 if all(c["httpCode"] == 200 for c in children) else 207
        return FakeResponse(status, {"httpCode": status, "errorList": [], "children": children})

    # -- placement ------------------------------------------------------------------------------------

    def _search_models(self, body: dict) -> FakeResponse:
        rows = list(self.models)
        for column in body.get("columns") or []:
            want = _wanted(column)
            rows = [r for r in rows if want in " ".join(map(str, _as_list(r.get(column["name"])))).casefold()]
        selected = [c["name"] for c in body.get("selectedColumns") or []]
        models = [{name: r.get(name) for name in selected if name in r} for r in rows]
        return FakeResponse(200, {"totalRows": len(rows), "pageNumber": 1, "searchResults": {"models": models}})

    def _create_model(self, body: dict) -> FakeResponse:
        missing = [
            f for f in ("make", "model", "class", "mounting", "formFactor", "ruHeight") if body.get(f) in (None, "")
        ]
        if missing:
            return _error(400, f"Missing required fields: {', '.join(missing)}")
        if not self._make(body["make"]):
            return _error(400, f"Make {body['make']} does not exist")
        if self._model(body["make"], body["model"]):
            return _error(400, f"Model {body['model']} already exists for make {body['make']}")
        self.add_model(body["make"], body["model"], item_class=body["class"], ru=int(body["ruHeight"]))
        self.created_models.append(dict(body))
        return FakeResponse(200, {"modelId": self.models[-1]["modelId"], "model": body["model"], "make": body["make"]})

    def _create(self, body: dict) -> tuple[int, Any]:
        """Create an item (or a cabinet) as dcTrack would: ``(200, item)`` or ``(status, error message)``."""
        missing = [f for f in ("tiName", "cmbLocation", "cmbMake", "cmbModel") if not body.get(f)]
        if missing:
            return 400, f"Missing required fields: {', '.join(missing)}"
        model = self._model(body["cmbMake"], body["cmbModel"])
        if model is None:
            return 400, "Specified make does not match the model"
        location = body["cmbLocation"]
        if not any(loc["tiLocationCode"] == location for loc in self.locations):
            return 400, f"Location {location} does not exist"
        name = str(body["tiName"]).upper()
        if any(i["tiName"] == name and i["cmbLocation"] == location for i in self.items.values()):
            return 400, f"An Item with name {name} already exists for another item in site {location}"
        item = {
            "id": next(self._ids),
            "tiName": name,
            "tiClass": model["class"],
            "cmbLocation": location,
            "cmbCabinet": "",
            "cmbUPosition": "-9",
            "radioRailsUsed": "",
            "tiRUs": model["ruHeight"],
            "tiSerialNumber": str(body.get("tiSerialNumber") or ""),
            "cmbStatus": body.get("cmbStatus") or "Planned",
            "cmbMake": model["make"],
            "cmbModel": model["model"],
            "mounting": model["mounting"],
        }
        if model["class"] == "Cabinet":
            item.update({k: body[k] for k in ("cmbRowLabel", "cmbRowPosition") if k in body})
        else:
            placement = {k: body[k] for k in PLACEMENT if k in body}
            refusal = self._placement_refusal({**item, "radioRailsUsed": "Both", **placement}, item["id"])
            if refusal:
                return 400, refusal
            item.update(placement)
        self.items[item["id"]] = item
        self.created.append(dict(body))
        return 200, item

    def _move_refusal(self, item: dict, row: dict) -> str:
        if item.get("cmbStatus") != "Planned":
            return f"Item {item['tiName']} is {item.get('cmbStatus')}: it can only be moved with a move request"
        return self._placement_refusal({**item, **{k: row[k] for k in PLACEMENT if k in row}}, item["id"])

    def _placement_refusal(self, item: dict, item_id: int) -> str:
        """Why ``item`` can't sit where it says (its cabinet missing, or its units taken), or ``""``."""
        cabinet = item.get("cmbCabinet")
        if not any(
            c["tiClass"] == "Cabinet" and c["tiName"] == cabinet and c["cmbLocation"] == item["cmbLocation"]
            for c in self.items.values()
        ):
            return "Selected cabinet is not available at the selected location"
        if item.get("mounting") == "ZeroU":
            return ""
        u, height = int(item.get("cmbUPosition") or -9), max(int(item.get("tiRUs") or 1), 1)
        if u <= 0:
            return "A U position is required to place a rackable item"
        for other in self.items.values():
            if other["id"] == item_id or other["tiClass"] == "Cabinet" or other.get("mounting") == "ZeroU":
                continue
            if other["cmbCabinet"] != cabinet or other["cmbLocation"] != item["cmbLocation"]:
                continue
            low = int(other["cmbUPosition"])
            high = low + max(int(other["tiRUs"] or 1), 1) - 1
            rails = {item.get("radioRailsUsed") or "Both", other.get("radioRailsUsed") or "Both"}
            if low <= u + height - 1 and u <= high and ("Both" in rails or len(rails) == 1):
                return f"The UPosition {u} is not available in Cabinet {cabinet}"
        return ""


def _wanted(column: dict) -> str:
    rule = column.get("filter") or {}
    return str(rule.get("eq", rule.get("contains", ""))).strip('"').casefold()


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else [value]
