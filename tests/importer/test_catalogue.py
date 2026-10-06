"""RailyardCatalogue against a fake catalogue API (the shapes of railyard/backend/internal/api/catalogue.go)."""

from __future__ import annotations

import ldn1
import pytest

from railyard_sync.importer import RailyardCatalogue

ENTRY_ID = "b" * 64
OTHER_ID = "c" * 64


class FakeAPI:
    """Serves GET /api/catalogue/search and /api/catalogue/entries/{id}, recording every call."""

    def __init__(self, entries: list[dict], revision: str = "2026.10.1"):
        self.entries = entries
        self.revision = revision
        self.calls: list[tuple[str, dict | None]] = []

    def __call__(self, path: str, params: dict | None):
        self.calls.append((path, params))
        if path == "/api/catalogue/search":
            q = params["q"].lower()
            hits = [
                {k: v for k, v in e.items() if k != "projection"}
                for e in self.entries
                if q in e["key"] or q in e["model"].lower()
            ]
            return {
                "revision": self.revision,
                "sourceCommit": "abc",
                "total": len(hits),
                "limit": 100,
                "offset": 0,
                "entries": hits,
            }
        entry_id = path.rsplit("/", 1)[1]
        entry = next(e for e in self.entries if e["id"] == entry_id)
        return {"revision": params["revision"], "sourceCommit": "abc", "entry": entry}


def _entry(entry_id, key, manufacturer="Cisco", supported=True, projection=None):
    projection = projection if projection is not None else {**ldn1.leaf_catalogue_entry(), "key": key}
    return {
        "id": entry_id,
        "kind": "device",
        "sourcePath": f"device-types/{manufacturer}/{key}.yaml",
        "key": key,
        "manufacturer": manufacturer,
        "model": key,
        "supported": supported,
        "warnings": [],
        "projection": projection if supported else None,
        "rawData": {},
    }


def test_exact_key_match_returns_the_projection_with_provenance():
    api = FakeAPI([_entry(OTHER_ID, ldn1.LEAF_SLUG + "-2"), _entry(ENTRY_ID, ldn1.LEAF_SLUG)])
    device_type = RailyardCatalogue(api).device_type(ldn1.LEAF_SLUG, "Cisco", "Nexus 93180YC-FX")
    assert device_type == {
        **ldn1.leaf_catalogue_entry(),
        "source": {"kind": "catalogue", "ref": ENTRY_ID, "revision": "2026.10.1"},
    }
    assert api.calls == [
        ("/api/catalogue/search", {"q": ldn1.LEAF_SLUG, "kind": "device", "limit": 100}),
        (f"/api/catalogue/entries/{ENTRY_ID}", {"revision": "2026.10.1"}),
    ]


def test_no_exact_match_is_none():
    api = FakeAPI([_entry(OTHER_ID, ldn1.LEAF_SLUG + "-2")])
    assert RailyardCatalogue(api).device_type(ldn1.LEAF_SLUG, "Cisco", "Nexus") is None
    assert [path for path, _ in api.calls] == ["/api/catalogue/search"]


def test_unsupported_entry_is_no_match():
    api = FakeAPI([_entry(ENTRY_ID, ldn1.LEAF_SLUG, supported=False)])
    assert RailyardCatalogue(api).device_type(ldn1.LEAF_SLUG, "Cisco", "Nexus") is None
    assert len(api.calls) == 1  # an unsupported summary is never fetched


def test_lookups_are_cached_per_slug_and_copies_are_independent():
    api = FakeAPI([_entry(ENTRY_ID, ldn1.LEAF_SLUG)])
    catalogue = RailyardCatalogue(api)
    first = catalogue.device_type(ldn1.LEAF_SLUG, "Cisco", "Nexus 93180YC-FX")
    first["model"] = "changed"
    second = catalogue.device_type(ldn1.LEAF_SLUG, "Cisco", "Nexus 93180YC-FX")
    assert second["model"] == "Nexus 93180YC-FX"
    assert len(api.calls) == 2
    catalogue.device_type("unknown-slug", "X", "Y")
    catalogue.device_type("unknown-slug", "X", "Y")
    assert len(api.calls) == 3


def test_a_shared_slug_prefers_the_dcims_manufacturer():
    arista = _entry(
        OTHER_ID,
        "shared",
        manufacturer="Arista",
        projection={**ldn1.leaf_catalogue_entry(), "key": "shared", "manufacturer": "Arista"},
    )
    cisco = _entry(
        ENTRY_ID, "shared", manufacturer="Cisco", projection={**ldn1.leaf_catalogue_entry(), "key": "shared"}
    )
    catalogue = RailyardCatalogue(FakeAPI([arista, cisco]))
    assert catalogue.device_type("shared", "cisco", "x")["source"]["ref"] == ENTRY_ID
    assert catalogue.device_type("shared", "Arista", "x")["source"]["ref"] == OTHER_ID


@pytest.mark.parametrize("slug", ["", "x" * 201])
def test_slugs_the_api_would_refuse_are_not_searched(slug):
    api = FakeAPI([])
    assert RailyardCatalogue(api).device_type(slug, "", "") is None
    assert api.calls == []


def test_api_errors_propagate():
    def failing(path, params):
        raise RuntimeError("catalogue unavailable")

    with pytest.raises(RuntimeError):
        RailyardCatalogue(failing).device_type("x", "y", "z")


def test_build_uses_the_catalogue_entry():
    from importer_helpers import build

    api = FakeAPI([_entry(ENTRY_ID, ldn1.LEAF_SLUG)])
    project = build(ldn1.snapshot(), catalogue=RailyardCatalogue(api)).project
    leaf = next(dt for dt in project["catalogue"] if dt["key"] == ldn1.LEAF_SLUG)
    assert leaf["source"] == {"kind": "catalogue", "ref": ENTRY_ID, "revision": "2026.10.1"}
