"""Ownership policy for an export into a DCIM, without any DCIM imports.

Ported from ``netbox_railyard/policy.py`` (the NetBox plugin) so the plugin and the command-line export
agree on which objects a Railyard project owns: both mark them with the same ownership tag, keyed by the
Railyard instance and the project's immutable id. A NetBox that has been synced by the plugin can be
synced again from the command line (and the other way round) without either treating the other's
objects as foreign, provided both name the same Railyard URL.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from urllib.parse import urlsplit

from .mappings import slugify

#: The Railyard instance a sync document comes from when the caller names none.
DEFAULT_RAILYARD_URL = "https://railyard.sh"

#: Written into the tag's description; its exact value is how the sync recognises its own tag.
TAG_MARKER = "railyard-sync:v1"

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class TagSpec:
    """The DCIM tag that marks the objects one Railyard project (on one Railyard instance) owns."""

    slug: str
    name: str
    description: str


def instance_key(url: str) -> str:
    """A stable identity for a Railyard instance: host[:port][/path], lower-cased, ignoring the scheme,
    default ports and a trailing slash (so ``https://Railyard.sh/`` and ``http://railyard.sh`` match)."""
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower()
    port = parts.port
    if port and port not in (80, 443):
        host = f"{host}:{port}"
    return host + parts.path.rstrip("/")


def ownership_tag(base_url: str, project_id: str, project_name: str = "") -> TagSpec:
    """The ownership tag for a project, keyed by the Railyard instance and the project's immutable id.

    Two projects with the same name, or the same project id on two Railyard instances, get different
    tags, so one sync can never treat another's objects as its own. The name carries a short digest for
    the same reason (NetBox tag names are unique too) and follows project renames; the slug and the
    description never change for a given project.
    """
    pid = (project_id or "").strip()
    if not pid:
        raise ValueError("The Railyard project has no id, so its objects can't be tracked safely.")
    instance = instance_key(base_url)
    digest = hashlib.sha256(f"{instance}\n{pid}".encode()).hexdigest()
    label = " ".join((project_name or pid).split())[:80]
    return TagSpec(
        slug=f"ry-{slugify(pid)[:60]}-{digest[:10]}",
        name=f"RY:{label} ({digest[:8]})",
        description=f"Managed by the Railyard sync. {TAG_MARKER} project={pid} instance={instance}"[:200],
    )


def insecure_url(url: str) -> bool:
    """True when ``url`` would send a token in clear text (plain http to anything but loopback)."""
    parts = urlsplit(url or "")
    if parts.scheme == "https":
        return False
    return not (parts.scheme == "http" and (parts.hostname or "") in _LOOPBACK_HOSTS)
