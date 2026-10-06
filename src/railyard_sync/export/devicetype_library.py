"""Resolve a Railyard catalogue entry to a device-type spec, preferring the canonical
netbox-community/devicetype-library definition.

Railyard's catalogue ``key`` is the devicetype-library slug, and the library stores files as
``device-types/<Manufacturer>/<Model>.yaml``. We fetch that YAML (pinned ``ref``, on-disk cache) and
read the canonical manufacturer/model/slug/u_height/is_full_depth/part_number plus the full component
templates (kept for a future full-import mode). When the library has no match we fall back to building
a minimal spec from the Railyard catalogue entry, so the sync never blocks on a missing library file.

The HTTP fetcher is injectable (``fetcher(url) -> str | None``) so tests need no network.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import quote

import yaml

from .mappings import slugify

RAW_BASE = "https://raw.githubusercontent.com/netbox-community/devicetype-library"

#: The library commit fetched by default. A full commit SHA rather than ``master`` so a sync is
#: reproducible and an upstream change (or compromise) of the moving branch can't alter what NetBox
#: receives. Bump deliberately after reviewing the upstream diff.
DEFAULT_REF = "0451a7c5977351e7a43fbff54eb44f26ed85344f"

#: A git ref we are prepared to put in a URL: a commit SHA, or a branch/tag name without ``..``.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")

#: Library YAML files are a few KiB; refuse anything absurdly large.
MAX_YAML_BYTES = 1024 * 1024

Fetcher = Callable[[str], str | None]


@dataclass
class DeviceTypeSpec:
    manufacturer: str
    model: str
    slug: str
    u_height: int
    is_full_depth: bool
    part_number: str = ""
    source: str = "catalogue"  # "library" | "catalogue"
    components: dict = field(default_factory=dict)  # full templates from the library YAML


def _default_fetcher(url: str) -> str | None:
    import requests

    resp = requests.get(url, timeout=30, allow_redirects=False)
    if resp.status_code == 200 and len(resp.content) <= MAX_YAML_BYTES:
        return resp.text
    return None


def _path_segment(value: str) -> str | None:
    """URL-escape one path segment (``/`` included), or ``None`` if it can't name a library file."""
    if not value or value in (".", "..") or "\x00" in value:
        return None
    return quote(value, safe="")


def _coerce_u_height(value: object, fallback: int) -> int:
    try:
        return int(round(float(value)))  # library allows 0.5U increments; the sketch uses whole U
    except (TypeError, ValueError):
        return fallback


class DeviceTypeLibrary:
    """Fetches and caches device-type definitions from netbox-community/devicetype-library."""

    #: component keys we carry through from the library YAML (for an optional future full-import)
    COMPONENT_KEYS = (
        "interfaces",
        "front-ports",
        "rear-ports",
        "power-ports",
        "power-outlets",
        "console-ports",
        "console-server-ports",
        "device-bays",
        "module-bays",
    )

    def __init__(
        self,
        ref: str = DEFAULT_REF,
        *,
        fetcher: Fetcher | None = None,
        cache_dir: str | None = None,
    ) -> None:
        ref = (ref or DEFAULT_REF).strip()
        if not _REF_RE.match(ref) or ".." in ref:
            raise ValueError(f"devicetype_library_ref {ref!r} is not a valid git ref")
        self.ref = ref
        self._fetch = fetcher or _default_fetcher
        self._cache_dir = cache_dir
        self._mem: dict[tuple[str, str], DeviceTypeSpec] = {}

    def _url(self, manufacturer: str, model: str) -> str | None:
        mfr, mdl = _path_segment(manufacturer), _path_segment(model)
        if mfr is None or mdl is None:
            return None
        return f"{RAW_BASE}/{self.ref}/device-types/{mfr}/{mdl}.yaml"

    def _read_yaml(self, manufacturer: str, model: str) -> dict | None:
        # on-disk cache first
        cache_file = None
        if self._cache_dir:
            cache_file = os.path.join(self._cache_dir, slugify(f"{manufacturer}-{model}") + ".yaml")
            if os.path.exists(cache_file):
                with open(cache_file, encoding="utf-8") as fh:
                    return yaml.safe_load(fh.read())
        url = self._url(manufacturer, model)
        if url is None:
            return None
        text = self._fetch(url)
        if text is None:
            return None
        if cache_file:
            os.makedirs(self._cache_dir, exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as fh:
                fh.write(text)
        return yaml.safe_load(text)

    def resolve(self, catalogue_entry: dict) -> DeviceTypeSpec:
        """A DeviceTypeSpec for a Railyard catalogue entry — library-sourced when available."""
        manufacturer = (catalogue_entry.get("manufacturer") or "").strip()
        model = (catalogue_entry.get("model") or "").strip()
        cat_u = int(catalogue_entry.get("uHeight") or 1)
        key = (manufacturer, model)
        if key in self._mem:
            return self._mem[key]

        spec = self._from_catalogue(catalogue_entry, manufacturer, model, cat_u)
        try:
            data = self._read_yaml(manufacturer, model)
        except Exception:
            data = None  # a malformed/unreachable library file must not fail the sync
        if data:
            spec = self._from_library(data, manufacturer, model, cat_u)

        self._mem[key] = spec
        return spec

    def _from_catalogue(self, entry: dict, manufacturer: str, model: str, u: int) -> DeviceTypeSpec:
        return DeviceTypeSpec(
            manufacturer=manufacturer,
            model=model,
            slug=(entry.get("key") or slugify(f"{manufacturer}-{model}")),
            u_height=u,
            is_full_depth=bool(entry.get("fullDepth", True)),
            part_number=entry.get("partNumber", "") or "",
            source="catalogue",
        )

    def _from_library(self, data: dict, manufacturer: str, model: str, cat_u: int) -> DeviceTypeSpec:
        components = {k: data.get(k, []) for k in self.COMPONENT_KEYS if data.get(k)}
        return DeviceTypeSpec(
            manufacturer=data.get("manufacturer", manufacturer),
            model=data.get("model", model),
            slug=data.get("slug") or slugify(f"{manufacturer}-{model}"),
            u_height=_coerce_u_height(data.get("u_height"), cat_u),
            is_full_depth=bool(data.get("is_full_depth", True)),
            part_number=data.get("part_number", "") or "",
            source="library",
            components=components,
        )
