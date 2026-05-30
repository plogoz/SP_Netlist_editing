"""
Netlist editing tool — CLI entry point.

Usage
-----
python -m netlist_tool INPUT.v OUTPUT.v --N 5
                                        [--bb-cell BLACKBOX]
                                        [--in-port IN --out-port OUT]
                                        [--lib path/to/cells.lib |
                                         --cdl path/to/cells.cdl [path/to/more.cdl ... | path/to/cdl_dir/]
                                         [--cell-meta path [path ...]]]
                                        [--visualize [FILE]]

Orchestration
-------------
1. Parse Verilog netlist  →  Module
2. Load library backend (Liberty .lib or CDL .cdl)
3. Build NetworkX DiGraph  →  Graph
4. Insert black boxes every N gates  →  modified Module
5. Write modified netlist to OUTPUT.v
6. (optional) Visualize graph
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="netlist_tool",
        description="Insert black-box cells into a gate-level Verilog netlist.",
    )
    p.add_argument("input", type=Path, help="Input Verilog netlist (.v)")
    p.add_argument("output", type=Path, help="Output Verilog netlist (.v)")
    p.add_argument(
        "--N",
        type=int,
        required=True,
        metavar="N",
        help="Maximum number of consecutive logic gates allowed between "
        "restoration points (flip-flops, buffers, or primary inputs). "
        "A buffer is inserted on any gate output that would otherwise "
        "exceed this depth. Inverters count as logic gates.",
    )
    p.add_argument(
        "--bb-cell",
        default=None,
        metavar="CELL",
        help="Cell type name for the inserted buffer. If omitted, a "
        "1-input/1-output buffer cell is auto-selected from --lib / "
        "--cdl; the tool errors out if no buffer is tagged in the "
        "library (see the sidecar's \"buffers\" list for the CDL flow).",
    )
    p.add_argument(
        "--in-port",
        default=None,
        metavar="PORT",
        help="Input port name (auto-derived when --bb-cell is "
        "auto-selected from --lib).",
    )
    p.add_argument(
        "--out-port",
        default=None,
        metavar="PORT",
        help="Output port name (auto-derived when --bb-cell is "
        "auto-selected from --lib).",
    )
    lib_group = p.add_mutually_exclusive_group()
    lib_group.add_argument(
        "--lib",
        type=Path,
        default=None,
        metavar="LIB",
        help="Liberty (.lib) file for accurate pin-direction lookup",
    )
    lib_group.add_argument(
        "--cdl",
        type=Path,
        nargs="+",
        default=None,
        metavar="CDL",
        help="One or more CDL files, or a directory of *.cdl. Used when the "
        "PDK ships no Liberty (closed-source flow). Multiple inputs are "
        "merged into one library; duplicate cell names across files cause an "
        "error. Pair with --cell-meta to classify buffers / sequential cells.",
    )
    p.add_argument(
        "--cell-meta",
        type=Path,
        nargs="+",
        default=None,
        metavar="JSON",
        help="Sidecar JSON(s) for CDL: {\"buffers\":[...], \"sequential\":[...]}. "
        "Omit to auto-discover <cdl_stem>.cells.json next to each CDL; pass "
        "explicitly (one master file or a list) to override auto-discovery.",
    )
    p.add_argument(
        "--visualize",
        nargs="?",
        const=True,
        metavar="FILE",
        help="Visualize the graph.  Optionally supply a file path to save.",
    )
    return p


def _load_library(args):
    """Construct the right library backend, or None if neither flag was given."""
    if args.lib is not None:
        from .lib_parser import LibParser

        return LibParser(args.lib)
    if args.cdl is not None:
        from .cdl_parser import CdlParser

        return CdlParser(args.cdl, cell_meta=args.cell_meta)
    if args.cell_meta is not None:
        print(
            "warning: --cell-meta has no effect without --cdl",
            file=sys.stderr,
        )
    return None


def _describe_lib_source(args, lib) -> str:
    """Human-readable label for the library source, for log messages."""
    if args.lib is not None:
        return args.lib.name
    return lib.source_label


def _auto_select_buffer(lib) -> tuple[str, list[tuple[str, str]]] | None:
    """Pick a buffer cell from the parsed library.

    Returns (cell_name, lanes) for the first buffer found in alphabetic order,
    where lanes is [(in_pin, out_pin), ...] — one lane for a single-ended
    buffer, two for a differential A,AN -> Z,ZN buffer. None if the library
    has no buffer cells.
    """
    candidates = [c for c in lib.parse().values() if c.is_buffer()]
    if not candidates:
        return None
    chosen = min(candidates, key=lambda c: c.name)
    return chosen.name, chosen.buffer_lanes()


def _format_lanes(lanes: list[tuple[str, str]]) -> str:
    return ", ".join(f"{in_pin}→{out_pin}" for in_pin, out_pin in lanes)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    from .graph_builder import build_graph
    from .inserter import insert_buffers
    from .netlist_parser import parse
    from .serializer import write

    if not args.input.exists():
        print(f"error: input file not found: {args.input}", file=sys.stderr)
        return 1

    print(f"Parsing {args.input} ...")
    module = parse(args.input)
    print(module.summary())

    lib = _load_library(args)

    # Resolve the buffer cell and its identity lanes [(in_pin, out_pin), ...].
    lanes: list[tuple[str, str]] | None = None

    # Explicit --in-port/--out-port is a single-lane override (escape hatch,
    # and the only way to name ports without a library).
    if args.in_port is not None and args.out_port is not None:
        lanes = [(args.in_port, args.out_port)]

    if lib is not None and lanes is None:
        if args.bb_cell is None:
            pick = _auto_select_buffer(lib)
            if pick is None:
                print(
                    f"error: no buffer cell found in "
                    f"{_describe_lib_source(args, lib)}. Tag a buffer in the "
                    f'sidecar\'s "buffers" list (CDL flow) — a differential '
                    f"buffer also needs its identity lanes in \"functions\" "
                    f'(e.g. {{"Z":"A","ZN":"AN"}}) — or supply --bb-cell '
                    f"explicitly. Silently falling back to a placeholder cell "
                    f"type would produce a netlist that verify-cdl cannot "
                    f"resolve.",
                    file=sys.stderr,
                )
                return 1
            args.bb_cell, lanes = pick
            print(
                f"\nAuto-selected buffer from {_describe_lib_source(args, lib)}: "
                f"{args.bb_cell} ({_format_lanes(lanes)})"
            )
        else:
            cell = lib.parse().get(args.bb_cell)
            if cell is None:
                print(
                    f"error: --bb-cell '{args.bb_cell}' not found in "
                    f"{_describe_lib_source(args, lib)}.",
                    file=sys.stderr,
                )
                return 1
            lanes = cell.buffer_lanes()
            if lanes is None:
                print(
                    f"error: --bb-cell '{args.bb_cell}' is not a recognized "
                    f"buffer (each output must be the identity of an input). "
                    f"Tag its lanes in the sidecar's \"functions\", or pass "
                    f"--in-port/--out-port for a single-lane override.",
                    file=sys.stderr,
                )
                return 1
            print(f"\nUsing buffer {args.bb_cell} ({_format_lanes(lanes)})")

    # The BLACKBOX/placeholder fallback only kicks in when no library was
    # supplied — and the lib-is-None check below rejects that case before we'd
    # ever write a buffer. Kept as a defensive default for any future caller
    # that bypasses the lib check.
    if args.bb_cell is None:
        args.bb_cell = "BLACKBOX"
    if lanes is None:
        lanes = [("IN", "OUT")]

    print("\nBuilding graph ...")
    graph = build_graph(module, lib)
    print(f"  {graph.number_of_nodes()} nodes, {graph.number_of_edges()} edges")

    if lib is None:
        print(
            "error: --lib or --cdl is required (depth-based insertion needs "
            "cell metadata to identify flip-flops and buffers)",
            file=sys.stderr,
        )
        return 1

    print(f"\nInserting buffers (max depth N={args.N} between restoration points) ...")
    modified = insert_buffers(
        module,
        graph,
        args.N,
        lib,
        bb_cell=args.bb_cell,
        lanes=lanes,
    )
    inserted = len(modified.instances) - len(module.instances)
    print(f"  Inserted {inserted} buffers instance(s)")

    print(f"\nWriting {args.output} ...")
    write(modified, args.output)
    print("  Done.")

    if args.visualize is not None:
        from .grapher import visualize

        out_file = None if args.visualize is True else args.visualize
        visualize(graph, output=out_file)

    return 0


if __name__ == "__main__":
    sys.exit(main())
