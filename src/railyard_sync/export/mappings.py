"""Value mappings ported faithfully from Railyard's Go export engine.

Every function here mirrors a function in ``railyard/backend/internal/export/netbox.go`` or
``cabling.go`` (or a helper in ``model.go``). Keep them in lockstep with the Go originals — the whole
point is that the objects this plugin creates match Railyard's own NetBox CSV export byte-for-byte on
these fields. Go source references are given per function.
"""

from __future__ import annotations

# ---- defaults (export.go Options.WithDefaults) -----------------------------
FALLBACK_SITE = "Railyard-Unassigned"
DEFAULT_STATUS = "Active"
DEFAULT_DEVICE_ROLE = "unassigned"
DEFAULT_ROLE_COLOR = "9e9e9e"  # neutral grey NetBox uses for an unstyled role (netbox.go:38)

# ---- model defaults (model.go) ---------------------------------------------
DEFAULT_STARTING_U = 1
DEFAULT_RACK_WIDTH_MM = 600
DEFAULT_U_HEIGHT = 42


def slugify(s: str) -> str:
    """Lowercase, collapse each run of non-alphanumerics to a single hyphen, trim hyphens.

    Port of ``slugify`` (netbox.go:555-571)."""
    s = (s or "").strip().lower()
    out: list[str] = []
    prev_hyphen = False
    for ch in s:
        if ("a" <= ch <= "z") or ("0" <= ch <= "9"):
            out.append(ch)
            prev_hyphen = False
        elif not prev_hyphen:
            out.append("-")
            prev_hyphen = True
    return "".join(out).strip("-")


def width_inches(width_mm: int | float | None) -> int:
    """Rail width in inches: 23 for >= 700mm, else 19. Port of ``Rack.WidthInches`` (model.go:474-479);
    a missing/zero width falls back to the 600mm default (=> 19)."""
    mm = int(width_mm or DEFAULT_RACK_WIDTH_MM)
    return 23 if mm >= 700 else 19


def face_for_netbox(face: str | None) -> str:
    """NetBox mounting face. ``rear`` stays rear; ``front`` and ``full`` both mount on the front.
    Port of ``faceForNetBox`` (netbox.go:581-586)."""
    return "rear" if (face or "").strip().lower() == "rear" else "front"


def status_slug(status: str | None, default: str = DEFAULT_STATUS) -> str:
    """A status as a NetBox slug, falling back to the default when empty (netbox.go:139-165)."""
    return slugify(status) or slugify(default)


def interface_type(media: str | None) -> str:
    """Media string -> interface speed/type enum. Port of ``interfaceType`` (cabling.go:314-331)."""
    m = (media or "").strip().upper()
    return {
        "RJ45": "1000base-t",
        "SFP": "1000base-x-sfp",
        "SFP+": "10gbase-x-sfpp",
        "SFP28": "25gbase-x-sfp28",
        "QSFP+": "40gbase-x-qsfpp",
        "QSFP28": "100gbase-x-qsfp28",
    }.get(m, "other")


def port_connector(media: str | None) -> str:
    """Media string -> front/rear-port connector type. Port of ``portConnector`` (cabling.go:298-311)."""
    m = (media or "").strip().upper()
    return {"LC": "lc", "SC": "sc", "MPO": "mpo", "ST": "st"}.get(m, "8p8c")


def outlet_type(family: str | None) -> str:
    """PDU outlet family -> DCIM power-outlet type. Port of ``outletType`` (cabling.go:334-348)."""
    u = (family or "").upper()
    if "C19" in u:
        return "iec-60320-c19"
    if "C21" in u:
        return "iec-60320-c21"
    if "C13" in u:
        return "iec-60320-c13"
    if "NEMA" in u:
        return "nema-5-15r"
    return "iec-60320-c13"  # the ubiquitous rack-PDU outlet


def inlet_type() -> str:
    """Device power inlet. Railyard does not model inlets; default the C14 that pairs with a C13
    outlet. Port of ``inletType`` (cabling.go:350-352)."""
    return "iec-60320-c14"
