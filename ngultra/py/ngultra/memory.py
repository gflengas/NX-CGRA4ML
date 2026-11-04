"""DAP-AXI access to NG-ULTRA memory through the NanoXplore embedded tools.

Needs an OpenOCD already attached to the board's AXI access port and the
nx_embedded_tools checkout (site setting nxtools).
"""

import os
import subprocess
import tempfile
from pathlib import Path

from . import site


def programmer():
    """Returns the DAP-AXI memory programmer of nx_embedded_tools."""
    return site.path("nxtools") / "py/ngultra_memory_programmer.py"


class Memory:
    """Reads and writes board memory word-aligned, one programmer call each.

    Use as a context manager; it owns a temporary directory for transfers.
    """

    def __enter__(self):
        if not programmer().is_file():
            raise FileNotFoundError(f"missing NanoXplore programmer: {programmer()}")
        self._temp = tempfile.TemporaryDirectory(prefix="ngultra-memory-")
        self._dir = Path(self._temp.name)
        return self

    def __exit__(self, *exc):
        self._temp.cleanup()

    def _run(self, *options, nbytes=0):
        env = dict(os.environ, PATH=str(programmer().parent) + os.pathsep + os.environ.get("PATH", ""))
        # The OpenOCD attached by ngultra.board uses the ANGIE adapter.
        env.setdefault("NX_EMBEDDED_TOOLS_IFACE", "openocd_angie")
        # Large transfers (a batch's result) take minutes over DAP-AXI: allow
        # 60 s plus the size at 32 KiB/s.
        result = subprocess.run([str(programmer()), "--axi", *options], env=env, text=True,
                                capture_output=True, timeout=60 + nbytes / 32768)
        if result.returncode:
            raise RuntimeError(f"DAP-AXI programmer failed ({result.returncode}): "
                               f"{result.stdout}\n{result.stderr}")
        return result.stdout.strip()

    def read(self, address, length):
        """Returns `length` bytes (a nonzero multiple of 4) read at `address`."""
        if length % 4 or not length:
            raise ValueError("readback must contain aligned words")
        # The programmer returns a scalar for count=1, which its dump does not
        # accept; read one word as a register instead.
        if length == 4:
            return int(self._run("--address", hex(address), "--read_register"), 0).to_bytes(4, "little")
        dump = self._dir / "readback.bin"
        self._run("--address", hex(address), "--count", str(length // 4), "--dump", str(dump),
                  nbytes=length)
        return dump.read_bytes()

    def write(self, address, data):
        """Writes bytes at `address`."""
        chunk = self._dir / "chunk.bin"
        chunk.write_bytes(data)
        self._run("--address", hex(address), "--write_file", str(chunk), nbytes=len(data))

    def write_word(self, address, value):
        """Writes one 32-bit word at `address`."""
        self._run("--address", hex(address), "--write_register", str(value))
