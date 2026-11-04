"""Command line of the CGRA4ML host tools: `python3 -m cgtools <command> ...`."""

import argparse
import sys
from pathlib import Path


def fixture(argv):
    """`cgtools fixture`: model tables, scheduled payload and manifest."""
    from .fixture import generate
    parser = argparse.ArgumentParser(prog="cgtools fixture", description=generate.__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("vectors", type=Path, help="the export's vectors/ directory")
    parser.add_argument("output", type=Path, help="model.h to write; model.c goes beside it")
    parser.add_argument("--hardware", type=Path, required=True, help="the export's hardware.json")
    parser.add_argument("--payload-output", type=Path, required=True,
                        help="scheduled W/B file; the manifest is <file>.json")
    parser.add_argument("--validation-data", type=int, choices=(0, 1), default=0,
                        help="embed the goldens for debug validation on target")
    parser.add_argument("--bundles", type=int, help="use only the first N bundles")
    args = parser.parse_args(argv)
    generate(args.vectors, args.output, args.hardware, args.payload_output,
             args.validation_data, args.bundles)


COMMANDS = {
    "fixture": fixture,
    "input": lambda argv: __import__("cgtools.prepare_input", fromlist=["main"]).main(argv),
    "sim": lambda argv: __import__("cgtools.sim", fromlist=["main"]).main(argv),
    "load": lambda argv: __import__("cgtools.host_link", fromlist=["main"]).main(argv),
    "board": lambda argv: __import__("cgtools.board", fromlist=["main"]).main(argv),
    "run": lambda argv: __import__("cgtools.run", fromlist=["main"]).main(argv),
}


def main():
    """Dispatches `python3 -m cgtools <command> ...`."""
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit("usage: python3 -m cgtools {" + ",".join(COMMANDS) + "} ...\n" +
                 (__import__("cgtools").__doc__ or ""))
    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
