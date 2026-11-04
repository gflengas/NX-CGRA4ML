"""Makes independent QONNX references for check_fixedpoint_model.

Runs a prepared graph (one NCHW image input, one NCHW output) with QONNX's own
executor on chosen images and writes, per image, what the checker reads: the
NCHW input, the full execution context (every tensor by name) and the final
output as int32 NHWC codes at the final quantizer's fractional bits.

    python3 frontend/qonnx/make_references.py prepared.onnx images.npy refs/ \\
        --images 0 1 --output-frac 11

images.npy holds float32 NHWC images, preprocessed as the model expects.
"""
import argparse
from pathlib import Path

import numpy as np
from qonnx.core.modelwrapper import ModelWrapper
from qonnx.core.onnx_exec import execute_onnx


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('model', type=Path)
    parser.add_argument('images', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--images', dest='indices', type=int, nargs='+', required=True)
    parser.add_argument('--output-frac', type=int, required=True)
    args = parser.parse_args()
    model = ModelWrapper(str(args.model))
    assert len(model.graph.input) == len(model.graph.output) == 1, 'one input and one output'
    images = np.load(args.images)
    assert images.ndim == 4 and images.dtype == np.float32, 'images must be float32 NHWC'
    args.output.mkdir(parents=True, exist_ok=False)
    for index in args.indices:
        x = np.ascontiguousarray(images[index:index + 1].transpose(0, 3, 1, 2))
        context = execute_onnx(model, {model.graph.input[0].name: x},
                               return_full_exec_context=True)
        y = context[model.graph.output[0].name]
        assert y.ndim == 4, 'output must be NCHW'
        codes = y.transpose(0, 2, 3, 1) * 2.0**args.output_frac
        assert np.array_equal(codes, np.round(codes)), 'output not on the --output-frac grid'
        np.save(args.output / f'{index}-nchw.npy', x)
        np.savez(args.output / f'{index}-context.npz', **context)
        np.save(args.output / f'{index}-output-int32-nhwc.npy', codes.astype(np.int32))
        print(f'image {index}: {len(context)} tensors, output {codes.shape}')


if __name__ == '__main__':
    main()
