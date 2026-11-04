"""Machine-specific tool locations, kept out of the code.

Each setting comes from its environment variable, else from `site.json` at the
repository root (Git-ignored; copy `site.example.json`), else from its default
when it has one. `CGRA4ML_SITE` names another site file; board runs set it so
the snapshot they execute from finds the same file.
"""

import json
import os
from pathlib import Path

from . import ROOT

# key: (environment variable, default or None if required)
SETTINGS = {
    "nxtools": ("NXTOOLS", None),
    "bsp_root": ("BSP_ROOT", None),
    "gcc_bin": ("GCC_BIN", "/opt/Xilinx/Vitis/2024.1/gnu/aarch32/lin/gcc-arm-none-eabi/bin"),
    "uart_port": ("UART_PORT", None),
    "verilator": ("VERILATOR", "verilator"),
}


def site_file():
    """Returns the site file in use (it may not exist)."""
    return Path(os.environ.get("CGRA4ML_SITE", ROOT / "site.json"))


def get(key):
    """Returns setting `key` (see SETTINGS) as a string.

    Raises:
        KeyError: If the setting is unset and has no default.
    """
    variable, default = SETTINGS[key]
    if os.environ.get(variable):
        return os.environ[variable]
    path = site_file()
    if path.exists():
        values = json.loads(path.read_text())
        if values.get(key):
            return values[key]
    if default is None:
        raise KeyError(f"set {variable} or '{key}' in {path} (see site.example.json)")
    return default


def path(key):
    """Returns setting `key` as a Path."""
    return Path(get(key))
