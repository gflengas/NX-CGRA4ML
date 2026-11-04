# QONNX Import for NG-ULTRA

The retained path is a shape-normalized QONNX graph -> graph-derived bundles
(`import_brevitas_qonnx.py`) -> canonical XLayers/DDR export
(`export_qonnx_to_cgra4ml.py`). It supports the tested Brevitas ResNet patterns,
not arbitrary QONNX graphs. U-Nets with grouped convolutions, nearest
upsampling and concat skips import as an explicit fixed-point graph
(`fixedpoint_graph.py`), checked node by node against QONNX's own execution
(`check_fixedpoint_model.py`, below). `plan_fixedpoint_graph.py` maps that
graph into ordered convolution stages, one accelerator bundle each, while
retaining quantization and prepool skip boundaries.
`export_fixedpoint_graph.py`, dispatched by the same export entry point, emits
DDR payloads using canonical CGRA4ML packers. Computed integer reference
vectors carry an explicit provenance tag; canonical C/RTL checks establish
execution correctness separately. Board results are in the root README.md. The
graph exporter takes float32 NHWC input, N images for a batch-N export
(at most the hardware's `max_batch_size`), and rejects legacy `--verify`; use
`python3 -m cgtools sim` for canonical execution.

## Preserved ResNet

`resnet_fixture/` is a frozen copy of accepted artifacts, not generated build
output.

- `model.onnx`: original Brevitas graph retained for provenance; not the import input.
- `shape-normalized.onnx`: accepted import input, with Quant nodes unchanged.
- `hardware.json`: the fixture's hardware configuration.
- `inputs/mnist-0.npy` and `inputs/mnist-1.npy`: preprocessed NHWC example inputs.
- `references/mnist-0-qonnx.npz`: independent intermediate tensors for checking
  the imported model's bundle boundaries during export.
- `references/C-logits.txt` and `D-logits.txt`: exact expected final integers.
- `references/C-softmax.txt` and `D-softmax.txt`: expected final probabilities.

C is MNIST 0; D is MNIST 1. This minimal example keeps one intermediate reference
case and two sets of final results, not the full historical regression suite.
Training checkpoints, additional NPZ cases and historical reports are not needed
to run it. The NPZ/TXT references are validation data, never runtime inputs;
they, not generated candidate outputs, are the comparison oracle.

## Rebuild and Compare

Run from the repository root with a fresh output directory, in the
`nxcgra4ml` environment (`environment.yml`: QONNX 0.4.0, ONNX 1.17.0,
TensorFlow/QKeras, Verilator 5.022). No training is needed. Simulation builds the repository's RTL (`cgtools.sim.RTL_SOURCES`).
The hardware JSON's frequency is exporter metadata, not a measured board clock.
For a debug simulation that validates every layer (`--mode debug`), add
`--verify` to the export command first: that generates the frontend raw
simulation references required by the checked fixture. The example below checks bundle boundaries during export
and compares C/D/C release softmax results on the host. The logits TXT files are
retained for exact comparison with the runtime's printed `result: logits`.

```sh
FIXTURE=frontend/qonnx/resnet_fixture
RUN=build/qonnx/resnet-smoke
python3 frontend/qonnx/import_brevitas_qonnx.py \
  "$FIXTURE/shape-normalized.onnx" "$RUN/imported"
CUDA_VISIBLE_DEVICES=-1 TF_NUM_INTEROP_THREADS=2 TF_NUM_INTRAOP_THREADS=4 \
  python3 frontend/qonnx/export_qonnx_to_cgra4ml.py \
  "$RUN/imported" "$FIXTURE/inputs/mnist-0.npy" "$RUN/export" \
  --hardware "$FIXTURE/hardware.json" \
  --reference-context "$FIXTURE/references/mnist-0-qonnx.npz"
python3 -m cgtools input "$FIXTURE/inputs/mnist-0.npy" "$RUN/C.bin" \
  --config "$RUN/export/vectors/config.json" --hardware "$RUN/export/hardware.json" \
  --mode float --frac 3 --preprocessing identity
python3 -m cgtools input "$FIXTURE/inputs/mnist-1.npy" "$RUN/D.bin" \
  --config "$RUN/export/vectors/config.json" --hardware "$RUN/export/hardware.json" \
  --mode float --frac 3 --preprocessing identity
python3 -m cgtools sim "$RUN/export" \
  --build-dir "$RUN/sim" --iterations 3 \
  --first-input "$RUN/C.bin" --input "$RUN/D.bin" --input "$RUN/C.bin" \
  --golden "$FIXTURE/references/C-softmax.txt" \
  --golden "$FIXTURE/references/D-softmax.txt" \
  --golden "$FIXTURE/references/C-softmax.txt"
```

Use `python3 -m cgtools run board` (a run config, see `ngultra/README.md`, *The run config*) with the
new export, explicit inputs and a compatible reviewed bitstream for board
deployment; `cgtools board` and `cgtools load` are its single steps;
the simulation above is not new board acceptance.
New raw exporter graphs still need explicit normalization and equivalence checking;
the retained importer does not silently rewrite unsupported graphs.


## Runtime regression checks

For C runtime or fixture-metadata changes, run the focused host checks from the
repository root (Python with NumPy and GCC required). The tests live in the
`test/` folder:

```sh
python3 -m unittest discover -s test -p 'test_fixedpoint_c.py'
python3 -m unittest discover -s test -p 'test_ngultra_*.py'
```

The tests cover C arithmetic, the host link, bounded waits and result
decoding. They are not deployment dependencies.
After runtime changes, also use `python3 -m cgtools sim` with the
affected export and independent goldens; use repeated C/D/C inputs for ownership
or reset changes. Include the retained ResNet and U-Net when shared code changes.

To run only the project-generator/timing-guard checks:

```sh
python3 -m unittest discover -s test -p 'test_ngultra_bitstream_guard.py'
```

This uses synthetic vendor reports, not an Impulse build or board access.

## Preparing a graph

`prepare_graph.py` turns an exported QONNX graph into the importer's input:
one graph input (Brevitas exports biases as graph inputs; they are bound to
their initializers), constant subgraphs folded (the shape arithmetic around
`Reshape` and `Pad`; `Quant` nodes stay) and a static shape for every tensor.
From the ResNet's `model.onnx` it gives a graph that imports identically to
`shape-normalized.onnx`.

```sh
python3 frontend/qonnx/prepare_graph.py model.onnx build/mymodel/prepared.onnx
```

## Checking a fixed-point graph

For a U-Net that imports as a fixed-point graph, check the integer graph
against QONNX before exporting. `make_references.py` runs the prepared model
with QONNX's own executor on two or more of your images (float32 NHWC,
preprocessed) and writes the references the checker needs; `--output-frac` is
the fractional bits of the graph's final quantizer. The checker then runs the
integer graph on the same inputs and compares every node:

```sh
python3 frontend/qonnx/make_references.py prepared.onnx images.npy build/refs \
  --images 0 1 --output-frac 11
python3 -m frontend.qonnx.check_fixedpoint_model prepared.onnx build/check \
  --input build/refs/0-nchw.npy --reference build/refs/0-context.npz \
  --expected-output build/refs/0-output-int32-nhwc.npy \
  --input build/refs/1-nchw.npy --reference build/refs/1-context.npz \
  --expected-output build/refs/1-output-int32-nhwc.npy \
  --expected-nodes <graph nodes> --expected-output-words <H*W*C> --expected-output-frac 11
```


The checker requires a fresh output directory and fails on missing artifacts.
It writes imported graph/parameters, a convolution plan, hashes and exact
per-node comparisons. It does not establish ARM memory fit or hardware acceptance.
