"""DCIM values → Railyard values: the reverse of Railyard's export maps.

Railyard's NetBox export (``railyard/backend/internal/export/dcim_types.go`` and ``cabling.go``) maps a
Railyard connector, cable media or outlet family onto a NetBox slug. The importer goes the other way,
so every function here is chosen to round-trip through those maps: a value imported from a NetBox slug
exports back to the same slug wherever Railyard can express it.

Connector classification is a port of the catalogue converter that builds Railyard's device catalogue
from the NetBox device-type library (``railyard/scripts/lib/catalogue_component_types.rb`` and
``catalogue_power_inlets.rb``), so a custom device type built here reads like a catalogue entry: a
port's ``type`` is the connector family the editor reasons about (``SFP+``, ``LC``), and its
``netboxType`` keeps the upstream slug when it says more than the family's usual one.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable

# ---- connectors (catalogue_component_types.rb) -------------------------------------------------

_X2 = re.compile(r"(?:\A|[^a-z0-9])x2(?:\Z|[^a-z0-9])")

# (Railyard family, the family's usual NetBox type, matcher), most specific first: "qsfp28" contains
# "sfp28", and "osfp"/"qsfpdd" contain "sfp".
INTERFACE_FAMILIES: list[tuple[str, str, Callable[[str], bool]]] = [
    ("RJ45", "1000base-t", lambda t: "8p8c" in t or "base-t" in t),
    ("OSFP-RHS", "400gbase-x-osfp-rhs", lambda t: "osfp" in t and "rhs" in t),
    ("OSFP-XD", "800gbase-x-osfp-xd", lambda t: "osfp" in t and "xd" in t),
    ("OSFP", "400gbase-x-osfp", lambda t: "osfp" in t),
    ("QSFP-DD", "400gbase-x-qsfpdd", lambda t: "qsfpdd" in t or "qsfp-dd" in t),
    ("QSFP112", "400gbase-x-qsfp112", lambda t: "qsfp112" in t),
    ("QSFP56", "200gbase-x-qsfp56", lambda t: "qsfp56" in t),
    ("QSFP28", "100gbase-x-qsfp28", lambda t: "qsfp28" in t),
    ("QSFP+", "40gbase-x-qsfpp", lambda t: "qsfp" in t),
    ("SFP-DD", "100gbase-x-sfpdd", lambda t: "sfpdd" in t or "sfp-dd" in t),
    ("DSFP", "100gbase-x-dsfp", lambda t: "dsfp" in t),
    ("SFP112", "100gbase-x-sfp112", lambda t: "sfp112" in t),
    ("SFP56", "50gbase-x-sfp56", lambda t: "sfp56" in t),
    ("SFP28", "25gbase-x-sfp28", lambda t: "sfp28" in t),
    ("SFP+", "10gbase-x-sfpp", lambda t: "sfpp" in t or "sfp+" in t),
    ("SFP", "1000base-x-sfp", lambda t: "sfp" in t),
    ("CFP8", "400gbase-x-cfp8", lambda t: "cfp8" in t),
    ("CFP4", "100gbase-x-cfp4", lambda t: "cfp4" in t),
    ("CFP2", "100gbase-x-cfp2", lambda t: "cfp2" in t),
    ("CDFP", "400gbase-x-cdfp", lambda t: "cdfp" in t),
    ("CFP", "100gbase-x-cfp", lambda t: "cfp" in t),
    ("XFP", "10gbase-x-xfp", lambda t: "xfp" in t),
    ("XENPAK", "10gbase-x-xenpak", lambda t: "xenpak" in t),
    ("X2", "10gbase-x-x2", lambda t: bool(_X2.search(t))),
    ("GBIC", "1000base-x-gbic", lambda t: "gbic" in t),
]

PORT_FAMILIES: list[tuple[str, str, Callable[[str], bool]]] = [
    ("RJ45", "8p8c", lambda t: t == "8p8c"),
    ("MPO", "mpo", lambda t: t.startswith("mpo")),
    ("LC", "lc", lambda t: bool(re.fullmatch(r"lc(?:-(?:apc|upc|pc))?", t))),
    ("SC", "sc", lambda t: bool(re.fullmatch(r"sc(?:-(?:apc|upc|pc))?", t))),
    ("ST", "st", lambda t: t == "st"),
]

# NetBox's virtual interface types and radios cannot terminate a cable (catalogue #775).
VIRTUAL_INTERFACES = frozenset({"virtual", "bridge", "lag"})
RADIO_INTERFACES = frozenset({"other-wireless", "gsm", "cdma", "lte", "4g", "5g"})
RADIO_INTERFACE_PREFIXES = ("ieee802.11", "ieee802.15")


def _slug_of(value: str | None) -> str | None:
    slug = (value or "").strip().lower()
    return None if not slug or slug == "other" else slug


def cableable_interface(type_slug: str | None) -> bool:
    """Whether a NetBox interface type is a physical port a cable can be patched into: not a LAG,
    bridge or virtual interface, and not a radio."""
    slug = (type_slug or "").strip().lower()
    if slug in VIRTUAL_INTERFACES or "virtual" in slug:
        return False
    return not (slug in RADIO_INTERFACES or slug.startswith(RADIO_INTERFACE_PREFIXES))


def _classify(type_slug: str | None, families) -> tuple[str | None, str | None]:
    slug = _slug_of(type_slug)
    if slug is None:
        return None, None
    for name, usual, match in families:
        if match(slug):
            return name, (None if slug == usual else slug)
    return slug, None


def interface_connector(type_slug: str | None) -> tuple[str | None, str | None]:
    """``(connector, netbox_type)`` for a NetBox interface type: the Railyard family (or the slug when
    it names none), and the slug itself when it says more than the family's usual type."""
    slug = _slug_of(type_slug)
    if slug is None or slug in VIRTUAL_INTERFACES:
        return None, None
    return _classify(slug, INTERFACE_FAMILIES)


def port_connector(type_slug: str | None) -> tuple[str | None, str | None]:
    """``(connector, netbox_type)`` for a NetBox front/rear-port type (``lc-apc`` → ``("LC", "lc-apc")``)."""
    return _classify(type_slug, PORT_FAMILIES)


def instance_connector(type_slug: str | None, kind: str) -> str | None:
    """The connector a device's own port records. A port has no ``netboxType`` of its own, so a type
    that says more than its family's usual one (``10gbase-t``) keeps the slug, which Railyard's export
    writes back unchanged; the usual type reads as its family (``SFP+``)."""
    connector, netbox_type = interface_connector(type_slug) if kind == "interface" else port_connector(type_slug)
    return netbox_type or connector


def interface_media(types_and_mgmt: Iterable[tuple[str, bool]]) -> str | None:
    """The connector most data interfaces have, the first listed on a tie. Management-only interfaces
    count only when there is nothing else."""
    items = list(types_and_mgmt)
    data = [t for t, mgmt in items if not mgmt] or [t for t, _ in items]
    counts: dict[str | None, int] = {}
    for type_slug in data:
        media = interface_connector(type_slug)[0]
        counts[media] = counts.get(media, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda item: item[1])[0]  # max keeps the first on a tie


# ---- power (catalogue_power_inlets.rb, catalogue_component_types.rb) ----------------------------

_IEC = re.compile(r"iec-60320-(c\d+[a-z]?)", re.IGNORECASE)
_NEMA = re.compile(r"nema-(.+)", re.IGNORECASE)


def power_connector(type_slug: str | None) -> str | None:
    """A power port's connector as Railyard names it: ``iec-60320-c14`` → ``C14``, ``nema-5-15p`` →
    ``NEMA 5-15P``; any other declared type is kept as it is. The export maps these back to the slug."""
    value = (type_slug or "").strip()
    if not value or value == "other":
        return None
    if m := _IEC.fullmatch(value):
        return m.group(1).upper()
    if m := _NEMA.fullmatch(value):
        return f"NEMA {m.group(1).upper()}"
    return value


def outlet_family(type_slug: str | None) -> str | None:
    """A power outlet's family: ``C13``, ``NEMA 5-20R``, or the slug for any other type."""
    slug = _slug_of(type_slug)
    if slug is None:
        return None
    if m := _IEC.fullmatch(slug):
        return m.group(1).upper()
    if slug.startswith("nema-"):
        return "NEMA " + slug.removeprefix("nema-").upper()
    return slug


def outlet_type_of(types: Iterable[str | None]) -> str | None:
    """A PDU's outlet families, most numerous first (``C13/C19``); the export gives every outlet the first."""
    counts: dict[str, int] = {}
    for type_slug in types:
        family = outlet_family(type_slug)
        if family:
            counts[family] = counts.get(family, 0) + 1
    if not counts:
        return None
    order = list(counts)
    return "/".join(sorted(order, key=lambda family: (-counts[family], order.index(family))))


# ---- cables (cabling.go cableType / cableColour) ----------------------------------------------

# Each media string is one Railyard's cableType maps back onto the slug.
_CABLE_MEDIA = {
    "cat3": "Cat3",
    "cat5": "Cat5",
    "cat5e": "Cat5e",
    "cat6": "Cat6",
    "cat6a": "Cat6A",
    "cat7": "Cat7",
    "cat7a": "Cat7A",
    "cat8": "Cat8",
    "mmf": "Multimode",
    "mmf-om1": "OM1",
    "mmf-om2": "OM2",
    "mmf-om3": "OM3",
    "mmf-om4": "OM4",
    "mmf-om5": "OM5",
    "smf": "Single-mode",
    "smf-os1": "OS1",
    "smf-os2": "OS2",
    "dac-active": "Active DAC",
    "dac-passive": "DAC",
    "aoc": "AOC",
    "coaxial": "Coax",
    "mrj21-trunk": "MRJ21",
}

_HEX_COLOUR = re.compile(r"[0-9a-f]{3}|[0-9a-f]{6}")


def cable_media(type_slug: str | None) -> str:
    """A cable's Railyard media from its DCIM cable type; an unknown type keeps its slug."""
    slug = (type_slug or "").strip().lower()
    return _CABLE_MEDIA.get(slug, slug)


def cable_colour(color: str | None) -> str:
    """NetBox's ``rrggbb`` as Railyard's ``#rrggbb``; anything else is dropped."""
    value = (color or "").strip().lower().removeprefix("#")
    if not _HEX_COLOUR.fullmatch(value):
        return ""
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    return "#" + value


# ---- racks and statuses ----------------------------------------------------------------------

RAIL_WIDTH_MM = {19: 600, 23: 800}
WIDTH_23_INCH_MIN_MM = 700  # model.Rack.WidthInches: widthMm >= 700 exports as a 23" rail


def rack_width_mm(width_in: int | None, outer_width_mm: float | None) -> int:
    """A rack's Railyard width: its outer width when known, else 600 mm for 19" and 800 mm for 23"."""
    if outer_width_mm:
        return max(1, round(outer_width_mm))
    return RAIL_WIDTH_MM.get(int(width_in or 19), 600)


def width_inches(width_mm: int) -> int:
    """The rail width Railyard's export derives from a width in millimetres."""
    return 23 if width_mm >= WIDTH_23_INCH_MIN_MM else 19


def status_label(slug: str | None) -> str:
    """A DCIM status slug as the label Railyard shows (``active`` → ``Active``); the export slugifies
    it back."""
    words = (slug or "").strip().replace("-", " ").replace("_", " ")
    return words[:1].upper() + words[1:] if words else ""


# ---- ordering and text limits ------------------------------------------------------------------

_DIGITS = re.compile(r"(\d+)")


def natural_key(value: str | None) -> tuple:
    """Sort key that orders embedded numbers numerically: ``Outlet 2`` before ``Outlet 10``."""
    parts = _DIGITS.split(value or "")
    return tuple((0, int(part), "") if part.isdigit() else (1, 0, part.lower()) for part in parts if part != "")


def digest(value: str) -> str:
    """A short, stable digest of a value: what keeps a shortened name or id unique."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]


def fit(value: str, limit: int) -> str:
    """``value`` within ``limit`` characters. A longer value keeps its start and gains ``~<digest>``,
    so two long names that share a prefix stay distinct."""
    if len(value) <= limit:
        return value
    suffix = "~" + digest(value)
    return value[: limit - len(suffix)].rstrip() + suffix


def identity_key(value: str) -> str:
    """Railyard's case-insensitive name identity (model.IdentityKey): trimmed, each code point
    lower-cased on its own."""
    return "".join(ch.lower() if len(ch.lower()) == 1 else ch for ch in value.strip())
