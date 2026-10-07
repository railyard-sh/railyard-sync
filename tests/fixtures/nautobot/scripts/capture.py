"""Capture the loader fixtures from a scratch Nautobot that ``populate.py`` filled.

    NAUTOBOT_URL=http://127.0.0.1:8089 python capture.py

Writes each list endpoint's results (at the depth the loader reads it) and ``/api/status/`` to the directory above,
with the server's URL replaced by https://nautobot.example.com.
"""

import json
import os
import pathlib
import urllib.request

BASE = os.environ.get("NAUTOBOT_URL", "http://127.0.0.1:8089").rstrip("/")
TOKEN = os.environ.get("NAUTOBOT_TOKEN", "0f" * 20)
OUT = pathlib.Path(__file__).parent.parent
DEPTH = {
    "locations": 1,
    "location-types": 1,
    "racks": 1,
    "devices": 1,
    "device-types": 1,
    "interface-templates": 0,
    "front-port-templates": 0,
    "rear-port-templates": 0,
    "power-port-templates": 0,
    "power-outlet-templates": 0,
    "console-port-templates": 0,
    "interfaces": 0,
    "front-ports": 0,
    "rear-ports": 0,
    "power-ports": 0,
    "power-outlets": 0,
    "console-ports": 0,
    "cables": 1,
    "power-panels": 1,
    "power-feeds": 1,
}


def get(path: str) -> dict:
    request = urllib.request.Request(
        BASE + path, headers={"Authorization": f"Token {TOKEN}", "Accept": "application/json"}
    )
    with urllib.request.urlopen(request) as response:  # noqa: S310 - a scratch server on loopback
        return json.load(response)


def main() -> None:
    for endpoint, depth in DEPTH.items():
        results = get(f"/api/dcim/{endpoint}/?limit=1000&depth={depth}")["results"]
        text = json.dumps(results, indent=1, ensure_ascii=False).replace(BASE, "https://nautobot.example.com")
        (OUT / f"{endpoint}.json").write_text(text + "\n")
        print(endpoint, len(results))
    status = get("/api/status/")
    status.pop("installed-apps", None)
    (OUT / "status.json").write_text(json.dumps(status, indent=1) + "\n")


if __name__ == "__main__":
    main()
