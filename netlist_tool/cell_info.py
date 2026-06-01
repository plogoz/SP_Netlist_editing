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
    clock_pin: str | None = None  # sidecar "clock" map (CDL only): the clock pin
    # of a sequential cell. Consumed only by emit_ff_model (verify-cdl) to build
    # `always @(posedge <clock_pin>)`; the insertion path never reads it. Liberty
    # path leaves this None.

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

    def buffer_lanes(self) -> list[tuple[str, str]] | None:
        """Identity lanes if this cell is a buffer, else None.

        A *buffer* re-drives each input onto a matching output unchanged. We
        generalize from the single-ended 1-in/1-out case to N lanes so that a
        differential buffer (2-in/2-out: A,AN -> Z,ZN) is recognized too:

            single-ended:  [("A", "Y")]
            differential:  [("A", "Z"), ("AN", "ZN")]

        A lane (in_pin, out_pin) exists when out_pin's function is the identity
        of in_pin. The cell qualifies iff it has an equal, non-zero number of
        inputs and outputs and every output maps to a distinct input this way.
        Lanes are returned in output-pin declaration order.

        Two derivation paths, mirroring the two backends:
          - Liberty: each output's `function` string equals an input pin name
            (parens stripped). This also covers single-ended sky130 buffers.
          - CDL sidecar: the `functions` map populates `pin_function` the same
            way, so a differential buffer needs `functions` (e.g. {"Z":"A",
            "ZN":"AN"}) to expose its lanes.

        Fallback: a 1-in/1-out cell flagged `is_buf` with no function still
        yields one lane, so single-ended CDL sidecars that only list a name
        under "buffers" keep working without a `functions` entry.
        """
        ins = self.input_pins()
        outs = self.output_pins()
        if not ins or len(ins) != len(outs):
            return None

        lanes: list[tuple[str, str]] = []
        used_inputs: set[str] = set()
        for out_pin in outs:
            func = self.pin_function.get(out_pin, "").strip()
            while func.startswith("(") and func.endswith(")"):
                func = func[1:-1].strip()
            if func in ins and func not in used_inputs:
                lanes.append((func, out_pin))
                used_inputs.add(func)
        if len(lanes) == len(outs):
            return lanes

        # No (complete) function-derived mapping. Single-ended is_buf cells from
        # a CDL sidecar carry no function, so honour the explicit flag here.
        if self.is_buf and len(ins) == 1 and len(outs) == 1:
            return [(ins[0], outs[0])]
        return None

    def is_buffer(self) -> bool:
        """True iff this cell re-drives its inputs unchanged (see buffer_lanes).

        Covers the single-ended 1-in/1-out buffer and the N-lane differential
        buffer alike.
        """
        return self.buffer_lanes() is not None

    def is_restoration_point(self) -> bool:
        """True iff this cell re-drives its output, resetting logic depth.

        Sequential cells, 1-in/1-out buffers, and `is_restoring` re-drivers
        (e.g. a buffering mux) all qualify. The inserter resets the depth
        counter on the outputs of any such cell. Note this is broader than the
        graph cut, which is keyed on `is_seq` alone (only flops/latches open
        feedback loops; buffers and restoring cells reset depth without cutting).
        """
        return self.is_seq or self.is_buffer() or self.is_restoring
