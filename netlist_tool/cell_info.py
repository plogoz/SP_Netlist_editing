"""
Shared CellInfo dataclass for library backends (Liberty and CDL).

Both lib_parser.py and cdl_parser.py populate this same structure so the
graph_builder / inserter / main pipeline is backend-agnostic.

Direction values mirror Liberty's:
    "input" | "output" | "inout" | "internal" | "power"

CDL collapses bias + power + ground into a single token ("B"); the CDL
parser maps that to "power" so `signal_pins()` filters them out the same
way it does for Liberty pg_pins.

is_seq and is_buf are explicit flags rather than derived properties so
either backend can set them: Liberty derives is_seq from ff()/latch()
groups and is_buffer() from the `function` attribute, while CDL has
neither and instead reads both from a sidecar JSON classification.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CellInfo:
    """Pin direction info for a single standard cell."""

    name: str
    pins: dict[str, str] = field(default_factory=dict)  # pin_name → direction
    pin_function: dict[str, str] = field(default_factory=dict)  # output_pin → fn expr
    is_seq: bool = False  # ff()/latch() in Liberty, or sidecar flag for CDL
    is_buf: bool = False  # sidecar flag (CDL only); Liberty path leaves False
    is_restoring: bool = False  # sidecar flag (CDL only): a multi-pin cell that
    # re-drives a signal (e.g. a buffering mux). It is a restoration point
    # (depth resets on its output) but is NOT a 1-in/1-out insertion buffer and
    # does NOT cut the graph — the loop it sits on is broken at a sequential
    # cell. Liberty path leaves this False.

    def signal_pins(self) -> dict[str, str]:
        """Signal pins only (excludes power/ground)."""
        return {p: d for p, d in self.pins.items() if d not in ("power", "internal")}

    def input_pins(self) -> list[str]:
        return [p for p, d in self.pins.items() if d == "input"]

    def output_pins(self) -> list[str]:
        return [p for p, d in self.pins.items() if d == "output"]

    def is_sequential(self) -> bool:
        """True iff this cell holds state (FF or latch)."""
        return self.is_seq

    def is_buffer(self) -> bool:
        """True iff this cell is a 1-input/1-output combinational buffer.

        Two paths:
          - Explicit: `is_buf` set (CDL sidecar classification).
          - Derived: Liberty `function` of the single output pin equals
            the single input pin name, ignoring surrounding parens.
        """
        ins = self.input_pins()
        outs = self.output_pins()
        if len(ins) != 1 or len(outs) != 1:
            return False
        if self.is_buf:
            return True
        func = self.pin_function.get(outs[0], "").strip()
        while func.startswith("(") and func.endswith(")"):
            func = func[1:-1].strip()
        return func == ins[0]

    def is_restoration_point(self) -> bool:
        """True iff this cell re-drives its output, resetting logic depth.

        Sequential cells, 1-in/1-out buffers, and `is_restoring` re-drivers
        (e.g. a buffering mux) all qualify. The inserter resets the depth
        counter on the outputs of any such cell. Note this is broader than the
        graph cut, which is keyed on `is_seq` alone (only flops/latches open
        feedback loops; buffers and restoring cells reset depth without cutting).
        """
        return self.is_seq or self.is_buffer() or self.is_restoring
