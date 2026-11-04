# ngultra: running on the NG-ULTRA DevKit

Everything that touches the board: the C platform layer the runtime is built
on (`c/`), the host-side Python library that builds, flashes, loads and
measures (`py/ngultra/`), and how the `cgtools` commands use them to run an
inference, debug it and measure its power. Setup (`site.json`, Python
environments, groups) is in the root README.

## How a board inference works

```
host (this machine)                             NG-ULTRA DevKit
------------------                              ---------------
cgtools board build-app  -> app (.bin, 128 KiB max, runs from DDR 0x80000000)
cgtools board flash-boot -> SPI flash 1: BL1 + bitstream (0x100000) + app (0x700000)
                            boot: BL1 loads the bitstream into the fabric,
                            the app into DDR, jumps to main
   UART <------------------ "external: ready ... address= bytes= sequence=1"
cgtools load --ready-line -> DAP-AXI (OpenOCD, JTAG): weights + input into DDR,
                            read back, CRC, mailbox COMPLETE
                            the runtime checks identity and CRC, runs the model
   UART <------------------ "result: ..."  "timing: inference_ms=..."
                            "external: result ... address= words="
cgtools load --result-line -> DAP-AXI read of the result, compare with the golden,
                            mailbox CONSUMED (only if the comparison passed)
   UART <------------------ next "external: ready" (new input only), or
                            "external: session complete requests=N"
```

The app holds the model tables only; weights, biases, inputs and results move
over JTAG through a DDR mailbox (`runtime/cg_host_link.h`, layout in
`c/nx_ddr_layout.h`). JTAG loading runs at about 32 KiB/s: a 240 x 240 input
takes seconds, a large batch result minutes. Every step is checked: model
identity, address ranges, CRC, exact readback, that the weights did not
change between requests, and the result against an independent golden.

## Running: one command

`cgtools run board` does the whole session from a run config.
[`example/resnet/`](example/resnet/) is a complete one to try first: a small
ResNet's export, three inputs, their expected results and `run.json` (format
below):

```sh
$NXTOOLS/venv/bin/python -m cgtools run board --config ngultra/example/resnet/run.json \
  --out build/example-board [--mode release|profile|debug] [--boots 2]
```

It snapshots `runtime/`, `ngultra/` and `cgtools/` into the output directory
and works only from that snapshot, builds the app, checks the image (size,
load segments, workspace and payload within their DDR reservations, the mode
strings), pins every input file, flashes, boots, captures the UART, answers
every ready and result line with `cgtools load`, compares the printed and the
saved results, and writes `boot<n>.accepted.json` (times, result hashes).
`--boots 2` repeats the flash and boot for an independent second session. Any
mismatch, trap, timeout or changed file stops it; nothing is retried or
overwritten.

### The run config

For your own model, copy `example/resnet/` and edit `run.json`. Paths are
relative to the file; every file named is pinned by hash when the run starts.

| Field | What to put |
| --- | --- |
| `export` | your export directory (`vectors/`, `hardware.json`) |
| `bundles` | number of bundles to run: the export's bundle count (`vectors/config.json`), or fewer to stop early while debugging |
| `print` | what the runtime prints to the UART: `logits` (dense output, e.g. a classifier), `output` (a tensor output, e.g. a segmentation map), or `none` (nothing printed; only the saved result is compared) |
| `requests` | one entry per inference in the session, in order. `input`: a packed input (`cgtools input`); `expect`: the integers the board must print, compared exactly; `golden`: the expected saved result, compared before the host acknowledges it (for a classifier, the softmax probabilities, one per line) |
| `sim.cycle_limit` | bound for `cgtools run sim`; a stuck simulation stops here |
| `board.bitstream` | `path` and `sha256` of the bitstream for your preset, and `flash_app_offset`: where the app goes in flash, after the bitstream (`7340032` = 0x700000 fits every preset; the run checks they do not overlap) |
| `board.timeout_s` | bound on the whole flash, boot and session |
| `board.result_sha256` | optional: accepted result hashes from an earlier run, to check a rerun gives the same bytes |
| `board.power` | optional: power measurement (see *Power measurement*) |

`cgtools run sim --config run.json --out <dir>` runs the same session in
Verilator first; it needs no board or bitstream.

## Running: step by step

For bring-up and diagnosis, the same session by hand.

1. **Build the app** (a new app directory per build):

   ```sh
   python3 -m cgtools board build-app --export <export> \
     --app-dir <bsp>/apps/cgra4ml_<name> --generated build/run/generated \
     --mode release
   ```

   Writes the model tables and payload into `--generated`, copies
   `runtime/`, `ngultra/c/` and the tables flat into the BSP app with
   `runtime/Makefile.bsp` as its Makefile, builds, and checks the entry point,
   size and markers. Output: `<app>/out/cgra4ml_ngultra_inference.bin`.
   `--bundles N` builds a prefix of the model (bring-up); `--no-print-result`
   skips the UART copy of the result (large results; the host still compares
   the DDR copy).

2. **Write the flash YAML** and check it:

   ```yaml
   bl1:
     path: <bsp>/apps/bsp_demo/out/bsp_demo.bin
   flash:
     number: 1
   bitstream:
     bin: /path/to/bitstream.nxb
     src_addr: 1048576        # 0x100000
   core_0:
     bin: <app>/out/cgra4ml_ngultra_inference.bin
     src_addr: 7340032        # 0x700000, after the bitstream
   ```

   ```sh
   python3 -m cgtools board dry-run --flash-yml flash.yml
   ```

3. **Open the UART** in another terminal (exit with Ctrl-A K):
   `python3 -m cgtools board uart`.

4. **Flash and boot:**

   ```sh
   python3 -m cgtools board flash-boot --flash-yml flash.yml --app <app>.bin \
     --bitstream-sha256 <pinned hash> --openocd-log build/run/openocd.log --keep-openocd
   ```

   Checks the bitstream against its pin, stops this user's stale OpenOCD,
   puts the board in SpaceWire boot, programs BL1, bitstream and app, boots
   from SPI flash 1, and with `--keep-openocd` attaches a fresh OpenOCD for
   loading (`Retained OpenOCD PID=<pid>`). Keep it until the session ends,
   then stop that process.

5. **Serve the session** with each complete UART line:

   ```sh
   load=("$NXTOOLS/venv/bin/python" -m cgtools load --manifest build/run/generated/scheduled_wb.bin.json
         --app-bin <app>.bin --bitstream <bitstream>.nxb
         --expected-app-sha256 <app hash> --expected-bitstream-sha256 <hash>
         --app-code-bytes <offset of .data from 0x80000000>)
   "${load[@]}" --ready-line "<external: ready ...>" --input input.bin --requests 1
   "${load[@]}" --result-line "<external: result ...>" --result-output result-1.bin --golden y_exp.txt
   ```

   The first ready line needs `--requests N` (the session length); every
   ready line needs its `--input`. A result that differs from its golden is
   saved but not acknowledged, so the board waits. `--check-only` validates
   the files without the board.

Don't reset or halt the CPU during a session, and don't run OpenOCD and
NxBase2 on the probe at the same time.

## Build modes and debugging

One switch, `--mode` (Makefile `MODE`, C `CG_MODE`):

| Mode | Prints | Use |
| --- | --- | --- |
| `release` | result, `timing: inference_ms` | performance; acceptance |
| `profile` | release plus `profile: stage=<name> ticks= ms=` per stage | where the time goes |
| `debug` | `log: <event> a=0x.. b=0x..`; register snapshot on a timeout | diagnosis |

Profile stages: `init_memset`, `zero_buffers`, `control`, `wait_packet`,
`accumulate`, `fused`, `postprocess`, `pool_copy`, `finish_other`, `pack`,
`pack_concat`, `clean_intermediate`, and their `sum`. The timers add ~25-45 ms
on large models, so compare profile runs with each other, not with release.

Debug options (`cgtools board build-app`; `cgtools sim` takes the same
`--log-level` and `--bundles`, and embeds the goldens by default in debug
mode, `--goldens 0` to skip):

- `--log-level 1` progress per bundle (default), `2` launches, packs and
  fused-path choices, `3` every raw output packet in hex.
- `--validation-data 1` embeds the export's goldens: every raw packet, bundle
  output, residual, softmax and packed input is compared on the board, and
  the first difference prints `validate: <what>_mismatch a= b= actual=
  expected=` and stops. Only for small models: a large model's goldens do not
  fit the 128 KiB app; validate it in simulation (`cgtools sim --mode debug`)
  instead.
- `--bundles N` runs only the first N bundles, to bisect a failure.

What a stop looks like on the UART:

| Line | Meaning |
| --- | --- |
| `device: timeout reg=0x.. expected=0x.. actual=0x..` | a register wait expired (1e8 polls; 1e9 in debug); in debug, preceded by `log: register_snapshot` |
| `external: <reason>` | a host-protocol check failed: `identity mismatch`, `payload CRC mismatch`, `parameter CRC mismatch`, `invalid command or sequence`, `load timeout`, `result timeout` |
| `validate: <what>_mismatch ...` | a debug golden differed |
| `assertion "<check>" failed. File: <file>:<line> in <function>` | an internal check failed (the BSP's assert handler) |

The board halts after any of these; the host tools stop with the UART log in
the output directory. Simulation (`cgtools sim`) runs the same runtime
against the RTL and prints the same lines, which is usually the faster place
to debug.

## Power measurement

The DevKit measures its supply rails over I2C (an ADS1115 ADC behind two
multiplexers), reached through the ANGIE probe's second USB interface while
OpenOCD uses JTAG. Rails: `VDD_CORE` (FPGA fabric), `SOC_CORE` (ARM SoC),
`1V8D`, `3V3D`, the three unused high-speed-link rails, and the 12 V input
current.

**Sampling alone:** `python -m ngultra.power --out samples.csv [--seconds N]`
(from `ngultra/py`, with the nx_embedded_tools environment) writes one row
per sweep: host time, V, A and W per rail, the rail sum and the 12 V input.

**Measured inference:** add `"power"` to the run config's `board`:

```json
"power": {"warmup_ms": 30000, "idle_ms": 20000, "active_ms": 20000, "cycles": 3}
```

`cgtools run board --mode release` then builds an app whose runtime, after
the first request arrives, repeats it for a warm-up and then alternates idle
and active windows, printing `power:` markers that the host timestamps while
the sampler runs. `boot<n>.accepted.json` holds the mean power per rail in the
idle and active windows, the energy per inference (total and dynamic, active
minus idle) and the number of inferences timed. Power builds skip the
UART copy of the result. Idle here means the CPU busy-polling (~1.84 W on
`SOC_CORE`), not sleeping.

## The libraries

**`c/` (built into the runtime):**

| File | What |
| --- | --- |
| `nx_platform.h` | MMIO reads/writes, barriers, cache clean/invalidate, the 100 MHz counter, UART; under `NX_SIM` the simulator implements the same calls |
| `nx_ddr_layout.h` | DDR reservations: app below `0x81000000`, workspace `0x81000000`-`0x8F000000`, payload `0x8F000000`, mailbox `0x8FFFF000`; the host tools parse this file |
| `nx_log.h` | UART printing without printf: strings, hex, integers, milliseconds, floats |

Porting the runtime to another board means replacing these three files.

**`py/ngultra/` (host side, model independent):**

| Module | What |
| --- | --- |
| `site` | tool locations from `site.json` or the environment |
| `bsp` | stage an app into the BSP, build it, check the image (128 KiB, entry `0x80000000`) |
| `board` | flash YAML, dry run, flash and boot (devkit control, BL1 programmer, OpenOCD), UART |
| `memory` | DAP-AXI reads and writes through an attached OpenOCD |
| `ddr_layout` | the DDR reservations, parsed from `c/nx_ddr_layout.h` |
| `power` | rail sampling and window averages |

`cgtools` reaches the board only through these modules.

## When things go wrong

- **No UART output after boot:** check `uart_port` is the CP2105 (not the
  CP2108 ZynqMP console) and that you are in `dialout`.
- **Flashing fails:** another OpenOCD or NxBase2 holds the probe (`pgrep
  openocd`), or the session lacks `plugdev` (log in again after adding it).
- **The board resets while the bitstream loads:** some placements do this;
  rebuild the bitstream with another `--seed` and load-test every new image.
- **`load timeout` / `result timeout`:** the host did not answer in time
  (each timeout scales with the transfer size); check the retained OpenOCD
  is still attached.
- **A result differs:** run the same export in simulation with `--mode
  debug` to find the first differing layer.
