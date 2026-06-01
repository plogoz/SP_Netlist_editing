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
- **CDL variant — `make verify-cdl`:** When the input metadata is a `.cdl` (no Liberty available), the recipe auto-generates two temporary files from the parsed CDL — a stub `.lib` for the combinational cells and a behavioural Verilog **FF model** for the sequential cells — then runs the `equiv_make` / `equiv_induct` flow against them. Combinational cells and buffers are reasoned about exactly via their `function:`; sequential cells are emitted as real `always @(posedge CLK) Q <= D;` flops so `equiv_induct` can do its temporal induction. (An earlier design left flops as functionless blackboxes; that fails — `equiv_induct` cannot model a blackbox flop and aborts with *No SAT model available*. See §8.10.) This proves structural equivalence (the inserted buffers are identity, the rest is unchanged); cycle-accurate FF semantics are still left to vendor LEC. Full rationale in [docs/cdl_backend.md](cdl_backend.md).

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

The first pass registers every cell as an empty blackbox module (port directions only) — required because functionless cells in the stub (physical cells like end-caps/fillers, and any cell left uncharacterised) would be dropped by a single-pass `read_liberty -ignore_miss_func`. The second pass upgrades the cells that *do* have a function (combinational cells plus tagged buffers) with their function expression. Physical cells stay as opaque-but-matching blackboxes on both sides of the miter, buffers are recognized as identity. **Sequential cells are not in the stub at all** — they are excluded (`--ff-model` implies `skip_seq`) and supplied separately as behavioural Verilog flops (§8.10), because a functionless blackbox flop makes `equiv_induct` abort. With those pieces the `equiv_make` / `equiv_induct` pipeline converges.

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
| Buffer (N identity lanes) | `"buffers"` | no              | yes           | yes (auto-selected) |
| Restoring (e.g. mux)  | `"restoring"`  | no              | yes           | no |

- **Cuts the graph** → makes the combinational graph a DAG (§8.6). Only
  sequential cells do this.
- **Resets depth** → the consecutive-logic counter restarts on the cell's
  output, so no buffer is inserted right after it (`is_restoration_point()`).
- **Insertable** → eligible to be the cell the tool *inserts*; must be a buffer
  whose every output is the identity of an input (1 lane single-ended, 2 lanes
  differential — §8.9). A mux can't be — it needs a select line.

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

### 8.9 Differential libraries — N-lane buffers

Some closed-source PDKs are **differential**: every signal travels as a
(true, complement) pair, so every cell — including the buffer — has paired I/O.
The buffer is typically 2-in/2-out (`A, AN → Z, ZN`), never the single-ended
1-in/1-out cell the early tool assumed. That mismatch surfaces as
`no buffer cell found` on a differential CDL while sky130 (single-ended) works.

The tool models a buffer as **N identity lanes** rather than 1-in/1-out. A lane
is a `(in_pin, out_pin)` pair where the output is the *identity* of the input.
The buffer qualifies when it has an equal, non-zero number of inputs and
outputs and every output maps to a distinct input this way:

| Buffer            | Lanes                              |
|-------------------|------------------------------------|
| single-ended      | `[("A", "Y")]`                     |
| differential      | `[("A", "Z"), ("AN", "ZN")]`       |

**Lanes are derived from `functions`** — the same data `verify-cdl` already
needs — so a differential buffer is declared by listing it in `"buffers"` *and*
giving its identity functions:

```json
{
  "buffers": ["BUFD"],
  "sequential": ["DFFD"],
  "functions": {
    "AND2D": { "Z": "(A0 & A1)", "ZN": "(A0N | A1N)" },
    "BUFD":  { "Z": "A", "ZN": "AN" }
  }
}
```

(A single-ended CDL buffer needs no `functions` entry — a 1-in/1-out cell listed
in `"buffers"` falls back to one lane automatically.)

**Pin directions: the `:B` trap, and why `functions` fixes it.** A differential
CDL routinely declares *every* signal pin `:B` in `*.PININFO`:

```
.SUBCKT BUFF_PP_DPTLSL I IB Q QB
*.PININFO I:B IB:B Q:B QB:B
.ENDS
```

`:B` (bias/bidirectional) is the *physically honest* label — current may flow
either way (e.g. a node sinking charge to discharge a transistor) — so the
vendor uses it, and **LVS/CDL-comparison decks assert it against the layout**.
But the parser maps `:B` to `"power"` (`cdl_parser.py`), so `input_pins()` and
`output_pins()` both come back empty: `buffer_lanes()` returns `None` and you
get `no buffer cell found` even with a correct `"buffers"` + `"functions"`. This
is the single most common bring-up failure on a real differential CDL, and it's
why sky130 (clean `:I`/`:O`) never hits it.

You **must not** rewrite the `.cdl` to `:I`/`:O`: the *same file* feeds the AMS
SPICE simulation and LVS. The `*.PININFO` line is a SPICE *comment* (`*` prefix),
so the simulator ignores it and your results are unchanged — **but** LVS does
read it, and flipping a genuinely bidirectional pin to `:I`/`:O` can raise
pin-direction mismatches. Liberty's `direction:` is the *logical* abstraction
digital tools need (asserted even when real current flows the other way); CDL
`:B` is the *physical* truth. Keep both honest by keeping them separate.

So instead the CDL backend **infers logical directions from `functions`**: a
function key is an output, a pin named inside an expression is an input. Only
pins still tagged `"power"` (i.e. the `:B` ones) are upgraded — explicit `:I`/
`:O` and true supply pins (never named in a function) are left alone, and the
`.cdl` is never touched. For the buffer above, `{"Q":"(I)","QB":"(IB)"}` recovers
`Q,QB` as outputs and `I,IB` as inputs, yielding lanes `[("I","Q"),("IB","QB")]`.
The practical consequence: **on a differential CDL, every cell that participates
in insertion needs a `functions` entry** — not only for `verify-cdl`, but to
give its pins a direction at all.

**Insertion is by lane chunks.** When a gate exceeds the depth limit, the tool
buffers its output nets in chunks of `len(lanes)`, emitting one buffer instance
per chunk. A single-ended buffer (1 lane) therefore drops one instance per
output net — e.g. a full adder's `SUM` and `COUT` each get their own — while a
differential buffer drives a true/complement pair through a single instance:

```verilog
// gate g3 exceeds N; one BUFD carries both phases of its output pair:
BUFD bb_0 (.A(w3), .Z(_bb_0_), .AN(w3n), .ZN(_bb_1_));
// the downstream gate is redirected on both phases:
AND2D g4 (.A0(_bb_0_), .A0N(_bb_1_), .A1(e), .A1N(en), .Z(y), .ZN(yn));
```

Each lane is an independent identity path, so the true/complement nets are
paired to lanes positionally — any consistent assignment buffers each net
correctly, and `verify-cdl` proves it (the stub emits `Z="A"`, `ZN="AN"`, so the
prover sees both lanes as identities).

> **`graph_builder` records every output phase** as a node attribute
> (`output_nets`). A `DiGraph` collapses the two parallel edges from a
> differential gate to a shared consumer into one edge, so the inserter reads
> the complete phase set from the node, not from `out_edges`.

**The Liberty path gets this for free.** Liberty populates `pin_function` from
its `function:` attribute, so a differential `.lib` buffer
(`pin(Z){function:"A";}  pin(ZN){function:"AN";}`) is recognized as a 2-lane
buffer with no extra code. **Caveat:** this only holds when each complement
output is the identity of the complement *input* (`ZN = "AN"`). A cell that
regenerates the complement from a single input (`ZN = "!A"`, a 1-in/2-out cell)
is *not* an identity lane and won't be classified as a buffer — tag such a cell
explicitly with `--bb-cell` and `--in-port/--out-port` if you need it.

The single-ended path is a strict special case (N=1), so sky130 results are
unchanged — `make all` still proves equivalence on 153 cells.

### 8.10 Sequential cells — behavioural FF models

`equiv_induct` does temporal induction and must treat each flop as a **real
state element**. A functionless blackbox flop has no FF semantics:
`clk2fflogic` skips it and the prover aborts with
`No SAT model available for cell … (DFF_…)`. So `verify-cdl` does **not** leave
flops as blackboxes — it generates a behavioural Verilog model for them.

Two sidecar fields drive this, both required per sequential cell:

```json
"sequential": ["DFFD"],
"functions":  { "DFFD": { "Q": "D", "QN": "DN" } },
"clock":      { "DFFD": "CK" }
```

- **`functions`** — next state per output (`Q<=D`, `QN<=DN`). A bare pin for a
  plain flop; any `& | ! ^` expression also works (those are Verilog operators
  too). The keys also give the outputs a direction via the usual `:B` inference
  (§8.9).
- **`clock`** — the clock pin, the one thing the CDL and the `functions` can't
  supply. `{cell: pin}`.

`emit_ff_model` (`cdl_parser.py`) turns each into one module:

```verilog
module DFFD (CK, D, DN, Q, QN);
  input  CK, D, DN;
  output reg Q, QN;
  always @(posedge CK) begin Q <= D; QN <= DN; end
endmodule
```

`emit_stub_lib` is called with `skip_seq=True` (implied by `--ff-model`) so the
flop is **not** also in the stub `.lib` — the Verilog module is its sole
definition (a duplicate would collide). The `verify-cdl` Yosys script gains
`read_verilog $(FF_MODEL)` and a `proc` pass (to lower the always-block to a
`$_DFF_`) before `clk2fflogic`.

The model is applied **identically to gold and gate**, so exact reset/edge
semantics don't affect the buffer-insertion proof — only self-consistency
matters. Posedge is assumed and reset/set pins are left unmodelled; cycle-
accurate FF semantics remain vendor-LEC's job. Latches (also `is_seq`) are
modelled as posedge flops — acceptable for structural equivalence.

**Differential clocks** need nothing special: name the true rail in `"clock"`
(`posedge CK` is the same instant as `negedge CKB`); the complement rail `CKB`
is a non-output pin, so it is declared as an unused `input` and ignored. A fully
differential flop (clock `CK/CKB`, data `D/DN`, outputs `Q/QN`) proves
equivalent unchanged.

**Temporary files.** Both `$(STUB_LIB)` and `$(FF_MODEL)` are regenerated each
run and removed by a cleanup `trap … EXIT` around the Yosys call, so they are
deleted on success *and* failure (the old trailing `rm` leaked on a failed
proof). Bootstrapping a sidecar from Liberty? `scripts/lib_to_cdl.py` grows a
`--clock CELL=PIN` flag (the clock can't be derived from Liberty here, same as
`--restoring`).

### 8.11 `verify-cdl` cell-coverage checklist

`equiv_induct`/`hierarchy` need every instantiated cell accounted for. Common
bring-up failures and their cause:

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Module '…' … is not part of the design` (at `hierarchy`) | A cell in the netlist (often a **physical** cell: end-cap, antenna, fill, tap, decap) is absent from the CDL set | Add its CDL to `CDL=` (the backend merges a directory), or a minimal empty `.SUBCKT` stub |
| `No SAT model available for cell … (<comb cell>)` | A **combinational** cell (incl. **clock/CTS buffers**) the prover must traverse has no `function:` | Give it a `functions` entry (a clock buffer is identity: `{"Z":"A"}`) |
| `No SAT model available for cell … (DFF_…)` | A **flop** left as a functionless blackbox | Give it `functions` + `clock` so it gets a behavioural model (§8.10) |
| A handful of `Unproven $equiv … \_bb_N__gate 1'x` + *"Circuit inherently diverges!"* (most cells prove) | An inserted buffer landed on a **bus-bit** net and was mis-referenced as `_bb_N_[i]` — an out-of-range select on the 1-bit buffer wire, read as `x` | Fixed in `inserter.py`: the redirect references the scalar buffer wire by its own name. See §8.12 |

Rule of thumb: **every combinational cell needs a `function`; every sequential
cell needs `functions` + `clock`; physical cells only need to *exist* as
blackboxes.** Transmission gates are not Boolean functions (bidirectional +
high-Z): model an always-on pass gate as a buffer, otherwise treat it as part
of the mux/latch it builds and leave the bare cell out of `functions`.

For the differential `functions` convention itself (true output as a positive
`& | ^` formula; complement output as `!(…)` over the complement input rails;
inverting cells = the non-inverting twin with the two outputs swapped) see §8.9.

### 8.12 Bus-bit nets — buffer-wire identity

The insertion pipeline rests on one assumption the gate level always satisfies:
**every cell pin is a single bit**, so each net a buffer touches is one bit and
the buffer wire is a fresh *scalar*. Connectivity is reasoned about per bit:
`graph_builder._resolve_net` canonicalises every connection to one of three
spellings —

| Resolved key   | Meaning            | Bits |
|----------------|--------------------|------|
| `name`         | scalar / whole net | 1    |
| `name[i]`      | single-bit select  | 1    |
| `name[hi:lo]`  | range select       | many |

This mirrors how the real tools model nets — connectivity is bit-level, and a
bus is only a *declaration* over those bits. In Yosys/RTLIL the atomic object is
a `SigBit` = `(Wire, offset)`; cell ports connect to a `SigSpec` (a bit vector);
inserting a buffer means replacing the bit in each consumer's `SigSpec`
(`SigSpec::replace`). Commercial DBs (OpenAccess-style, used by Genus/Innovus)
make each bit a first-class **scalar net** named `w[2]`, grouped under a bus
`w`. In both, the writer asks each net for *its own name* — a scalar's name is
`_bb_0_`, a bus bit's name is `w[2]` — so neither can ever emit `_bb_0_[2]`,
because no object is "bit 2 of the scalar `_bb_0_`".

**The bug this section fixed.** When a buffered net was a bus bit (e.g. `w[2]`),
the consumer-redirect built the replacement reference by name-swapping the new
wire onto the consumer's *old* `NetRef` while **keeping its bit-select**:

```python
NetRef(new_wire, ref.msb, ref.lsb)   # -> _bb_0_[2]  (msb/lsb belonged to bus w)
```

`_bb_0_` is a 1-bit wire, so `_bb_0_[2]` is an out-of-range select that Yosys
reads as `x`. The base case of `equiv_induct` then can't prove the buffered net
equals its source, and the run aborts with *"Circuit inherently diverges!"* and
an unproven `\_bb_N__gate 1'x` per affected wire. Two traps in reading that
output:

- **It looks global but is local.** Only buffers that land on bus bits break;
  buffers on scalar nets had `ref.msb is None`, so they copied `None,None` and
  serialised correctly. A design with mostly scalar intermediate nets shows just
  a few unproven cells (a differential pair fails as two consecutive
  `_bb_N_/_bb_N+1_`), while thousands prove.
- **It is not a name collision.** `equiv_make` names the checkpoint after the
  *gate-side* net at a matched cell's input (`\_bb_455__gate`); it does **not**
  require `_bb_455_` to exist in the gold netlist. Grepping the original netlist
  for the reported name finds nothing — that is expected, not a second bug.

**The fix** is the bit-replacement the real tools do: reference the scalar
buffer wire by its own name, dropping the source bus's select. The mapping
`net_remap` already holds the right identity (`"w[2]" → "_bb_0_"`):

```python
NetRef(new_wire)   # connect the pin to the scalar net _bb_0_, by its own name
```

**The guard** makes the single-bit assumption a checked invariant rather than an
implicit one. `inserter._is_multibit_net` rejects a `name[hi:lo]` output net
before any wiring happens:

```
ValueError: cannot buffer 'g2' (AND_TEST): its output net 'y[1:0]' selects
multiple bits, but a buffer lane drives a single-bit wire. …
```

Multi-bit cell output pins don't occur in a gate-level netlist, so this never
fires in practice — but if a design ever carries one, the tool stops loudly at
the source instead of emitting a netlist that diverges thousands of cells later.

> **Regression coverage.** The pre-existing fixtures (`diff_*`, `fsm`,
> `TEST_CELLS`) are all scalar at the buffered net, which is why this shipped. A
> minimal bus-bit netlist (a chain driving `w[0..2]` with `N=2`, so the third
> gate's bus-bit output is buffered) reproduces the divergence before the fix
> and proves after it; keep such a fixture in the verify loop so the class can't
> regress.

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
