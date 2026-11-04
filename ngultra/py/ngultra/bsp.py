"""Bare-metal application builds in the NanoXplore NG-ULTRA BSP.

The BSP builds an application from one flat directory (its sources plus a
Makefile that includes the BSP's common rules). The board boots core 0 from a
flash image of at most 128 KiB whose entry point and `.start` section are at
0x80000000.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

from . import site

CORE0_MAX_BYTES = 131072
ENTRY = 0x80000000


def apps_dir():
    """Returns the BSP's apps/ directory (site setting bsp_root)."""
    return site.path("bsp_root") / "apps"


def gcc_bin():
    """Returns the ARM GCC bin directory (site setting gcc_bin)."""
    return site.path("gcc_bin")


def stage(app_dir, sources, makefile):
    """Copies sources flat into a BSP application directory.

    Args:
        app_dir: Directory under the BSP's apps/ (created if missing).
        sources: Files to copy, by path; their names must be unique.
        makefile: The application Makefile, installed as `Makefile`.
    """
    names = [Path(path).name for path in sources]
    if len(set(names)) != len(names):
        raise ValueError("BSP sources must have unique file names")
    app_dir.mkdir(parents=True, exist_ok=True)
    for path in sources:
        shutil.copy(path, app_dir / Path(path).name)
    shutil.copy(makefile, app_dir / "Makefile")


def build(app_dir, make_variables=(), log=None):
    """Runs `make clean all` in the application with the ARM toolchain on PATH.

    Args:
        app_dir: Staged application directory.
        make_variables: NAME=value arguments for make (e.g. MODE=release).
        log: Open file for the build output (default: inherit).
    """
    env = dict(os.environ, PATH=os.pathsep.join(
        [str(gcc_bin()), str(site.path("nxtools") / "py"), os.environ.get("PATH", "")]))
    subprocess.run(["make", "clean", "all", *make_variables], cwd=app_dir, env=env,
                   stdout=log, stderr=subprocess.STDOUT if log else None, check=True)


def verify(app_bin, markers=()):
    """Checks a built core 0 image: entry point, `.start` address, size, strings.

    Args:
        app_bin: The application's .bin; its .elf is beside it.
        markers: Regular expressions (bytes) that must occur in the image.

    Returns:
        The image size in bytes.
    """
    elf = app_bin.with_suffix(".elf")
    readelf = str(gcc_bin() / "arm-none-eabi-readelf")
    header = subprocess.check_output([readelf, "-h", str(elf)], text=True)
    entry = int(re.search(r"Entry point address:\s+(0x[0-9a-fA-F]+)", header)[1], 16)
    sections = subprocess.check_output([readelf, "-S", str(elf)], text=True)
    start = re.search(r"\]\s+\.start\s+\w+\s+([0-9a-fA-F]+)", sections)
    size = app_bin.stat().st_size
    print(f"core0 bin: {app_bin}\ncore0 bin size: {size} bytes\n"
          f"ELF entry point: {entry:#x}\n.start address: {start and '0x' + start[1]}", flush=True)
    if entry != ENTRY:
        raise RuntimeError(f"ELF entry point is not {ENTRY:#x}")
    if start is None or int(start[1], 16) != ENTRY:
        raise RuntimeError(f".start section is not at {ENTRY:#x}")
    if size > CORE0_MAX_BYTES:
        raise RuntimeError(f"core0 image exceeds the {CORE0_MAX_BYTES}-byte boot window")
    data = app_bin.read_bytes()
    for marker in markers:
        if not re.search(marker, data):
            raise RuntimeError(f"marker {marker!r} is not present in {app_bin.name}")
    return size
