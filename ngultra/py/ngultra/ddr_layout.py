"""DDR reservations shared by the runtime, the simulator and the host tools.

The single source is ngultra/c/nx_ddr_layout.h; this module parses it.
"""

import re

from . import ROOT

HEADER = ROOT / "ngultra/c/nx_ddr_layout.h"
NAMES = ("WORKSPACE_BASE", "PAYLOAD_BASE", "MAILBOX_BASE", "RESERVED_END")


def read(header=HEADER):
    """Returns the reservations as {"workspace_base": ..., ...} byte addresses."""
    text = header.read_text()
    reservations = {}
    for name in NAMES:
        match = re.search(r"#define NX_DDR_" + name + r" UINT32_C\((0x[0-9a-fA-F]+)\)", text)
        if not match:
            raise ValueError(f"missing DDR reservation NX_DDR_{name} in {header}")
        reservations[name.lower()] = int(match[1], 16)
    return reservations
