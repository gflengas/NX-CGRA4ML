"""Board application of the runtime: build it in the BSP, flash and boot it.

    build-app   generate the model tables, stage runtime/ + ngultra/c/ + model
                flat into a BSP app directory, build with MODE, check the image
    dry-run     print the flash layout of a programmer YAML
    flash-boot  flash BL1, bitstream and app, boot from SPI flash 1
    uart        open the board UART

Board access goes through ngultra.bsp and ngultra.board.
"""

import argparse
from pathlib import Path

from . import ROOT
from .fixture import generate

APP_NAME = "cgra4ml_ngultra_inference"
MARKERS = (rb"result: (logits|output)=", rb"TARGET PREFIX")
# Builds without the UART copy of the result (power builds, large results).
NOT_PRINTED_MARKERS = (rb"result: not_printed", rb"TARGET PREFIX")
POWER_MARKERS = (*NOT_PRINTED_MARKERS, rb"power: active_end")


def app_bin(app_dir):
    """Returns the core 0 image path of a BSP application directory."""
    return app_dir / "out" / f"{APP_NAME}.bin"


def build_app(export, app_dir, generated, mode="release", log_level=None,
              validation_data=0, log=None, bundle_count=None, power=None,
              print_result=True):
    """Generates the model, stages and builds the app, and checks the image.

    Args:
        export: CGRA4ML export directory (vectors/, hardware.json).
        app_dir: BSP application directory.
        generated: Directory for model.h/.c and the scheduled payload.
        mode: release, profile or debug (runtime/cg_config.h).
        log_level: Debug log level 1-3, or None.
        validation_data: 1 embeds the goldens (small models only).
        log: Open file for the build output.
        bundle_count: Run only the first bundles (default: all).
        power: (idle_ms, active_ms, warmup_ms, cycles) measurement windows, or None.
        print_result: False prints `result: not_printed` instead of the values
            (the host compares the result in DDR).

    Returns:
        Path of the built core 0 image.
    """
    from ngultra import bsp
    generated.mkdir(parents=True, exist_ok=True)
    generate(export / "vectors", generated / "model.h", export / "hardware.json",
             generated / "scheduled_wb.bin", validation_data, bundle_count)
    # The runtime and platform sources, then the model tables.
    sources = [*sorted((ROOT / "runtime").glob("*.[ch]")), *sorted((ROOT / "ngultra/c").glob("*.[ch]"))]
    bsp.stage(app_dir, [*sources, generated / "model.h", generated / "model.c"],
              ROOT / "runtime/Makefile.bsp")
    variables = [f"MODE={mode}"] + ([f"CG_LOG_LEVEL={log_level}"] if log_level else [])
    if power is not None:
        variables += [f"CG_POWER_IDLE_MS={power[0]}", f"CG_POWER_MS={power[1]}",
                      f"CG_POWER_WARMUP_MS={power[2]}", f"CG_POWER_CYCLES={power[3]}"]
    if not print_result:
        variables.append("CG_PRINT_RESULT=0")
    bsp.build(app_dir, variables, log)
    image = app_bin(app_dir)
    bsp.verify(image, POWER_MARKERS if power is not None else
               MARKERS if print_result else NOT_PRINTED_MARKERS)
    return image


def main(argv=None):
    """`cgtools board`: see the module docstring."""
    parser = argparse.ArgumentParser(prog="cgtools board", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build-app")
    build.add_argument("--export", type=Path, required=True)
    build.add_argument("--app-dir", type=Path, required=True,
                       help="BSP application directory, e.g. <bsp>/apps/cgra4ml_inference")
    build.add_argument("--generated", type=Path, required=True)
    build.add_argument("--mode", choices=("release", "profile", "debug"), default="release")
    build.add_argument("--log-level", type=int, choices=(1, 2, 3))
    build.add_argument("--validation-data", type=int, choices=(0, 1), default=0)
    build.add_argument("--bundles", type=int, help="run only the first N bundles")
    build.add_argument("--power", type=int, nargs=4,
                       metavar=("IDLE_MS", "ACTIVE_MS", "WARMUP_MS", "CYCLES"),
                       help="power measurement windows (release builds)")
    build.add_argument("--no-print-result", action="store_true",
                       help="print `result: not_printed` instead of the result")
    dry = commands.add_parser("dry-run")
    dry.add_argument("--flash-yml", type=Path, required=True)
    flash = commands.add_parser("flash-boot")
    flash.add_argument("--flash-yml", type=Path, required=True)
    flash.add_argument("--app", type=Path, required=True)
    flash.add_argument("--bitstream-sha256", required=True)
    flash.add_argument("--openocd-log", type=Path, required=True)
    flash.add_argument("--keep-openocd", action="store_true",
                       help="leave an OpenOCD attached after boot for DDR loading")
    commands.add_parser("uart")
    args = parser.parse_args(argv)
    from ngultra import board
    if args.command == "build-app":
        if args.log_level and args.mode != "debug":
            parser.error("--log-level requires --mode debug")
        if args.power and args.mode != "release":
            parser.error("--power requires --mode release")
        build_app(args.export, args.app_dir, args.generated, args.mode,
                  args.log_level, args.validation_data, bundle_count=args.bundles,
                  power=args.power, print_result=not args.no_print_result)
    elif args.command == "dry-run":
        board.dry_run(args.flash_yml)
    elif args.command == "flash-boot":
        board.flash_boot(args.flash_yml, args.app, args.bitstream_sha256, args.openocd_log,
                         args.keep_openocd)
    else:
        board.open_uart()
