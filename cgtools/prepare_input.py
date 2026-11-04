"""Prepare one explicit scheduled X tensor; never run or export a network.

Use the CGRA4ML Python environment. Inputs are .npy arrays with the compiled
NHWC shape (convolution) or NC shape (dense), already preprocessed by the caller.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from . import ROOT


def prepare_array(values, config, hardware, mode, frac=None):
    """Quantizes (float mode) and packs one input in the first bundle's pass order.

    Args:
        values: numpy array of the compiled NHWC (or NC, dense) shape.
        config: The export's config.json.
        hardware: The export's hardware.json.
        mode: "float" quantizes with CGRA4ML's input quantizer, "quantized"
            takes signed integers as they are.
        frac: Fractional bits when the export does not record them.

    Returns:
        (scheduled bytes, metadata dict).
    """
    import numpy as np
    if str(ROOT / "CGRA4ML") not in sys.path:
        sys.path.insert(0, str(ROOT / "CGRA4ML"))
    bundle = config["bundles"][0]
    bits = hardware["bits_input"]
    rows = hardware["processing_elements"][0]
    if bits != 1 << config["defines"]["X_BITS_L2"] or rows != config["defines"]["PE_ROWS"]:
        raise ValueError("hardware does not match compiled input layout")
    quant = bundle.get("input_quant")
    if quant is not None:
        if quant["bits"] != bits or (frac is not None and frac != quant["frac"]):
            raise ValueError("explicit quantization disagrees with model metadata")
        frac = quant["frac"]
    if frac is None:
        raise ValueError("input fractional bits missing; provide validated --frac explicitly")
    if not isinstance(frac, int) or not 0 <= frac < bits:
        raise ValueError("supported signed input requires 0 <= frac < bits")
    dense = bool(bundle.get("is_dense", 0))
    shape = ((bundle["h"], bundle["ci"]) if dense else
             (bundle["n"], bundle["h"], bundle["w"], bundle["ci"]))
    # A batched export takes the batch's images as one NHWC array.
    if dense and (bundle["n"] != 1 or bundle["h"] != 1 or bundle["w"] != 1):
        raise ValueError("dense input preparation supports compiled batch 1 only")
    if not isinstance(values, np.ndarray) or values.shape != shape:
        raise ValueError(f"input shape must be {shape}")
    if not (np.issubdtype(values.dtype, np.integer) or np.issubdtype(values.dtype, np.floating)):
        raise ValueError("input must contain numeric values")
    if not np.isfinite(values).all():
        raise ValueError("input must be finite")
    low, high = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    if mode == "quantized":
        if not np.issubdtype(values.dtype, np.integer) or np.any(values < low) or np.any(values > high):
            raise ValueError("quantized input must contain in-range signed integers")
        integers = values.astype(np.int64)
    elif mode == "float":
        from deepsocflow.py.xmodel import XInputAct
        from deepsocflow.py.utils import XTensor
        integer_bits = bits - frac - 1
        quantizer = XInputAct(f"quantized_bits({bits},{integer_bits},False,True,1)")
        tensor = XTensor(quantizer(values.astype(np.float32)), bits=bits, frac=frac)
        tensor.assert_valid()
        integers = tensor.itensor.numpy().astype(np.int64)
    else:
        raise ValueError("mode must be float or quantized")

    # Preserve CGRA4ML's dense-to-engine reshape and call its original packers.
    from deepsocflow.py.dataflow import reorder_x_q2e_conv, pack_words_into_bytes
    engine = integers.reshape(bundle["n"], bundle["h"], bundle["w"], bundle["ci"])
    hw = SimpleNamespace(ROWS=rows, X_BITS=bits)
    runtime = SimpleNamespace(XN=bundle["n"], XH=bundle["h"], XW=bundle["w"],
                              XL=bundle["l"], CI=bundle["ci"], X_PAD=bundle["x_pad"],
                              CP=bundle["p"], CM_0=bundle["cm_p0"], CM=bundle["cm"])
    passes = reorder_x_q2e_conv(engine, hw, runtime)
    scheduled = b"".join(pack_words_into_bytes(x, bits).tobytes() for x in passes)
    if len(scheduled) != config["defines"]["X_BYTES"]:
        raise ValueError("packed input length differs from compiled X_BYTES")
    metadata = {"shape": list(shape), "layout": "NC" if dense else "NHWC",
                "signed": True, "bits": bits, "frac": frac, "scale": 2.0 ** -frac,
                "rounding": "nearest_even", "saturation": [low, high],
                "mode": mode, "preprocessing": "identity",
                "quantization_applied": mode == "float",
                "packing": "CGRA4ML engine order",
                "logical_bytes": len(scheduled), "sha256": hashlib.sha256(scheduled).hexdigest()}
    return scheduled, metadata


def main(argv=None):
    """`cgtools input`: one scheduled input tensor and its metadata."""
    import numpy as np
    parser = argparse.ArgumentParser(prog="cgtools input", description=__doc__)
    parser.add_argument("input", type=Path, help=".npy tensor, already preprocessed")
    parser.add_argument("output", type=Path, help="scheduled packed X and adjacent .json metadata")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--hardware", type=Path, required=True)
    parser.add_argument("--mode", choices=("float", "quantized"), required=True)
    parser.add_argument("--frac", type=int, help="input fractional bits when the export does not record them")
    parser.add_argument("--preprocessing", choices=("identity",), required=True,
                        help="explicitly confirm no normalization/centering is needed")
    args = parser.parse_args(argv)
    values = np.load(args.input, allow_pickle=False)
    payload, metadata = prepare_array(values, json.loads(args.config.read_text()),
                                      json.loads(args.hardware.read_text()),
                                      args.mode, args.frac)
    metadata["sources"] = {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in (args.input, args.config, args.hardware)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(payload)
    args.output.with_suffix(args.output.suffix + ".json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(f"prepared input: {len(payload)} bytes; sha256={metadata['sha256']}; no inference performed")

