"""CGRA4ML host tools for the NG-ULTRA inference runtime.

One command line, `python3 -m cgtools <command>` (see __main__.py):

    fixture  model tables (model.h, model.c), scheduled W/B payload, manifest
    input    one scheduled input tensor from a preprocessed .npy array
    sim      canonical C runtime against the RTL in Verilator
    load     host side of the DDR mailbox on the board (input or result)
    board    BSP application build, flash and boot
    run      a whole simulation or board session from a run config

Modules: schedule, tables, goldens, emit_c, manifest and fixture make
the model tables; host_link is the mailbox protocol; prepare_input, sim and
run drive the rest. The core uses only the standard library; board access goes
through the `ngultra` device library in ngultra/py.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
"""Repository root."""

# The NanoXplore device library lives beside the runtime's C platform layer.
if str(ROOT / "ngultra/py") not in sys.path:
    sys.path.insert(0, str(ROOT / "ngultra/py"))
