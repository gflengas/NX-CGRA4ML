"""Prepares an exported QONNX graph for the importer, changing no quantizer.

Binds parameter graph inputs to their initializers (Brevitas exports biases as
graph inputs), leaving one graph input; folds constant subgraphs (the shape
arithmetic Brevitas exports around Reshape and Pad; Quant nodes stay); infers
a static shape for every tensor (QONNX InferShapes).

    python3 frontend/qonnx/prepare_graph.py model.onnx prepared.onnx
"""
import argparse
from pathlib import Path

from qonnx.core.modelwrapper import ModelWrapper
from qonnx.transformation.fold_constants import FoldConstants
from qonnx.transformation.general import RemoveStaticGraphInputs, RemoveUnusedTensors
from qonnx.transformation.infer_shapes import InferShapes


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('model', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    assert not args.output.exists(), f'{args.output} exists'
    model = ModelWrapper(str(args.model))
    for step in (RemoveStaticGraphInputs(), InferShapes(), FoldConstants(), InferShapes(),
                 RemoveUnusedTensors()):
        model = model.transform(step)
    graph = model.graph
    assert len(graph.input) == 1, [i.name for i in graph.input]
    for value in [*graph.input, *graph.value_info, *graph.output]:
        tensor = value.type.tensor_type  # Scalars have a shape with no dimensions.
        assert tensor.HasField('shape') and all(d.dim_value > 0 for d in tensor.shape.dim), \
            f'{value.name}: no static shape'
    model.save(str(args.output))
    print(f'Prepared {len(graph.node)} nodes; input {graph.input[0].name}')


if __name__ == '__main__':
    main()
