"""A Railyard device type built from a DCIM device type's component templates.

Used when Railyard's catalogue has no entry for the type (a locally defined model, or one the library
does not carry). The projection follows the catalogue converter
(``railyard/scripts/build-catalogue-snapshot.rb`` ``project_device_type``) so a custom type reads like a
catalogue one:

- a pass-through device (front ports *and* rear ports) is a patch panel: ``ports.passThrough`` with
  each front port's ``rear``/``rearPos``; otherwise its physical interfaces are front ports;
- power-port templates are the ``powerInlets``, their connector named as Railyard names it (``C14``);
- power-outlet templates make it a PDU: ``outlets`` with the count, families and feeds;
- a PDU's power draw is its throughput, not its consumption, so only other devices get ``powerW``
  (the largest allocated draw of one input).
"""

from __future__ import annotations

import math
from typing import Any

from ..dcim.snapshot import ComponentTemplate, DeviceType
from . import mappings as m

MAX_PORT_DEFS = 4096
MAX_POWER_INLETS = 256
MAX_NAME = 100
MAX_IDENTIFIER = 256


def normalise_inlet_name(name: str) -> str:
    """Railyard's power-input identity (model.NormalisePowerInlet): trimmed, ASCII case folded."""
    return "".join(ch.lower() if "A" <= ch <= "Z" else ch for ch in name.strip())


def _port_def(template: ComponentTemplate, kind: str) -> dict[str, Any]:
    if kind == "interface":
        connector, netbox_type = m.interface_connector(template.type)
    else:
        connector, netbox_type = m.port_connector(template.type)
    out: dict[str, Any] = {"name": m.fit(template.name.strip(), MAX_NAME)}
    if connector:
        out["type"] = m.fit(connector, MAX_NAME)
    if netbox_type:
        out["netboxType"] = m.fit(netbox_type, MAX_NAME)
    return out


def _named(templates: list[ComponentTemplate], kind: str) -> list[ComponentTemplate]:
    return [t for t in templates if t.kind == kind and t.name.strip()]


def _pass_through_spec(fronts, rears, warnings: list[str], label: str) -> dict[str, Any] | None:
    rear_positions: dict[str, int] = {}
    for rear in rears:
        rear_positions.setdefault(rear.name, max(1, int(rear.positions or 1)))
    if len(rear_positions) != len(rears):
        warnings.append(f"device type {label}: duplicate rear port names; its port template is left out")
        return None
    front_defs = []
    for front in fronts:
        capacity = rear_positions.get(front.rear_port_name)
        position = max(1, int(front.rear_port_position or 1))
        if capacity is None or position > capacity:
            # Pairing an unmapped front port by list position would miswire a cassette; leave the
            # template out instead (each device still carries its real ports).
            warnings.append(
                f"device type {label}: front port {front.name!r} has no rear port mapping; "
                "its port template is left out"
            )
            return None
        port = _port_def(front, "front-port")
        port["rear"] = m.fit(front.rear_port_name, MAX_NAME)
        if position > 1:
            port["rearPos"] = position
        front_defs.append(port)
    spec: dict[str, Any] = {"front": len(fronts), "rear": len(rears), "passThrough": True}
    media = m.port_connector(fronts[0].type)[0] if fronts else None
    if media:
        spec["media"] = m.fit(media, MAX_NAME)
    spec["frontPorts"] = front_defs
    spec["rearPorts"] = [_port_def(rear, "rear-port") for rear in rears]
    return spec


def _ports_spec(templates: list[ComponentTemplate], warnings: list[str], label: str) -> dict[str, Any] | None:
    fronts = _named(templates, "front-port")
    rears = _named(templates, "rear-port")
    if fronts and rears:
        if len(fronts) > MAX_PORT_DEFS or len(rears) > MAX_PORT_DEFS:
            warnings.append(f"device type {label}: more than {MAX_PORT_DEFS} ports; its port template is left out")
            return None
        return _pass_through_spec(fronts, rears, warnings, label)
    interfaces = [t for t in _named(templates, "interface") if m.cableable_interface(t.type)]
    if not interfaces:
        return None
    if len(interfaces) > MAX_PORT_DEFS:
        warnings.append(f"device type {label}: more than {MAX_PORT_DEFS} interfaces; its port template is left out")
        return None
    spec: dict[str, Any] = {"front": len(interfaces), "passThrough": False}
    media = m.interface_media((t.type, t.mgmt_only) for t in interfaces)
    if media:
        spec["media"] = m.fit(media, MAX_NAME)
    spec["frontPorts"] = [_port_def(t, "interface") for t in interfaces]
    return spec


def _power_inlets(templates: list[ComponentTemplate], warnings: list[str], label: str) -> list[dict[str, Any]]:
    inlets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for template in _named(templates, "power-port"):
        name = m.fit(template.name.strip(), MAX_NAME)
        if normalise_inlet_name(name) in seen:
            warnings.append(f"device type {label}: duplicate power port {template.name!r} left out of its template")
            continue
        seen.add(normalise_inlet_name(name))
        inlet: dict[str, Any] = {"name": name}
        connector = m.power_connector(template.type)
        if connector:
            inlet["connector"] = m.fit(connector, MAX_NAME)
        inlets.append(inlet)
    if len(inlets) > MAX_POWER_INLETS:
        warnings.append(f"device type {label}: only the first {MAX_POWER_INLETS} power ports are kept in its template")
        inlets = inlets[:MAX_POWER_INLETS]
    return inlets


def _outlets_spec(templates: list[ComponentTemplate], device_outlets: list[str], power_ports: int) -> dict | None:
    outlets = [t for t in templates if t.kind == "power-outlet"]
    if outlets:
        count, families = len(outlets), m.outlet_type_of(t.type for t in outlets)
    elif device_outlets:
        # The type declares no outlets but its devices have them: size the strip from the devices.
        count, families = len(device_outlets), m.outlet_type_of(device_outlets)
    else:
        return None
    spec: dict[str, Any] = {"count": min(count, 4096)}
    if families:
        spec["type"] = families
    if power_ports > 1:
        spec["feeds"] = power_ports
    draws = [t.maximum_draw_w for t in templates if t.kind == "power-port"]
    if len(draws) == 1 and draws[0] and draws[0] > 0:
        spec["capacityW"] = int(round(draws[0]))
    return spec


def custom_device_type(dt: DeviceType, key: str, device_outlets: list[str], warnings: list[str]) -> dict[str, Any]:
    """The Railyard ``DeviceType`` for ``dt`` under ``key``.

    ``device_outlets`` lists the outlet types of the type's largest PDU in the snapshot, used only when
    the type itself declares no outlet templates. Problems that leave part of the template out are
    appended to ``warnings``.
    """
    label = f"{dt.manufacturer} {dt.model}".strip() or dt.slug
    out: dict[str, Any] = {
        "key": key,
        "manufacturer": m.fit(dt.manufacturer or "", MAX_IDENTIFIER),
        "model": m.fit(dt.model or dt.slug or key, MAX_IDENTIFIER),
        "uHeight": max(0, math.ceil(dt.u_height or 0)),
        "fullDepth": bool(dt.is_full_depth),
    }
    outlets = _outlets_spec(dt.components, device_outlets, len(_named(dt.components, "power-port")))
    if not outlets:
        draws = [t.allocated_draw_w for t in dt.components if t.kind == "power-port" and t.allocated_draw_w]
        if draws and max(draws) > 0:
            out["powerW"] = int(round(max(draws)))
    inlets = _power_inlets(dt.components, warnings, label)
    if inlets:
        out["powerInlets"] = inlets
    ports = _ports_spec(dt.components, warnings, label)
    if ports:
        out["ports"] = ports
    if outlets:
        out["outlets"] = outlets
    if dt.part_number:
        out["partNumber"] = dt.part_number
    if dt.weight_kg and dt.weight_kg > 0:
        out["weightKg"] = round(dt.weight_kg, 2)
    if dt.airflow:
        out["airflow"] = dt.airflow
    out["source"] = {"kind": "custom"}
    return out
