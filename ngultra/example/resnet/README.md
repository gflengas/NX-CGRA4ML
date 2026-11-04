# Example: ResNet on the board

A complete, small session to try the board tools on: a 4-bit ResNet (13k
parameters, MNIST) exported for the `regression4` preset, three requests
(digits 0, 1, 0), and the expected results.

| Path | What |
| --- | --- |
| `run.json` | the run config: export, requests, simulation bound, bitstream |
| `export/` | the CGRA4ML export (`vectors/`, `hardware.json`), as the frontend wrote it |
| `inputs/C.bin`, `D.bin` | packed inputs (MNIST 0 and 1) |
| `references/*-logits.txt` | expected printed integers, compared exactly |
| `references/*-softmax.txt` | expected probabilities, compared with the saved result before it is acknowledged |

The board needs the `regression4` bitstream, which is not in git: build it
once from `presets/regression4` (see `presets/README.md`). `run.json`
expects it at `build/bitstreams/regression4/qualified-bitstream/bitstream.nxb`
and pins its SHA-256; if your build's hash differs (another Impulse version),
load-test it and put its hash in `run.json`. Simulation needs no bitstream.

From the repository root:

```sh
# simulation (~1 min; RTL in Verilator, the real runtime, the host emulated)
python3 -m cgtools run sim --config ngultra/example/resnet/run.json \
  --out build/example-sim

# board: build, flash, boot, three requests, results compared (~5 min)
$NXTOOLS/venv/bin/python -m cgtools run board \
  --config ngultra/example/resnet/run.json --out build/example-board
```

Add `--mode profile` for per-stage times or `--mode debug` for the log;
`--boots 2` repeats the flash and boot. The board run writes the UART log,
each result and `boot1.accepted.json` (times, result hashes) to `--out`.

Expected: every request PASS, about 2.35 ms per inference on the board,
47,960 simulated cycles. To make your own, copy this folder and replace the
export, inputs and references with your model's (root README, *Using your
own model*).
