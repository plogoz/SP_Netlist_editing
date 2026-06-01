"""
CDL (.cdl) parser — closed-source-PDK companion to lib_parser.

CDL is a SPICE-subset format. For our purposes only three line kinds
matter:

    .SUBCKT <name> <pin1> <pin2> ...
    *.PININFO <pin>:<I|O|B> ...
    .ENDS

Everything else (transistor primitives, header comments, the `…`
placeholder used in stub files) is ignored.

The CDL format gives us pin direction but **none** of the metadata the
Liberty path derives from `function:` strings or `ff()`/`latch()`
groups. To classify buffers and sequential cells — and to characterise
combinational cells well enough for Yosys's equivalence prover — we
read a sidecar JSON file:

    {
      "buffers":    ["BUFF_TEST", "CLKBUF_X1"],
      "sequential": ["DFF_TEST",  "DLAT_TEST"],
      "restoring":  ["MUX_TEST"],
      "functions": {
        "AND_TEST": { "Y": "(A & B)" },
        "INV_TEST": { "Y": "(!A)"    },
        "DFF_TEST": { "Q": "D"       }
      },
      "clock": { "DFF_TEST": "CLK" }
    }

The `functions` map populates `CellInfo.pin_function` for combinational
cells, which `emit_stub_lib` then emits as `function : "..."` lines so
that `equiv_induct` can reason through them.

Sequential cells need a `functions` entry (next-state per output) and a
`clock` entry (their clock pin) too: `emit_ff_model` turns those into a
behavioural Verilog flop (`always @(posedge CLK) Q <= D;`) that
`equiv_induct` can reason about. A bare blackbox flop has no FF semantics
and makes the prover fail with "No SAT model available". See
docs/netlist_editing_workflow.md §8.10.

`functions` doubles as a *direction* source. Differential CDLs often
declare every signal pin `:B` (bias/bidirectional — the label LVS and
SPICE want, since current may flow either way), which maps to "power"
and hides the logical in/out the inserter needs. So after loading a
cell's functions we infer directions: a function key is an output, a pin
referenced in an expression is an input. Only pins still tagged "power"
are upgraded, so explicit `:I`/`:O` and true supply pins are untouched,
and the `.cdl` itself is never edited. See
docs/netlist_editing_workflow.md §8.9.

The `restoring` list tags multi-pin cells that re-drive a signal (e.g. a
buffering mux). They act as restoration points for depth-based insertion
(the counter resets on their output) but are NOT 1-in/1-out insertion
buffers and do NOT cut the graph — see docs/netlist_editing_workflow.md
§8.6. The flag only affects insertion; a restoring cell still carries its
`functions` entry so `verify-cdl` reasons through it normally.

Multi-input: the parser accepts a single CDL path, a list of CDL paths,
or a directory of CDLs (top-level *.cdl only). All cells are merged
into one library; a cell name defined in two files is a hard error and
names both source paths.

Sidecar precedence: if `cell_meta` is omitted, each CDL auto-discovers
its own `<stem>.cells.json` next to it. If `cell_meta` is passed
(single path or list), auto-discovery is skipped entirely — useful for
one master sidecar covering many CDLs. Names in any sidecar that
aren't in the parsed CDLs produce a warning, not an error.

Public API mirrors LibParser so graph_builder / inserter / main use it
through duck typing without modification.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from .cell_info import CellInfo

# ---------------------------------------------------------------------------
# Direction mapping
# ---------------------------------------------------------------------------
# CDL collapses bias + supply + ground into one token. Mapping to "power"
# (Liberty's category) lets CellInfo.signal_pins() filter them out the
# same way it does for Liberty pg_pins.
_DIR_MAP = {"I": "input", "O": "output", "B": "power"}


# ---------------------------------------------------------------------------
# Line classifier
# ---------------------------------------------------------------------------
# CDL line-continuation marker `+` at start of a line is technically
# legal SPICE but the stubs we target keep .SUBCKT / *.PININFO on a
# single line, so we don't try to splice continuations.

_SUBCKT_RE = re.compile(r"^\s*\.SUBCKT\s+(\S+)\s*(.*)$", re.IGNORECASE)
_ENDS_RE = re.compile(r"^\s*\.ENDS\b", re.IGNORECASE)
_PININFO_RE = re.compile(r"^\s*\*\.PININFO\s+(.*)$", re.IGNORECASE)
_PIN_TOKEN_RE = re.compile(r"(\S+?):([IOB])\b")


def _scan(text: str) -> dict[str, CellInfo]:
    """Walk the CDL text line-by-line and build {name: CellInfo}."""
    cells: dict[str, CellInfo] = {}
    current: CellInfo | None = None

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue

        # Order matters: *.PININFO is a comment-prefixed directive, so
        # check it before generic comment skipping.
        m_pin = _PININFO_RE.match(line)
        if m_pin is not None:
            if current is None:
                continue  # stray PININFO outside a subckt; ignore
            for pin_name, code in _PIN_TOKEN_RE.findall(m_pin.group(1)):
                # First occurrence wins, matching lib_parser semantics.
                if pin_name not in current.pins:
                    current.pins[pin_name] = _DIR_MAP[code]
            continue

        # Comment line — skip.
        if line.lstrip().startswith("*"):
            continue

        m_sub = _SUBCKT_RE.match(line)
        if m_sub is not None:
            name = m_sub.group(1)
            current = cells.setdefault(name, CellInfo(name=name))
            # Seed pin order from the .SUBCKT port list; direction is
            # filled in by the subsequent *.PININFO line. If PININFO is
            # missing, pins remain with no direction entry, which is
            # fine — graph_builder falls back to its heuristic.
            continue

        if _ENDS_RE.match(line):
            current = None
            continue

        # Body line (transistor, .PARAM, …) — ignored.

    # EOF implicitly closes any still-open cell (TEST_CELLS.cdl is
    # missing the final .ENDS).
    return cells


# ---------------------------------------------------------------------------
# Sidecar classification
# ---------------------------------------------------------------------------

# Identifier token in a Liberty-style function expression. Operators
# (& | ! ^ ' parens) and constants (0/1) are not identifiers, so this picks
# out only pin names.
_FUNC_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _infer_directions_from_functions(cell: CellInfo) -> None:
    """Recover signal-pin directions from a cell's functions.

    CDL `*.PININFO` often declares pins as `:B` (bias/bidirectional) — the
    physically honest label that LVS and SPICE want — which maps to "power"
    and hides the logical in/out the inserter needs. The sidecar `functions`
    encode that logical view: a function *key* is an output, and any pin name
    *referenced* in an expression is an input. We use that to upgrade pins that
    are still "power", leaving explicit `:I`/`:O` and true power/ground pins
    (those never named in a function) untouched.
    """
    if not cell.pin_function:
        return

    outputs = set(cell.pin_function)
    inputs: set[str] = set()
    for expr in cell.pin_function.values():
        for tok in _FUNC_IDENT_RE.findall(expr):
            if tok in cell.pins:
                inputs.add(tok)
    inputs -= outputs  # a key is an output even if it also appears in an expr

    for pin in outputs:
        if cell.pins.get(pin) == "power":
            cell.pins[pin] = "output"
    for pin in inputs:
        if cell.pins.get(pin) == "power":
            cell.pins[pin] = "input"


def _apply_meta(
    cells: dict[str, CellInfo],
    meta: dict,
    meta_source: str,
) -> None:
    """Set is_buf / is_seq / is_restoring / pin_function from a sidecar dict. Warn on unknown names."""
    for key, attr in (
        ("buffers", "is_buf"),
        ("sequential", "is_seq"),
        ("restoring", "is_restoring"),
    ):
        names = meta.get(key, []) or []
        if not isinstance(names, list):
            raise ValueError(
                f"{meta_source}: '{key}' must be a list, got {type(names).__name__}"
            )
        for name in names:
            cell = cells.get(name)
            if cell is None:
                print(
                    f"warning: {meta_source}: '{name}' listed under '{key}' "
                    f"but not present in the CDL",
                    file=sys.stderr,
                )
                continue
            setattr(cell, attr, True)

    functions = meta.get("functions", {}) or {}
    if not isinstance(functions, dict):
        raise ValueError(
            f"{meta_source}: 'functions' must be an object, "
            f"got {type(functions).__name__}"
        )
    for cell_name, pin_map in functions.items():
        cell = cells.get(cell_name)
        if cell is None:
            print(
                f"warning: {meta_source}: '{cell_name}' listed under 'functions' "
                f"but not present in the CDL",
                file=sys.stderr,
            )
            continue
        if not isinstance(pin_map, dict):
            raise ValueError(
                f"{meta_source}: functions['{cell_name}'] must be an object, "
                f"got {type(pin_map).__name__}"
            )
        for pin_name, expr in pin_map.items():
            if pin_name not in cell.pins:
                print(
                    f"warning: {meta_source}: functions['{cell_name}']['{pin_name}'] "
                    f"references a pin not declared in the CDL",
                    file=sys.stderr,
                )
                continue
            cell.pin_function[pin_name] = expr

        # Recover logical in/out for any pin the CDL left as :B (-> "power").
        _infer_directions_from_functions(cell)

    # Clock pins for sequential cells. emit_ff_model needs to know which pin
    # clocks each flop to build `always @(posedge <pin>)`; the CDL can't carry
    # it. {cell_name: clock_pin_name}.
    clocks = meta.get("clock", {}) or {}
    if not isinstance(clocks, dict):
        raise ValueError(
            f"{meta_source}: 'clock' must be an object, "
            f"got {type(clocks).__name__}"
        )
    for cell_name, pin_name in clocks.items():
        cell = cells.get(cell_name)
        if cell is None:
            print(
                f"warning: {meta_source}: '{cell_name}' listed under 'clock' "
                f"but not present in the CDL",
                file=sys.stderr,
            )
            continue
        if not isinstance(pin_name, str):
            raise ValueError(
                f"{meta_source}: clock['{cell_name}'] must be a string pin name, "
                f"got {type(pin_name).__name__}"
            )
        if pin_name not in cell.pins:
            print(
                f"warning: {meta_source}: clock['{cell_name}'] = '{pin_name}' "
                f"is not a pin declared in the CDL",
                file=sys.stderr,
            )
            continue
        if not cell.is_seq:
            print(
                f"warning: {meta_source}: clock['{cell_name}'] is set but the cell "
                f"is not in 'sequential'; the clock pin is only used for sequential "
                f"cells (verify-cdl FF model)",
                file=sys.stderr,
            )
        cell.clock_pin = pin_name


# ---------------------------------------------------------------------------
# Multi-input expansion
# ---------------------------------------------------------------------------


def _normalize_paths(
    inputs: str | Path | Sequence[str | Path],
) -> list[Path]:
    """Accept a single path or a sequence of paths; return list[Path]."""
    if isinstance(inputs, (str, Path)):
        return [Path(inputs)]
    return [Path(p) for p in inputs]


def _expand_inputs(inputs: list[Path]) -> list[Path]:
    """Expand directories to their top-level *.cdl files; keep files as-is.

    Dotfiles are skipped. Output is sorted and deduped by resolved path so
    behavior is deterministic across filesystems with different listing
    orders.
    """
    expanded: list[Path] = []
    for inp in inputs:
        if inp.is_dir():
            for cdl in sorted(inp.glob("*.cdl")):
                if cdl.name.startswith("."):
                    continue
                expanded.append(cdl)
        else:
            expanded.append(inp)

    seen: set[Path] = set()
    deduped: list[Path] = []
    for p in expanded:
        try:
            key = p.resolve()
        except OSError:
            key = p
        if key in seen:
            continue
        seen.add(key)
        deduped.append(p)
    return sorted(deduped, key=lambda p: str(p))


def _discover_sidecars(cdl_paths: list[Path]) -> list[Path]:
    """For each CDL, return its `<stem>.cells.json` sibling if it exists."""
    found: list[Path] = []
    for cdl in cdl_paths:
        candidate = cdl.with_suffix(".cells.json")
        if candidate.exists():
            found.append(candidate)
    return found


# ---------------------------------------------------------------------------
# CdlParser — public class
# ---------------------------------------------------------------------------


class CdlParser:
    """CDL file parser, duck-typed compatible with LibParser.

    Accepts one or more CDL inputs (files or directories) and zero or
    more sidecar JSONs. See module docstring for the precedence rules.
    """

    _CACHE_VERSION = 6
    _CACHE_DIR = Path(".cdlcache")

    def __init__(
        self,
        cdl_paths: str | Path | Sequence[str | Path],
        cell_meta: str | Path | Sequence[str | Path] | None = None,
    ) -> None:
        self.cdl_paths: list[Path] = _expand_inputs(_normalize_paths(cdl_paths))
        if not self.cdl_paths:
            raise ValueError("CdlParser: no .cdl files found in the given inputs")

        if cell_meta is None:
            self.meta_paths: list[Path] = _discover_sidecars(self.cdl_paths)
        else:
            self.meta_paths = _normalize_paths(cell_meta)

        self._db: dict[str, CellInfo] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def source_label(self) -> str:
        """Human-readable description of the input set for logging."""
        if len(self.cdl_paths) == 1:
            return self.cdl_paths[0].name
        head = ", ".join(p.name for p in self.cdl_paths[:3])
        tail = "…" if len(self.cdl_paths) > 3 else ""
        return f"{len(self.cdl_paths)} CDL files: {head}{tail}"

    def parse(self, use_cache: bool = True) -> dict[str, CellInfo]:
        if self._db is not None:
            return self._db

        cache = self._cache_path()
        if use_cache and cache.exists() and self._cache_is_fresh(cache):
            loaded = self._load_cache(cache)
            if loaded is not None:
                self._db = loaded
                return self._db

        db: dict[str, CellInfo] = {}
        cell_sources: dict[str, Path] = {}
        for cdl in self.cdl_paths:
            text = cdl.read_text(encoding="utf-8", errors="replace")
            scanned = _scan(text)
            for name, cell in scanned.items():
                prev = cell_sources.get(name)
                if prev is not None:
                    raise ValueError(
                        f"cell '{name}' defined in both {prev} and {cdl}"
                    )
                cell_sources[name] = cdl
                db[name] = cell
        self._db = db

        for meta_path in self.meta_paths:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            _apply_meta(self._db, meta, str(meta_path))

        if use_cache:
            self._write_cache(cache)

        return self._db

    def get_pin_direction(self, cell_type: str, pin_name: str) -> str | None:
        cell = self.parse().get(cell_type)
        return cell.pins.get(pin_name) if cell else None

    def get_output_pins(self, cell_type: str) -> list[str]:
        cell = self.parse().get(cell_type)
        return cell.output_pins() if cell else []

    def get_input_pins(self, cell_type: str) -> list[str]:
        cell = self.parse().get(cell_type)
        return cell.input_pins() if cell else []

    def get_signal_pins(self, cell_type: str) -> dict[str, str]:
        cell = self.parse().get(cell_type)
        return cell.signal_pins() if cell else {}

    def cell_exists(self, cell_type: str) -> bool:
        return cell_type in self.parse()

    def summary(self) -> str:
        db = self.parse()
        lines = [
            f"CDL library     : {self.source_label}",
            f"Total cells     : {len(db)}",
            f"Total pins      : {sum(len(c.pins) for c in db.values())}",
            f"Buffers tagged  : {sum(1 for c in db.values() if c.is_buf)}",
            f"Sequential tag  : {sum(1 for c in db.values() if c.is_seq)}",
            "",
        ]
        for i, (name, cell) in enumerate(db.items()):
            if i >= 5:
                lines.append(f"  … and {len(db) - 5} more")
                break
            sig = cell.signal_pins()
            ins = [p for p, d in sig.items() if d == "input"]
            outs = [p for p, d in sig.items() if d == "output"]
            lines.append(f"  {name}: in={ins}  out={outs}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _cache_key(self) -> str:
        parts: list[tuple[str, int, int]] = []
        for p in self.cdl_paths + self.meta_paths:
            try:
                st = p.stat()
            except FileNotFoundError:
                continue
            parts.append((str(p.resolve()), st.st_mtime_ns, st.st_size))
        parts.sort()
        digest = hashlib.sha1(repr(parts).encode("utf-8")).hexdigest()
        return digest

    def _cache_path(self) -> Path:
        return self._CACHE_DIR / f"{self._cache_key()}.json"

    def _cache_is_fresh(self, cache: Path) -> bool:
        ctime = cache.stat().st_mtime
        for p in self.cdl_paths + self.meta_paths:
            if not p.exists():
                continue
            if ctime < p.stat().st_mtime:
                return False
        return True

    def _write_cache(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": self._CACHE_VERSION,
            "cells": {
                name: {
                    "pins": cell.pins,
                    "pin_function": cell.pin_function,
                    "is_seq": cell.is_seq,
                    "is_buf": cell.is_buf,
                    "is_restoring": cell.is_restoring,
                    "clock_pin": cell.clock_pin,
                }
                for name, cell in self._db.items()
            },
        }
        path.write_text(json.dumps(data, indent=2))

    @classmethod
    def _load_cache(cls, path: Path) -> dict[str, CellInfo] | None:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get("version") != cls._CACHE_VERSION:
            return None
        return {
            name: CellInfo(
                name=name,
                pins=entry.get("pins", {}),
                pin_function=entry.get("pin_function", {}),
                is_seq=entry.get("is_seq", False),
                is_buf=entry.get("is_buf", False),
                is_restoring=entry.get("is_restoring", False),
                clock_pin=entry.get("clock_pin"),
            )
            for name, entry in data["cells"].items()
        }


# ---------------------------------------------------------------------------
# Stub-Liberty emitter
# ---------------------------------------------------------------------------
# Generates the minimal `.lib` Yosys needs to consume a CDL-only design
# under `read_liberty -ignore_miss_func`. Every cell becomes a black box
# with explicit pin directions; buffers (cells tagged `is_buf`) additionally
# carry `function : "<input_pin>"` so `equiv_induct` recognizes inserted
# buffer instances as identity stages. Sequential cells get no `ff()` block
# — their semantic equivalence is left to vendor LEC.


def emit_stub_lib(
    db: dict[str, CellInfo],
    out_path: str | Path,
    source_name: str = "stub",
    skip_seq: bool = False,
) -> None:
    """Write a minimal Liberty stub from a parsed cell database.

    Used by both the CDL flow (input from CdlParser, function info comes
    from the sidecar's `is_buf` flag) and by Liberty round-trip tests
    (input from LibParser, function info comes from `pin_function`).

    Notes:
    - `is_buf` cells get `function : "<input_pin>"` on the single output.
    - Otherwise, any `pin_function` entry is emitted verbatim (Liberty
      round-trip). When neither is set, the output pin gets no function
      attribute and Yosys (with -ignore_miss_func) treats the cell as a
      blackbox.
    - Sequential cells are not given ff()/latch() blocks here; the CDL
      sidecar doesn't carry the clk/D mapping needed to synthesize them.
      For Liberty round-trip this means clk2fflogic won't recognize
      sequential cells from the stub.
    - `skip_seq`: when True, omit `is_seq` cells entirely. The CDL verify
      flow sets this and instead defines flops via emit_ff_model (a real
      behavioural FF that equiv_induct can reason about); leaving them in
      the stub too would collide with that Verilog module. Default False
      keeps the Liberty round-trip behaviour (flops stay as blackboxes).
    """
    lines: list[str] = []
    lines.append(f"/* Auto-generated from {source_name} — do not edit. */")
    lines.append("library (cdl_stub) {")
    for name, cell in db.items():
        if skip_seq and cell.is_seq:
            continue
        lines.append(f"  cell ({name}) {{")
        outs = cell.output_pins()
        single_in = (
            cell.input_pins()[0]
            if cell.is_buf and len(cell.input_pins()) == 1
            else None
        )
        for pin, direction in cell.pins.items():
            stub_dir = direction if direction in ("input", "output", "inout") else "input"
            func: str | None = None
            if direction == "output":
                if cell.is_buf and single_in is not None and pin in outs:
                    func = single_in
                elif pin in cell.pin_function and not cell.is_seq:
                    # Skip function: on sequential cells — Liberty
                    # functions of an FF output reference internal ff()
                    # nodes (e.g. "IQ") that we don't emit, so Yosys
                    # would fail to resolve them. Drop the function and
                    # let -ignore_miss_func black-box the cell.
                    func = cell.pin_function[pin]
            if func is not None:
                lines.append(f"    pin ({pin}) {{")
                lines.append(f"      direction : {stub_dir};")
                lines.append(f'      function  : "{func}";')
                lines.append("    }")
            else:
                lines.append(f"    pin ({pin}) {{ direction : {stub_dir}; }}")
        lines.append("  }")
    lines.append("}")
    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Behavioural FF-model emitter
# ---------------------------------------------------------------------------
# Generates a Verilog module per sequential cell so verify-cdl has a real
# flip-flop equiv_induct can reason about. A bare blackbox flop has no FF
# semantics — clk2fflogic skips it and equiv_induct dies with "No SAT model
# available". The model registers each output (next-state from `functions`) on
# the clock edge; reset/set pins are left unmodelled. Exact reset/edge semantics
# don't matter for buffer-insertion equivalence: the *same* model sits on both
# sides of the miter, so it only has to be self-consistent. Cycle-accurate FF
# semantics remain vendor-LEC's job. See docs/netlist_editing_workflow.md §8.10.


def emit_ff_model(
    db: dict[str, CellInfo],
    out_path: str | Path,
    source_name: str = "stub",
) -> None:
    """Write a behavioural Verilog model for every sequential cell in `db`.

    One `module` per `is_seq` cell: outputs are `output reg`, every other pin
    (clock, data, power) is `input`, and the body is one
    `always @(posedge <clock_pin>) <out> <= <next_state>;` per output, where
    `<next_state>` is the cell's `functions` expression (Liberty `& | ! ^`
    operators are valid Verilog, so a plain flop's `"D"` and a more elaborate
    next-state expression both pass through verbatim).

    Raises ValueError, naming the cell, when a sequential cell lacks a clock pin
    (sidecar `"clock"` map) or has an output with no `functions` entry — both are
    required to build a deterministic register.
    """
    lines: list[str] = [
        f"// Auto-generated behavioural FF models from {source_name} — do not edit.",
        "// One module per sequential cell for verify-cdl; see",
        "// docs/netlist_editing_workflow.md §8.10.",
        "",
    ]

    for name, cell in db.items():
        if not cell.is_seq:
            continue

        outs = cell.output_pins()
        if not outs:
            raise ValueError(
                f"sequential cell '{name}' has no output pin; give its output(s) "
                f"a direction via a 'functions' entry (keys are outputs) so the FF "
                f"model can register them."
            )
        if cell.clock_pin is None:
            raise ValueError(
                f"sequential cell '{name}' has no clock pin: add it to the sidecar "
                f'"clock" map, e.g. "clock": {{"{name}": "CK"}}. The FF model needs '
                f"it to build `always @(posedge ...)`."
            )
        missing = [p for p in outs if p not in cell.pin_function]
        if missing:
            raise ValueError(
                f"sequential cell '{name}': output pin(s) {missing} have no "
                f"'functions' entry. Each registered output needs its next state, "
                f'e.g. "functions": {{"{name}": {{"{outs[0]}": "D"}}}}.'
            )

        ports = list(cell.pins.keys())
        inputs = [p for p in ports if p not in outs]

        lines.append(f"module {name} ({', '.join(ports)});")
        if inputs:
            lines.append(f"  input  {', '.join(inputs)};")
        lines.append(f"  output reg {', '.join(outs)};")
        lines.append(f"  always @(posedge {cell.clock_pin}) begin")
        for out_pin in outs:
            lines.append(f"    {out_pin} <= {cell.pin_function[out_pin]};")
        lines.append("  end")
        lines.append("endmodule")
        lines.append("")

    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI entry: python -m netlist_tool.cdl_parser --emit-stub-lib FILE.cdl -o OUT
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m netlist_tool.cdl_parser",
        description="CDL parser utilities (stub-.lib emitter).",
    )
    ap.add_argument(
        "--emit-stub-lib",
        type=Path,
        nargs="+",
        metavar="CDL",
        required=True,
        help="One or more CDL files, or a directory of *.cdl. "
        "All inputs are merged; duplicate cell names cause an error.",
    )
    ap.add_argument(
        "--cell-meta",
        type=Path,
        nargs="+",
        default=None,
        metavar="JSON",
        help="Sidecar classification JSON(s). Omit to auto-discover "
        "<cdl_stem>.cells.json next to each CDL; pass explicitly to "
        "override auto-discovery entirely.",
    )
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        metavar="LIB",
        help="Destination path for the stub Liberty file.",
    )
    ap.add_argument(
        "--ff-model",
        type=Path,
        default=None,
        metavar="V",
        help="Also emit a behavioural Verilog model of the sequential cells to "
        "this path. When given, the stub omits sequential cells (they are "
        "defined by the Verilog instead, so verify-cdl gets a real flop "
        "equiv_induct can reason about). Requires a 'clock' map and 'functions' "
        "for each sequential cell in the sidecar.",
    )
    args = ap.parse_args(argv)

    parser = CdlParser(args.emit_stub_lib, cell_meta=args.cell_meta)
    db = parser.parse()
    emit_stub_lib(
        db,
        args.output,
        source_name=parser.source_label,
        skip_seq=args.ff_model is not None,
    )
    print(
        f"Wrote {args.output} ({len(db)} cells, "
        f"{sum(1 for c in db.values() if c.is_buf)} buffer(s) with function attr)"
    )
    if args.ff_model is not None:
        emit_ff_model(db, args.ff_model, source_name=parser.source_label)
        print(
            f"Wrote {args.ff_model} "
            f"({sum(1 for c in db.values() if c.is_seq)} sequential cell model(s))"
        )
    return 0


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(_main())
