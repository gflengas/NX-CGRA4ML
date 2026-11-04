# Hardware presets

One directory per qualified bitstream, holding the configuration it was built
from: `hardware.json` (what a model export must match) and the RTL headers
`config_hw.svh` and `config_hw.tcl` generated from it. The bitstreams are not
in git; build the one you need from its directory (below). The source of
truth for each `hardware.json` is `HARDWARE_PRESETS` in
`create_nx_cgra4ml_project.py` (with the clock and seed that closed timing);
`test/test_presets.py` checks the files still match it.

| Preset | Models | Data | Edge RAM | Clock | Seed | Worstcase setup / hold | Bitstream SHA-256 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `regression4/` | ResNet, U-Net, the example | 4-bit | 3,584 | 40 MHz | tool default | +0.924 / +0.031 ns | `94d84780942e086517bc14c64d7da7de680cac8ba0a1e1482fcc6b8de8fcbf54` |

## Building a preset's bitstream

Needs NanoXplore Impulse (design suite 24.3, `nxpython`) and its licence;
a few hours per image.

```sh
python3 create_nx_cgra4ml_project.py --hardware-preset regression4 \
  --config-dir presets/regression4 \
  --project-dir "$PWD/build/bitstreams/regression4"
```

The result is `build/bitstreams/regression4/qualified-bitstream/bitstream.nxb`,
written only after both routed timing corners pass. With Impulse 24.3 the
build is deterministic: its SHA-256 should equal the table's
(compare with `sha256sum`). A different hash (another tool version) is not
an error, but load-test the image before relying on it, and use its own hash
wherever a run config pins the bitstream. `--seed` changes the placement if
timing fails.

## Using a preset for your model

Export the model with the preset's `hardware.json` (the frontend's
`--hardware presets/<name>/hardware.json`) and run it on that bitstream. If
no preset fits your model, add one to `HARDWARE_PRESETS` and write its
directory: `python3 create_nx_cgra4ml_project.py --hardware-preset <name>
--export-hardware presets/<name>/hardware.json`, then copy `config_hw.svh`
and `config_hw.tcl` from your model's export into it.

`frequency_mhz` is the nominal clock the bitstream is built for; the runtime
and the host tools do not use it.
