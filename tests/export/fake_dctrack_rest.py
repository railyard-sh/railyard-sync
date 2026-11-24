"""An in-memory Sunbird dcTrack REST API, just enough of it for ``export sunbird``'s tests.

It speaks the request side of ``requests.Session`` and answers as dcTrack 9.3's API guide documents: https only, a
trailing slash is a 404, basic auth or an OAuth2 bearer token on every call (401 otherwise), ``/api/v1/locations``,
quick search with ``eq`` as a case-insensitive "contains" match and ``pageNumber``/``pageSize`` paging, cabinetItems
without serials and with ``cmbUPosition`` as an int, the item itself under ``{"item": …}`` with ``tiClass`` as
"Device / Standard", and the bulk endpoint's per-row ``children`` (207 when some rows fail). Failures carry
``errorList``, ``message`` and a ``trace`` that must never be shown.

Items are stored flat by id; ``add_location``, ``add_cabinet`` and ``add_item`` seed them as an operator would have.
"""

from __future__ import annotations

import base64
import itertools
from urllib.parse import urlsplit

from conftest import FakeResponse

#: Fields an update may set; anything else in a bulk body is refused, so tests prove only the serial is sent.
UPDATABLE = {"tiSerialNumber"}


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
        }
        return item_id

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
            want = str((column.get("filter") or {}).get("eq", "")).casefold()
            rows = [r for r in rows if want in str(r.get(column["name"], "")).casefold()]  # "contains", as dcTrack
        selected = [c["name"] for c in body.get("selectedColumns") or []]
        if not self.serials_in_search:
            selected = [c for c in selected if c != "tiSerialNumber"]
        size = int(params.get("pageSize") or 50)
        page = int(params.get("pageNumber") or 1)
        chunk = rows[(page - 1) * size : page * size] if size else rows
        items = [{name: (str(r.get(name, "")) if name != "id" else r["id"]) for name in selected} for r in chunk]
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
                "cmbStatus": "Installed",
            }
            for r in self.items.values()
            if r["tiClass"] != "Cabinet" and r["cmbCabinet"] == cab["tiName"] and r["cmbLocation"] == cab["cmbLocation"]
        ]
        return FakeResponse(200, {"cabinetItems": rows})

    def _bulk(self, body: dict) -> FakeResponse:
        if body.get("method") != "PUT" or not isinstance(body.get("bodies"), list):
            return _error(400, "Invalid bulk request")
        children = []
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
            else:
                item.update({k: v for k, v in row.items() if k != "id"})
                self.writes.append(dict(row))
                children.append({"httpCode": 200, "errorList": [], "request": row, "response": dict(item)})
        status = 200 if all(c["httpCode"] == 200 for c in children) else 207
        return FakeResponse(status, {"httpCode": status, "errorList": [], "children": children})
