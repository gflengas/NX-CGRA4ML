#!/usr/bin/env python3
"""Every-input check of the runtime's fast fixed-point arithmetic.

For each regression fixture: generate its model tables with `cgtools
fixture`, compile test/fixed_exhaustive.c with them and run it. Every
distinct fast postprocess/pool plan (compiled, as the runtime runs it) must
equal the reference cg_fixed_postprocess, and every concat transition
quantizer the reference cg_fixed_quantize, for all 2^32 int32 inputs. Needs
gcc with OpenMP; about 2 minutes on 24 cores for an 8-bit model's quantizers.

Usage (repository root): python3 test/check_fixed_exhaustive.py [fixture ...]
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cgtools.fixture import generate  # noqa: E402


def main():
    catalog = json.loads((ROOT / "test/regression/fixtures.json").read_text())
    names = sys.argv[1:] or catalog["default_set"]
    failed = []
    for name in names:
        export = ROOT / "fixtures" / name / catalog["fixtures"][name]["export"]
        with tempfile.TemporaryDirectory(prefix=f"fixed-exhaustive-{name}-") as work:
            work = Path(work)
            generate(export / "vectors", work / "model.h", export / "hardware.json",
                     work / "scheduled_wb.bin")
            binary = work / "fixed_exhaustive"
            subprocess.run(["gcc", "-std=gnu11", "-O2", "-fopenmp", "-Wall", "-Wextra", "-Werror",
                            "-Wno-unused-function", "-I", str(work), "-I", str(ROOT / "runtime"),
                            str(ROOT / "test/fixed_exhaustive.c"), str(work / "model.c"),
                            "-o", str(binary)], check=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            print(f"{name}: {result.stdout.strip()}", flush=True)
            if result.returncode != 0:
                failed.append(name)
    if failed:
        sys.exit(f"FAIL: {failed}")


if __name__ == "__main__":
    main()
