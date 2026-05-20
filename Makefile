.PHONY: help sim surfer synth net editing visualize verify editing-cdl verify-cdl clean all

# ============================================================================
# Variables  (override on the command line, e.g. `make editing N_BUFF=3`)
# ============================================================================
#
# Design entry
#   VHDL       VHDL source file driving the whole flow (sim, synth, net,
#              editing, verify).
#   TOP        Top entity name inside $(VHDL).
#   TB         Testbench entity name (file is $(TB).vhdl). Defaults to
#              tb_$(TOP).
#   NETLIST    Verilog netlist: output of `net`/`synth`, input to
#              `editing`/`verify`. Defaults to <vhdl-stem>_netlist.v, so a
#              single VHDL=... override drives the whole flow. Override
#              NETLIST directly to enter at the editing stage with your own
#              Verilog netlist (no VHDL needed).
#   MODIFIED   Edited netlist produced by `editing`. Defaults to
#              <netlist-stem>_modified.v (a `_netlist` suffix on the netlist
#              filename is stripped first).
#
# Shared
#   N_BUFF     Max consecutive logic gates between restoration points
#              (flip-flops / buffers). Drives buffer insertion in `editing`
#              and `editing-cdl`.
#
# sky130 / Liberty flow
#   LIB        Liberty file used by `net`, `editing`, and `verify`.
#
# Closed-source / CDL flow
#   CDL        CDL input(s): a single file, a quoted list, or a directory.
#                CDL=foo.cdl   CDL="a.cdl b.cdl"   CDL=pdk_cdls/
#              Duplicate cell names across files are a hard error.
#   CELL_META  Sidecar JSON(s) flagging buffer / sequential cells.
#              Omit to auto-discover <stem>.cells.json next to each CDL.
#   STUB_LIB   Auto-generated Liberty stub consumed by `verify-cdl`.

VHDL       ?= fsm.vhdl
TOP        ?= flip_flop_adder
TB         ?= tb_$(TOP)

_VHDL_STEM := $(basename $(notdir $(VHDL)))
NETLIST    ?= $(_VHDL_STEM)_netlist.v
_NET_STEM  := $(patsubst %_netlist,%,$(basename $(notdir $(NETLIST))))
MODIFIED   ?= $(_NET_STEM)_modified.v

LIB       = skywater-pdk-libs-sky130_fd_sc_hd/timing/sky130_fd_sc_hd__tt_025C_1v80.lib
N_BUFF    = 5

CDL       ?= TEST_CELLS.cdl
CELL_META ?= TEST_CELLS.cells.json
STUB_LIB  ?= cdl_stub.lib

# ============================================================================
# Help
# ============================================================================

help:
	@echo "=== $(TOP) Project ==="
	@echo ""
	@echo "Available targets:"
	@echo ""
	@echo "  make sim       - Compile and simulate the design"
	@echo "  make surfer    - Open waveform viewer (requires tb.vcd)"
	@echo "  make synth     - Synthesize design to Verilog netlist (generic cells)"
	@echo "  make net       - Synthesize design to Verilog netlist (SkyWater130nm)"
	@echo "  make editing   - Edit the netlist with the netlist_tool"
	@echo "  make visualize - Visualize the netlist with the netlist_tool"
	@echo "  make verify    - Prove $(MODIFIED) is logically equivalent to $(NETLIST)"
	@echo "  make editing-cdl - Edit the netlist using a CDL backend (closed-source flow)"
	@echo "  make verify-cdl  - Structural-equivalence check via auto-generated stub .lib"
	@echo "  make all       - Clean, synthesize, and verify the design"
	@echo "  make clean     - Remove generated files and artifacts"
	@echo "  make help      - Show this help message"
	@echo ""
	@echo "Variables (current values):"
	@echo "  VHDL=$(VHDL)  TOP=$(TOP)  TB=$(TB)"
	@echo "  NETLIST=$(NETLIST)  MODIFIED=$(MODIFIED)"
	@echo "  N_BUFF=$(N_BUFF)  LIB=$(LIB)"
	@echo "  CDL=$(CDL)  CELL_META=$(CELL_META)  STUB_LIB=$(STUB_LIB)"
	@echo ""
	@echo "Examples:"
	@echo "  make all VHDL=adder.vhdl TOP=adder_top   # rebrand the whole flow"
	@echo "  make editing NETLIST=my.v                # editing-only from a Verilog netlist"
	@echo ""

# ============================================================================
# Simulation (GHDL) — analyse, elaborate, run, view waveforms
# ============================================================================

sim:
	ghdl -a --std=08 $(VHDL)
	ghdl -a --std=08 $(TB).vhdl
	ghdl -e --std=08 $(TB)
	ghdl -r --std=08 $(TB) --vcd=tb.vcd

surfer:
	surfer tb.vcd

# ============================================================================
# Synthesis (Yosys + GHDL plugin) — VHDL → Verilog netlist
# ============================================================================

synth: # generic library synthesis
	GHDL_PREFIX=/opt/homebrew/lib/ghdl yosys -m ghdl -p "\
    	ghdl --std=08 $(VHDL) -e $(TOP); \
        synth -top $(TOP); \
     	write_verilog $(NETLIST)"

net: # synthesis with SkyWater130nm mapping
	GHDL_PREFIX=/opt/homebrew/lib/ghdl yosys -m ghdl -p "\
		ghdl --std=08 $(VHDL) -e $(TOP); \
		synth -top $(TOP); \
		dfflibmap -liberty $(LIB); \
		abc -liberty $(LIB); \
		write_verilog $(NETLIST)"

# ============================================================================
# Editing & verify — sky130 / Liberty flow
# ============================================================================

editing:
	uv run python -m netlist_tool $(NETLIST) $(MODIFIED) --N $(N_BUFF) --lib $(LIB)

visualize:
	uv run python -m netlist_tool $(NETLIST) $(MODIFIED) --N $(N_BUFF) --visualize

# Formal equivalence check: prove $(MODIFIED) == $(NETLIST).
# Buffer insertion preserves logic, so equiv_induct should converge instantly.
verify: $(NETLIST) $(MODIFIED)
	yosys -p "\
		read_liberty -ignore_miss_func $(LIB); \
		read_verilog $(NETLIST); \
		rename $(TOP) gold; \
		read_verilog $(MODIFIED); \
		rename $(TOP) gate; \
		equiv_make gold gate equiv; \
		hierarchy -top equiv; \
		clk2fflogic; \
		async2sync; \
		prep -flatten; \
		equiv_induct -seq 10; \
		equiv_status -assert"

all : clean net editing verify

# ============================================================================
# Editing & verify — closed-source / CDL flow
# ============================================================================
# Same pipeline as the sky130 flow, but uses CDL + sidecar JSON instead of
# Liberty. `verify-cdl` proves structural equivalence via an auto-generated
# stub .lib; sequential semantics are deferred to vendor LEC.
# See docs/cdl_backend.md and docs/netlist_editing_workflow.md §5.5.

editing-cdl:
	uv run python -m netlist_tool $(NETLIST) $(MODIFIED) \
	    --N $(N_BUFF) --cdl $(CDL) --cell-meta $(CELL_META)

$(STUB_LIB): $(CDL) $(CELL_META)
	uv run python -m netlist_tool.cdl_parser --emit-stub-lib $(CDL) \
	    --cell-meta $(CELL_META) -o $@

verify-cdl: $(NETLIST) $(MODIFIED) $(STUB_LIB)
	yosys -p "\
		read_liberty -lib $(STUB_LIB); \
		read_liberty -ignore_miss_func -overwrite $(STUB_LIB); \
		read_verilog $(NETLIST); \
		rename $(TOP) gold; \
		read_verilog $(MODIFIED); \
		rename $(TOP) gate; \
		equiv_make gold gate equiv; \
		hierarchy -top equiv; \
		clk2fflogic; \
		async2sync; \
		prep -flatten; \
		equiv_induct -seq 10; \
		equiv_status -assert"
		rm -f $(STUB_LIB)
		rm -rf .cdlcache
# ============================================================================
# Clean
# ============================================================================

clean:
	rm -f *.o *.cf *.vcd
	rm -f $(TB)
	rm -f $(NETLIST) $(MODIFIED)
	rm -f $(STUB_LIB)
	rm -rf .cdlcache
	@echo "Cleaned: object files, config files, waveforms, testbench, and synthesis results"
