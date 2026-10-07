# railyard-sync

Bring an existing **NetBox** site or **Nautobot** location into **[Railyard](https://railyard.sh)** as a
design baseline, and refresh it later without losing what you designed; then push the design back out into
NetBox or Nautobot. It is also the shared core of Railyard's NetBox plugin and Nautobot app.

> Status: **early development (0.2).** NetBox (4.0–4.6) and Nautobot (2.x) import and export work.

## What it does

```
NetBox / Nautobot REST API ──► Snapshot (sites, racks, devices, ports, cables, power) ──► Railyard project
                                                                                             │
                                    re-import: merge into the existing estate ◄──────────────┘

Railyard project ──► NetBox / Nautobot sync document (a Railyard deliverable) ──► the DCIM's REST API (export)
```

- **Import a site:** spaces from the site's locations, racks, device types (matched to Railyard's
  catalogue by their devicetype-library slug, otherwise recreated from NetBox's templates), racked and
  0U devices with their real ports and power inlets, data cables, and power cables to PDU outlets.
- **Re-import as a baseline:** every imported object keeps an id derived from its NetBox id, so running
  the import again updates the estate from NetBox. What you added in Railyard is kept; objects deleted
  in NetBox are reported, and removed only with `--allow-deletes`.
- **Honest about gaps:** anything Railyard does not model (unracked or child devices, modules,
  console ports, circuits, power panels and feeds) is listed in the report. Power feeds set each
  rack's power capacity.
- **Plan limits apply:** Railyard caps racks per estate by plan (Community 25). An import that would
  go past it is refused with an upgrade message; it never imports part of a site silently.
- **Export an estate into NetBox:** create and update the estate's sites, locations, racks, device
  types, devices, ports and cables in NetBox, touching only the objects the export created (see
  [Exporting to NetBox](#exporting-to-netbox)).

## Usage

```bash
pip install railyard-sync    # (once published)

export NETBOX_TOKEN=…        # a read-only NetBox API token
export RAILYARD_TOKEN=ry_…   # a Railyard personal access token (User settings → API tokens)

# First import: creates a new estate from the site.
railyard-sync import netbox --netbox-url https://netbox.example.com --site ldn1 \
  --org my-org --name "LDN1 baseline"

# Later: refresh that estate from NetBox (preview first).
railyard-sync import netbox --netbox-url https://netbox.example.com --site ldn1 \
  --org my-org --project ldn1-baseline --dry-run
```

Repeat `--site` to bring several sites into one estate, or pass `--all-sites` for every site the
NetBox token can see. They all become one estate, so the plan's rack limit applies to the total (a
refresh with `--all-sites` also picks up sites added in NetBox since). For one estate per site, run the
import once per site with its own `--name`.

Railyard is `https://railyard.sh` unless you pass `--railyard-url` (a self-hosted Railyard, say).
Tokens are read from the environment, never from the command line. `--snapshot-out`/`--from-snapshot`
save and replay what was read from NetBox, and `--dry-run --out project.json` writes the Railyard
document without uploading it (open it in Railyard with **Open project file**). A refresh is saved
with the revision it was merged into, so a change made in Railyard meanwhile is never overwritten;
`--name-version` names the saved version "NetBox import <date>" (estates with version control on).

Exit codes: `0` done, `1` error (including conflicts between NetBox and the Railyard design, which
are listed and must be resolved in Railyard first), `2` usage, `3` refused by the plan's rack limit.

### Exporting to NetBox

```bash
export NETBOX_TOKEN=…        # a NetBox API token that may create what the export writes
export RAILYARD_TOKEN=ry_…

# Preview: what would be created, updated and deleted. Nothing is written.
railyard-sync export netbox --org my-org --project ldn1-design \
  --netbox-url https://netbox.example.com

# Write it.
railyard-sync export netbox --org my-org --project ldn1-design \
  --netbox-url https://netbox.example.com --apply
```

- **A dry run by default.** Without `--apply` the export prints the planned changes, per object type
  and one line per object, and writes nothing. `--json` prints the full result instead.
- **Ownership tag.** Every object the export creates carries the project's ownership tag
  (`RY:<project name> (<digest>)`), keyed by the Railyard URL and the project's id, and only objects
  with that tag are ever updated or deleted. Objects that already exist in NetBox (a site, a rack, a
  manufacturer or device type) are used as they are and never changed; an object that would clash
  with one the export does not own (a device of the same name in the site, a port already cabled) is
  reported as a conflict and skipped, with what depends on it. Pass the same `--railyard-url` as the
  NetBox plugin, if you use it, so both recognise the same objects.
- **Deletes are opt-in.** Owned objects that are gone from Railyard are reported as stale and kept;
  `--allow-deletes` removes them, except where NetBox would cascade to, or disconnect, objects the
  export does not own.
- **Versions.** The document is written for the NetBox release the server reports (`/api/status/`),
  NetBox 4.0 to 4.6; `--netbox-version 4.4` chooses one. `--change-request ID` exports a merge
  request's draft instead of main. `--import-components` gives new device types their component
  templates from the netbox-community devicetype-library.
- **A paid deliverable on hosted Railyard.** The NetBox sync document is a deliverable, so the estate's
  plan must include deliverables and its rack count must be within the plan's deliverable limit. A
  refusal exits `3` with the plans that would allow it and whether a Project Pass for the estate would;
  nothing is written to NetBox. A self-hosted Railyard with billing off allows it.

Exit codes: `0` done, `1` NetBox refused writes or there are conflicts (both listed; the rest is
written, and running the export again converges), `2` usage, `3` refused by the Railyard plan.

### Nautobot

Nautobot 2.x has no sites: every place is a location, typed by a location type (Region → Site → Building →
Room, as your Nautobot defines them). An import is built around the locations you name with `--location`
(a name, or the UUID when a name is not unique): each becomes a data centre in Railyard, the locations below
it its rooms and rows (keeping their location types), and the ones above it the groups it sits in.
`--all-locations` takes every location of the first type down the tree that may hold racks or devices (your
sites, typically).

```bash
export NAUTOBOT_TOKEN=…      # a Nautobot API token (read-only for an import)
export RAILYARD_TOKEN=ry_…

railyard-sync import nautobot --nautobot-url https://nautobot.example.com --location LDN1 \
  --org my-org --name "LDN1 baseline"

railyard-sync export nautobot --org my-org --project ldn1-design \
  --nautobot-url https://nautobot.example.com          # a dry run; add --apply to write
```

Everything else is as for NetBox: the same flags, refreshes, dry runs, exit codes and troubleshooting. What
differs on export:

- **Ownership.** Objects the export creates carry the project's ownership tag, enabled for every model it
  tags. Nautobot's location types, statuses, manufacturers and roles cannot be tagged, so for those the
  export records the owner in a `railyard_owner` custom field (it creates the field when the token may; if
  not, it creates those objects without an owner and never changes them afterwards). Devices carry their
  Railyard id in the `railyard_id` custom field, as in NetBox.
- **Statuses, roles and location types are shared.** One that already exists is used as it is; if it is not
  enabled for what uses it (a role for devices, a location type for racks), the objects that need it are
  reported as conflicts rather than written. Design tags are created enabled for racks and devices; an
  existing tag that is not is left off those objects, with a warning.
- **Versions.** Nautobot 2.0 to 2.4 (tested with 2.4); 3.x is read with a warning; 1.x is refused.
- **A paid deliverable on hosted Railyard**, like the NetBox document.

## Troubleshooting

Results (the import summary, the re-import diff, the export report, `--json`) go to **stdout**; progress,
warnings and errors go to **stderr**, so `--json` output and redirected results stay clean.

| Flag | What it does |
|---|---|
| *(none)* | One progress line per step, with counts and timings: reading NetBox, fetching the estate, building, merging, checking, saving. |
| `-v`, `--verbose` | Also every HTTP request: method, path, status, time, sizes and the request id (Railyard's or NetBox's). |
| `-q`, `--quiet` | Errors only. |
| `--debug` | `--verbose`, plus a traceback for an error. |
| `--log-file FILE` | Everything at debug level, with timestamps, appended to `FILE` (readable only by you), whatever the console shows. |
| `--no-validate` | Save without checking the document with Railyard first (import). |
| `--save-document FILE` | Keep a copy of the document sent to Railyard, also when the save succeeds (import; readable only by you). |
| `--failed-dir DIR` | Where a refused save keeps its document (import; default: the current directory). |

No log, at any level, contains a token, an `Authorization` header or a request or response body; they hold
sizes instead. Logs do name hosts, estate ids, site slugs and API paths.

**Errors say what to do.** A Railyard error names the request (`PUT /api/projects/…`), the status, Railyard's
message and code, the problems it listed (by rack and device name) and the request id to quote, then the fix:
an expired token (401: create a new one under **User settings → API tokens**), a missing role or terms
acceptance (403), a taken name (409), an estate that changed meanwhile (412: run the import again, it merges
onto the latest revision), a document too large (413: its size and the limit; import sites into separate
estates), or a Railyard fault (5xx: a Railyard bug, not your data). NetBox errors name the endpoint, NetBox's
`detail` and request id, and for a 403 the permission to grant (such as `view` on `dcim.rack`). A busy
Railyard (429/503) is retried, after the wait it asks for and at most three times, for reads and for saves
that name the revision they replace.

**Railyard's check.** Before saving, the import sends the document to Railyard's `/api/validate`. Errors stop
it, listed by rack and device, and nothing is saved; warnings are reported and the import goes ahead. An
error the estate already had before the import does not stop a refresh, and a Railyard that cannot run the
check only warns. `--dry-run` runs the check too; `--no-validate` skips it.

**Failed documents.** When Railyard refuses a save, the document that was sent is written to
`railyard-sync-failed-<project id>-<UTC time>.json` in the current directory (or `--failed-dir`), with mode
`0600`, and the error says where. It holds your estate's design: share it only with Railyard support.

**Reporting a problem.** Run the command again with `-v --log-file railyard-sync.log` and send:

1. the complete error from stderr, which ends with how far the run got and the request id to quote;
2. `railyard-sync.log`;
3. the `railyard-sync-failed-….json` it names, privately and only if Railyard support asks for it.

## Development

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/pytest
```

See `CLAUDE.md` for the design: the snapshot contract, the identity scheme and the re-import policy. With
`RAILYARD_BIN` set to a built Railyard CLI (`go build -o /tmp/railyard ./cmd/railyard` in `railyard/backend`)
the suite also round-trips imported estates back out through Railyard's own `netbox-sync` and `nautobot-sync`
documents.

## Licence

Apache-2.0.
