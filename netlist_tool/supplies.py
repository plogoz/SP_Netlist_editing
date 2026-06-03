"""
Netlist-paired supply sidecar — rail connections for inserted buffers.

In the full-custom CDL flow, standard cells expose dedicated supply/bias pins
(VDD, VSS, BIAS_P, …) that the synthesized Verilog wires to top-level rail
ports. The buffers `inserter.py` inserts wire only their identity lanes, so
their supply pins would float. This module supplies the missing mapping.

The mapping is **netlist-specific** (rail port names vary per netlist), so it
lives next to the netlist as `<netlist_stem>.supplies.json`, not in the
library `.cdl` sidecar:

    { "rails": { "VDD": "vdd", "VSS": "vss", "BIAS_P": "bias_p" } }

Left = supply *pin name* on the cells (uniform across the library); right =
the *top-level net* in this netlist. A single global map suffices because the
supply pin names are the same on every cell.

Consumed only by netlist_tool — the `.v`→`.sp` converter does not read it.

The Liberty flow is a natural no-op: `lib_parser` does not parse pg_pins, so
its cells have no power pins and `resolve_supply_connections` returns an empty
map. Only CDL `:B`→power pins (those not named in a `functions` expression)
participate.

See docs/netlist_editing_workflow.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cell_info import CellInfo
    from .netlist_parser import Module


def discover_supplies(netlist_path: str | Path) -> Path | None:
    """Return the `<stem>.supplies.json` sibling of *netlist_path* if it exists."""
    candidate = Path(netlist_path).with_suffix(".supplies.json")
    return candidate if candidate.exists() else None


def load_supplies(path: str | Path) -> dict[str, str]:
    """Read a supplies sidecar and return the `rails` map {pin_name: net_name}.

    Raises ValueError (naming the file) on a malformed structure.
    """
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be an object, got {type(data).__name__}")

    rails = data.get("rails", {})
    if not isinstance(rails, dict):
        raise ValueError(
            f"{path}: 'rails' must be an object {{pin: net}}, got {type(rails).__name__}"
        )
    for pin, net in rails.items():
        if not isinstance(net, str):
            raise ValueError(
                f"{path}: rails['{pin}'] must be a string net name, "
                f"got {type(net).__name__}"
            )
    return dict(rails)


def resolve_supply_connections(
    rails: dict[str, str],
    bb_cell: str,
    cells: dict[str, CellInfo],
    module: Module,
    source: str = "supplies sidecar",
) -> dict[str, str]:
    """Validate *rails* against the buffer cell and netlist; return pin→net.

    The returned map is restricted to `bb_cell`'s power pins — exactly the pins
    that need wiring on each inserted buffer instance.

    Hard errors (ValueError, naming the offender):
      - a power pin of `bb_cell` is absent from `rails` (it would float — the
        bug this whole feature exists to fix), or
      - a mapped rail net is not declared as a port or wire in `module`
        (references an undeclared net → implicit floating wire).

    A `rails` entry that does not match any `bb_cell` power pin is a warning
    (typo / leftover), not an error — other cells may legitimately share the map.
    """
    bb_info = cells.get(bb_cell)
    bb_power = bb_info.power_pins() if bb_info is not None else []

    # Every supply pin of the inserted buffer must be mapped, or it floats.
    missing = [p for p in bb_power if p not in rails]
    if missing:
        raise ValueError(
            f"{source}: buffer cell '{bb_cell}' has power pin(s) {missing} with no "
            f"'rails' entry — they would float on every inserted buffer. Add them, "
            f'e.g. "rails": {{"{missing[0]}": "<top_net>"}}.'
        )

    declared = set(module.ports) | set(module.wires)
    conns: dict[str, str] = {}
    for pin in bb_power:
        net = rails[pin]
        if net not in declared:
            raise ValueError(
                f"{source}: rails['{pin}'] = '{net}' is not a port or wire in "
                f"module '{module.name}'. The rail must be a top-level supply net "
                f"declared in the netlist."
            )
        conns[pin] = net

    # Entries that don't apply to this buffer: harmless, but flag likely typos.
    for pin in rails:
        if pin not in bb_power:
            print(
                f"warning: {source}: rails['{pin}'] does not match any power pin of "
                f"buffer cell '{bb_cell}' — ignored for this insertion.",
                file=sys.stderr,
            )

    return conns


# ---------------------------------------------------------------------------
# Self-test:  python -m netlist_tool.supplies
# ---------------------------------------------------------------------------


def _run_self_tests() -> None:
    import tempfile

    from .cell_info import CellInfo
    from .netlist_parser import Module, PortDecl, WireDecl

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

    print("supplies self-test")
    print("=" * 40)

    # A differential buffer with two power pins.
    buf = CellInfo(
        name="BUFD",
        pins={"A": "input", "Z": "output", "VDD": "power", "VSS": "power"},
    )
    cells = {"BUFD": buf}
    module = Module(
        name="m",
        ports={"vdd": PortDecl("vdd", "input"), "vss": PortDecl("vss", "input")},
        wires={"vdd": WireDecl("vdd"), "vss": WireDecl("vss")},
    )

    # load_supplies round-trip + structural validation
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"rails": {"VDD": "vdd", "VSS": "vss"}}, f)
        good_path = f.name
    rails = load_supplies(good_path)
    check(rails == {"VDD": "vdd", "VSS": "vss"}, "load_supplies reads rails map")

    conns = resolve_supply_connections(rails, "BUFD", cells, module)
    check(conns == {"VDD": "vdd", "VSS": "vss"}, "resolve maps power pins to rails")

    # Missing power pin → hard error
    try:
        resolve_supply_connections({"VDD": "vdd"}, "BUFD", cells, module)
        check(False, "missing power pin raises")
    except ValueError as e:
        check("VSS" in str(e), "missing power pin raises (names VSS)")

    # Rail net not declared → hard error
    try:
        resolve_supply_connections(
            {"VDD": "vdd", "VSS": "no_such_net"}, "BUFD", cells, module
        )
        check(False, "undeclared rail net raises")
    except ValueError as e:
        check("no_such_net" in str(e), "undeclared rail net raises (names net)")

    # Liberty no-op: buffer with no power pins → empty map
    sig_buf = CellInfo(name="buf", pins={"A": "input", "X": "output"})
    empty = resolve_supply_connections(
        {"VDD": "vdd"}, "buf", {"buf": sig_buf}, module
    )
    check(empty == {}, "buffer without power pins → empty (Liberty no-op)")

    # Malformed sidecar → ValueError
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"rails": ["not", "a", "dict"]}, f)
        bad_path = f.name
    try:
        load_supplies(bad_path)
        check(False, "malformed rails raises")
    except ValueError:
        check(True, "malformed rails raises")

    print("=" * 40)
    print(f"  {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    _run_self_tests()
