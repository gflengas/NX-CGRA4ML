"""NanoXplore NG-ULTRA device library for host tools (model independent).

Modules:
    site: Machine-specific tool locations (site.json or environment).
    ddr_layout: DDR reservations, read from ngultra/c/nx_ddr_layout.h.
    memory: DAP-AXI reads and writes through nx_embedded_tools and OpenOCD.
    bsp: Stages, builds and checks a bare-metal application in the BSP.
    board: Board control: boot source, OpenOCD, flash programming, UART.

Only ddr_layout uses nothing but the standard library and no tools; the
others need the NanoXplore embedded tools and a connected board.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
"""Repository root."""
