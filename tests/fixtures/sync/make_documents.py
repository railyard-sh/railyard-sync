"""Regenerate the NetBox sync-document fixtures from Railyard's real NetBox CSV bundle.

Until Railyard serves ``POST /api/projects/{id}/deliverables/netbox-sync``, the fixtures are built the
way that deliverable is specified: one JSON row per row of the bundle Railyard's own exporter writes,
with the bundle's column names, plus a ``railyard: {kind, id}`` identity. Build the Railyard CLI and
run this script from the repository root:

    cd ../railyard/backend && go build -o /tmp/railyard ./cmd/railyard && cd -
    .venv/bin/python tests/fixtures/sync/make_documents.py --railyard-cli /tmp/railyard

It exports each ``*-project.json`` here with ``railyard export --format netbox-csv`` and writes
``netbox-sync-<name>[-<netbox version>].json`` beside it. The identities are recovered from the
project (the CSV does not carry them); see ``_identities``.
"""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import re
import subprocess
import tempfile

HERE = pathlib.Path(__file__).parent

# CSV file prefix -> object kind. Location batches (04a, 04b-…) are concatenated in file order.
KINDS = [
    ("00a-tags", "tags"),
    ("01-manufacturers", "manufacturers"),
    ("02-device-types", "device-types"),
    ("03-device-roles", "device-roles"),
    ("04-sites", "sites"),
    ("04a-locations", "locations"),
    ("04b-locations", "locations"),
    ("05-racks", "racks"),
    ("06-devices", "devices"),
    ("07-interfaces", "interfaces"),
    ("08-rear-ports", "rear-ports"),
    ("09-front-ports", "front-ports"),
    ("10-power-outlets", "power-outlets"),
    ("11-power-ports", "power-ports"),
    ("12-cables", "cables"),
]
ALL_KINDS = list(dict.fromkeys(kind for _, kind in KINDS))

# (project file stem, NetBox versions to write a document for)
DOCUMENTS = [("example", ["4.5"]), ("cabled", ["4.5", "4.4"])]

_ALIAS = re.compile(r" \[[0-9a-f]{10}\]$")


def _read_bundle(directory: pathlib.Path) -> dict[str, list[dict]]:
    objects: dict[str, list[dict]] = {kind: [] for kind in ALL_KINDS}
    for path in sorted(directory.glob("*.csv")):
        kind = next((k for prefix, k in KINDS if path.name.startswith(prefix)), None)
        if kind is None:
            raise SystemExit(f"unexpected bundle file {path.name}")
        with path.open(newline="", encoding="utf-8") as fh:
            objects[kind].extend(dict(row) for row in csv.DictReader(fh))
    return objects


def _identities(project: dict, objects: dict[str, list[dict]]) -> None:
    """Attach ``railyard: {kind, id}`` to every row, recovered from the project by name."""
    containers = project.get("containers", [])
    placements = {p.get("label"): p for r in project.get("racks", []) for p in r.get("placements", [])}
    racks = {r["name"]: r for r in project.get("racks", [])}
    catalogue = {(c["manufacturer"], c["model"]): c["key"] for c in project.get("catalogue", [])}
    cables = {c.get("label"): c["id"] for c in project.get("cables", []) if c.get("label")}
    by_id = {c["id"]: c for c in containers}

    def container(name: str, site: str | None) -> str:
        base = _ALIAS.sub("", name)
        for c in containers:
            if c["name"] != base:
                continue
            if site is None:
                return c["id"]
            parent = by_id.get(c.get("parentId", ""))
            while parent is not None:
                if parent["name"] == site:
                    return c["id"]
                parent = by_id.get(parent.get("parentId", ""))
        return ""

    def placement_id(device: str) -> str:
        return placements.get(device, {}).get("id", "")

    for row in objects["tags"]:
        row["railyard"] = {"kind": "tag", "id": row["name"]}
    for row in objects["manufacturers"]:
        row["railyard"] = {"kind": "manufacturer", "id": row["name"]}
    for row in objects["device-types"]:
        row["railyard"] = {"kind": "catalogue", "id": catalogue.get((row["manufacturer"], row["model"]), "")}
    for row in objects["device-roles"]:
        row["railyard"] = {"kind": "role", "id": row["name"]}
    for row in objects["sites"]:
        row["railyard"] = {"kind": "container", "id": container(row["name"], None)}
    for row in objects["locations"]:
        row["railyard"] = {"kind": "container", "id": container(row["name"], row["site"])}
    for row in objects["racks"]:
        row["railyard"] = {"kind": "rack", "id": racks.get(row["name"], {}).get("id", "")}
    for row in objects["devices"]:
        row["railyard"] = {"kind": "placement", "id": placement_id(row["name"])}
    for kind in ("interfaces", "rear-ports", "front-ports", "power-outlets", "power-ports"):
        for row in objects[kind]:
            row["railyard"] = {"kind": "port", "id": f"{placement_id(row['device'])}:{row['name']}"}
    power = {link["device"]["placementId"]: link["id"] for link in project.get("powerLinks", [])}
    for row in objects["cables"]:
        if row["side_a_type"] == "dcim.powerport":
            row["railyard"] = {"kind": "powerLink", "id": power.get(placement_id(row["side_a_device"]), "")}
        else:
            row["railyard"] = {"kind": "cable", "id": cables.get(row["label"], "")}


def build(cli: str, stem: str, version: str) -> dict:
    project_path = HERE / f"{stem}-project.json"
    project = json.loads(project_path.read_text())
    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run(
            [cli, "export", "--format", "netbox-csv", "--netbox-version", version, str(project_path), "-o", tmp],
            check=True,
            capture_output=True,
            text=True,
        )
        objects = _read_bundle(pathlib.Path(tmp))
    _identities(project, objects)
    warnings = [line[len("warning: ") :] for line in result.stderr.splitlines() if line.startswith("warning: ")]
    return {
        "format": "railyard-netbox-sync",
        "version": 1,
        "netboxVersion": version,
        "project": {"id": project["id"], "name": project["name"], "revision": 1},
        "objects": objects,
        "warnings": warnings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--railyard-cli", required=True, help="path to a built railyard CLI binary")
    args = parser.parse_args()
    for stem, versions in DOCUMENTS:
        for i, version in enumerate(versions):
            doc = build(args.railyard_cli, stem, version)
            suffix = "" if i == 0 else f"-{version}"
            out = HERE / f"netbox-sync-{stem}{suffix}.json"
            out.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"wrote {out.relative_to(HERE.parent.parent.parent)}")


if __name__ == "__main__":
    main()
