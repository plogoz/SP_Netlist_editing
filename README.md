# Netlist Editing Tools:
These tools provide a set of scripts for editing netlists, and have been tailored to insert buffers into netlists at specified depths. It can be easily extended to other non-logic netlist editing tasks.

The tools also provide a script to check logical equivalence (using Yosys) to catch any editing mistakes.

## Prerequisites :

### Dependencies :

Before being able to use these tools, you need to have the following prerequisites installed (both .lib and .cdl flows):
- UV : https://github.com/astral-sh/uv.git
- Yosys : https://github.com/YosysHQ/yosys.git

In case uv won't install matplotlib, you can do without it. The visualization tools will not work, but the netlist editing tools will function.

To enter from a VHDL file, you need :
- GHDL : https://github.com/ghdl/ghdl.git
- GHDL yosys plug-in : https://github.com/ghdl/ghdl-yosys-plugin.git

### Sidecar files (.cdl flow only) :
- a JSON dependency file is needed to provide additional informations about the cells themselves (logical function, pin information, …)
- a JSON dependency file for the power supply rails structured as follows :
```json
{
    "rails" : {
        "VDD" : "VDD",
        "VSS" : "VSS",
        "BIAS_N" : "BIAS_N",
        "BIAS_P" : "BIAS_P"
    }
}
```
where the left side are cell pin names, and the right side is the netlist power rail names. If this file is not provided, the tool won't issue any warnings, and the SPICE simulation will report floating cells. The logical equivalence will not catch this error, as it's a hardware only issue. If the file is incomplete / incorrect, the tool will raise an error. 

## Arguments :

### .lib flow :
- NETLIST : the netlist file to edit
- MODIFIED : the output netlist file after editing
- LIB : .lib file
- N_BUFF : buffer insertion depth

### .cdl flow :
- NETLIST : the netlist file to edit
- MODIFIED : the output netlist file after editing
- CDL : .cdl file(s) or folder
- CELL_META : JSON cell metadata file
- SUPPLIES : JSON file with power supply net labels
- N_BUFF : buffer insertion depth

## Makefile :
General commands :
- make help : displays a help message
- make clean : cleans *ALL* verilog netlists and all generated files

For .lib flow :
- make editing : edits the netlist
- make verify : checks logical equivalence of the netlist

For .cdl flow: 
- make editing-cdl : edits the netlist
- make verify-cdl : checks logical equivalence of the netlist using the .cdl flow

For tool exploration / testing:
- make visualize : visualizes the netlist as a graph. Don't do this on netlists larger than a few cells, as it may be slow and may crash.
- make sim : compiles and simulate a netlist form a VHDL file and testbench
- make surfer : opens surfer with the waveforms generated at simulation time
- make synth : synthesizes a technology agnostic netlist using yosys
- make net : synthesizes a PDK aware netlist using a .lib file
- make all : runs clean, net, editing and verify in one run.

## Usage :
To edit a netlist, you'll need :
1. The netlist file (in Verilog) depending on the technology. Technology agnostic files may not work well.
2. The corresponding technology file (.cdl or .lib) for the library you're using.
3. For the .cdl flow only, a small JSON sidecar file (similar to TEST_CELLS.cells.json), such that the tool can know what cells are clocks, buffers, sequential, and their logical function.
4. Run the tool using the Makefile.
