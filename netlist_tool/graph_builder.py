"""
Gate-level Verilog netlist → NetworkX DiGraph.

Public API
----------
build_graph(module, lib=None)  ->  nx.DiGraph

Graph model
-----------
Nodes
    One node per Instance, keyed by instance name.
    One pseudo-node per module port, keyed by port name.
    Attrs: kind ('gate'|'port'), cell_type (str), instance (Instance|None)

Edges
    (driver_node, consumer_node)
    A directed edge exists when a driver's output net feeds a consumer's
    input pin (after assign-alias resolution).
    Attrs: net (str) — the resolved wire name

Graph attrs
    G.graph['module'] = module  — lossless handle for the serializer

Assign resolution
    Yosys emits  assign lhs = rhs  aliases.  Before building edges, each
    LHS net is resolved to its canonical RHS net (chains followed).

Pin direction
    1. LibParser.get_pin_direction(cell_type, pin) when lib is provided.
    2. Heuristic for Yosys generic/standard cells: output pins are named
       in _OUTPUT_PINS.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import networkx as nx

from .netlist_parser import Assign, Instance, Module, NetRef

if TYPE_CHECKING:
    from .lib_parser import LibParser

# Yosys generic cells and common standard-cell output pin names.
_OUTPUT_PINS: frozenset[str] = frozenset(
    {"Y", "Q", "Z", "ZN", "X", "CO", "CON", "SUM", "COUT", "S"}
)

# Prefix for port pseudo-node keys (avoids collision with instance names).
_PORT_PREFIX = "__port__"


def _port_node(port_name: str) -> str:
    return f"{_PORT_PREFIX}{port_name}"


def _build_alias_map(assigns: list[Assign]) -> dict[str, str]:
    """Map each assign LHS net name → canonical RHS net name (chain-resolved)."""
    # Only scalar assign aliases are meaningful for gate routing.
    # Bus slices are left as-is (the connection string already encodes position).
    direct: dict[str, str] = {}
    for a in assigns:
        if a.lhs.msb is None and a.rhs.msb is None:
            direct[a.lhs.name] = a.rhs.name

    def resolve(name: str, seen: set[str]) -> str:
        if name in seen:
            return name  # break cycle (shouldn't happen in valid netlists)
        seen.add(name)
        if name in direct:
            return resolve(direct[name], seen)
        return name

    return {lhs: resolve(lhs, set()) for lhs in direct}


def _resolve_net(ref: NetRef, alias: dict[str, str]) -> str:
    """Return the canonical net name for a NetRef after alias resolution."""
    canonical = alias.get(ref.name, ref.name)
    if ref.msb is None:
        return canonical
    if ref.msb == ref.lsb:
        return f"{canonical}[{ref.msb}]"
    return f"{canonical}[{ref.msb}:{ref.lsb}]"


def _is_output_pin(pin: str, cell_type: str, lib: LibParser | None) -> bool:
    if lib is not None:
        direction = lib.get_pin_direction(cell_type, pin)
        if direction is not None:
            return direction == "output"
    return pin in _OUTPUT_PINS


def build_graph(module: Module, lib: LibParser | None = None) -> nx.DiGraph:
    """Build a gate-only DiGraph from a parsed Module.

    Parameters
    ----------
    module:
        Parsed netlist (from netlist_parser.parse).
    lib:
        Optional LibParser for accurate pin-direction lookup.  When None,
        a heuristic is used (sufficient for Yosys generic synthesis output).

    Returns
    -------
    nx.DiGraph
        Nodes keyed by instance/port name; G.graph['module'] = module.
    """
    alias = _build_alias_map(module.assigns)

    G = nx.DiGraph()
    G.graph["module"] = module

    # --- Add port pseudo-nodes -----------------------------------------------
    for port_name, port_decl in module.ports.items():
        G.add_node(
            _port_node(port_name),
            kind="port",
            cell_type=None,
            instance=None,
            direction=port_decl.direction,
            port_name=port_name,
        )

    # --- Add gate nodes -------------------------------------------------------
    # `output_nets` records every output phase (resolved, in connection order)
    # so the inserter sees the complete set even for a differential gate, whose
    # two output phases feeding one consumer collapse to a single DiGraph edge.
    for inst in module.instances:
        out_nets: list[str] = []
        for pin, ref in inst.connections.items():
            if _is_output_pin(pin, inst.cell_type, lib):
                net_key = _resolve_net(ref, alias)
                if net_key not in out_nets:
                    out_nets.append(net_key)
        G.add_node(
            inst.name,
            kind="gate",
            cell_type=inst.cell_type,
            instance=inst,
            output_nets=out_nets,
        )

    # --- Build net → driver map -----------------------------------------------
    # Module input ports are drivers of their own net.
    net_driver: dict[str, str] = {}
    for port_name, port_decl in module.ports.items():
        if port_decl.direction in ("input", "inout"):
            net_driver[port_name] = _port_node(port_name)

    # Sequential cells (flip-flops / latches) terminate a combinational cone:
    # their output samples the *previous* clock cycle, so it must not carry a
    # combinational edge forward. Registering no driver for a flop output makes
    # its Q a graph source, which opens every register-feedback loop and leaves
    # the combinational graph acyclic — required by depth-based insertion.
    #
    # Yosys-synthesized netlists masked the need for this: they route flop
    # fan-out through bus-slice `assign` aliases that _build_alias_map drops,
    # so the feedback edges never formed. Netlists from other tools wire nets
    # directly (no aliases), so the cut must be explicit here. Needs the library
    # to tag flops sequential (ff()/latch() in Liberty, or the CDL sidecar
    # "sequential" list). See docs/netlist_editing_workflow.md §8.6.
    seq_types: set[str] = set()
    if lib is not None:
        seq_types = {name for name, ci in lib.parse().items() if ci.is_seq}

    for inst in module.instances:
        if inst.cell_type in seq_types:
            continue  # flop output is a source — cut here to break feedback
        for pin, ref in inst.connections.items():
            if _is_output_pin(pin, inst.cell_type, lib):
                net_key = _resolve_net(ref, alias)
                net_driver[net_key] = inst.name

    # --- Add edges (input pins → consumer nodes) -----------------------------
    for inst in module.instances:
        for pin, ref in inst.connections.items():
            if not _is_output_pin(pin, inst.cell_type, lib):
                net_key = _resolve_net(ref, alias)
                driver_node = net_driver.get(net_key)
                if driver_node is not None:
                    G.add_edge(driver_node, inst.name, net=net_key)

    # Module output ports consume the net they are connected to.
    inout_self_loops = 0
    for port_name, port_decl in module.ports.items():
        if port_decl.direction in ("output", "inout"):
            # The port net name equals the port name itself (Yosys convention).
            # Also check assign aliases.
            net_key = alias.get(port_name, port_name)
            driver_node = net_driver.get(net_key)
            if driver_node is None:
                continue
            # An inout port that drives its own undriven net (typical for
            # supply nets) would close a self-loop and break topological_sort.
            # The module's port + connection data is unaffected — only this
            # edge in the working graph is filtered.
            if driver_node == _port_node(port_name):
                inout_self_loops += 1
                continue
            G.add_edge(driver_node, _port_node(port_name), net=net_key)

    if inout_self_loops:
        print(
            f"  Skipped {inout_self_loops} inout-port self-loop(s) "
            "(likely supply nets driven only by the port itself)"
        )

    return G
