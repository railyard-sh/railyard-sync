# railyard-sync

Bring an existing **NetBox** site into **[Railyard](https://railyard.sh)** as a design baseline, and
refresh it later without losing what you designed; then push the design back out into NetBox. It is
also the shared core of Railyard's NetBox (and, later, Nautobot) plugins.

> Status: **early development (0.1).** NetBox import and export work; Nautobot follows.

## What it does

```
NetBox REST API ──► Snapshot (sites, racks, devices, ports, cables, power) ──► Railyard project
                                                                                  │
                         re-import: merge into the existing estate ◄──────────────┘

Railyard project ──► NetBox sync document (a Railyard deliverable) ──► NetBox REST API (export)
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
  --railyard-url https://railyard.sh --org my-org --name "LDN1 baseline"

# Later: refresh that estate from NetBox (preview first).
railyard-sync import netbox --netbox-url https://netbox.example.com --site ldn1 \
  --railyard-url https://railyard.sh --org my-org --project ldn1-baseline --dry-run
```

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
railyard-sync export netbox --railyard-url https://railyard.sh --org my-org --project ldn1-design \
  --netbox-url https://netbox.example.com

# Write it.
railyard-sync export netbox --railyard-url https://railyard.sh --org my-org --project ldn1-design \
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

## Development

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check . && .venv/bin/pytest
```

See `CLAUDE.md` for the design: the snapshot contract, the identity scheme and the re-import policy.

## Licence

Apache-2.0.
