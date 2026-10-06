# railyard-sync — guide for Claude

The shared Python core of Railyard's DCIM integrations, and the `railyard-sync` CLI.

- **Import (DCIM → Railyard):** read a NetBox (later Nautobot) site, build a Railyard project from it,
  and re-import it later as a refreshed baseline without losing what was designed in Railyard.
- **Export (Railyard → DCIM):** the DiffSync models and Railyard source adapter that
  `netbox-plugin-railyard` (and a future Nautobot plugin) sync from. Extracted from that plugin's
  `netbox_railyard/railyard/` core; keep it in step with Railyard's Go export
  (`railyard/backend/internal/export/`).

The plugins depend on this package. Nothing here may import Django, NetBox or Nautobot: a plugin
reads its own ORM into a `Snapshot` and calls the same importer the CLI uses.

## Layout

| Path | What |
|---|---|
| `src/railyard_sync/client.py`, `errors.py`, `project.py` | Railyard REST client (`Authorization: Bearer ry_…`, `X-Org-Id`), typed errors, Project JSON wrapper |
| `src/railyard_sync/export/` | Railyard → DCIM: canonical DiffSync models, source adapter, mappings and cabling plan (ports of the Go export) |
| `src/railyard_sync/export/sync_document.py` | Source adapter over Railyard's `netbox-sync` deliverable (the NetBox bundle as JSON rows). **All knowledge of that document's shape lives here** |
| `src/railyard_sync/export/netbox_rest.py`, `run.py`, `policy.py` | NetBox REST target (the plugin's ownership rules: tag-owned objects only, shared objects used as-is, conflicts skipped, guarded deletes) and `sync_to_netbox()`; `policy.py` is the plugin's ownership tag, byte for byte |
| `src/railyard_sync/deliverables.py` | `deliverable_json()` / `netbox_sync_document()` and the 402 `plan_required`/`plan_limit` errors (to move onto the client and into `errors.py`) |
| `src/railyard_sync/dcim/snapshot.py` | **The contract**: a DCIM site as plain dataclasses (sites, locations, racks, device types, devices, components, cables, power panels/feeds), source-neutral, mm/kg/W units |
| `src/railyard_sync/dcim/netbox.py` | NetBox REST loader → `Snapshot` (one or more sites) |
| `src/railyard_sync/importer/` | `build_project(snapshot)` → Railyard Project JSON; `merge(existing, imported)` for re-import; the import report |
| `src/railyard_sync/cli.py` | `railyard-sync import netbox …` |

## The Railyard side (facts the importer depends on)

Read `railyard/schema/project.schema.json` and `railyard/backend/internal/model/` before changing the
builder. Key rules:

- `model.Load` rejects unknown fields everywhere. Required root fields: `schemaVersion: "1"`, `id`, `name`.
- **Spaces are `containers`** (`{id, name, type, parentId, layout: group|floor|row, status, facility,
  exportSite}`), and the physical records are projections the importer must write consistently
  (the server does not re-derive them on a REST save): every `floor` container has a `dataCentres`
  record with the same id; every `group` container a `locations` record; every `row` a `rows` record;
  `rack.containerId` is its space and `rack.dcId`/`rack.rowId` its nearest floor/row ancestors.
  See `backend/internal/model/container_layout.go`.
- Ids are free strings chosen by the client (≤256 chars; port/inlet ids ≤200; **no `:` in rack ids**).
  Placement ids are unique across the project. Rack names are unique per space (case-insensitive).
- A device whose label must stay as NetBox named it sets `namingMode: "manual"`.
- Power is only device inlet → PDU outlet (`powerLinks`). There are no power panels, feeds, phases
  or voltages; there is no device status.
- A PUT that **reorders** a collection forces a full replacement on the server. Keep collection order
  stable between imports (existing order, new items appended).
- Rack caps are per estate and server-enforced: a create or replace that adds racks past the plan's cap
  is refused with 402 `{"code": "plan_limit", "resource": "racks", "limit", "current", "requiredPlans",
  "projectPass"}`. Writes that do not add racks are never refused.

## Identity (what makes re-import work)

Every object the importer creates gets a **deterministic id** from its source primary key:
`nb-site-<id>`, `nb-loc-<id>`, `nb-rack-<id>`, `nb-dev-<id>`, `nb-if-<id>`, `nb-fp-<id>`, `nb-rp-<id>`,
`nb-pp-<id>` (power port → placement power inlet), `nb-cable-<id>`, `nb-power-<cable id>` (power link),
`nb-dt-<slug>` (custom device types), `nb-rt-<slug>` (rack types). Nautobot uses `nbt-` with the same
suffixes. `project.meta.railyardSync` records the source (kind, URL, version, sites), the last import
time and what was not modelled (power panels and feeds, device statuses, skipped objects).

Anything **without** that prefix was designed in Railyard and is never touched by a re-import.

## Export (Railyard → NetBox, `sync_to_netbox`)

Same rules as the plugin's target (`netbox_railyard/target.py`), over REST. The ownership tag is keyed
by the Railyard URL + project id, so pass the same `railyard_url` the plugin uses. Order: renames, owned
cable deletes, creates/updates in `TOP_LEVEL` order, other deletes in reverse; deletes only with
`allow_deletes`, and never when NetBox would protect the object or cascade to, modify or disconnect
untagged objects. Front ports: `rear_port` up to 4.4, a `rear_ports` list from 4.5. Fixtures in
`tests/fixtures/sync/` come from `railyard export --format netbox-sync` (`make_documents.py`); tests run
against `tests/export/fake_netbox.py`.

## Mapping (DCIM → Railyard)

| DCIM | Railyard |
|---|---|
| Region chain above the site | `group` containers (type "Region") |
| Site | `floor` container (type "Site", `exportSite: true`) + `dataCentres` record |
| Location (nested) | `group` containers under the site (type = Nautobot location type, else "Location") |
| Rack | rack in its location (or the site): `uHeight`, `startingUnit`, `descendingUnits`, width 19"→600 mm / 23"→800 mm unless an outer width is given, `depthMm` from outer depth, `status`, `role`, `maxLoadKg`, `rackTypeKey`, tags, comments → notes |
| Rack type | `rackTypes` entry (`nb-rt-<slug>`, `formFactor` in NetBox spelling) |
| Power feeds to a rack | `rack.powerCapacityW` = sum of the **primary** feeds' available power; feeds and panels kept in meta |
| Device type | the Railyard catalogue entry whose key equals the NetBox slug (looked up through `GET /api/catalogue/search` + `/entries/{id}`, `source.kind: "catalogue"`), else a custom type `nb-dt-<slug>` built from the component templates |
| Racked device | placement: `startU` = position, `heightU` = type height (fractional U rounded up, reported), face front/rear, **full-depth → `face: "full"`**, `label` = name, `namingMode: "manual"`, serial (≤50), role, tags, comments → notes, and explicit `ports` / `powerInlets` from the device's real components (ids above) |
| 0U device in a rack (PDUs) | placement `mount: "zeroU"`, `side` alternating left/right; a device with power outlets is a PDU (its type gets an `outlets` spec) |
| Unracked or child (device-bay) devices, modules, console ports, circuits | not modelled: skipped and listed in the report |
| Cable between two imported interfaces/front/rear ports | `cables` entry with `portId` ends, `media` from the cable type, `label`, `colour` (`#rrggbb`), `kind: "patch"` |
| Cable power port ↔ power outlet | `powerLinks` entry: device inlet `nb-pp-<id>` → PDU outlet number (the outlet's 1-based position in the PDU's outlets, name-sorted) |
| Cable to a power feed, circuit, console port, or another site | skipped and reported |

## Re-import policy

`merge(existing, imported, allow_deletes=False)`:

- Objects are matched by id. An imported object updates the fields the DCIM owns (names, positions,
  heights, faces, types, serials, ports, cable ends, rack dimensions); fields only Railyard has
  (colour, power draw, notes added in Railyard, naming, topology, meet-me rooms, pods) are kept.
- New DCIM objects are added (appended, so collection order stays stable).
- Prefixed objects that disappeared from the DCIM are **kept and reported as stale**, unless
  `allow_deletes`; then they are removed (and cables/power links that referenced them).
- Railyard-only objects are never modified. A conflict the server would refuse (a Railyard device now
  overlapping an imported one) is reported, not silently resolved.
- The merge returns the new document plus a diff summary (added / updated / stale / removed per kind).

## Validate

`.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/pytest`. Tests never touch the
network: the Railyard client and the NetBox loader take an injected session (`tests/conftest.py`).
British spelling in user-facing text.
