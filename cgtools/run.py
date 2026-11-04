"""Whole simulation or board sessions of one model, from a run config.

A run config is JSON; relative paths are relative to the config's directory:

    {
      "export": "export",                 CGRA4ML export (vectors/, hardware.json)
      "bundles": 7,                       bundles to run (all, or a prefix)
      "print": "logits",                  UART result label: logits or output, or
                                          none (no UART copy; DDR compared)
      "requests": [                       one per inference of the session
        {"input": "inputs/C.bin",         scheduled input (cgtools input)
         "expect": "references/C.txt",    printed integers, compared exactly
         "golden": "references/C-y.txt"}  saved result, compared before ACK
      ],
      "sim": {"cycle_limit": 20000000},   simulation bound
      "board": {
        "bitstream": {"path": "...", "sha256": "...", "flash_app_offset": 5242880},
        "timeout_s": 300,                 whole flash-boot-session bound
        "result_sha256": ["..."],         optional accepted result hashes
        "power": {"warmup_ms": 30000,     optional power measurement (release):
                  "idle_ms": 20000,       warm-up, then `cycles` idle and
                  "active_ms": 20000,     active windows (the first request
                  "cycles": 3}            repeated back to back)
      }
    }

`cgtools run sim` builds and simulates the session (cgtools.sim). `cgtools
run board` snapshots the sources, builds the BSP app, checks the image,
flashes and boots it, serves every request with `cgtools load`, compares the
printed and saved results and writes evidence. Evidence directories are never
overwritten and nothing is retried: any mismatch, timeout or trap stops the run.
With "power", the DevKit's rail monitor (ngultra.power) samples the board
during the runtime's idle and active windows; the evidence holds the mean
power per rail in each window and the energy per inference.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import ROOT

MODES = ("release", "profile", "debug")
# Source trees copied into a board run's snapshot; the run executes from it.
SNAPSHOT = ("runtime", "ngultra", "cgtools")
STATS = re.compile(r"ngultra-sim: cycles=(\d+) .* packets=(\d+)")


def sha256(path):
    """Returns the SHA-256 hex digest of a file."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    """Writes JSON evidence."""
    Path(path).write_text(json.dumps(value, indent=2) + "\n")


def load_config(path, base=None):
    """Reads a run config and resolves its paths.

    Args:
        path: Config file, or an already loaded dict.
        base: Directory of relative paths (default: the config file's).
    """
    if isinstance(path, dict):
        config, base = json.loads(json.dumps(path)), Path(base)
    else:
        config, base = json.loads(Path(path).read_text()), Path(base or Path(path).parent)
    config["export"] = (base / config["export"]).resolve()
    for request in config["requests"]:
        for key in ("input", "expect", "golden"):
            request[key] = (base / request[key]).resolve()
    if "board" in config:
        bitstream = config["board"]["bitstream"]
        bitstream["path"] = (base / bitstream["path"]).resolve()
    return config


def expected_results(requests):
    """Returns the expected printed integers of each request."""
    return [list(map(int, Path(r["expect"]).read_text().split())) for r in requests]


def simulate(config, out, mode="release", env=None):
    """Simulates one session; returns its summary (empty "failures" = pass).

    Debug mode validates the first request against the embedded goldens
    (every raw packet, bundle output and packed input) instead of the session.

    Args:
        config: Loaded run config.
        out: New build directory; the log is <out>.log.
        mode: Runtime build mode.
        env: Environment of the simulator (VERILATOR selects the binary).
    """
    out = Path(out)
    if out.exists():
        raise SystemExit(f"{out} exists; use a fresh output directory")
    requests = config["requests"][:1] if mode == "debug" else config["requests"]
    command = [sys.executable, "-m", "cgtools", "sim", str(config["export"]),
               "--build-dir", str(out), "--first-input", str(requests[0]["input"]),
               "--iterations", str(len(requests)),
               "--mode", mode, "--cycle-limit", str(config["sim"]["cycle_limit"]),
               "--bundles", str(config["bundles"])]
    for request in requests[1:]:
        command += ["--input", str(request["input"])]
    for request in requests:
        command += ["--golden", str(request["golden"])]
    log = out.with_suffix(".log")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".command.json").write_text(json.dumps(command, indent=2) + "\n")
    with log.open("w") as stream:
        status = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, cwd=ROOT,
                                env=dict(env or os.environ, PYTHONPATH=str(ROOT))).returncode
    text = log.read_text()
    body = "\n".join(line for line in text.splitlines() if not line.startswith("+ "))
    failures = []
    if status:
        failures.append(f"simulator exit {status}")
    for word in ("register_snapshot", "timeout", "mismatch"):
        if word in body.lower():
            failures.append(f"log contains '{word}'")
    marker = "PASS" if mode == "debug" else "COMPLETE"
    if f"TARGET PREFIX {config['bundles']} {marker}" not in text:
        failures.append(f"missing TARGET PREFIX {marker}")
    if f"session complete requests={len(requests)}" not in text:
        failures.append("missing session complete")
    if config["print"] != "none":
        prefix = f"result: {config['print']}=["
        printed = [list(map(int, line[len(prefix):-1].split(", ")))
                   for line in text.splitlines() if line.startswith(prefix)]
        if printed != expected_results(requests):
            failures.append(f"printed results differ ({len(printed)} lines)")
    stats = STATS.findall(text)
    hashes = [sha256(out / f"results/result-{i}.bin")
              if (out / f"results/result-{i}.bin").exists() else None
              for i in range(1, len(requests) + 1)]
    return {"mode": mode, "failures": failures,
            "cycles": int(stats[-1][0]) if stats else None,
            "packets": int(stats[-1][1]) if stats else None,
            "result_sha256": hashes, "log": str(log)}


def _source_env(source):
    """Environment that runs `python -m cgtools` from a snapshot, with this
    checkout's site settings."""
    from ngultra import site
    return dict(os.environ, PYTHONPATH=str(source), CGRA4ML_SITE=str(site.site_file().resolve()))


def board_prepare(config, out, app_dir, mode):
    """Snapshots the sources, builds and checks the app; returns the run context."""
    from ngultra import board as nxboard
    source = out / "source"
    for relative in SNAPSHOT:
        shutil.copytree(ROOT / relative, source / relative,
                        ignore=shutil.ignore_patterns("__pycache__"))
    if app_dir.exists():
        raise SystemExit(f"{app_dir} exists; use a new tag")
    bitstream = config["board"]["bitstream"]
    image = Path(bitstream["path"])
    if sha256(image) != bitstream["sha256"]:
        raise SystemExit("bitstream differs from its pin")
    from .board import app_bin
    app = app_bin(app_dir)
    flash = out / "flash.yml"
    nxboard.flash_yaml(flash, image, app, bitstream["flash_app_offset"])
    build = [sys.executable, "-m", "cgtools", "board", "build-app",
             "--export", str(config["export"]), "--app-dir", str(app_dir),
             "--generated", str(out / "generated"), "--mode", mode,
             "--bundles", str(config["bundles"])]
    power = config["board"].get("power")
    if power is not None:
        if mode != "release":
            raise SystemExit("power measurement needs a release build")
        build += ["--power", *(str(power[key]) for key in
                               ("idle_ms", "active_ms", "warmup_ms", "cycles"))]
    if config["print"] == "none":
        build.append("--no-print-result")
    save(out / "environment.json", {"build": build, "source": str(source)})
    for name, command in (("build-app", build),
                          ("dry-run", [sys.executable, "-m", "cgtools", "board", "dry-run",
                                       "--flash-yml", str(flash)])):
        with (out / f"{name}.log").open("w") as log:
            subprocess.run(command, cwd=source, env=_source_env(source), stdout=log,
                           stderr=subprocess.STDOUT, check=True)
    # Check the built image without board access.
    from ngultra import bsp
    gcc = bsp.gcc_bin()
    expected = {p.name: p for d in ("runtime", "ngultra/c") for p in (source / d).glob("*.[ch]")}
    expected["Makefile"] = source / "runtime/Makefile.bsp"
    for name in ("model.h", "model.c"):
        expected[name] = out / "generated" / name
    for name, original in expected.items():
        if sha256(app.parent.parent / name) != sha256(original):
            raise SystemExit(f"BSP copy of {name} differs from the snapshot")
    header = (out / "generated/model.h").read_text()
    if "#define MODEL_HAS_VALIDATION_DATA 0" not in header:
        raise SystemExit("board fixture carries validation data")
    data = app.read_bytes()
    if (b"profile:" in data) != (mode == "profile") or (b"log: " in data) != (mode == "debug"):
        raise SystemExit(f"app strings do not match {mode} mode")
    if (len(data) > bsp.CORE0_MAX_BYTES or
            0x100000 + image.stat().st_size >= bitstream["flash_app_offset"]):
        raise SystemExit("app exceeds core0 window or overlaps the bitstream")
    elf = app.with_suffix(".elf")
    sections = subprocess.check_output([str(gcc / "arm-none-eabi-readelf"), "-SW", str(elf)], text=True)
    segments = subprocess.check_output([str(gcc / "arm-none-eabi-readelf"), "-lW", str(elf)], text=True)
    loads = [tuple(int(v, 16) for v in m) for m in re.findall(
        r"LOAD\s+(0x[0-9a-f]+)\s+(0x[0-9a-f]+)\s+(0x[0-9a-f]+)\s+(0x[0-9a-f]+)\s+(0x[0-9a-f]+)",
        segments)]
    if not loads or not all(0x80000000 <= v < v + mem <= 0x81000000 for _, v, _, _, mem in loads):
        raise SystemExit("ELF LOAD segment outside 0x80000000..0x81000000")
    starts = [int(v, 16) for v in re.findall(r"\]\s+\.(?:data|bss)\s+\w+\s+([0-9a-f]+)", sections)]
    if not starts:
        raise SystemExit("no .data or .bss section")
    code = min(starts) - 0x80000000
    # .data starts inside the image, or .bss up to 7 alignment bytes after it.
    if code <= 0 or code % 4 or code - len(data) >= 8:
        raise SystemExit("unexpected start of mutable data")
    code = min(code, len(data))
    probe = out / "workspace_size.c"
    probe.write_text('#include "cg_workspace.h"\n'
                     "const unsigned int workspace_bytes = sizeof(cg_workspace_t);\n")
    subprocess.run([str(gcc / "arm-none-eabi-gcc"), "-std=c11", "-O2", "-w", "-DNX_SIM=1",
                    "-DNGULTRA_CACHE=1", "-I", str(out / "generated"),
                    "-I", str(source / "runtime"), "-I", str(source / "ngultra/c"),
                    "-S", str(probe), "-o", str(out / "workspace_size.s")], check=True)
    workspace = int(re.search(r"workspace_bytes:\s*\n\s*\.(?:word|long)\s+(\d+)",
                              (out / "workspace_size.s").read_text())[1])
    manifest = json.loads((out / "generated/scheduled_wb.bin.json").read_text())
    reserved = manifest["reservations"]
    if (reserved["workspace_base"] + workspace > reserved["payload_base"] or
            reserved["payload_base"] + manifest["transfer_bytes"] >= reserved["mailbox_base"]):
        raise SystemExit("workspace or payload exceeds its DDR reservation")
    facts = dict(app_bytes=len(data), app_sha256=sha256(app), elf_sha256=sha256(elf),
                 immutable_code_bytes=code, workspace_bytes=workspace,
                 transfer_bytes=manifest["transfer_bytes"])
    save(out / "preflight.json", dict(status="passed", **facts))
    return dict(config=config, source=source, app=app, elf=app.with_suffix(".elf"),
                image=image, flash=flash, bitstream=bitstream, mode=mode, out=out, **facts)


def board_boot(ctx, index, pins):
    """One flash-and-boot session serving every request; returns evidence."""
    import serial
    from ngultra import board as nxboard
    config, out, source = ctx["config"], ctx["out"], ctx["source"]
    tag = f"boot{index}"
    requests = config["requests"]
    expected = expected_results(requests)
    groups = set(subprocess.check_output(["id", "-nG"], text=True).split())
    if not Path(nxboard.uart_port()).exists() or not {"dialout", "plugdev"} <= groups:
        raise SystemExit("UART port missing or user not in dialout/plugdev")
    for process in ("openocd", "NxBase2"):
        if subprocess.run(["pgrep", "-x", process], capture_output=True).returncode == 0:
            raise SystemExit(f"{process} already running")

    def check():
        """Raises if any pinned file changed since the build."""
        changed = [p for p, h in pins.items() if sha256(p) != h]
        if changed:
            raise RuntimeError(f"pinned files changed: {changed[:3]}")

    check()
    printed, timing, profile, queue, commands = 0, [], [], [], []
    readies = results = loaders_done = 0
    flash = loader = sampler = None
    marks = {}
    transcript, pending = bytearray(), b""
    loader_base = [sys.executable, "-m", "cgtools", "load",
                   "--manifest", str(out / "generated/scheduled_wb.bin.json"),
                   "--app-bin", str(ctx["app"]), "--bitstream", str(ctx["image"]),
                   "--expected-app-sha256", ctx["app_sha256"],
                   "--expected-bitstream-sha256", ctx["bitstream"]["sha256"],
                   "--app-code-bytes", str(ctx["immutable_code_bytes"])]
    prefix = f"result: {config['print']}=["
    env = _source_env(source)
    try:
        with serial.Serial(nxboard.uart_port(), 115200, timeout=0.1, exclusive=True) as uart, \
                (out / f"{tag}.uart.log").open("wb") as uart_log, \
                (out / f"{tag}.flash.log").open("w") as flash_log, \
                (out / f"{tag}.loader.log").open("w") as loader_log:
            flash = subprocess.Popen(
                [sys.executable, "-m", "cgtools", "board", "flash-boot",
                 "--flash-yml", str(ctx["flash"]), "--app", str(ctx["app"]),
                 "--bitstream-sha256", ctx["bitstream"]["sha256"],
                 "--openocd-log", str(out / f"{tag}.openocd.log"), "--keep-openocd"],
                cwd=source, env=env, stdout=flash_log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + config["board"]["timeout_s"]
            while time.monotonic() < deadline:
                # Returns as soon as bytes arrive: `power:` lines are stamped
                # with their arrival time (the power windows' host times).
                chunk = uart.read(max(1, uart.in_waiting))
                if chunk:
                    transcript.extend(chunk)
                    pending += chunk
                    uart_log.write(chunk)
                    uart_log.flush()
                    while b"\n" in pending:
                        raw, pending = pending.split(b"\n", 1)
                        line = raw.decode(errors="replace").strip()
                        if line.startswith(prefix):
                            values = list(map(int, line[len(prefix):-1].split(", ")))
                            if values != expected[printed]:
                                raise RuntimeError(f"request {printed + 1}: printed result differs")
                            printed += 1
                        if line == "result: not_printed":
                            # Published without the UART copy (power builds,
                            # print "none"); the loader compares the DDR result.
                            if config["board"].get("power") is None and config["print"] != "none":
                                raise RuntimeError("unprinted result outside a power or unprinted run")
                            printed += 1
                        if line.startswith("timing: inference_ms="):
                            timing.append(float(line.split("=")[1].split()[0]))
                        if line.startswith("profile:"):
                            profile.append(line)
                        if line.startswith("power: "):
                            power = config["board"]["power"]
                            words = line.split()
                            key = " ".join(words[1:3]) if words[1] != "warmup_begin" and \
                                words[1] != "warmup_end" else words[1]
                            marks[key] = (time.time(), line)
                            first = "warmup_begin" if power["warmup_ms"] else "idle_begin 1"
                            if key == first:
                                sampler = subprocess.Popen(
                                    [sys.executable, "-m", "ngultra.power",
                                     "--out", str(out / f"{tag}.power.csv")],
                                    cwd=source, env=dict(env, PYTHONPATH=str(source / "ngultra/py")),
                                    stdout=loader_log, stderr=subprocess.STDOUT)
                            if key == f"active_end {power['cycles']}":
                                sampler.terminate()
                                sampler.wait(timeout=10)
                        lowered = line.lower()
                        if "mismatch" in lowered or "timeout" in lowered or "trap" in lowered:
                            raise RuntimeError(line[:200])
                        if line.startswith(("external: ready ", "external: result ")):
                            queue.append(line)
                if flash.poll() not in (None, 0):
                    raise RuntimeError(f"flash exit {flash.returncode}")
                if loader is not None and loader.poll() is not None:
                    if loader.returncode:
                        raise RuntimeError(f"loader exit {loader.returncode}")
                    loaders_done += 1
                    loader = None
                if queue and flash.poll() == 0 and loader is None:
                    check()
                    line = queue.pop(0)
                    command = list(loader_base)
                    if line.startswith("external: ready "):
                        command += ["--ready-line", line, "--input", str(requests[readies]["input"])]
                        if readies == 0:
                            command += ["--requests", str(len(requests))]
                        readies += 1
                    else:
                        results += 1
                        if printed != results:
                            raise RuntimeError("result released before its printed line")
                        command += ["--result-line", line,
                                    "--result-output", str(out / f"{tag}.result-{results}.bin"),
                                    "--golden", str(requests[results - 1]["golden"])]
                    commands.append(command)
                    save(out / f"{tag}.commands.json", commands)
                    loader = subprocess.Popen(command, cwd=source, env=env,
                                              stdout=loader_log, stderr=subprocess.STDOUT)
                complete = f"TARGET PREFIX {config['bundles']} COMPLETE".encode()
                if complete in transcript and loaders_done == 2 * len(requests) and loader is None and not queue:
                    break
            else:
                raise RuntimeError("bounded board session expired")
        if f"external: session complete requests={len(requests)}".encode() not in transcript:
            raise RuntimeError("session did not complete")
        hashes = []
        for sequence in range(1, len(requests) + 1):
            result = out / f"{tag}.result-{sequence}.bin"
            metadata = json.loads(result.with_suffix(".bin.json").read_text())
            if metadata["comparison"] != "passed" or metadata["sequence"] != sequence:
                raise RuntimeError(f"result {sequence} not accepted by the loader")
            hashes.append(sha256(result))
        accepted = config["board"].get("result_sha256")
        if accepted is not None and hashes != accepted:
            raise RuntimeError("board result hashes differ from the accepted results")
        check()
        evidence = dict(status="passed", mode=ctx["mode"], boot=index, timing_ms=timing,
                        profile=profile, result_sha256=hashes, app_sha256=ctx["app_sha256"],
                        uart_sha256=sha256(out / f"{tag}.uart.log"))
        if config["board"].get("power") is not None:
            evidence["power"] = power_summary(out / f"{tag}.power.csv", marks,
                                              config["board"]["power"]["cycles"])
        save(out / f"{tag}.accepted.json", evidence)
        return evidence
    finally:
        for process in (sampler, loader, flash):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        # Stop the OpenOCD that flash-boot retained for loading, if this user owns it.
        flash_log = out / f"{tag}.flash.log"
        match = re.search(r"Retained OpenOCD PID=(\d+)", flash_log.read_text()) \
            if flash_log.exists() else None
        proc = Path(f"/proc/{match[1]}") if match else None
        if (proc is not None and proc.exists() and proc.stat().st_uid == os.getuid()
                and b"/openocd\x00" in (proc / "cmdline").read_bytes()):
            os.kill(int(match[1]), signal.SIGTERM)


def power_summary(samples, marks, cycles):
    """Mean rail power over the idle and the active windows (each cycle and
    all together) and the energy per inference, from the sampler's CSV and
    the host times of the `power:` lines."""
    from ngultra.power import summarize
    spans = {"idle": [], "active": []}
    inferences = ticks = 0
    for cycle in range(1, cycles + 1):
        for state in spans:
            begin, end = f"{state}_begin {cycle}", f"{state}_end {cycle}"
            if begin not in marks or end not in marks:
                raise RuntimeError(f"power marker {begin} or {end} missing")
            spans[state].append((marks[begin][0], marks[end][0]))
        fields = dict(item.split("=") for item in marks[f"active_end {cycle}"][1].split()[3:])
        inferences += int(fields["inferences"])
        ticks += int(fields["ticks"])
    groups = dict(spans)
    for state in ("idle", "active"):
        for cycle, span in enumerate(spans[state], 1):
            groups[f"{state} {cycle}"] = [span]
    windows = summarize(samples, groups)
    if min(windows[name]["samples"] for name in groups) < 10:
        raise RuntimeError("fewer than 10 power samples in a window")
    seconds = ticks / 1e8 / inferences
    energy = {}
    for column, stats in windows["active"].items():
        if column.endswith("_W"):
            idle = windows["idle"][column]["mean"]
            energy[column[:-2]] = dict(active_w=stats["mean"], idle_w=idle,
                                       joules=stats["mean"] * seconds,
                                       dynamic_joules=(stats["mean"] - idle) * seconds)
    return dict(windows=windows, inferences=inferences, inference_s=seconds, energy=energy)


def board_run(config, out, app_dir, mode="release", boots=1, report=None):
    """Builds once, then flashes, boots and serves the session `boots` times.

    Args:
        config: Loaded run config.
        out: New evidence directory.
        app_dir: New BSP application directory.
        mode: Runtime build mode.
        boots: Independent flash-and-boot sessions (2 for integrated acceptance).
        report: Called with each boot's evidence as it passes.

    Returns:
        The evidence of every boot.
    """
    out = Path(out)
    if out.exists():
        raise SystemExit(f"{out} exists; use a new tag")
    out.mkdir(parents=True)
    ctx = board_prepare(config, out, Path(app_dir), mode)
    inputs = [Path(r[key]) for r in config["requests"] for key in ("input", "expect", "golden")]
    pins = {str(p): sha256(p) for p in [
        ctx["app"], ctx["elf"], ctx["image"], ctx["flash"], *inputs,
        *[p for d in (ctx["source"], config["export"], out / "generated")
          for p in Path(d).rglob("*") if p.is_file() and "__pycache__" not in p.parts]]}
    save(out / "pins.json", pins)
    evidence = []
    for index in range(1, boots + 1):
        evidence.append(board_boot(ctx, index, pins))
        if report:
            report(evidence[-1])
    return evidence


def main(argv=None):
    """`cgtools run`: see the module docstring."""
    parser = argparse.ArgumentParser(prog="cgtools run", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", choices=("sim", "board"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="new evidence directory")
    parser.add_argument("--mode", choices=MODES, default="release")
    parser.add_argument("--boots", type=int, default=1, choices=(1, 2), help="board sessions")
    parser.add_argument("--app-dir", type=Path,
                        help="board: new BSP app directory (default: <bsp>/apps/cgra4ml_<out name>)")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if args.target == "sim":
        summary = simulate(config, args.out.resolve(), args.mode)
        print(json.dumps(summary, indent=2))
        sys.exit(1 if summary["failures"] else 0)
    from ngultra import bsp
    app_dir = args.app_dir or bsp.apps_dir() / f"cgra4ml_{args.out.name}".replace("-", "_")
    board_run(config, args.out.resolve(), app_dir, args.mode, args.boots,
              lambda e: print(f"boot{e['boot']} PASS timing_ms={e['timing_ms']}", flush=True))
