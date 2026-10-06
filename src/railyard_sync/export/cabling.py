"""Resolve a project's structured cabling + power into a target-neutral plan.

A faithful Python port of ``buildCablingPlan`` (railyard/backend/internal/export/cabling.go). A DCIM
tool cables *components*, never bare devices, so this resolves every cable/power-link end to a concrete
termination (interface / front-port / rear-port / power-port / power-outlet), emitting the components
those cables touch — synthesizing an interface for a port-less device exactly as the Go export does.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..project import Project
from .mappings import (
    inlet_type,
    interface_type,
    outlet_type,
    port_connector,
)

# Content-type ids for cable terminations (dcim.* set, shared by NetBox and Nautobot). cabling.go:30-36
CT_INTERFACE = "dcim.interface"
CT_FRONT_PORT = "dcim.frontport"
CT_REAR_PORT = "dcim.rearport"
CT_POWER_PORT = "dcim.powerport"
CT_POWER_OUTLET = "dcim.poweroutlet"

# Upper bounds on the components emitted for one device. Real hardware is far below these (a dense
# patch panel has a few hundred ports); they stop a malformed or hostile project document from making a
# sync create an unbounded number of NetBox objects.
MAX_PORTS_PER_SIDE = 1024
MAX_OUTLETS = 512

# name_of(placement_id) -> (device_name, ok); ok=False when the placement produced no device row.
NameOf = Callable[[str], "tuple[str, bool]"]


@dataclass
class Component:
    device: str
    name: str
    ctype: str  # connector/speed enum, or power type
    rear_port: str = ""  # front-port only
    rear_pos: int = 1  # front-port only
    positions: int = 1  # rear-port only


@dataclass
class CableLink:
    a_dev: str
    a_type: str
    a_name: str
    b_dev: str
    b_type: str
    b_name: str
    power: bool = False
    label: str = ""


@dataclass
class CablingPlan:
    interfaces: list[Component] = field(default_factory=list)
    rear_ports: list[Component] = field(default_factory=list)
    front_ports: list[Component] = field(default_factory=list)
    power_outlets: list[Component] = field(default_factory=list)
    power_ports: list[Component] = field(default_factory=list)
    cables: list[CableLink] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    synthesized: int = 0


# ---- naming helpers (cabling.go) -------------------------------------------


def _port_def_name(defs: list[dict], index: int) -> str:
    if 1 <= index <= len(defs) and (defs[index - 1].get("name") or "").strip():
        return defs[index - 1]["name"]
    return str(index)


def _front_port_name(sp: dict, i: int) -> str:
    return _port_def_name(sp.get("frontPorts", []), i)


def _rear_port_name(sp: dict, i: int) -> str:
    return _port_def_name(sp.get("rearPorts", []), i)


def _count(value: object) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _bounded(n: int, limit: int, what: str, dev: str, warnings: list[str]) -> int:
    if n > limit:
        warnings.append(f"{dev}: {n} {what} exceeds the limit of {limit}; only the first {limit} are synced")
        return limit
    return n


def _front_count(sp: dict) -> int:
    return _count(sp.get("front"))


def _rear_count(sp: dict) -> int:
    if _count(sp.get("rear")) > 0:
        return _count(sp.get("rear"))
    return len(sp.get("rearPorts", []))


def _front_rear_ref(sp: dict, i: int) -> tuple[str, int]:
    """Which rear port (and position) front port i couples to. cabling.go:265-276."""
    fronts = sp.get("frontPorts", [])
    if 1 <= i <= len(fronts):
        fp = fronts[i - 1]
        if (fp.get("rear") or "").strip():
            pos = int(fp.get("rearPos") or 0)
            return fp["rear"], pos if pos > 0 else 1
    return _rear_port_name(sp, i), 1


def _media_of_port(defs: list[dict], index: int, fallback: str) -> str:
    if 1 <= index <= len(defs) and (defs[index - 1].get("type") or "").strip():
        return defs[index - 1]["type"]
    return fallback


def _outlet_name(i: int) -> str:
    return f"OUT{i}"


def _endpoint_label(a_dev: str, a_name: str, b_dev: str, b_name: str, power: bool) -> str:
    """A human cable label built from its two terminations, used when Railyard supplies none. Power
    cables read directionally (device -> PDU); data cables are bidirectional."""
    sep = "->" if power else "<->"
    return f"{a_dev}:{a_name} {sep} {b_dev}:{b_name}"


def _cable_ref(c: dict) -> str:
    return (c.get("label") or "").strip() or c.get("id", "")


# ---- planner ---------------------------------------------------------------


def build_cabling_plan(project: Project, name_of: NameOf) -> CablingPlan:
    plan = CablingPlan()

    # Index placements and their resolved device types once, in stable project order.
    dt_by_id: dict[str, dict] = {}
    order: list[str] = []
    for _rack, pl in project.iter_placements():
        pid = pl.get("id", "")
        order.append(pid)
        ref = (pl.get("deviceTypeRef") or "").strip()
        if ref:
            dt = project.device_type_by_key(ref)
            if dt is not None:
                dt_by_id[pid] = dt

    panel_touched: set[str] = set()
    pdu_touched: set[str] = set()
    switch_ifaces: dict[str, dict[str, str]] = {}
    synth_count: dict[str, int] = {}

    def note_iface(pid: str, name: str, ctype: str) -> None:
        switch_ifaces.setdefault(pid, {})[name] = ctype

    def synth_iface_name(pid: str) -> str:
        synth_count[pid] = synth_count.get(pid, 0) + 1
        plan.synthesized += 1
        return f"iface{synth_count[pid]}"

    def end_resolvable(e: dict) -> bool:
        _, ok = name_of(e.get("placementId", ""))
        if not ok:
            return False
        dt = dt_by_id.get(e.get("placementId", ""))
        ports = dt.get("ports") if dt else None
        if ports and ports.get("passThrough") and int(e.get("portIndex") or 0) <= 0:
            return False  # a panel end must name its port
        return True

    def resolve_end(e: dict, media: str) -> tuple[str, str, str]:
        pid = e.get("placementId", "")
        dev, _ = name_of(pid)
        side = (e.get("side") or "").strip().lower()
        port_index = int(e.get("portIndex") or 0)
        dt = dt_by_id.get(pid)
        sp = dt.get("ports") if dt else None
        if sp:
            if sp.get("passThrough"):  # patch panel: front[i] <-> rear[i]
                panel_touched.add(pid)
                if side == "rear":
                    return dev, CT_REAR_PORT, _rear_port_name(sp, port_index)
                return dev, CT_FRONT_PORT, _front_port_name(sp, port_index)
            # switch: a real interface when the port is known, else a synthetic one
            if port_index > 0:
                iname = _port_def_name(sp.get("frontPorts", []), port_index)
                ct = interface_type(_media_of_port(sp.get("frontPorts", []), port_index, sp.get("media", "")))
            else:
                iname = synth_iface_name(pid)
                ct = interface_type(media)
            note_iface(pid, iname, ct)
            return dev, CT_INTERFACE, iname
        # port-less device: synthesize an interface so the cable can terminate somewhere
        iname = synth_iface_name(pid)
        note_iface(pid, iname, interface_type(media))
        return dev, CT_INTERFACE, iname

    # Data cables.
    for c in project.cables:
        a, b = c.get("a", {}), c.get("b", {})
        if not end_resolvable(a) or not end_resolvable(b):
            plan.warnings.append(f"cable {_cable_ref(c)} skipped: an end has no importable device/port termination")
            continue
        media = c.get("media", "")
        a_dev, a_type, a_name = resolve_end(a, media)
        b_dev, b_type, b_name = resolve_end(b, media)
        label = (c.get("label") or "").strip() or _endpoint_label(a_dev, a_name, b_dev, b_name, False)
        plan.cables.append(CableLink(a_dev, a_type, a_name, b_dev, b_type, b_name, False, label))

    # Power links -> a synthetic inlet on each fed device joined to a PDU outlet.
    psu_count: dict[str, int] = {}
    for link in project.power_links:
        dev_end = link.get("device", {})
        pdu_end = link.get("pdu", {})
        dev_name, ok = name_of(dev_end.get("placementId", ""))
        pdu_name, ok2 = name_of(pdu_end.get("placementId", ""))
        if not ok or not ok2:
            plan.warnings.append(f"power link {link.get('id', '')} skipped: an end has no importable device")
            continue
        pdu_dt = dt_by_id.get(pdu_end.get("placementId", ""))
        outlets = pdu_dt.get("outlets") if pdu_dt else None
        outlet = int(pdu_end.get("outlet") or 0)
        if not outlets or outlet < 1 or outlet > min(_count(outlets.get("count")), MAX_OUTLETS):
            plan.warnings.append(
                f"power link {link.get('id', '')} skipped: outlet {outlet} has no importable termination on the PDU"
            )
            continue
        pdu_touched.add(pdu_end.get("placementId", ""))
        did = dev_end.get("placementId", "")
        psu_count[did] = psu_count.get(did, 0) + 1
        pp_name = f"PSU{psu_count[did]}"
        outlet_name = _outlet_name(outlet)
        plan.power_ports.append(Component(device=dev_name, name=pp_name, ctype=inlet_type()))
        plan.cables.append(
            CableLink(
                dev_name,
                CT_POWER_PORT,
                pp_name,
                pdu_name,
                CT_POWER_OUTLET,
                outlet_name,
                True,
                _endpoint_label(dev_name, pp_name, pdu_name, outlet_name, True),
            )
        )

    # Emit components in project order for stable, diff-friendly output.
    for pid in order:
        dt = dt_by_id.get(pid)
        dev, _ = name_of(pid)
        if dt and pid in panel_touched and dt.get("ports"):
            sp = dt["ports"]
            # Size each rear port's fan-out first (NetBox rejects a front port whose position exceeds it).
            n_front = _bounded(_front_count(sp), MAX_PORTS_PER_SIDE, "front ports", dev, plan.warnings)
            n_rear = _bounded(_rear_count(sp), MAX_PORTS_PER_SIDE, "rear ports", dev, plan.warnings)
            rear_positions: dict[str, int] = {}
            for i in range(1, n_front + 1):
                rp, pos = _front_rear_ref(sp, i)
                rear_positions[rp] = max(rear_positions.get(rp, 0), pos)
            for i in range(1, n_rear + 1):
                name = _rear_port_name(sp, i)
                plan.rear_ports.append(
                    Component(
                        device=dev,
                        name=name,
                        ctype=port_connector(_media_of_port(sp.get("rearPorts", []), i, sp.get("media", ""))),
                        positions=max(rear_positions.get(name, 0), 1),
                    )
                )
            for i in range(1, n_front + 1):
                rp, pos = _front_rear_ref(sp, i)
                plan.front_ports.append(
                    Component(
                        device=dev,
                        name=_front_port_name(sp, i),
                        ctype=port_connector(_media_of_port(sp.get("frontPorts", []), i, sp.get("media", ""))),
                        rear_port=rp,
                        rear_pos=pos,
                    )
                )
        ifaces = switch_ifaces.get(pid)
        if ifaces:
            for nm in sorted(ifaces):
                plan.interfaces.append(Component(device=dev, name=nm, ctype=ifaces[nm]))
        if dt and pid in pdu_touched and dt.get("outlets"):
            outlets = dt["outlets"]
            n_out = _bounded(_count(outlets.get("count")), MAX_OUTLETS, "outlets", dev, plan.warnings)
            for i in range(1, n_out + 1):
                plan.power_outlets.append(
                    Component(device=dev, name=_outlet_name(i), ctype=outlet_type(outlets.get("type", "")))
                )

    return plan
