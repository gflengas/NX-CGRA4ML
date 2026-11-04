"""Build and run the canonical NG-ULTRA C runtime against the RTL in Verilator.

Generates the model tables, compiles runtime/ and ngultra/c/ with the
simulation platform, builds the Verilator model of the repository's RTL and
testbench (RTL_SOURCES) with the C++ harness, and serves the session: the
harness plays the host side of the DDR mailbox and hands each result to
`cgtools load --saved-result` for its golden comparison.
"""

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from . import ROOT
from .fixture import generate
from .host_link import assemble_input
from ngultra import site

RUNTIME_DIR = ROOT / "runtime"
PLATFORM_DIR = ROOT / "ngultra/c"
SIM_DIR = ROOT / "sim"
SIM_SOURCES = sorted(SIM_DIR.glob("*.cpp"))
TB = "CGRA4ML/deepsocflow/test/sv"
RTL = "CGRA4ML/deepsocflow/rtl"
# Testbench and RTL, in the order of CGRA4ML's own source lists.
RTL_SOURCES = [
    *(f"{TB}/{name}" for name in (
        "axis_sync_tb.sv", "axis_weight_rotator_tb.sv", "dma_controller_tb.sv",
        "axis_pixels_tb.sv", "cgra4ml_axi2ram_tb.sv", "axis_adapter_any_tb.sv",
        "ext/skidbuffer.v", "ext/zipcpu_axi2ram.v", "ext/axi_addr.v")),
    *(f"{RTL}/{name}" for name in (
        "axi_cgra4ml_2m.v", "dnn_engine.v", "axi_cgra4ml.v", "nx_rams_sp_wf.sv",
        "ext/alex_axis_register.v", "ext/alex_axis_pipeline_register.v",
        "axis_weight_rotator.sv", "ram.sv", "n_delay.sv", "axis_pixels.sv",
        "axis_out_shift.sv", "dma_controller.sv", "cyclic_bram.sv", "counter.sv",
        "proc_engine.sv", "ext/alex_axis_adapter_any.sv",
        "ext/alex_axis_adapter.sv", "ext/alex_axi_dma_wr.sv",
        "ext/alex_axi_dma_rd.sv", "ext/xilinx_sdp.sv",
        "skid_buffer.sv", "ngultra_llpp_regs.v",
        "ngultra_read_reserve.v")),
]


def run(command, **kwargs):
    """Runs a command, echoing it with a '+ ' prefix; raises on failure."""
    print("+", " ".join(map(str, command)), flush=True)
    subprocess.run([str(item) for item in command], check=True, **kwargs)


def digest(path):
    """Returns the SHA-256 hex digest of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv=None):
    """`cgtools sim`: one simulated session of an export."""
    parser = argparse.ArgumentParser(prog="cgtools sim", description=__doc__)
    parser.add_argument("export", type=Path,
                        help="export with hardware.json, vectors/ and the config_*.svh headers")
    parser.add_argument("--bundles", type=int, help="run only the first N bundles (default: all)")
    parser.add_argument("--build-dir", type=Path, default=ROOT / "build/ngultra_runtime_sim")
    parser.add_argument("--mode", choices=("release", "profile", "debug"), default="release",
                        help="runtime build mode (runtime/cg_config.h)")
    parser.add_argument("--log-level", type=int, choices=(1, 2, 3),
                        help="debug log detail: 1 progress, 2 launches, 3 raw packets")
    parser.add_argument("--goldens", type=int, choices=(0, 1),
                        help="embed per-layer goldens for validation (default: 1 in debug mode)")
    parser.add_argument("--cycle-limit", type=int, default=400000000,
                        help="total session watchdog cycles (1..1000000000)")
    parser.add_argument("--first-input", type=Path, required=True,
                        help="explicit scheduled X for the first external inference")
    parser.add_argument("--input", type=Path, action="append", default=[],
                        help="packed replacement X for each inference after the first")
    parser.add_argument("--golden", type=Path, action="append", default=[],
                        help="optional host golden for each external result, checked before ACK")
    parser.add_argument("--iterations", type=int, default=1,
                        help="host session request count (default: 1)")
    parser.add_argument("--trace-file", type=Path, help="write MMIO, AXI and ownership events")
    parser.add_argument("--power", type=int, nargs=4,
                        metavar=("IDLE_MS", "ACTIVE_MS", "WARMUP_MS", "CYCLES"),
                        help="build the runtime's power windows (1 ms = 100,000 cycles)")
    parser.add_argument("--axi-seed", type=int,
                        help="realistic shared-memory slave with this nonzero PRNG seed: "
                             "outstanding bursts, latency and random ready/valid gaps")
    parser.add_argument("--axi-outstanding", type=int, default=4,
                        help="accepted bursts per channel in the realistic slave (1..64)")
    parser.add_argument("--axi-latency", type=int, nargs=2, default=(8, 64),
                        metavar=("MIN", "MAX"), help="cycles before read data/write response")
    parser.add_argument("--axi-ready-percent", type=int, default=50,
                        help="per-cycle probability of each ready/valid offer (1..100)")
    parser.add_argument("--axi-read-order", action="store_true",
                        help="return read data of both read ports in address-acceptance "
                             "order, as a shared in-order SoC return path would")
    parser.add_argument("--axi-read-buffer", type=int, default=16,
                        help="--axi-read-order: per-port read buffer in beats (1..4096)")
    args = parser.parse_args(argv)
    if args.axi_seed is not None:
        if args.axi_seed <= 0:
            parser.error("--axi-seed must be a positive integer")
        if not 1 <= args.axi_outstanding <= 64 or not 1 <= args.axi_ready_percent <= 100:
            parser.error("--axi-outstanding must be 1..64 and --axi-ready-percent 1..100")
        if not 0 <= args.axi_latency[0] <= args.axi_latency[1] <= 10000:
            parser.error("--axi-latency requires 0 <= MIN <= MAX <= 10000")
    elif args.axi_read_order:
        parser.error("--axi-read-order requires --axi-seed")
    if not 1 <= args.cycle_limit <= 1000000000:
        parser.error("--cycle-limit must be 1..1000000000")
    if args.log_level and args.mode != "debug":
        parser.error("--log-level requires --mode debug")
    if args.goldens is None:
        args.goldens = int(args.mode == "debug")
    validate = args.mode == "debug" and args.goldens
    if args.golden and len(args.golden) != args.iterations:
        parser.error("--golden requires one golden per inference")
    if len(args.input) != args.iterations - 1:
        parser.error("external repeats require one --input for each inference after the first")
    if not 1 <= args.iterations <= 0xffffffff:
        parser.error("--iterations must be a positive uint32 count")
    if args.iterations > 1 and validate:
        parser.error("golden validation checks one request; use --goldens 0 for repeats")
    export = args.export.resolve()
    build = args.build_dir.resolve()
    build.mkdir(parents=True, exist_ok=True)
    header = build / "model.h"
    payload = build / "scheduled_wb.bin"
    metadata = generate(export / "vectors", header, export / "hardware.json",
                        payload, args.goldens, args.bundles)
    initial_payload = build / "request-1-wbx.bin"
    initial_payload.write_bytes(assemble_input(payload.read_bytes(), metadata,
                                               args.first_input, first=True))

    objects = []
    for source in (*sorted(RUNTIME_DIR.glob("*.c")), *sorted(PLATFORM_DIR.glob("*.c")),
                   build / "model.c"):
        target = build / (source.stem + ".o")
        run(["gcc", "-std=c11", "-O2", "-Wall", "-Wextra",
             "-DNX_SIM=1", "-DNGULTRA_CACHE=1",
             f"-DCG_MODE={('release', 'profile', 'debug').index(args.mode)}",
             *([f"-DCG_LOG_LEVEL={args.log_level}"] if args.log_level else []),
             *([f"-DCG_POWER_IDLE_MS={args.power[0]}", f"-DCG_POWER_MS={args.power[1]}",
                f"-DCG_POWER_WARMUP_MS={args.power[2]}", f"-DCG_POWER_CYCLES={args.power[3]}"]
               if args.power else []),
             "-I", build, "-I", RUNTIME_DIR, "-I", PLATFORM_DIR,
             "-c", source, "-o", target])
        objects.append(target)

    obj_dir = build / "obj_dir"
    rtl_list = build / "rtl_sources.txt"
    rtl_list.write_text("".join(f"{ROOT / path}\n" for path in RTL_SOURCES) + "-Wno-SELRANGE\n")
    verilator = site.get("verilator")
    defines = ["+define+CGRA4ML_2M_SIM", "+define+CGRA4ML_NGULTRA_HOST_DIAGNOSTICS"]
    cflags = (f"-std=c++14 -DNX_SIM=1 -I{RUNTIME_DIR} -I{PLATFORM_DIR} -I{SIM_DIR} "
              f"-DCGRA4ML_SIM_CYCLE_LIMIT={args.cycle_limit}")
    run([verilator, "--cc", "--exe", "--top-module", "cgra4ml_axi2ram_tb",
         "--relative-includes", "-Wno-fatal", "--assert",
         *defines, "-I" + str(export), "-F", rtl_list,
         "--Mdir", obj_dir, "-CFLAGS", cflags,
         "-LDFLAGS", "-lm", *SIM_SOURCES])
    link_deps = build / "ngultra_runtime_link_deps.mk"
    link_deps.write_text("Vcgra4ml_axi2ram_tb: " + " ".join(map(str, objects)) + "\n")
    run(["make", "-C", obj_dir, "-f", "Vcgra4ml_axi2ram_tb.mk",
         "-f", link_deps, "-j4"])
    manifest = {
        "export": str(export),
        "fixture_sha256": digest(header),
        "hardware_sha256": digest(export / "hardware.json"),
        "vectors_config_sha256": digest(export / "vectors/config.json"),
        "vectors_parameters_sha256": digest(export / "vectors/wb.bin"),
        "verilator_version": subprocess.check_output([verilator, "--version"], text=True).strip(),
        "gcc_version": subprocess.check_output(["gcc", "--version"], text=True).splitlines()[0],
        "cycle_limit": args.cycle_limit,
        "mode": args.mode,
        "log_level": args.log_level,
        "goldens": args.goldens,
        "request_count": args.iterations,
        "protocol_version": 2,
        "scheduled_payload_sha256": digest(payload),
        "result_quantization": json.loads(payload.with_suffix(".bin.json").read_text())["result_quantization"],
        "external_payload": str(initial_payload.resolve()),
        "external_payload_sha256": digest(initial_payload),
        "first_input": {"path": str(args.first_input.resolve()), "sha256": digest(args.first_input)},
        "replacement_inputs": [{"path": str(path.resolve()), "sha256": digest(path)}
                               for path in args.input],
        "host_goldens": [{"path": str(path.resolve()), "sha256": digest(path)}
                         for path in args.golden],
        "sources": {str(path.relative_to(ROOT)): digest(path) for path in
                    (*sorted(RUNTIME_DIR.glob("*.[ch]")), *sorted(PLATFORM_DIR.glob("*.[ch]")),
                     *sorted((ROOT / "cgtools").glob("*.py")),
                     *sorted((ROOT / "ngultra/py/ngultra").glob("*.py")),
                     *(ROOT / path for path in RTL_SOURCES), *SIM_SOURCES)},
    }
    if args.axi_seed is not None:
        manifest["realistic_axi"] = {
            "seed": args.axi_seed, "outstanding": args.axi_outstanding,
            "latency": list(args.axi_latency), "ready_percent": args.axi_ready_percent,
            "read_order": args.axi_read_order, "read_buffer": args.axi_read_buffer}
    (build / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    os.environ["CGRA4ML_SIM_EXTERNAL_PAYLOAD"] = str(initial_payload.resolve())
    os.environ["CGRA4ML_SIM_REQUEST_COUNT"] = str(args.iterations)
    for sequence, path in enumerate(args.input, start=2):
        os.environ[f"CGRA4ML_SIM_INPUT_{sequence}"] = str(path.resolve())
    result_dir = build / "results"
    result_dir.mkdir(exist_ok=True)
    os.environ["CGRA4ML_SIM_RESULT_DIR"] = str(result_dir)
    for sequence in range(1, args.iterations + 1):
        result_path = result_dir / f"result-{sequence}.bin"
        result_path.unlink(missing_ok=True)
        result_path.with_suffix(".bin.json").unlink(missing_ok=True)
        command = [sys.executable, "-m", "cgtools", "load",
                   "--manifest", str(payload.with_suffix(".bin.json")),
                   "--saved-result", str(result_path), "--sequence", str(sequence),
                   "--requests", str(args.iterations)]
        if args.golden:
            command += ["--golden", str(args.golden[sequence - 1].resolve())]
        os.environ[f"CGRA4ML_SIM_RESULT_COMMAND_{sequence}"] = shlex.join(command)
    for name in ("SEED", "OUTSTANDING", "LATENCY_MIN", "LATENCY_MAX",
                 "READY_PERCENT", "READ_ORDER", "READ_BUFFER"):
        os.environ.pop(f"CGRA4ML_SIM_AXI_{name}", None)
    if args.axi_seed is not None:
        os.environ.update(
            CGRA4ML_SIM_AXI_SEED=str(args.axi_seed),
            CGRA4ML_SIM_AXI_OUTSTANDING=str(args.axi_outstanding),
            CGRA4ML_SIM_AXI_LATENCY_MIN=str(args.axi_latency[0]),
            CGRA4ML_SIM_AXI_LATENCY_MAX=str(args.axi_latency[1]),
            CGRA4ML_SIM_AXI_READY_PERCENT=str(args.axi_ready_percent),
            CGRA4ML_SIM_AXI_READ_ORDER=str(int(args.axi_read_order)),
            CGRA4ML_SIM_AXI_READ_BUFFER=str(args.axi_read_buffer))
    if args.trace_file:
        trace_path = args.trace_file.resolve()
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        os.environ["CGRA4ML_SIM_TRACE_FILE"] = str(trace_path)
    # The harness runs each result command in this process's environment.
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), *filter(None, [os.environ.get("PYTHONPATH")])])
    run([obj_dir / "Vcgra4ml_axi2ram_tb"])

