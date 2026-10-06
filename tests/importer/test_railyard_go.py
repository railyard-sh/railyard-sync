"""Built projects pass Railyard's own Go loader and save checks.

Two optional checks, each skipped unless its environment variable is set:

- ``RAILYARD_BIN``: a ``railyard`` CLI binary (``cd railyard/backend && go build -o <dir>/railyard
  ./cmd/railyard``). ``railyard export --format json`` loads the file with ``model.LoadStandalone``,
  which applies the structural limits, the schema bounds and the container checks; the NetBox CSV
  export then runs the rest of the export pipeline over it.
- ``RAILYARD_BACKEND``: a Railyard ``backend`` checkout, with ``go`` on the PATH. The checks in
  ``railyard_checks_test.go.txt`` are overlaid into its ``internal/model`` package (``go test
  -overlay``; the checkout is not modified) and run every check the server applies when it saves a
  new project: connections, power, rack names and bounds, free text, type references, device naming
  and ``Validate``'s errors.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import ldn1
import pytest
from importer_helpers import build
from test_build import _power_cable, _power_snapshot

HERE = pathlib.Path(__file__).parent


def _projects() -> dict[str, dict]:
    return {
        "ldn1": build(ldn1.snapshot(), catalogue=ldn1.FakeCatalogue()).project,
        "ldn1-custom": build(ldn1.snapshot(), prefix="nbt").project,
        "power": build(_power_snapshot(_power_cable("1", "11", "107"))).project,
    }


@pytest.fixture
def project_files(tmp_path) -> dict[str, pathlib.Path]:
    files = {}
    for name, project in _projects().items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(project))
        files[name] = path
    return files


@pytest.mark.skipif(not os.environ.get("RAILYARD_BIN"), reason="set RAILYARD_BIN to a railyard CLI binary")
def test_railyard_cli_loads_and_exports_the_project(project_files, tmp_path):
    binary = os.environ["RAILYARD_BIN"]
    for name, path in project_files.items():
        loaded = subprocess.run([binary, "export", "--format", "json", str(path)], capture_output=True, text=True)
        assert loaded.returncode == 0, f"{name}: {loaded.stderr}"
        assert json.loads(loaded.stdout)["id"] == json.loads(path.read_text())["id"]
        assert "unresolved" not in loaded.stderr.lower(), loaded.stderr
        out = tmp_path / f"{name}-netbox"
        exported = subprocess.run(
            [binary, "export", "--format", "netbox-csv", "-o", str(out), str(path)], capture_output=True, text=True
        )
        assert exported.returncode == 0, f"{name}: {exported.stderr}"
        assert (out / "06-devices.csv").exists()


@pytest.mark.skipif(
    not os.environ.get("RAILYARD_BACKEND") or shutil.which("go") is None,
    reason="set RAILYARD_BACKEND to a Railyard backend checkout (and put go on the PATH)",
)
def test_railyard_save_checks_accept_the_project(project_files, tmp_path):
    backend = pathlib.Path(os.environ["RAILYARD_BACKEND"]).resolve()
    checks = tmp_path / "railyard_sync_checks_test.go"
    checks.write_text((HERE / "railyard_checks_test.go.txt").read_text())
    overlay = tmp_path / "overlay.json"
    target = backend / "internal" / "model" / "zz_railyard_sync_test.go"
    overlay.write_text(json.dumps({"Replace": {str(target): str(checks)}}))
    for name, path in project_files.items():
        run = subprocess.run(
            [
                "go",
                "test",
                "-overlay",
                str(overlay),
                "-run",
                "^TestRailyardSyncImport$",
                "-count=1",
                "-v",
                "./internal/model/",
            ],
            cwd=backend,
            env={**os.environ, "RAILYARD_SYNC_PROJECT": str(path)},
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0, f"{name}:\n{run.stdout}\n{run.stderr}"
        assert "--- PASS: TestRailyardSyncImport" in run.stdout, run.stdout  # the overlay ran
