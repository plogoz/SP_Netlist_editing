"""
Liberty (.lib) -> CDL (.cdl) + sidecar JSON round-trip.

One-off bootstrap utility for the closed-source / CDL flow. Real CDLs
arrive from the vendor; this script exists so the netlist_tool's CDL
backend can be exercised end-to-end on the existing sky130 fixture by
deriving a CDL+sidecar from the sky130 Liberty.

Reuses the existing netlist_tool.lib_parser.LibParser; no edits to
lib_parser are needed. Sequential cells are listed in the sidecar's
"sequential" array but get no ff() metadata — they stay opaque
blackboxes per docs/cdl_backend.md §4.

Usage
-----
    uv run python scripts/lib_to_cdl.py <lib_path> -o <out.cdl>
    uv run python scripts/lib_to_cdl.py <lib_path> -o <out.cdl> \
        --restoring sky130_fd_sc_hd__mux2_1

Outputs
-------
    <out.cdl>          .SUBCKT / *.PININFO blocks
    <out.cells.json>   sidecar with buffers / sequential / restoring / functions

The `restoring` list cannot be derived from Liberty (it is a
signal-integrity judgement), so cells are named explicitly with
--restoring. They reset insertion depth like a buffer but are multi-pin
and do not cut the graph. See docs/netlist_editing_workflow.md §8.6.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running directly: `python scripts/lib_to_cdl.py ...`
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from netlist_tool.cell_info import CellInfo  # noqa: E402
from netlist_tool.lib_parser import LibParser  # noqa: E402

# CDL collapses bias / supply / ground / inout / internal into one token.
_DIR_TO_CDL = {
    "input": "I",
    "output": "O",
    "inout": "B",
    "power": "B",
    "internal": "B",
}


def _emit_cdl(db: dict[str, CellInfo], source_name: str) -> str:
    lines: list[str] = [
        f"* Auto-generated from {source_name} by scripts/lib_to_cdl.py.",
        "* Do not edit; regenerate by re-running the script.",
        "",
    ]
    for name, cell in db.items():
        pins = list(cell.pins.keys())
        if not pins:
            # Liberty cell with no pin declarations — skip rather than emit
            # an empty SUBCKT that has no port list.
            print(f"warning: cell '{name}' has no pins; skipped", file=sys.stderr)
            continue
        lines.append(f".SUBCKT {name} {' '.join(pins)}")
        pininfo_tokens = [
            f"{pin}:{_DIR_TO_CDL.get(direction, 'B')}"
            for pin, direction in cell.pins.items()
        ]
        lines.append(f"*.PININFO {' '.join(pininfo_tokens)}")
        lines.append(".ENDS")
        lines.append("")
    return "\n".join(lines)


def _emit_sidecar(
    db: dict[str, CellInfo],
    restoring: list[str] | None = None,
    clock: dict[str, str] | None = None,
) -> dict:
    buffers: list[str] = []
    sequential: list[str] = []
    functions: dict[str, dict[str, str]] = {}

    for name, cell in db.items():
        if cell.is_seq:
            sequential.append(name)
        elif cell.is_buffer():
            buffers.append(name)
        if not cell.is_seq and cell.pin_function:
            # Only carry the function strings for cells we're willing to
            # characterise. A Liberty flop's function references internal ff()
            # nodes (e.g. "IQ") that are not pins, so it's useless to the CDL
            # FF model — sequential next-state + clock are added manually
            # (--clock), just like --restoring. Restoring cells keep their
            # function here so verify-cdl can still reason through them.
            functions[name] = dict(cell.pin_function)

    # `restoring` cannot be derived from Liberty (it is a signal-integrity
    # judgement the user makes), so it is supplied explicitly via --restoring.
    restoring = restoring or []
    for name in restoring:
        if name not in db:
            print(
                f"warning: --restoring '{name}' is not a cell in the parsed "
                "Liberty; emitting it anyway",
                file=sys.stderr,
            )

    # `clock` likewise can't be derived here (it would need ff().clocked_on
    # parsing in lib_parser), so it is supplied explicitly via --clock CELL=PIN.
    clock = clock or {}
    for name in clock:
        if name not in db:
            print(
                f"warning: --clock '{name}' is not a cell in the parsed "
                "Liberty; emitting it anyway",
                file=sys.stderr,
            )

    return {
        "buffers": sorted(buffers),
        "sequential": sorted(sequential),
        "restoring": sorted(set(restoring)),
        "functions": dict(sorted(functions.items())),
        "clock": dict(sorted(clock.items())),
    }


def _sidecar_path(cdl_out: Path) -> Path:
    return cdl_out.with_suffix(".cells.json")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="lib_to_cdl",
        description=(
            "Round-trip a Liberty file into a CDL plus sidecar JSON. "
            "Intended for bootstrapping the CDL backend's test fixture "
            "from an existing Liberty."
        ),
    )
    ap.add_argument("lib", type=Path, help="Source Liberty (.lib) file.")
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        metavar="CDL",
        help="Destination CDL path. Default: <lib_stem>.cdl next to the source.",
    )
    ap.add_argument(
        "--restoring",
        nargs="*",
        default=[],
        metavar="CELL",
        help="Cell names to tag as 'restoring' in the sidecar: multi-pin "
        "re-drivers (e.g. a buffering mux) that reset insertion depth but do "
        "not cut the graph. Cannot be derived from Liberty, so it is listed "
        "explicitly. Example: --restoring sky130_fd_sc_hd__mux2_1",
    )
    ap.add_argument(
        "--clock",
        nargs="*",
        default=[],
        metavar="CELL=PIN",
        help="Clock-pin assignments for sequential cells, used by verify-cdl's "
        "behavioural FF model. Cannot be derived from Liberty here, so listed "
        "explicitly. Example: --clock sky130_fd_sc_hd__dfxtp_1=CLK",
    )
    args = ap.parse_args(argv)

    clock: dict[str, str] = {}
    for item in args.clock:
        if "=" not in item:
            print(
                f"error: --clock entry '{item}' must be CELL=PIN", file=sys.stderr
            )
            return 1
        cell_name, pin_name = item.split("=", 1)
        clock[cell_name] = pin_name

    if not args.lib.exists():
        print(f"error: {args.lib}: not found", file=sys.stderr)
        return 1

    cdl_out = args.output or args.lib.with_suffix(".cdl")
    sidecar_out = _sidecar_path(cdl_out)

    print(f"Parsing {args.lib} ...")
    parser = LibParser(args.lib)
    db = parser.parse()
    print(f"  {len(db)} cells")

    cdl_text = _emit_cdl(db, args.lib.name)
    cdl_out.parent.mkdir(parents=True, exist_ok=True)
    cdl_out.write_text(cdl_text, encoding="utf-8")
    print(f"Wrote {cdl_out}")

    sidecar = _emit_sidecar(db, restoring=args.restoring, clock=clock)
    sidecar_out.write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")
    print(
        f"Wrote {sidecar_out} "
        f"(buffers={len(sidecar['buffers'])}, "
        f"sequential={len(sidecar['sequential'])}, "
        f"restoring={len(sidecar['restoring'])}, "
        f"functions={len(sidecar['functions'])}, "
        f"clock={len(sidecar['clock'])})"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
