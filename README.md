# NX-CGRA4ML: CGRA4ML on NanoXplore NG-ULTRA

Built on an older fork of [CGRA4ML](https://github.com/KastnerRG/cgra4ml),
based on its commit
[`4fcb373`](https://github.com/KastnerRG/cgra4ml/commit/4fcb3731b291e0bc2a7b91ff409d8501839fa656)
(2025-09-25), and released under the same license as upstream (Apache 2.0,
`CGRA4ML/LICENSE`). The fork is in [`CGRA4ML/`](CGRA4ML/), with upstream's
own README.

NX-CGRA4ML runs quantized neural networks on the NanoXplore NG-ULTRA SoC
FPGA end to end: a QONNX (or CGRA4ML Keras) model is exported to fixed-point
tables, the accelerator runs every convolution in the FPGA fabric, and a
bare-metal runtime on the SoC's Arm Cortex-R52 does the rest (bias,
activation, quantization, pooling, upsampling, skips, packing) and serves
inputs and results to a host over DDR. Classifiers (ResNet) and segmentation
U-Nets (grouped convolutions, nearest upsampling, concat skips) are
supported.

Everything here runs exactly (integer outputs equal the reference) on the
NG-ULTRA DevKit, in simulation and on the board.

## Results

Release builds on the board, one image per inference; batch 8 per image in
brackets. Times are accepted input to computed output, ARM Cortex-R52 at
600 MHz.

| Model | Preset | Accelerator | Time (batch 8, per image) |
| --- | --- | --- | --- |
| ResNet 13k (MNIST, 4-bit; `ngultra/example/resnet/`) | `regression4` | 40 MHz | **2.347 ms** (2.211 ms) |
| U-Net (small, 4-bit; `CGRA4ML/run/example_unet_small.py`) | `regression4` | 40 MHz | **0.965 ms** (0.842 ms) |

## How a model gets to the board

1. **Export** (`frontend/`, `CGRA4ML/run/`): the QONNX or Keras model becomes
   a CGRA4ML export: `vectors/config.json` (one *bundle* per layer group:
   convolution plus its bias, activation, pool, residual, upsample, concat),
   `vectors/wb.bin` (weights and biases), `hardware.json`, `config_hw.svh`.
2. **Bitstream** (`create_nx_cgra4ml_project.py`): the RTL with the export's
   hardware constants, synthesized, placed and routed in NanoXplore Impulse,
   accepted only when both timing corners pass.
3. **Model tables** (`cgtools fixture`): `model.h`/`model.c` (per-bundle
   descriptors and postprocess plans) and the scheduled weight payload; the
   export is checked against the hardware here, so the runtime trusts it.
4. **Firmware** (`cgtools board build-app`): `runtime/` plus the tables built
   into one bare-metal app in the NanoXplore BSP. It holds no weights, inputs
   or goldens.
5. **Session** (`cgtools load` / `cgtools run board`): flash bitstream and
   app, boot; the host writes weights and input into DDR over JTAG (DAP-AXI),
   the board runs the model and publishes the result, the host reads it back
   and compares it with a golden. Later requests replace only the input.

Inside one inference: one launch holds every bundle; the accelerator streams
output packets into two ping-pong banks in DDR, and for each packet the ARM
postprocesses it straight into the next bundle's packed input (the *fused
pass*, NEON four lanes at a time). Every bundle runs in a single pass over all
its input channels. Batches stream every image through each layer with the
same weights.

## Repository

| Path | What |
| --- | --- |
| `runtime/` | The inference runtime (C, one translation unit): `main.c` (host mailbox, launch, packet loop, session) includes `cg_config.h` (build modes, log, profile), `cg_device.h` (accelerator registers), `cg_workspace.h` (all buffers in one DDR struct, golden checks), `cg_fixedpoint.h` (QONNX fixed-point arithmetic, scalar and NEON), `cg_pack.h` (packing into the accelerator's tiled input), `cg_postprocess.h` (bundle postprocess and the fused pass); `cg_host_link.h` is the mailbox format shared with the simulator. `Makefile.bsp` builds it in the BSP. |
| `ngultra/c/` | NG-ULTRA platform layer: `nx_platform.h` (MMIO, barriers, caches, counter, UART; a simulation variant under `NX_SIM`), `nx_ddr_layout.h` (DDR reservations), `nx_log.h` (UART printing). |
| `ngultra/py/ngultra/` | Host-side board library: BSP staging and builds, flashing, OpenOCD, UART, DAP-AXI memory access, power sampling, site settings. |
| `ngultra/README.md` | How to run on the board: the session, every tool step by step, build modes and debugging, power measurement, troubleshooting. |
| `ngultra/example/resnet/` | A complete example session (export, inputs, expected results, `run.json`) to run in simulation and on the board. |
| `cgtools/` | Host tools, `python3 -m cgtools fixture\|input\|sim\|load\|board\|run`: model tables and payload, input packing, simulation, the host side of a board session, whole runs from a run config. |
| `sim/sim.cpp` | Verilator harness: runs the real runtime against the RTL with a DDR model and plays the host. Models RTL cycles, not ARM time or caches. |
| `frontend/` | QONNX frontend (`frontend/qonnx/`, see its README) and the converter for upstream-Keras reference vectors. |
| `CGRA4ML/` | Upstream CGRA4ML (RTL, testbench, Keras/QKeras frontend; its README) with our RTL changes for NG-ULTRA (`deepsocflow/rtl/`: `ngultra_*`, `nx_rams_sp_wf.sv`, `axi_cgra4ml_2m.v`); `CGRA4ML/run/example_unet_small.py` exports the small U-Net. |
| `create_nx_cgra4ml_project.py` | Bitstream generation: hardware presets, PLL, Impulse project, two-corner timing acceptance, bitstream. |
| `presets/` | One directory per qualified bitstream: its build configuration (`hardware.json`, `config_hw.*`), to export models for it and to build it; `presets/README.md` has each one's clock, seed, timing and hash. |
| `test/` | Unit tests (`pytest test/`). |
| `build/` | Git-ignored: everything you build and run (bitstreams, apps' outputs, simulation and board results). |
| `site.example.json` | Template for `site.json`, this machine's tool locations. |

## Getting started

### What you need

| What | Where from | For |
| --- | --- | --- |
| NG-ULTRA DevKit, with its ANGIE JTAG probe and USB UART | NanoXplore | the board |
| NG-ULTRA BSP | NanoXplore (the BSP ships `download_ngultra_bsp.sh`) | building the firmware |
| nx_embedded_tools | NanoXplore | flashing, OpenOCD, DDR loading, rail power |
| ARM GCC (`arm-none-eabi`) | tested: the one in Xilinx Vitis 2024.1 | building the firmware |
| Verilator 5.022 | installed by `environment.yml` | simulation |
| NanoXplore Impulse 24.3 and its licence | NanoXplore | building bitstreams |
| Linux groups `plugdev` (USB) and `dialout` (UART) | your system | the board; log in again after adding them |

### Environments

Two:

- **`nxcgra4ml`** (conda, from `environment.yml`): everything in this
  repository: the exporters (TensorFlow/QKeras), the QONNX import, `cgtools`,
  the unit tests, and Verilator for simulation.

  ```sh
  conda env create -f environment.yml
  conda activate nxcgra4ml
  ```

  The commands below assume it is active (`python3` is its python).
- **nx_embedded_tools' own virtual environment** (`$NXTOOLS/venv/bin/python`,
  with pyserial and NanoXplore's libraries): every command that talks to the
  board.

### Configure

Copy `site.example.json` to `site.json` and fill in the tool locations (each
can be overridden by its environment variable):

| Setting | Variable | What |
| --- | --- | --- |
| `nxtools` | `NXTOOLS` | the nx_embedded_tools checkout |
| `bsp_root` | `BSP_ROOT` | the NG-ULTRA BSP; apps build under `apps/` |
| `gcc_bin` | `GCC_BIN` | the ARM GCC `bin/` directory |
| `uart_port` | `UART_PORT` | the board UART: the CP2105 port, not the CP2108 ZynqMP console |
| `verilator` | `VERILATOR` | the Verilator binary (the environment's: `$CONDA_PREFIX/bin/verilator`) |

Build the boot loader once: flashing writes the BSP's demo app as BL1
(`<bsp>/apps/bsp_demo/out/bsp_demo.bin`), so build it with
`make -C <bsp>/apps/bsp_demo` (ARM GCC on `PATH`).

### First run

All from the repository root.

1. **Unit tests** (no board, no FPGA tools):
   `python3 -m pytest test/`.
2. **Simulate the example** (Verilator; about a minute): the small ResNet in
   `ngultra/example/resnet/`, three requests, results compared.

   ```sh
   python3 -m cgtools run sim --config ngultra/example/resnet/run.json --out build/example-sim
   ```
3. **Build the example's bitstream** (Impulse; hours, once):

   ```sh
   python3 create_nx_cgra4ml_project.py --hardware-preset regression4 \
     --config-dir presets/regression4 --project-dir "$PWD/build/bitstreams/regression4"
   ```

   See `presets/README.md` for the expected hash and the other presets.
4. **Run the example on the board** (about five minutes): builds the
   firmware, flashes bitstream and app, serves the three requests over JTAG
   and compares the results.

   ```sh
   $NXTOOLS/venv/bin/python -m cgtools run board \
     --config ngultra/example/resnet/run.json --out build/example-board
   ```

   Expected: three PASS lines, about 2.35 ms per inference.

`ngultra/README.md` explains the board tools in detail: every step on its
own, debug modes, power measurement and troubleshooting.

## Usage

**Simulate a model:**

```sh
python3 -m cgtools sim <export> --build-dir build/run/sim \
  --first-input <export>/vectors/x.bin --golden <export>/vectors/y_exp.txt [--mode debug]
```

**Run a model on the board:** copy `ngultra/example/resnet/`, put your
export, inputs and references in it, adjust `run.json` and use
`cgtools run board` as above. It snapshots the sources, builds the app,
checks the image, flashes and boots, serves every request and compares every
result; any mismatch, trap or timeout stops it.

**Build a bitstream:** `create_nx_cgra4ml_project.py --hardware-preset <name>
--config-dir presets/<name> --project-dir <new directory>`. The flow writes
the bitstream only after both routed timing corners pass; `--no-run` stages
the project only. Load-test every new image on the board: some placements
reset the SoC while the bitstream loads.

**Build modes** (`--mode`): `release` (results and total time; use it for
performance), `profile` (per-stage times), `debug` (log, golden validation).

**After changing the runtime, tools or RTL:** run the unit tests, simulate
the example and your models, and repeat a board run on two boots.

## Using your own model

### Two frontends

| | Upstream CGRA4ML (Keras/QKeras) | This repository (QONNX) |
| --- | --- | --- |
| Model source | written in Python with CGRA4ML's `XModel`/`XBundle` layers (`CGRA4ML/run/` has examples) | a QONNX graph (e.g. exported from Brevitas) built from the supported patterns (*What fits*) |
| Goldens | the Keras model's simulation, per layer | fixed-point path: an exact integer re-execution of the QONNX graph; bundle path: upstream's simulation, checked against QONNX reference tensors |
| Quantization | CGRA4ML's own activation (shift, leaky scale, clamp) | QONNX `Quant` semantics exactly: rounding, saturation, narrow range, every quantizer boundary kept |
| Layers | upstream's: convolution, dense, pool, residual add, flatten, softmax | the same, plus depthwise convolution (expanded to dense), nearest 2x upsampling and concat skips (U-Nets) |
| Export format | upstream's vectors (converted with one command, see below) | the `computed_integer_graph_v1` format the runtime reads (the bundle path converts automatically) |

The QONNX frontend lives in `frontend/qonnx/`; the upstream frontend is in
`CGRA4ML/deepsocflow/`. The runtime, the tools and the bitstream
are the same for both.

### QONNX: step by step

All with `nxcgra4ml` active (*Getting started*).

1. **Prepare the graph.** One graph input (parameter inputs such as Brevitas
   biases bound to their initializers), constant subgraphs folded, static
   shapes everywhere; no quantizer changes:

   ```sh
   python3 frontend/qonnx/prepare_graph.py model.onnx build/mymodel/prepared.onnx
   ```

   Use `prepared.onnx` as `model.onnx` in the steps below.
2. **Pick a hardware preset** (next section): `presets/<name>/hardware.json`
   (e.g. `presets/regression4/hardware.json`), used as is.
3. **Import.** The importer reads the graph's edges and picks the path
   itself: graphs with `Resize`, `Concat` or grouped convolutions become a
   fixed-point graph (the U-Net path, planned into one accelerator bundle per
   convolution); others become graph-derived bundles (the Brevitas ResNet
   path, exported through upstream's exporter).

   ```sh
   python3 frontend/qonnx/import_brevitas_qonnx.py model.onnx build/mymodel/imported
   ```
4. **Check it (fixed-point graphs, recommended).** Runs the integer graph on
   your inputs and compares every node with QONNX's own execution:
   `frontend/qonnx/make_references.py` makes the QONNX references, then
   `python3 -m frontend.qonnx.check_fixedpoint_model` compares (both in
   `frontend/qonnx/README.md`, *Checking a fixed-point graph*).
5. **Export.** The input is float32 NHWC; N images give a batch-N export (up
   to the preset's `max_batch_size`):

   ```sh
   python3 frontend/qonnx/export_qonnx_to_cgra4ml.py \
     build/mymodel/imported input.npy build/mymodel/export \
     --hardware presets/regression4/hardware.json
   ```

   On the bundle path, `--reference-context <npz>` (QONNX intermediate
   tensors for this input) checks every bundle boundary during export.
   The export holds `vectors/` (tables, weights, the packed input
   `vectors/x.bin`, the expected output `vectors/y_exp.txt`) and the
   `hardware.json`/`config_hw.*` the bitstream must match.
6. **More inputs** (optional): pack further requests in the model's layout
   with `python3 -m cgtools input in.npy in.bin --config
   build/mymodel/export/vectors/config.json --hardware
   build/mymodel/export/hardware.json --mode float --preprocessing identity`.
   `in.npy` is the input exactly as the model sees it (NHWC, already
   normalized: the tool does no preprocessing). `--mode float` quantizes
   float values with the model's input quantizer; `--mode quantized` takes
   integers already in the input's range. The input's fractional bits come
   from the export (both frontends record them); `--frac` is needed only for
   an export that does not.
7. **Simulate**, then **run on the board** with the preset's bitstream:

   ```sh
   python3 -m cgtools sim build/mymodel/export --build-dir build/mymodel/sim \
     --first-input build/mymodel/export/vectors/x.bin \
     --golden build/mymodel/export/vectors/y_exp.txt
   ```

   For the board, copy `ngultra/example/resnet/`, put your export, inputs
   and references in it, adjust `run.json` (every field is explained in
   `ngultra/README.md`, *The run config*) and use `cgtools run board`.

Worked example: `frontend/qonnx/README.md` takes the same ResNet from its
QONNX file to an export like the one in `ngultra/example/resnet/`.

### Upstream Keras models

Build the model with CGRA4ML's layers (see `CGRA4ML/run/example_unet_small.py`,
the small U-Net), export it with upstream's `export_inference`, then
convert its vectors to the runtime's format with
`python3 frontend/convert_legacy_vectors.py <export>/vectors`. From there the
steps are the same (5-7).

### What fits

`cgtools fixture` checks every export against its hardware and rejects what
cannot run, with the reason. The limits: bit widths equal to the preset's;
kernel, image size, input channels and batch within its maxima; at most 64
bundles; every layer's input channels x width within the edge RAM (each layer
runs in one pass); signed, zero-point-zero, power-of-two QONNX quantizers.
Unsupported QONNX patterns fail at import; the importer does not rewrite
graphs.

## Hardware presets

A preset (`HARDWARE_PRESETS` in `create_nx_cgra4ml_project.py`) is one
qualified bitstream: the `hardware.json` an export must match, plus the build
options that closed timing (clock, seed). Every preset uses the same RTL;
only these parameters change:

| Preset | Models | Data | Max kernel / image | Edge RAM | Max batch | Clock | Worstcase setup/hold |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `regression4` | ResNet, small U-Net | 4-bit in/weights, 20-bit sum, 16-bit bias | 9 / 512 | 3,584 | 64 | 40 MHz | +0.924 / +0.031 ns |

Where the values come from:

- **8x24 processing elements** (192 multipliers, on DSPs at 8 bits), 512
  input channels, 128-bit AXI and a 512-word weight RAM: fixed for all
  presets, sized for the NG-ULTRA fabric.
- **Bit widths**: the models' quantization (4-bit ResNet and small U-Net);
  the sum and bias widths follow from them. The RTL also builds at 8 bits
  (multipliers on DSPs) for 8-bit models, as a new preset.
- **Kernel, image, edge RAM**: the smallest values that fit the target
  models. The edge RAM holds one layer's input channels x width, so wide
  layers on large images need a preset with a larger edge RAM.
- **Batch**: the largest batch tested for those models.
- **Clock and seed**: what closed timing in both corners and loaded cleanly
  on the board (40 MHz for the 4-bit image; 8-bit builds with DSPs closed at
  30 MHz). Some placements reset the SoC while the bitstream loads; another
  `--seed` fixes that.

**Using a preset:** if your model fits one (above, *What fits*), export with
its `presets/<name>/hardware.json` and run on its bitstream, built once from
the same directory (`presets/README.md`).

**A new preset:** add an entry to `HARDWARE_PRESETS` (start from the closest
one and change only what your model needs), write its `hardware.json` with
`python3 create_nx_cgra4ml_project.py --hardware-preset <name>
--export-hardware presets/<name>/hardware.json`, export your model with it,
copy the export's `config_hw.svh` and `config_hw.tcl` into
`presets/<name>/`, then build the bitstream (*Usage*). The unit tests check
that every preset directory matches the script.
Larger RAMs, wider data or a faster clock may not close timing; the flow
then stops before writing a bitstream, and `--seed` tries another placement.
Load-test every new image on the board before relying on it.

NG-ULTRA-specific RTL shared by all presets: an LLPP register slave and
Verilog shell, pixel reads on their own SoC port, native write-first RAM, and
read-reservation FIFOs on both read ports (without them the SoC's shared
in-order read path deadlocks the weight and pixel DMAs).

## Things worth knowing

- **DDR layout** (`ngultra/c/nx_ddr_layout.h`): app below `0x81000000`,
  workspace `0x81000000`-`0x8F000000` (224 MiB), payload from `0x8F000000`,
  mailbox at `0x8FFFF000`.
- **Memory system**: the BSP maps DDR write-through and a store that misses
  L1 goes to the bus, so timing depends on where hot code and data land
  (builds of the same algorithm vary by a few percent). The fused pass keeps
  its per-packet scratch in a fixed, cache-aligned block for that reason.
- **Interrupts were rejected**: the CPU almost never waits for the
  accelerator (at most 1.3% of an inference on ResNet), and an interrupt round
  trip (0.67-0.98 us) costs more than the register poll it would replace
  (0.44 us). Idle power between requests could still be saved with WFI and a
  timer wake while waiting for the host.
- **Multicore was assessed, not built**: a second Cortex-R52 could
  nearly halve the CPU-bound U-Nets' time, but the two packet banks alternate, the 2x2 pool
  carries state between neighbouring packets, and the cores' caches are not
  coherent (neighbouring columns share cache lines in the packed input).
  Weeks of work; the BSP's `multicore_binary` app shows core bring-up.
- **The packet control logic** (handshake, barriers) once failed a large
  model with posted writes; test any change there on the largest model, on
  two boots.
- **Not validated**: dataset accuracy, shapes beyond the tested models. Simulation
  checks correctness and protocol, not ARM time.
