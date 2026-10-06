# railyard-sync

Bring an existing **NetBox** site into **[Railyard](https://railyard.sh)** as a design baseline, and
refresh it later without losing what you designed. It is also the shared core of Railyard's NetBox
(and, later, Nautobot) plugins.

> Status: **early development (0.1).** NetBox import is being built; Nautobot follows.

## What it does

```
NetBox REST API ──► Snapshot (sites, racks, devices, ports, cables, power) ──► Railyard project
                                                                                  │
                         re-import: merge into the existing estate ◄──────────────┘
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
save and replay what was read from NetBox, and `--out project.json` writes the Railyard document
without uploading it (open it in Railyard with **Open project file**).

## Development

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check . && .venv/bin/pytest
```

See `CLAUDE.md` for the design: the snapshot contract, the identity scheme and the re-import policy.

## Licence

Apache-2.0.
