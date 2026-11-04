"""NG-ULTRA board control: boot source, OpenOCD, flash programming, UART.

Wraps the NanoXplore embedded tools (devkit control, BL1 flash programmer,
boot-source switch) and OpenOCD with the ANGIE adapter. Requires the plugdev
group (and dialout for the UART). Flashing programs BL1, the bitstream and the
core 0 application from a programmer YAML (see flash_yaml), then boots from
SPI flash 1.
"""

import hashlib
import os
import re
import subprocess
import time
from pathlib import Path

from . import site

BITSTREAM_FLASH_OFFSET = 0x100000
OPENOCD_READY = "[AXI] Examination succeed"
OPENOCD_WAIT_S = 45


def nxtools():
    """Returns the nx_embedded_tools checkout (site setting nxtools)."""
    return site.path("nxtools")


def uart_port():
    """Returns the board UART device (site setting uart_port)."""
    return site.get("uart_port")


def require_group(group):
    """Raises unless the current session is in `group`."""
    groups = subprocess.check_output(["id", "-nG"], text=True).split()
    if group not in groups:
        raise RuntimeError(f"current session is not in group '{group}'; "
                           "start a fresh login and verify with: id")


def _tools_env(**extra):
    return dict(os.environ, PATH=str(nxtools() / "py") + os.pathsep + os.environ.get("PATH", ""),
                **extra)


def nx_tool(tool, *args, **extra_env):
    """Runs one nx_embedded_tools script (e.g. ngultra_devkit.py) and waits."""
    require_group("plugdev")
    subprocess.run([str(nxtools() / "py" / tool), *args], env=_tools_env(**extra_env), check=True)


def flash_yaml(path, bitstream, app, app_offset):
    """Writes the programmer YAML: BL1 (the BSP's demo app), the bitstream and
    core 0 in flash 1."""
    bl1 = site.path("bsp_root") / "apps/bsp_demo/out/bsp_demo.bin"
    path.write_text(
        f"bl1:\n  path: {bl1}\nflash:\n  number: 1\n"
        f"bitstream:\n  bin: {bitstream}\n  src_addr: {BITSTREAM_FLASH_OFFSET}\n"
        f"core_0:\n  bin: {app}\n  src_addr: {app_offset}\n")


def dry_run(flash_yml):
    """Prints the flash image layout without touching the board."""
    subprocess.run([str(nxtools() / "py/ngultra_bsp_demo_programmer.py"), "-c", str(flash_yml), "-d"],
                   cwd="/tmp", env=_tools_env(), check=True)


class OpenOCD:
    """An OpenOCD process attached to the board, logging to a file."""

    def __init__(self, log):
        self.log = Path(log)
        self.process = None

    def start(self):
        """Starts OpenOCD and waits until the AXI access port is examined."""
        require_group("plugdev")
        self.log.unlink(missing_ok=True)
        install = nxtools() / "ext/openocd/openocd_install"
        binary, scripts = install / "bin/openocd", install / "share/openocd/scripts"
        with self.log.open("w") as stream:
            self.process = subprocess.Popen(
                [str(binary), "-f", str(scripts / "interface/angie.cfg"),
                 "-f", str(scripts / "target/ngultra.cfg")],
                stdout=stream, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + OPENOCD_WAIT_S
        while time.monotonic() < deadline:
            if OPENOCD_READY in self.log.read_text(errors="replace"):
                print(f"OpenOCD is ready; log: {self.log}", flush=True)
                return self
            if self.process.poll() is not None:
                print(self.log.read_text(errors="replace")[-4000:])
                raise RuntimeError("OpenOCD exited before AXI examination succeeded")
            time.sleep(1)
        print(self.log.read_text(errors="replace")[-6000:])
        raise RuntimeError("OpenOCD did not report AXI examination success")

    def stop(self):
        """Kills the process if it is still running."""
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        self.process = None


def flash_boot(flash_yml, app, bitstream_sha256, openocd_log, keep_openocd=False):
    """Flashes the selection and boots it from SPI flash 1.

    With keep_openocd, a fresh OpenOCD is attached after boot for DDR loading
    and left running; its PID is printed as "Retained OpenOCD PID=<pid>" so the
    caller can stop it. Otherwise no OpenOCD outlives this call.
    """
    dry_run(flash_yml)
    # The YAML selects `app` and a bitstream with the pinned hash.
    text = flash_yml.read_text()
    selected = dict(re.findall(r"^(bitstream|core_0):\n  bin: (.+)$", text, re.M))
    if Path(selected.get("core_0", "")).resolve() != Path(app).resolve():
        raise RuntimeError("flash app differs from built app")
    if not re.fullmatch(r"[0-9a-f]{64}", bitstream_sha256):
        raise ValueError("invalid expected bitstream SHA256")
    image = Path(selected["bitstream"])
    for path in (Path(app), image):
        print("selected:", path.resolve(), "sha256:",
              hashlib.sha256(path.read_bytes()).hexdigest(), flush=True)
    if hashlib.sha256(image.read_bytes()).hexdigest() != bitstream_sha256:
        raise RuntimeError("selected bitstream differs from reviewed image pin")
    # Stop this user's stale OpenOCD and hold the device for SpaceWire boot.
    running = subprocess.run(["pgrep", "-x", "openocd"], capture_output=True).returncode == 0
    if keep_openocd and running:
        raise RuntimeError("an OpenOCD session is already active; refusing to replace it")
    subprocess.run(["pkill", "-9", "-x", "-u", str(os.getuid()), "openocd"], capture_output=True)
    if subprocess.run(["pgrep", "-x", "openocd"], capture_output=True).returncode == 0:
        raise RuntimeError("OpenOCD is running as another user; stop that process first")
    for option, value in (("--por_reset", "0"), ("--boot_src", "SPACEWIRE"),
                          ("--flash_mode", "SEQUENTIAL"), ("--por_reset", "1")):
        nx_tool("ngultra_devkit.py", "--set", option, "--value", value)
    openocd = OpenOCD(openocd_log)
    retained = False
    try:
        openocd.start()
        # BL1, bitstream and core 0, through the running OpenOCD.
        subprocess.run([str(nxtools() / "py/ngultra_bsp_demo_programmer.py"), "-c", str(flash_yml)],
                       env=_tools_env(NX_EMBEDDED_TOOLS_IFACE="openocd_angie"), check=True)
        nx_tool("switch_boot_source.py", "-b", "SPI", "-n", "1")
        nx_tool("ngultra_devkit.py", "--set", "--por_reset")
        # POR resets the DAP registers while OpenOCD keeps its cached AP state:
        # attach a fresh debugger after boot; do not reset the running CPU again.
        openocd.stop()
        if keep_openocd:
            Path(openocd_log).rename(str(openocd_log) + ".program")
            openocd.start()
            retained = True
            print(f"Retained OpenOCD PID={openocd.process.pid} for post-boot DAP-AXI loading; "
                  f"log={openocd_log}", flush=True)
        print("Watch UART for: result: logits= or result: output=", flush=True)
    finally:
        if not retained:
            openocd.stop()


def open_uart(port=None):
    """Opens the board UART in `screen` (interactive)."""
    port = port or uart_port()
    if not Path(port).exists():
        raise RuntimeError(f"UART port is not present: {port}; reconnect the CP2105 NG-ULTRA "
                           "UART (not the CP2108 channel-0 ZynqMP console)")
    require_group("dialout")
    os.execvp("screen", ["screen", port, "115200"])
