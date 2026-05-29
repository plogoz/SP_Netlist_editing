"""
Depth-aware buffer insertion into a gate-level netlist.

Public API
----------
insert_buffers(module, graph, N, lib, bb_cell, in_port, out_port)  ->  Module

Algorithm
---------
Forward depth labeling in topological order.

`depth[net]` = "longest chain of un-restored logic gates ending at this net".

A *restoration point* (depth resets to 0 on the output) is:
  - a primary input  (default depth 0; PIs arrive via a strong external driver)
  - a flip-flop / latch  (samples and re-drives — `lib.cell.is_sequential()`)
  - a buffer cell  (function == input pin — `lib.cell.is_buffer()`)

Inverters do NOT reset depth — they are regular logic stages (`+1`).

For each combinational gate visited:
    out_depth = max(input_depths) + 1
    if out_depth > N:
        insert a buffer on the output net, redirect gate consumers
        depth of the new (buffered) wire = 0
        depth of the original stub net   = out_depth  (just gate→buffer)

The input Module and graph are not mutated.
"""

from __future__ import annotations

import copy
import sys
from collections import Counter
from typing import TYPE_CHECKING

import networkx as nx

from .netlist_parser import Instance, Module, NetRef, WireDecl

if TYPE_CHECKING:
    from .lib_parser import LibParser


def _diagnose_cycle(graph: nx.DiGraph, cells: dict) -> None:
    """Compact cycle report for when topological_sort fails.

    Output is bounded (<~50 lines) regardless of graph size and biased
    toward design-anonymous aggregates (SCC sizes, cell-type histogram,
    pin directions) over instance-name dumps.
    """
    max_path_edges = 20

    sccs = [c for c in nx.strongly_connected_components(graph) if len(c) > 1]
    if sccs:
        sizes = sorted(len(c) for c in sccs)
        print(
            f"  SCC summary: {len(sccs)} cycle group(s); "
            f"smallest={sizes[0]}, largest={sizes[-1]}",
            file=sys.stderr,
        )

    try:
        cycle_edges = nx.find_cycle(graph)
    except nx.NetworkXNoCycle:
        print(
            "  (no cycle found — graph may have mutated during iteration)",
            file=sys.stderr,
        )
        return

    type_counts: Counter[str] = Counter()
    has_sequential = False
    restoring_in_cycle: set[str] = set()
    for u, _ in cycle_edges:
        t = graph.nodes[u].get("cell_type") or "(port)"
        type_counts[t] += 1
        ci = cells.get(t)
        if ci is not None and ci.is_seq:
            has_sequential = True
        if ci is not None and ci.is_restoring:
            restoring_in_cycle.add(t)

    seq_label = "yes" if has_sequential else "no"
    print(
        f"  Example cycle: {len(cycle_edges)} edges; "
        f"contains sequential cell? {seq_label}",
        file=sys.stderr,
    )
    print(f"  Cell-type histogram in cycle: {dict(type_counts)}", file=sys.stderr)
    if not has_sequential:
        # Only sequential cells cut the graph; restoring cells reset depth but
        # keep their edges, so a loop with no sequential cell cannot be broken.
        print(
            "  This loop has no sequential cell, so nothing cuts it. Whatever "
            "holds state in this loop must be tagged in the sidecar's "
            '"sequential" list (not "restoring"). See '
            "docs/netlist_editing_workflow.md §8.6.",
            file=sys.stderr,
        )
        if restoring_in_cycle:
            print(
                f"  Note: {sorted(restoring_in_cycle)} are tagged \"restoring\" "
                "(depth reset only) — that does not break the loop.",
                file=sys.stderr,
            )

    shown = min(max_path_edges, len(cycle_edges))
    print(f"  Cycle path (first {shown} edge(s)):", file=sys.stderr)
    for u, v in cycle_edges[:shown]:
        u_type = graph.nodes[u].get("cell_type") or "(port)"
        v_type = graph.nodes[v].get("cell_type") or "(port)"
        net = graph.edges[u, v].get("net", "?")
        print(f"    {u} ({u_type})  --{net}-->  {v} ({v_type})", file=sys.stderr)
    if len(cycle_edges) > shown:
        print(f"    ... +{len(cycle_edges) - shown} more edge(s)", file=sys.stderr)

    print("  Pin directions for cell types in this cycle:", file=sys.stderr)
    for ct in sorted(type_counts):
        if ct == "(port)":
            continue
        ci = cells.get(ct)
        if ci is None:
            print(f"    {ct}: not in library", file=sys.stderr)
            continue
        flags = []
        if ci.is_seq:
            flags.append("seq")
        if ci.is_buf:
            flags.append("buf")
        tag = f" [{','.join(flags)}]" if flags else ""
        print(
            f"    {ct}{tag}: in={ci.input_pins()}  out={ci.output_pins()}",
            file=sys.stderr,
        )


def insert_buffers(
    module: Module,
    graph: nx.DiGraph,
    N: int,
    lib: LibParser,
    bb_cell: str = "BLACKBOX",
    in_port: str = "IN",
    out_port: str = "OUT",
) -> Module:
    """Insert buffers so no path has more than N consecutive logic gates
    between restoration points (flip-flops, latches, buffers, primary inputs).

    Parameters
    ----------
    module:
        Original parsed netlist. Not mutated.
    graph:
        DiGraph produced by build_graph(module).  Must be a DAG.
    N:
        Maximum allowed depth (logic-gate count) between restoration points.
    lib:
        LibParser, required to identify sequential cells and buffers.
    bb_cell, in_port, out_port:
        Cell type and port names of the buffer to insert.

    Returns
    -------
    Module
        Deep copy of the input module with buffer instances and wires added.
    """
    if N < 1:
        raise ValueError(f"N must be >= 1, got {N}")
    if lib is None:
        raise ValueError(
            "lib is required: depth-based insertion needs Liberty info "
            "to identify flip-flops and existing buffers"
        )

    cells = lib.parse()
    mod = copy.deepcopy(module)
    inst_by_name: dict[str, Instance] = {inst.name: inst for inst in mod.instances}

    # Per-net depth. Missing keys default to 0 (covers PIs, constants, unconnected).
    depth: dict[str, int] = {}

    bb_index = 0
    max_depth_seen = 0

    try:
        topo_order = list(nx.topological_sort(graph))
    except nx.NetworkXUnfeasible:
        print(
            "error: graph contains a cycle — depth-based insertion needs a DAG",
            file=sys.stderr,
        )
        _diagnose_cycle(graph, cells)
        raise

    for node in topo_order:
        attrs = graph.nodes[node]
        if attrs.get("kind") != "gate":
            continue

        cell_info = cells.get(attrs.get("cell_type"))

        # Gather output nets (most std cells have exactly one).
        output_nets: list[str] = []
        for _, _, edge_data in graph.out_edges(node, data=True):
            net = edge_data.get("net", "")
            if net and net not in output_nets:
                output_nets.append(net)

        # Restoration point → outputs at depth 0, no buffering.
        # Covers sequential cells, 1-in/1-out buffers, and `restoring`
        # re-drivers (e.g. a buffering mux tagged in the CDL sidecar).
        if cell_info is not None and cell_info.is_restoration_point():
            for n in output_nets:
                depth[n] = 0
            continue

        # Logic gate (incl. inverters): out_depth = max(inputs) + 1.
        in_depths = [
            depth.get(edge_data.get("net", ""), 0)
            for _, _, edge_data in graph.in_edges(node, data=True)
        ]
        out_depth = (max(in_depths) if in_depths else 0) + 1
        max_depth_seen = max(max_depth_seen, out_depth)

        if out_depth <= N:
            for n in output_nets:
                depth[n] = out_depth
            continue

        # Threshold exceeded → insert a buffer per output net.
        for original_net in output_nets:
            new_wire = f"_bb_{bb_index}_"
            bb_index += 1

            mod.wires[new_wire] = WireDecl(new_wire)

            bb_inst = Instance(
                cell_type=bb_cell,
                name=f"bb_{bb_index - 1}",
                connections={
                    in_port: NetRef(original_net),
                    out_port: NetRef(new_wire),
                },
            )
            mod.instances.append(bb_inst)

            # Redirect gate consumers (port consumers stay on original_net;
            # the chain ends at a port, so over-shoot by 1 stage is acceptable).
            # Mutate graph edge attrs to match the new wiring so later
            # topo-walk visits compute input depth from the buffered net.
            for consumer_node in graph.successors(node):
                edge_data = graph.edges[node, consumer_node]
                if edge_data.get("net") != original_net:
                    continue
                if graph.nodes[consumer_node].get("kind") != "gate":
                    continue
                consumer_inst = inst_by_name.get(consumer_node)
                if consumer_inst is None:
                    continue
                for pin, ref in list(consumer_inst.connections.items()):
                    if str(ref) == original_net or ref.name == original_net:
                        consumer_inst.connections[pin] = NetRef(
                            new_wire, ref.msb, ref.lsb
                        )
                edge_data["net"] = new_wire

            depth[original_net] = out_depth  # short stub between gate and buffer
            depth[new_wire] = 0              # consumers see fresh signal

    print(f"  Max depth observed: {max_depth_seen} (target ≤ {N})")
    return mod
