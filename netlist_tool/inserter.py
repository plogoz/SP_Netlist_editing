"""
Depth-aware buffer insertion into a gate-level netlist.

Public API
----------
insert_buffers(module, graph, N, lib, bb_cell, lanes)  ->  Module

Algorithm
---------
Forward depth labeling in topological order.

`depth[net]` = "longest chain of un-restored logic gates ending at this net".

A *restoration point* (depth resets to 0 on the output) is:
  - a primary input  (default depth 0; PIs arrive via a strong external driver)
  - a flip-flop / latch  (samples and re-drives — `lib.cell.is_sequential()`)
  - a buffer cell  (each output is the identity of an input — `is_buffer()`;
    1 lane single-ended, 2 lanes for a differential A,AN -> Z,ZN buffer)

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


def _is_multibit_net(net_key: str) -> bool:
    """True if a resolved net key selects more than one bit (a `[hi:lo]` range).

    graph_builder._resolve_net produces one of three spellings:
      - ``name``         (scalar / whole net)      — single bit here
      - ``name[i]``      (single-bit select)        — single bit
      - ``name[hi:lo]``  (range select)             — multi-bit

    Buffer lanes drive a single-bit scalar wire, so only the range form is a
    problem: one scalar `_bb_N_` cannot carry several bits. This is the checked
    form of the tool's core assumption — gate-level cell pins are 1-bit — so a
    multi-bit output pin fails loudly instead of mis-wiring silently.
    """
    i = net_key.rfind("[")
    return i != -1 and ":" in net_key[i:]


def insert_buffers(
    module: Module,
    graph: nx.DiGraph,
    N: int,
    lib: LibParser,
    bb_cell: str = "BLACKBOX",
    lanes: list[tuple[str, str]] | None = None,
    supplies: dict[str, str] | None = None,
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
    bb_cell:
        Cell type of the buffer to insert.
    lanes:
        Identity lanes of the buffer as [(in_pin, out_pin), ...]: one lane for a
        single-ended buffer, two for a differential A,AN -> Z,ZN buffer. A gate's
        output nets are buffered in chunks of `len(lanes)` — so a single-ended
        buffer drops one buffer per output net (e.g. a full adder's SUM and COUT
        each get one), while a differential buffer drives a true/complement pair
        through one instance. Defaults to a single ("IN", "OUT") lane for the
        library-less placeholder path.
    supplies:
        Resolved {power_pin: rail_net} map (from supplies.resolve_supply_connections).
        Each inserted buffer instance also connects these power pins to the named
        netlist rails, so its supply pins don't float. Empty/None (the Liberty
        flow, whose cells expose no power pins) leaves instances exactly as before.

    Returns
    -------
    Module
        Deep copy of the input module with buffer instances and wires added.
    """
    if lanes is None:
        lanes = [("IN", "OUT")]
    if not lanes:
        raise ValueError("lanes must contain at least one (in_pin, out_pin) pair")
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

    # Supply rails are global, so every inserted buffer connects the same power
    # pins to the same nets. Build the NetRefs once. Empty for the Liberty flow.
    supply_conns: dict[str, NetRef] = {
        pin: NetRef(net) for pin, net in (supplies or {}).items()
    }

    # Per-net depth. Missing keys default to 0 (covers PIs, constants, unconnected).
    depth: dict[str, int] = {}

    bb_index = 0       # counts buffered wires (one per lane)
    bb_inst_index = 0  # counts inserted buffer instances
    max_depth_seen = 0
    lane_n = len(lanes)

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

        # Output nets recorded by build_graph (every phase, in pin order). Fall
        # back to scanning out_edges if the attr is absent (e.g. a hand-built
        # graph) — that misses phases collapsed into a shared DiGraph edge, but
        # is fine for the single-output cells such graphs typically hold.
        output_nets: list[str] = list(attrs.get("output_nets") or [])
        if not output_nets:
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

        # Threshold exceeded → buffer the gate's output nets in chunks of
        # `lane_n`. A single-ended buffer (1 lane) drops one instance per output
        # net; a differential buffer (2 lanes) drives a true/complement pair
        # through a single instance. Each lane is an independent identity path,
        # so nets are paired to lanes positionally — any consistent assignment
        # buffers each net correctly.
        if len(output_nets) % lane_n != 0:
            raise ValueError(
                f"cannot buffer '{node}' ({attrs.get('cell_type')}): it drives "
                f"{len(output_nets)} output net(s), not a multiple of buffer "
                f"'{bb_cell}'s {lane_n} lane(s). A differential buffer needs the "
                f"gate's outputs to come in matching phase groups. See "
                f"docs/netlist_editing_workflow.md."
            )

        for chunk_start in range(0, len(output_nets), lane_n):
            chunk = output_nets[chunk_start : chunk_start + lane_n]

            connections: dict[str, NetRef] = {}
            # original_net → buffered wire for every net in this chunk.
            net_remap: dict[str, str] = {}
            for (in_pin, out_pin), original_net in zip(lanes, chunk):
                if _is_multibit_net(original_net):
                    raise ValueError(
                        f"cannot buffer '{node}' ({attrs.get('cell_type')}): its "
                        f"output net '{original_net}' selects multiple bits, but a "
                        f"buffer lane drives a single-bit wire. Multi-bit cell "
                        f"output pins don't occur in a gate-level netlist; if this "
                        f"is real, the design must be split so each bit is buffered "
                        f"separately. See docs/netlist_editing_workflow.md."
                    )
                new_wire = f"_bb_{bb_index}_"
                bb_index += 1
                mod.wires[new_wire] = WireDecl(new_wire)
                connections[in_pin] = NetRef(original_net)
                connections[out_pin] = NetRef(new_wire)
                net_remap[original_net] = new_wire
                depth[original_net] = out_depth  # short stub gate→buffer
                depth[new_wire] = 0              # consumers see fresh signal

            # Tie the buffer's supply pins to the netlist rails (full-custom CDL
            # flow); empty for Liberty, leaving the instance unchanged.
            connections.update(supply_conns)

            mod.instances.append(
                Instance(
                    cell_type=bb_cell,
                    name=f"bb_{bb_inst_index}",
                    connections=connections,
                )
            )
            bb_inst_index += 1

            # Redirect gate consumers onto the buffered wires (port consumers
            # stay on the original net; the chain ends at a port, so over-shoot
            # by 1 stage is acceptable). A consumer may take several phases from
            # this gate on different pins, but a DiGraph stores only one edge per
            # consumer — so match on the connection nets, not the edge's net.
            # Mutate the edge's net attr too so the later topo-walk computes input
            # depth from the buffered wire.
            for consumer_node in graph.successors(node):
                if graph.nodes[consumer_node].get("kind") != "gate":
                    continue
                consumer_inst = inst_by_name.get(consumer_node)
                if consumer_inst is None:
                    continue
                for pin, ref in list(consumer_inst.connections.items()):
                    new_wire = net_remap.get(str(ref)) or net_remap.get(ref.name)
                    if new_wire is None:
                        continue
                    # The buffered wire is a fresh *scalar* carrying exactly the
                    # one bit `ref` selected. Reference it by its own name with no
                    # bit-select: ref.msb/lsb belonged to the source bus (e.g.
                    # w[2]), and copying it here would emit `_bb_N_[2]` — an
                    # out-of-range select on a 1-bit wire that Yosys reads as x,
                    # making equiv_induct diverge on that net. (Scalar source nets
                    # had msb=None, which is why only bus bits were affected.)
                    consumer_inst.connections[pin] = NetRef(new_wire)
                edge_data = graph.edges[node, consumer_node]
                remapped = net_remap.get(edge_data.get("net"))
                if remapped is not None:
                    edge_data["net"] = remapped

    print(f"  Max depth observed: {max_depth_seen} (target ≤ {N})")
    return mod
