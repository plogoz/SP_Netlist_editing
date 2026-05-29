# Netlist Editing Workflow — Full Report

## 1. Problem Statement

A hardware design requires post-synthesis netlist modifications to address signal integrity issues. The modifications are **not functional** — they do not change the logic behavior of the circuit. Instead, they involve inserting black-box elements at regular intervals (every N gate-level instances) throughout the netlist.

The closed-source EDA flow used for the real design has a 40-minute simulation cycle, making iterative development of the insertion script impractical. This report describes a parallel open-source workflow for fast prototyping and learning, with the goal of transferring the final tool to the closed-source flow.

### Constraints

- The insertion element is a **black box** — its internals are irrelevant to the script.
- The parameter **N** (insert every N elements) must be configurable at runtime.
- The netlist is **hierarchical**, but editing only happens at the **gate level** (subcircuit internals are not modified).
- The tool must be **PDK-independent** — same script for open-source and closed-source flows.
- Scale: small circuits for testing, thousands of gates (possibly more) in production.

---

## 2. Key Decision: Edit at Verilog Level

Two netlist formats were considered:

| Aspect              | Verilog Netlist                        | SPICE Netlist                          |
|---------------------|----------------------------------------|----------------------------------------|
| Format              | Structural, hierarchical, clean        | Flat or hierarchical, more verbose     |
| Parsing             | Well-defined syntax, regular structure | More irregular                         |
| PDK dependence      | Cell names change, structure doesn't   | Transistor-level, PDK-specific         |
| Transferability     | High — same logic across flows         | Flow-specific                          |
| Downstream          | Feeds into PnR tools                   | Final simulation input                 |

**Decision: edit the Verilog netlist.** The structure is identical across tools and PDKs, making the Python script portable. The modified Verilog netlist then flows through the standard synthesis/PnR/extraction pipeline normally.

---

## 3. Overall Workflow

```
┌─────────────────────────────────────────────┐
│  VHDL FSM (written by hand)                 │
│  Entity: flip_flop_adder                    │
│  File: fsm.vhdl                             │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│  YOSYS + GHDL Plugin                        │
│  Synthesizes VHDL → gate-level Verilog      │
│  (generic cells for now, sky130 later)      │
│  Output: fsm_netlist.v                      │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│  PYTHON SCRIPT (NetworkX-based)             │
│  1. Parse Verilog netlist                   │
│  2. Build directed graph (DiGraph)          │
│  3. Visualize graph (learning/debugging)    │
│  4. Topological walk, count gates           │
│  5. Insert black box every N gates          │
│  6. Serialize modified Verilog netlist      │
│  Input param: N (configurable)              │
│  Output: fsm_netlist_modified.v             │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│  Yosys formal equivalence check             │
│  Prove fsm_modified.v ≡ fsm_netlist.v       │
│  via equiv_make + equiv_induct              │
│  (Signal integrity is deferred to the       │
│   closed-source flow at tapeout.)           │
└─────────────────────────────────────────────┘
```

---

## 4. Iteration Strategy

Not every step runs on every iteration. The development loop is layered:

### Fast loop (seconds) — run constantly
```
Edit Python script → run on Verilog netlist → inspect output / visualize graph → repeat
```

### Validation loop (occasionally, minutes)
```
Run modified netlist through OpenROAD → ngspice → confirm equivalence
```

### Final transfer (once)
```
Point Python script at closed-source Verilog netlist → run through closed-source flow (40 min)
```

---

## 5. Tools

### 5.1 GHDL — VHDL Compiler

- **Purpose:** Analyze and elaborate the VHDL design.
- **Install:** `brew install ghdl`
- **Status:** Installed and working.
- **Usage (standalone simulation):**
  ```bash
  ghdl -a --std=08 fsm.vhdl
  ghdl -a --std=08 tb_flip_flop_adder.vhdl
  ghdl -e --std=08 tb_flip_flop_adder
  ghdl -r --std=08 tb_flip_flop_adder --vcd=tb.vcd
  ```

### 5.2 Yosys + GHDL Plugin — Synthesis

- **Purpose:** Synthesize VHDL into a gate-level Verilog netlist.
- **Install:** `brew install yosys` + build `ghdl-yosys-plugin` from source.
- **Status:** Installed and working (`yosys -m ghdl` loads successfully).
- **Usage (generic synthesis, no PDK):**
  ```bash
  yosys -m ghdl -p "\
      ghdl --std=08 fsm.vhdl -e flip_flop_adder; \
      synth -top flip_flop_adder; \
      write_verilog fsm_netlist.v"
  ```
- **Usage (sky130-mapped synthesis, for later):**
  ```bash
  yosys -m ghdl -p "\
      ghdl --std=08 fsm.vhdl -e flip_flop_adder; \
      synth -top flip_flop_adder; \
      dfflibmap -liberty sky130_fd_sc_hd__tt_025C_1v80.lib; \
      abc -liberty sky130_fd_sc_hd__tt_025C_1v80.lib; \
      write_verilog fsm_netlist.v; \
      write_spice fsm_netlist.spice"
  ```

### 5.3 Python + NetworkX — Netlist Editing Tool

- **Purpose:** Parse, visualize, modify, and re-serialize Verilog netlists.
- **Package manager:** `uv` (use `uv run python` instead of `python`).
- **Visualization:** matplotlib + pygraphviz (`dot` layout, falls back to spring layout).
- **Architecture:**
  ```
  netlist_tool/
    netlist_parser.py  — Verilog netlist → Module dataclass
    cell_info.py       — shared CellInfo dataclass (both backends)
    lib_parser.py      — Liberty (.lib) → cell pin directions
    cdl_parser.py      — CDL (.cdl) → cell pin directions (closed-source PDKs)
    graph_builder.py   — Module → NetworkX DiGraph
    inserter.py        — topological walk + black-box injection every N gates
    serializer.py      — Module → Verilog string / file
    grapher.py         — DiGraph → matplotlib visualization
    main.py            — CLI orchestrator
  ```

- **Status:** All modules implemented and self-tested.

- **Running the tool:**
  ```bash
  # Basic usage (N=5, default placeholder cell BLACKBOX)
  uv run python -m netlist_tool input.v output.v --N 5

  # With custom black-box cell and port names
  uv run python -m netlist_tool input.v output.v --N 5 \
      --bb-cell MY_BB --in-port A --out-port Z

  # With sky130 Liberty file for accurate pin-direction lookup
  uv run python -m netlist_tool input.v output.v --N 5 \
      --lib sky130_fd_sc_hd__tt_025C_1v80.lib

  # Closed-source flow: CDL instead of Liberty.
  # Sidecar TEST_CELLS.cells.json is auto-discovered next to the CDL;
  # pass --cell-meta to point elsewhere. The sidecar lists which cell
  # names are buffers / sequential, since CDL carries no function: or
  # ff()/latch() metadata. See docs/cdl_backend.md for details.
  uv run python -m netlist_tool input.v output.v --N 5 \
      --cdl TEST_CELLS.cdl

  # Multi-file / directory CDL — real PDKs ship cells split across many
  # .cdl files. Either pass an explicit list or a folder; in both cases
  # all cells are merged into one library and a duplicate cell name is
  # a hard error. Sidecars auto-discover per-file (foo.cdl ↔
  # foo.cells.json) unless --cell-meta is given explicitly as one
  # master file or a list.
  uv run python -m netlist_tool input.v output.v --N 5 \
      --cdl pdk/inv.cdl pdk/nand.cdl pdk/dff.cdl
  uv run python -m netlist_tool input.v output.v --N 5 \
      --cdl pdk_cdls/ --cell-meta pdk_master.cells.json

  # Emit a stub .lib from a CDL (or set of CDLs) so Yosys can run
  # equivalence on the CDL-edited netlist (see §5.5 and
  # docs/cdl_backend.md).
  uv run python -m netlist_tool.cdl_parser --emit-stub-lib TEST_CELLS.cdl \
      -o TEST_CELLS.cdl.stub.lib
  uv run python -m netlist_tool.cdl_parser --emit-stub-lib pdk_cdls/ \
      -o pdk_cdls.stub.lib

  # Show graph in interactive window after processing
  uv run python -m netlist_tool input.v output.v --N 5 --visualize

  # Save graph image instead
  uv run python -m netlist_tool input.v output.v --N 5 --visualize graph.png
  ```

- **Parser limitation:** The identifier rule does not allow `$` as a first character, so Yosys **generic** synthesis output (`$_AND_`, `$_DFF_P_`, …) will not parse. Use `make net` (sky130-mapped synthesis) to produce a compatible netlist. Generic cell support can be added to `netlist_parser.py` later if needed.

- **Self-tests:**
  ```bash
  uv run python -m netlist_tool.netlist_parser   # 19 tests
  uv run python -m netlist_tool.lib_parser       # 11 tests
  ```

### 5.4 OpenLane 2 (wraps OpenROAD + sky130 PDK) — Place & Route

- **Purpose:** Take the modified Verilog netlist through physical design and SPICE extraction.
- **Install:** Docker-based (recommended for macOS ARM).
  ```bash
  docker pull efabless/openlane2
  ```
- **PDK:** SkyWater 130nm (`sky130_fd_sc_hd`). Most mature open PDK, best community support.
- **Alternatives considered:** GlobalFoundries 180nm (gf180mcu), IHP 130nm (sg13g2).

### 5.5 Yosys formal equivalence checking

- **Purpose:** Prove that `fsm_modified.v` is logically identical to `fsm_netlist.v`.
  Buffer insertion preserves logic, so this should always pass; the check is the gate that catches inserter bugs early.
- **Tool:** Yosys (already installed for synthesis). No extra install.
- **Run:** `make verify`
- **Approach:** SAT-based equivalence (`equiv_make` + `equiv_induct -seq 10`) on the two netlists merged into a miter circuit. Sequential FFs are translated to combinational logic via `clk2fflogic` + `async2sync` so induction can prove state equivalence in one step.
- **Why this instead of ngspice:** signal integrity is not a goal on sky130 (that's the closed-source flow's job at tapeout). For the open-source loop we only need to confirm functional equivalence, and EC proves it (rather than testing it) in ~0.1 s. ngspice would also require installing `sky130_fd_pr` transistor primitives and writing a power-aware SPICE writer, since Yosys's `write_spice` output is logic-abstract and not directly simulatable.
- **Reproducibility:** The same conceptual flow exists in every closed-source EC tool (Cadence Conformal LEC, Synopsys Formality, Mentor Questa Formal). Keep `make verify` as an open-source sanity layer that runs independently of the vendor LEC step.
- **CDL variant — `make verify-cdl`:** When the input metadata is a `.cdl` (no Liberty available), the recipe first auto-generates a stub `.lib` from the parsed CDL, then runs the same `equiv_make` / `equiv_induct` flow against it. The stub omits `function:` for sequential cells (their Liberty function references internal `ff()` nodes that we don't synthesize), and the Yosys command uses a two-pass `read_liberty` — `-lib` to register blackbox modules for all cells, then `-overwrite` to add function info on top — so flops appear in the design as opaque-but-matching blackboxes while combinational cells and buffers are reasoned about exactly. This proves structural equivalence (the inserted buffers are identity, the rest is unchanged); cycle-level FF semantics are still left to vendor LEC. Full rationale in [docs/cdl_backend.md](cdl_backend.md).

### 5.6 Surfer — Waveform Viewer

- **Purpose:** View simulation waveforms (VCD files) from GHDL simulation.
- **Already in use.**

---

## 6. Installation Summary (macOS, M1 Max)

| Tool                | Install method       | Runs on        |
|---------------------|----------------------|----------------|
| GHDL                | `brew install ghdl`  | Native macOS   |
| Yosys               | `brew install yosys` | Native macOS   |
| ghdl-yosys-plugin   | Build from source    | Native macOS   |
| Python + NetworkX   | `pip install networkx` | Native macOS |
| ngspice             | `brew install ngspice` | Native macOS |
| OpenLane 2 / OpenROAD | Docker             | Docker container |
| sky130 PDK          | Via OpenLane or git clone | Docker / local |

---

## 7. Verilog Netlist Format Reference

### What Yosys generic synthesis produces

```verilog
module flip_flop_adder (clk, rst, A, B, Y);
  input clk, rst, A, B;
  output Y;
  wire n1, n2, n3;

  $_AND_ g1 (.A(A), .B(B), .Y(n1));
  $_OR_  g2 (.A(n1), .B(rst), .Y(n2));
  $_DFF_P_ g3 (.D(n2), .C(clk), .Q(Y));
endmodule
```

### What a closed-source tool might produce

```verilog
module flip_flop_adder (clk, rst, A, B, Y);
  input clk, rst, A, B;
  output Y;
  wire n1, n2, n3;

  AN2D1 U1 (.A1(A), .A2(B), .Z(n1));
  OR2D1 U2 (.A1(n1), .A2(rst), .Z(n2));
  DFCNQD1 U3 (.D(n2), .CP(clk), .Q(Y));
endmodule
```

Different cell names, different port names — but the **structure is identical**. The Python parser handles both by treating each instantiation line as: `cell_type instance_name (.port(net), ...)`.

---

## 8. CDL Workflow (closed-source PDK flow)

In the production flow the PDK is closed-source and the only cell metadata accessible to the Python tool is a CDL (`.SUBCKT` + `*.PININFO`) file — no Liberty, no characterized timing, no `function:` or `ff()` markers. The pipeline (parse → graph → insert → serialize) is unchanged; only the library backend swaps.

### 8.1 What changes vs. the Liberty flow

| Aspect                | Liberty (sky130)                          | CDL (closed-source)                          |
|-----------------------|-------------------------------------------|----------------------------------------------|
| Pin direction source  | `pin () { direction : … }` blocks         | `*.PININFO A:I B:I Y:O …` line               |
| Buffer / FF detection | `function:` equality / `ff()` group       | Sidecar JSON `<cdl>.cells.json`              |
| Equivalence Liberty   | The real PDK `.lib`                       | Auto-generated stub from CDL                 |
| Make targets          | `make editing` / `make verify`            | `make editing-cdl` / `make verify-cdl`       |

Two pieces close the gap:

- **Sidecar JSON.** `<cdl_stem>.cells.json` lists which cell names are buffers and which are sequential. It is the single place where classification the CDL does not carry gets injected — auto-discovered next to the CDL, overridable with `--cell-meta`. The tool never guesses from cell names.
- **Stub `.lib` generator.** `python -m netlist_tool.cdl_parser --emit-stub-lib` writes a minimal Liberty file from the parsed CDL: a `cell()` block per CDL cell with pin directions, plus `function : "<input>"` only on the cells the sidecar tagged as buffers. The Makefile builds this on demand under `$(STUB_LIB)`.

### 8.2 Running the flow

```bash
make editing-cdl     # insert buffers using the CDL backend
make verify-cdl      # structural-equivalence check via auto-generated stub
```

The variables `CDL`, `CELL_META`, `STUB_LIB` are overridable on the command line — `make editing-cdl CDL=foo.cdl CELL_META=foo.cells.json` swaps the fixture for a real PDK CDL without touching the Makefile.

### 8.3 Yosys two-pass `read_liberty`

`verify-cdl` uses a two-pass library read:

```
read_liberty -lib                         $(STUB_LIB)
read_liberty -ignore_miss_func -overwrite $(STUB_LIB)
```

The first pass registers every cell as an empty blackbox module (port directions only) — required because sequential cells in the stub intentionally carry no `function:`, and a single-pass `read_liberty -ignore_miss_func` would drop them entirely. The second pass upgrades the cells that *do* have a function (combinational cells plus tagged buffers) with their function expression. Flops then appear as opaque-but-matching blackboxes on both sides of the miter, buffers are recognized as identity, and the existing `equiv_make` / `equiv_induct` pipeline converges.

### 8.4 Validation

The stub generator was validated by round-trip against the real sky130 Liberty: parse with `LibParser`, emit through the same `emit_stub_lib` the CDL flow uses, then run `make verify LIB=<roundtripped-stub>` on the existing `fsm_netlist.v` / `fsm_modified.v` pair. Both that round-trip and the original `make verify` (with the real `.lib`) print **Equivalence successfully proven**, confirming the writer preserves enough information for Yosys to do its job without ever needing to emit `ff()` blocks.

### 8.5 Limitations

- Cycle-level FF semantics are out of scope for `verify-cdl` — the sidecar does not carry clk/D pin mapping. Vendor LEC handles that at tapeout; `verify-cdl` catches inserter bugs structurally in ~0.1 s.
- `:B` PININFO pins collapse bias, VDD, and VSS into one category (mapped to Liberty `"power"`). Sufficient for the tool's signal-pin filter; can be refined if a PDK ever needs the distinction.

Full rationale, fixture quirks (`*.PININFO`/`.SUBCKT` mismatches, missing final `.ENDS`), and file map in [docs/cdl_backend.md](cdl_backend.md).

### 8.6 Sequential feedback and the DAG assumption

Depth-based insertion walks the netlist in topological order (`inserter.py`
→ `nx.topological_sort`), so **the gate graph must be acyclic**. Real designs
are not: any register feedback (FSM state, counters, load-enable hold) forms a
`flop → logic → flop` loop. The tool is *supposed* to break these at
sequential cells — a flip-flop's output samples the **previous** clock cycle,
so it starts a fresh combinational cone and must not carry a combinational
edge forward.

**The open-source flow never actually exercised this.** On `fsm_netlist.v`,
Yosys routes every flop's fan-out through a **bus-slice** `assign` alias:

```verilog
assign _214_ = bxdp[10];   // consumer reads _214_; flop drives bxdp[10]
```

`graph_builder._build_alias_map` only resolves **scalar→scalar** assigns
(`graph_builder.py:64` — `if a.lhs.msb is None and a.rhs.msb is None`). A
bus-bit RHS like `bxdp[10]` is skipped, so `_214_` never resolves to the
flop's output net, the `flop → mux` feedback edge is never created, and the
graph comes out acyclic **by accident** — real edges simply go missing.
Verified empirically: in the sky130 graph, flop `_527_` (`Q = bxdp[10]`) has
**zero out-edges**.

**The closed-source netlist removes the accident.** It contains **no `assign`
aliases at all** (the vendor tool wires nets directly), so the feedback edges
survive and `topological_sort` hits genuine cycles — the SCCs reported by
`inserter._diagnose_cycle` (e.g. "4 cycle group(s); smallest=3, largest=9").

Two traps when diagnosing this:

- **`contains sequential cell? no` is unreliable.** It is computed from
  `CellInfo.is_seq` (`inserter.py:79`). A stub `.lib` / CDL that does not tag
  its flops reports "no" even when the loop physically runs through a flop.
  Trust the `Cell-type histogram in cycle:` line instead — the flop cell type
  will be in it.
- **`inout` pins are not the cause.** They are treated as consumers, never
  drivers (`graph_builder.py:88-93`), so mislabeled directions *drop* edges
  rather than add the back-edge a loop needs. Fix them for correctness, but
  they do not create these cycles.

**Fix — two parts, both required:**

1. **Tag the flops as sequential.** Liberty derives this from `ff()`/`latch()`
   groups; the hand-edited stub has none. In the closed-source flow the
   authoritative place is the **CDL sidecar JSON `"sequential"` list**
   (`<cdl>.cells.json`) — every flip-flop / latch cell name must be listed
   there. Without this the tool cannot know where to cut.
2. **Cut the graph at sequential cells** in `build_graph`: skip registering a
   sequential cell's output as a net driver, so its `Q` becomes a graph source
   and every register loop opens into a DAG. *(Implemented — `build_graph`
   computes `seq_types` from `is_seq` and skips those instances when building
   the net→driver map. Verified: sky130 still inserts 3 buffers, and a directly
   wired feedback netlist with no `assign` aliases now becomes a DAG.)*

### 8.7 `restoring` cells — depth reset without a cut

Some libraries have a cell that performs logic **and** re-drives the signal —
e.g. a **mux that also acts as a buffer**. For signal integrity it is a
restoration point (consecutive-logic depth should reset on its output), but it
is multi-pin so it cannot go in `"buffers"` (that list doubles as the pool of
1-in/1-out cells eligible to be *inserted*, which a mux cannot be).

The CDL sidecar carries a third classification list for these:

```json
{
  "sequential": ["DFF_TEST"],
  "buffers":    ["BUFF_TEST"],
  "restoring":  ["MUX_TEST"],
  "functions":  { "AND_TEST": { "Y": "(A & B)" } }
}
```

A `restoring` cell **resets the insertion depth counter on its output but does
not cut the graph** (`inserter.py` treats it as a restoration point via
`CellInfo.is_restoration_point()`; `graph_builder` still cuts on `is_seq`
alone). This is deliberate: the inserter runs `topological_sort` *before* the
depth walk, so a reset-only flag is a placement refinement and **cannot break a
cycle**. Loop breaking remains the job of the sequential cut (§8.6).

**Consequence:** every loop must still contain a `sequential` cell. If a cell
genuinely *holds state* — a mux-latch with feedback and no separate flip-flop —
it belongs in `"sequential"` (cut + reset), not `"restoring"` (reset only).
`_diagnose_cycle` now says so explicitly when a surviving cycle contains no
sequential cell.

`restoring` is orthogonal to equivalence: the cell keeps its `functions` entry,
so `emit_stub_lib` emits its function and `verify-cdl` reasons through it
normally.

`scripts/lib_to_cdl.py` grows a `--restoring CELL …` option to tag cells when
bootstrapping a CDL+sidecar from a Liberty (the flag can't be derived from
Liberty — it's a signal-integrity judgement):

```bash
uv run python scripts/lib_to_cdl.py <lib> -o out.cdl \
    --restoring sky130_fd_sc_hd__mux2_1
```

### 8.8 Cell-class reference and the real-CDL bring-up loop

The three sidecar lists answer three independent questions. A cell can be in
at most one (a `restoring` cell never goes in `buffers` and vice-versa):

| Cell class            | Sidecar key    | Cuts the graph? | Resets depth? | Insertable buffer? |
|-----------------------|----------------|-----------------|---------------|--------------------|
| Sequential (flop/latch) | `"sequential"` | **yes** (opens loops) | yes | no |
| Buffer (1-in/1-out)   | `"buffers"`    | no              | yes           | yes (auto-selected) |
| Restoring (e.g. mux)  | `"restoring"`  | no              | yes           | no |

- **Cuts the graph** → makes the combinational graph a DAG (§8.6). Only
  sequential cells do this.
- **Resets depth** → the consecutive-logic counter restarts on the cell's
  output, so no buffer is inserted right after it (`is_restoration_point()`).
- **Insertable** → eligible to be the cell the tool *inserts*; must be a
  1-in/1-out buffer (a mux can't be — it needs a select line).

**Bringing the flow up on a real vendor CDL.** The sidecar is the only place
the classification the CDL omits gets injected, so expect to iterate on it:

1. Run `make editing-cdl CDL=… CELL_META=…`. If it fails with
   `graph contains a cycle`, read the diagnostic, **not** the
   `contains sequential cell?` line (it's `is_seq`-derived and lies when flops
   are untagged — §8.6). Read the `Cell-type histogram in cycle:` line.
2. Every cell type in that histogram that **holds state** (flip-flop, latch, or
   a mux-latch that closes a feedback loop itself) goes in `"sequential"`. That
   is what breaks the loop. Re-run.
3. If a cycle still survives and its histogram contains **no** sequential cell,
   `_diagnose_cycle` now prints exactly that, plus a reminder that `"restoring"`
   does not break loops — find the state element in the loop and move it to
   `"sequential"`.
4. Once editing succeeds, tag your re-driving combinational cells (e.g. a
   buffering mux) in `"restoring"` to refine *where* buffers land. This never
   affects whether the run succeeds — only placement.
5. Run `make verify-cdl` to confirm the edit is logically equivalent (catches
   inserter bugs in ~seconds, instead of the 40-min SPICE run). Sequential
   semantics are still deferred to vendor LEC.

The split matters: **`sequential` decides whether the run *works*; `restoring`
only decides whether the buffers land in the *right place*.**

---

## 9. Next Steps

1. ~~**Run Yosys synthesis** on `fsm.vhdl` to produce the first Verilog netlist.~~ *(Makefile ready: `make synth` / `make net`)*
2. ~~**Build the Python parser** to read the netlist into a NetworkX graph.~~ *(Done: `netlist_parser.py`, `lib_parser.py`, `graph_builder.py`)*
3. ~~**Build a graph visualizer** to inspect and understand the circuit structure.~~ *(Done: `grapher.py`)*
4. ~~**Implement the insertion logic** (topological walk + black box injection).~~ *(Done: `inserter.py`, `serializer.py`, `main.py`)*
5. ~~**Validate** with OpenLane + ngspice on the small FSM.~~
   *(Replaced by Yosys formal equivalence — see §5.5. SPICE / OpenLane were ruled out: the goal on sky130 is functional equivalence, not signal integrity, and Yosys EC proves it in ~0.1 s.)*
   ```bash
   make net          # sky130-mapped synthesis → fsm_netlist.v
   make editing      # insert buffers → fsm_modified.v
   make verify       # prove equivalence (Yosys equiv_induct)
   ```
6. ~~**Transfer** to the closed-source Verilog netlist.~~ *(CDL backend integrated: `make editing-cdl` runs the insertion pipeline against a `.cdl` + sidecar JSON; `make verify-cdl` runs Yosys equivalence via an auto-generated stub `.lib`. Both verified end-to-end against the sky130 fixture by round-tripping the real Liberty file through the same writer. See [docs/cdl_backend.md](cdl_backend.md).)* Run the vendor LEC tool for the final equivalence check; keep `make verify-cdl` as the dev-time sanity layer that runs independently of the vendor toolchain.
