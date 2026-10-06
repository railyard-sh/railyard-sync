"""Regenerate the NetBox sync-document fixtures with Railyard's own exporter.

The documents are exactly what ``POST /api/projects/{id}/deliverables/netbox-sync`` serves, written
offline by the Railyard CLI. Build it and run this script from the repository root:

    cd ../railyard/backend && go build -o /tmp/railyard ./cmd/railyard && cd -
    .venv/bin/python tests/fixtures/sync/make_documents.py --railyard-cli /tmp/railyard

It exports each ``<name>-project.json`` here with ``railyard export --format netbox-sync`` and writes
``netbox-sync-<name>[-<netbox version>].json`` beside it. The server adds ``project.revision`` (and
``changeRequestId`` for a merge request's draft), which an offline export has no way to know; the
fixtures carry a revision so they look like the served document.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import tempfile

HERE = pathlib.Path(__file__).parent

# (project file stem, NetBox versions to write a document for; the first is the unsuffixed fixture)
DOCUMENTS = [("example", ["4.5"]), ("cabled", ["4.5", "4.4"])]


def build(cli: str, stem: str, version: str) -> dict:
    project_path = HERE / f"{stem}-project.json"
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "doc.json"
        subprocess.run(
            [cli, "export", "--format", "netbox-sync", "--netbox-version", version, str(project_path), "-o", str(out)],
            check=True,
            capture_output=True,
            text=True,
        )
        doc = json.loads(out.read_text())
    doc["project"].setdefault("revision", 1)
    return doc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--railyard-cli", required=True, help="path to a built railyard CLI binary")
    args = parser.parse_args()
    for stem, versions in DOCUMENTS:
        for i, version in enumerate(versions):
            doc = build(args.railyard_cli, stem, version)
            suffix = "" if i == 0 else f"-{version}"
            path = HERE / f"netbox-sync-{stem}{suffix}.json"
            path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"wrote {path.relative_to(HERE.parent.parent.parent)}")


if __name__ == "__main__":
    main()
