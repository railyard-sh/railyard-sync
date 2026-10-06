"""Find a DCIM device type in Railyard's device catalogue.

Railyard's public catalogue is built from the NetBox device-type library, and an entry's ``key`` is
the library's slug. A NetBox device type created from the library therefore has the same slug as its
catalogue entry, and importing it as that entry gives the estate Railyard's own definition (ports,
power inputs, outlets) with ``source.kind: "catalogue"``, exactly as picking it in the editor does
(``frontend/src/ui/DeviceTypeField.tsx`` stamps the entry id and catalogue revision).

The API (``railyard/backend/internal/api/catalogue.go``):

- ``GET /api/catalogue/search?q=&kind=device&limit=`` → ``{revision, entries: [Summary]}``; a summary
  has ``id``, ``kind``, ``key``, ``manufacturer``, ``model`` and ``supported``.
- ``GET /api/catalogue/entries/{id}?revision=`` → ``{revision, entry: {…Summary, projection}}``; the
  projection is the Railyard ``DeviceType`` (``null`` for an unsupported entry).
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any, Protocol

GetJSON = Callable[[str, "dict | None"], Any]

SEARCH_PATH = "/api/catalogue/search"
ENTRY_PATH = "/api/catalogue/entries/{id}"
SEARCH_LIMIT = 100  # the API's maximum page
MAX_QUERY_LENGTH = 200  # the API refuses longer queries


class CatalogueLookup(Protocol):
    """Resolves a DCIM device type to a Railyard ``DeviceType`` JSON, or ``None`` when there is none."""

    def device_type(self, slug: str, manufacturer: str, model: str) -> dict | None: ...


class RailyardCatalogue:
    """:class:`CatalogueLookup` over Railyard's catalogue API.

    ``get_json(path, params)`` performs a GET against the Railyard instance and returns the decoded
    JSON (the CLI wires it to :class:`railyard_sync.client.RailyardClient`); errors it raises propagate,
    so an unreachable catalogue is never mistaken for "no match". Searches are cached per slug and
    entries per id, so a site with fifty switches of one model costs two requests.
    """

    def __init__(self, get_json: GetJSON):
        self._get_json = get_json
        self._searches: dict[str, tuple[str, list[dict]]] = {}
        self._entries: dict[str, dict | None] = {}

    def device_type(self, slug: str, manufacturer: str, model: str) -> dict | None:
        slug = (slug or "").strip()
        if not slug or len(slug) > MAX_QUERY_LENGTH:
            return None
        revision, matches = self._search(slug)
        if not matches:
            return None
        # Library slugs are unique in practice; when two manufacturers share one, prefer the DCIM's.
        wanted = (manufacturer or "").strip().casefold()
        matches = sorted(matches, key=lambda e: str(e.get("manufacturer", "")).strip().casefold() != wanted)
        for summary in matches:
            device_type = self._entry(str(summary["id"]), revision)
            if device_type is not None:
                return copy.deepcopy(device_type)
        return None

    def _search(self, slug: str) -> tuple[str, list[dict]]:
        if slug not in self._searches:
            page = self._get_json(SEARCH_PATH, {"q": slug, "kind": "device", "limit": SEARCH_LIMIT}) or {}
            matches = [
                entry
                for entry in page.get("entries") or []
                if isinstance(entry, dict)
                and entry.get("key") == slug
                and entry.get("supported", True)
                and entry.get("kind", "device") == "device"
                and entry.get("id")
            ]
            self._searches[slug] = (str(page.get("revision") or ""), matches)
        return self._searches[slug]

    def _entry(self, entry_id: str, revision: str) -> dict | None:
        if entry_id not in self._entries:
            params = {"revision": revision} if revision else None
            detail = self._get_json(ENTRY_PATH.format(id=entry_id), params) or {}
            entry = detail.get("entry") or {}
            projection = entry.get("projection")
            if not entry.get("supported", True) or not isinstance(projection, dict) or not projection.get("key"):
                self._entries[entry_id] = None
            else:
                device_type = copy.deepcopy(projection)
                source: dict[str, str] = {"kind": "catalogue", "ref": entry_id}
                if detail.get("revision") or revision:
                    source["revision"] = str(detail.get("revision") or revision)
                device_type["source"] = source
                self._entries[entry_id] = device_type
        return self._entries[entry_id]
