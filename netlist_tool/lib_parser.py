"""
----------

Liberty (.lib) file parser.

Extracts cell → pin → direction mappings from any PDK's Liberty file.

Public API
----------
parse(lib_path)  ->  dict[str, dict[str, str]]
    Thin wrapper around LibParser for use by graph_builder.
    Returns {cell_name: {pin_name: direction}} with plain dicts.

LibParser(lib_path)
    Richer interface: helper methods, JSON caching.

Direction values: "input" | "output" | "inout" | "internal" | "power"

Design
------
1. pyparsing strips comments (whole-file pass) — correctly handles
   multi-line /* … */ blocks that fool line-by-line approaches.
2. A single compiled regex tokenises the comment-free text into six
   token types: cell-header, pin-header, pg_pin-header,
   direction-attribute, "{", "}".
3. A state machine walks the token stream tracking brace depth.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .cell_info import CellInfo

# ---------------------------------------------------------------------------
# 1. Comment stripping  (regex — handles multi-line /* */ correctly)
# ---------------------------------------------------------------------------
# Plain re.sub instead of pyparsing.transform_string: the latter's packrat
# cache evicts via O(n) dict iteration, which turns into multi-minute hangs
# on the 12 MB sky130 .lib under Python 3.14.

_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)


def _strip_comments(text: str) -> str:
    return _COMMENT.sub("", text)


# ---------------------------------------------------------------------------
# 2. Token regex  (one compiled pass over the comment-free text)
# ---------------------------------------------------------------------------
# Priority order: pg_pin before pin (so "pin" inside "pg_pin" is never
# matched separately).  Names may be quoted or bare; [^")\s]+ accepts
# brackets, dots, exclamation marks as used in bus pins ("A[0]").

_TOKEN_RE = re.compile(
    r"""
    # \bpg_pin\s*\(\s*(?P<pg_name>   "?[^")\s]+"?)\s*\)   # pg_pin(name)
  | \bcell\s*\(\s*  (?P<cell_name> "?[^")\s]+"?)\s*\)   # cell(name)
  | \bpin\s*\(\s*   (?P<pin_name>  "?[^")\s]+"?)\s*\)   # pin(name)
  | \bdirection\s*:\s*
    (?P<dir_val>     "?(?:input|output|inout|internal)"?)\s*;
  | \bfunction\s*:\s*
    (?P<func_val>    "[^"]*"|[^\s;]+)\s*;                # function : "expr" ;
  | \b(?P<seq_kind>ff|latch)\s*\(                        # ff(...) / latch(...) header
  | (?P<open>  \{)
  | (?P<close> \})
    """,
    re.VERBOSE,
)


def _unquote(s: str) -> str:
    s = s.strip()
    return s[1:-1] if s.startswith('"') and s.endswith('"') else s


# ---------------------------------------------------------------------------
# 3. Data structures
# ---------------------------------------------------------------------------
# CellInfo lives in cell_info.py — shared with cdl_parser.


# ---------------------------------------------------------------------------
# 4. Core state machine
# ---------------------------------------------------------------------------


def _scan(text: str) -> dict[str, CellInfo]:
    """
    Tokenise *text* and build the cell → pin → direction database.

    State variables
    ---------------
    depth       current brace-nesting depth
    cell_name   name of the cell we are currently inside (or None)
    cell_depth  depth when the cell body was opened
    pin_name    name of the signal pin we are currently inside (or None)
    pin_depth   depth when the pin body was opened
    pending_*   a header was seen; waiting for the next '{' to open the body
    """
    cells: dict[str, CellInfo] = {}

    depth = 0
    cell_name: str | None = None
    cell_depth = -1
    pin_name: str | None = None
    pin_depth = -1

    pending_cell: str | None = None
    pending_pin: str | None = None
    # pending_pg_pin: str | None = None

    for m in _TOKEN_RE.finditer(text):
        if m.group("open"):
            depth += 1

            if pending_cell is not None:
                cell_name = pending_cell
                cell_depth = depth
                pending_cell = None
                cells[cell_name] = CellInfo(name=cell_name)

            elif pending_pin is not None:
                pin_name = pending_pin
                pin_depth = depth
                pending_pin = None

        elif m.group("close"):
            # Exit pin scope first, then cell scope.
            if depth == pin_depth:
                pin_name = None
                pin_depth = -1

            if depth == cell_depth:
                cell_name = None
                cell_depth = -1
                pin_name = None
                pin_depth = -1

            depth -= 1

        elif m.group("cell_name") is not None:
            pending_cell = _unquote(m.group("cell_name"))

        elif m.group("pin_name") is not None:
            if cell_name is not None:
                pending_pin = _unquote(m.group("pin_name"))

        elif m.group("dir_val") is not None:
            # Store only when inside a signal-pin body; first occurrence wins.
            if cell_name is not None and pin_name is not None:
                cell = cells[cell_name]
                if pin_name not in cell.pins:
                    cell.pins[pin_name] = _unquote(m.group("dir_val"))

        elif m.group("func_val") is not None:
            # Function attributes only matter on output pins; record the first.
            if cell_name is not None and pin_name is not None:
                cell = cells[cell_name]
                if pin_name not in cell.pin_function:
                    cell.pin_function[pin_name] = _unquote(m.group("func_val"))

        elif m.group("seq_kind") is not None:
            # Mark cell as sequential whenever an ff() or latch() header
            # appears at cell scope (i.e. depth == cell_depth, not nested
            # inside a pin's timing/internal_power group).
            if cell_name is not None and depth == cell_depth:
                cells[cell_name].is_seq = True

    return cells


# ---------------------------------------------------------------------------
# 5. LibParser — richer interface with JSON caching
# ---------------------------------------------------------------------------


class LibParser:
    """
    Liberty file parser with helper methods and optional JSON caching.

    Parse once, reload from cache on subsequent runs.
    """

    def __init__(self, lib_path: str | Path) -> None:
        self.lib_path = Path(lib_path)
        self._db: dict[str, CellInfo] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse(self, use_cache: bool = True) -> dict[str, CellInfo]:
        """
        Return {cell_name: CellInfo}.

        If *use_cache* is True and a valid .lib.json cache exists, use it.
        """
        if self._db is not None:
            return self._db

        cache = self.lib_path.with_suffix(self.lib_path.suffix + ".json")
        if use_cache and cache.exists():
            if cache.stat().st_mtime >= self.lib_path.stat().st_mtime:
                loaded = self._load_cache(cache)
                if loaded is not None:
                    self._db = loaded
                    return self._db

        text = self.lib_path.read_text(encoding="utf-8", errors="replace")
        text = _strip_comments(text)
        self._db = _scan(text)

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
            f"Liberty library : {self.lib_path.name}",
            f"Total cells     : {len(db)}",
            f"Total pins      : {sum(len(c.pins) for c in db.values())}",
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

    _CACHE_VERSION = 3

    def _write_cache(self, path: Path) -> None:
        data = {
            "version": self._CACHE_VERSION,
            "cells": {
                name: {
                    "pins": cell.pins,
                    "pin_function": cell.pin_function,
                    "is_seq": cell.is_seq,
                }
                for name, cell in self._db.items()
            },
        }
        path.write_text(json.dumps(data, indent=2))

    @classmethod
    def _load_cache(cls, path: Path) -> dict[str, CellInfo] | None:
        """Return cells dict, or None if the cache is in an older format."""
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get("version") != cls._CACHE_VERSION:
            return None
        return {
            name: CellInfo(
                name=name,
                pins=entry.get("pins", {}),
                pin_function=entry.get("pin_function", {}),
                is_seq=entry.get("is_seq", False),
            )
            for name, entry in data["cells"].items()
        }


# ---------------------------------------------------------------------------
# 6. Module-level parse() — used by graph_builder and main
# ---------------------------------------------------------------------------


def parse(lib_path: str | Path) -> dict[str, dict[str, str]]:
    """
    Parse *lib_path* and return plain-dict pin directions.

    Returns
    -------
    {cell_name: {pin_name: direction}}
    direction ∈ {"input", "output", "inout", "internal", "power"}
    """
    db = LibParser(lib_path).parse()
    return {name: dict(cell.pins) for name, cell in db.items()}


# ---------------------------------------------------------------------------
# 7. Self-test  (python -m netlist_tool.lib_parser  or  python lib_parser.py)
# ---------------------------------------------------------------------------

_FIXTURE = r"""
/* top-level block comment
   spanning two lines */
library (test_lib) {

  // unquoted cell name; pg_pin with no space; unquoted direction
  cell (simple_and2) {
    area : 1.0 ;
    # pg_pin(VGND) { pg_type : primary_ground ; }
    # pg_pin(VPWR) { pg_type : primary_power  ; }
    pin (A) { direction : input ; }
    pin (B) { direction : input ; }
    pin (X) {
      direction : output ;
      function  : "(A&B)" ;
      timing () {
        /* nested block — skipped cleanly */
        cell_rise (lu_table) {
          values ("0.1, 0.2, 0.3") ;
        }
      }
    }
  }

  // quoted names + quoted direction values + bus pin
  cell ("buf_x1") {
    pg_pin ("VGND") { }
    pg_pin ("VPWR") { }
    pin ("A[0]") { direction : "input"  ; }
    pin ("Z[0]") { direction : "output" ; }
  }

  // inout + internal
  cell (bidir_cell) {
    pin (IO) { direction : inout      ; }
    pin (Q)  { direction : "internal" ; }
  }

  // library-level group — must be skipped
  lu_table_template (delay_template) {
    variable_1 : input_net_transition ;
    index_1 ("0.01, 0.05") ;
  }
}
"""


def _run_self_tests() -> None:
    clean = _strip_comments(_FIXTURE)
    db = _scan(clean)

    passed = 0
    failed = 0

    def check(cond: bool, msg: str) -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  [PASS] {msg}")
        else:
            failed += 1
            print(f"  [FAIL] {msg}")

    print("lib_parser self-test")
    print("=" * 40)

    check("simple_and2" in db, "unquoted cell found")
    check(db["simple_and2"].pins["A"] == "input", "pin A: unquoted direction")
    check(db["simple_and2"].pins["B"] == "input", "pin B: input")
    check(
        db["simple_and2"].pins["X"] == "output", "pin X: output (nested timing skipped)"
    )

    check("buf_x1" in db, "quoted cell name")
    check(db["buf_x1"].pins["A[0]"] == "input", "bus pin: quoted direction")
    check(db["buf_x1"].pins["Z[0]"] == "output", "bus pin output")

    check("bidir_cell" in db, "bidir cell found")
    check(db["bidir_cell"].pins["IO"] == "inout", "inout direction")
    check(db["bidir_cell"].pins["Q"] == "internal", "internal direction stored")

    check("delay_template" not in db, "library-level group skipped")

    print("=" * 40)
    print(f"  {passed} passed, {failed} failed")
    print(f"  Cells found: {sorted(db)}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 1:
        _run_self_tests()
    elif len(sys.argv) == 2:
        import logging

        logging.basicConfig(level=logging.INFO, format="%(message)s")
        p = LibParser(sys.argv[1])
        print(p.summary())
    elif len(sys.argv) == 3:
        p = LibParser(sys.argv[1])
        db = p.parse()
        cell = db.get(sys.argv[2])
        if cell is None:
            print(f"Cell '{sys.argv[2]}' not found.")
            matches = [n for n in db if sys.argv[2] in n]
            if matches:
                print(f"Suggestions: {matches[:8]}")
        else:
            for pin, direction in sorted(cell.pins.items()):
                arrow = {"input": "←", "output": "→", "inout": "↔"}.get(direction, "⏚")
                print(f"  {arrow} {pin:<14} {direction}")
