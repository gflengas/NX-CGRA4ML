#!/usr/bin/env python3
"""Convert an upstream-Keras-frontend export to the computed reference format.

The upstream frontend (Keras XModel: the ResNet's QONNX path and the U-Net
example) writes its per-layer goldens next to Python simulation copies
(`*_sim.*`). The fixture generator reads only the computed format, so this
converter checks every simulation copy against its expected file, keeps the
packed next-layer inputs under their computed-format name and relabels the
export. Run it once on each such export; it refuses an export that is already
converted or whose simulation copies disagree with the goldens.

Usage: python3 frontend/convert_legacy_vectors.py <export>/vectors
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cgtools.goldens import compact_valid_words, read_ints  # noqa: E402

LEGACY = "legacy_frontend_simulation"
COMPUTED = "computed_integer_graph_v1"


def convert(vectors):
    """Checks the simulation copies and relabels the export in place.

    Args:
        vectors: The export's vectors directory (contains config.json).

    Returns:
        Number of simulation files checked.
    """
    config_path = vectors / "config.json"
    config = json.loads(config_path.read_text())
    if config.get("reference_format", LEGACY) != LEGACY:
        raise ValueError(f"{vectors} is not a legacy export")
    pe_rows = config["defines"]["PE_ROWS"]
    checked = 0
    for ib, bundle in enumerate(config["bundles"]):
        for ip in range(bundle["p"]):
            for it in range(bundle["t"]):
                simulated = vectors / f"{ib}_{ip}_{it}_y_raw_sim.txt"
                if simulated.exists():
                    expected = read_ints(vectors / f"{ib}_{ip}_{it}_y_exp.txt")
                    if (compact_valid_words(read_ints(simulated), bundle, pe_rows, it) !=
                            compact_valid_words(expected, bundle, pe_rows, it)):
                        raise ValueError(f"{simulated.name} differs from its golden")
                    checked += 1
        simulated = vectors / f"{ib}_y_nhwc_sim.txt"
        if simulated.exists() and not bundle["is_softmax"]:
            if read_ints(simulated) != read_ints(vectors / f"{ib}_y_nhwc_exp.txt"):
                raise ValueError(f"{simulated.name} differs from its golden")
            checked += 1
        if ib == 0:
            continue
        packed_input = vectors / f"{ib}_x_sim.bin"
        if packed_input.exists():
            packed_output = vectors / f"{ib - 1}_y_packed_sim.bin"
            if packed_output.exists() and packed_output.read_bytes() != packed_input.read_bytes():
                raise ValueError(f"{packed_output.name} differs from {packed_input.name}")
            shutil.copyfile(packed_input, vectors / f"{ib}_x_exp.bin")
            checked += 1
    config["reference_format"] = COMPUTED
    config["converted_from"] = LEGACY
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    return checked


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("vectors", type=Path)
    args = parser.parse_args()
    print(f"converted {args.vectors}: {convert(args.vectors)} simulation files checked")


if __name__ == "__main__":
    main()
