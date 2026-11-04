"""Exports the small 4-bit U-Net (depthwise/pointwise pairs, two pools, two
upsample/concat joins) for the regression4 hardware, at a chosen batch size,
and checks its depthwise and concat references.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import tensorflow as tf

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "CGRA4ML"))
sys.path.insert(0, str(ROOT))

from deepsocflow import Hardware, export_inference, verify_inference
from deepsocflow.py.utils import BUNDLES
from unet_small_model import build_small_unet, lower_depthwise_kernel
from cgtools.tables import build_concat_transitions


def target_hardware():
    """The `regression4` image's hardware (create_nx_cgra4ml_project.py)."""
    return Hardware(
        processing_elements=(8, 24),
        frequency_mhz=250,
        bits_input=4,
        bits_weights=4,
        bits_sum=20,
        bits_bias=16,
        max_batch_size=64,
        max_channels_in=512,
        max_kernel_size=9,
        max_image_size=512,
        max_n_bundles=64,
        ram_weights_depth=512,
        ram_edges_depth=3584,
        axi_width=128,
        config_baseaddr="B0000000",
        target_cpu_int_bits=32,
        valid_prob=1,
        ready_prob=1,
        data_dir="vectors",
    )


def check_depthwise_references(fixture, kernels):
    for bundle, kernel in zip(fixture.depthwise, kernels):
        core = bundle.core
        np.testing.assert_array_equal(core.w.ftensor.numpy(), lower_depthwise_kernel(kernel))
        expected = tf.nn.depthwise_conv2d(tf.cast(core.x.itensor, tf.float32),
                                         kernel * 8, [1, 1, 1, 1], 'SAME')
        np.testing.assert_array_equal(expected.numpy(), core.y.itensor.numpy())
    print("All five native depthwise reference checks PASS")


def check_concat_references():
    """Compare exported integers with float-domain concat/round/saturate."""
    checks = []
    for consumer in BUNDLES:
        if consumer.concat_act is None:
            continue
        decoder = BUNDLES[consumer.prev_ib].out
        skip = BUNDLES[consumer.concat_source_ib].out
        branches = [skip, decoder] if consumer.skip_first else [decoder, skip]
        real = np.concatenate([
            branch.itensor.numpy().astype(np.float64) * 2.0 ** -branch.frac
            for branch in branches
        ], axis=-1)
        quant = consumer.core.x
        expected = np.clip(np.rint(real * 2.0 ** quant.frac),
                           -(2 ** (quant.bits - 1)), 2 ** (quant.bits - 1) - 1)
        np.testing.assert_array_equal(expected, quant.itensor.numpy())
        if not np.any(expected):
            raise ValueError("degenerate all-zero concat input")
        checks.append(dict(consumer=consumer.ib, shape=list(expected.shape),
                           skip=consumer.concat_source_ib,
                           nonzero=int(np.count_nonzero(expected))))
    if len(checks) != 2:
        raise ValueError("fixture must exercise two concatenations")
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("--sim", choices=["verilator"])
    parser.add_argument("--profile", choices=("default", "grouped"), default="default")
    parser.add_argument("--batch-size", type=int, default=1,
                        help="images per inference (the export's batch dimension)")
    args = parser.parse_args()
    work = args.work_dir.resolve()
    if work.exists() and any(work.iterdir()):
        raise ValueError("use an empty work directory; existing evidence will not be overwritten")
    work.mkdir(parents=True, exist_ok=True)
    model, fixture, kernels = build_small_unet(profile=args.profile)
    hw = target_hardware()
    os.chdir(work)
    hw.export_json()
    hw.export()
    with Path("sources.txt").open("a") as sources:
        sources.write("\n-Wno-SELRANGE\n")
    export_inference(model, hw, batch_size=args.batch_size)
    check_depthwise_references(fixture, kernels)
    config = json.loads(Path("vectors/config.json").read_text())
    transitions, retained, slots = build_concat_transitions(config["bundles"])
    checks = check_concat_references()
    report = dict(reference_checks=checks, transitions=transitions,
                  retained=retained, slot_words=slots,
                  evidence="frontend references only; no RTL or board validation")
    Path("concat_reference_report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if args.sim:
        verify_inference(model, hw, SIM=args.sim)


if __name__ == "__main__":
    main()
