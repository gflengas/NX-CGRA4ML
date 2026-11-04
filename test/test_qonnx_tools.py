"""frontend/qonnx/prepare_graph.py and make_references.py on a small synthetic graph:
a bias exported as a graph input, a constant shape subgraph, an NCHW convolution."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper

ROOT = Path(__file__).resolve().parents[1]
WEIGHT = np.arange(-6, 6, dtype=np.float32).reshape(3, 2, 2, 1)


def model():
    """X [1,2,4,4] -> Conv(W, bias input) -> Reshape to a folded constant shape."""
    bias = numpy_helper.from_array(np.array([1, -2, 3], np.float32), 'bias')
    weight = numpy_helper.from_array(WEIGHT, 'weight')
    nodes = [
        helper.make_node('Conv', ['x', 'weight', 'bias'], ['conv'], kernel_shape=[2, 1]),
        helper.make_node('Shape', ['conv'], ['shape']),
        helper.make_node('Reshape', ['conv', 'shape'], ['y']),
    ]
    graph = helper.make_graph(
        nodes, 'synthetic',
        [helper.make_tensor_value_info('x', TensorProto.FLOAT, [1, 2, 4, 4]),
         helper.make_tensor_value_info('bias', TensorProto.FLOAT, [3])],
        [helper.make_tensor_value_info('y', TensorProto.FLOAT, None)],
        initializer=[bias, weight])
    return helper.make_model(graph, opset_imports=[helper.make_opsetid('', 17)])


def run(script, *args):
    return subprocess.run([sys.executable, str(ROOT / 'frontend/qonnx' / script), *map(str, args)],
                          capture_output=True, text=True, check=True).stdout


class QonnxToolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        onnx.save(model(), self.root / 'model.onnx')

    def tearDown(self):
        self.directory.cleanup()

    def test_prepare_binds_inputs_folds_constants_and_infers_shapes(self):
        run('prepare_graph.py', self.root / 'model.onnx', self.root / 'prepared.onnx')
        graph = onnx.load(self.root / 'prepared.onnx').graph
        self.assertEqual([i.name for i in graph.input], ['x'])
        self.assertEqual([n.op_type for n in graph.node], ['Conv', 'Reshape'])
        self.assertEqual([d.dim_value for d in graph.output[0].type.tensor_type.shape.dim],
                         [1, 3, 3, 4])
        with self.assertRaises(subprocess.CalledProcessError):
            run('prepare_graph.py', self.root / 'model.onnx', self.root / 'prepared.onnx')

    def test_references_hold_context_and_nhwc_codes(self):
        run('prepare_graph.py', self.root / 'model.onnx', self.root / 'prepared.onnx')
        images = np.arange(2 * 4 * 4 * 2, dtype=np.float32).reshape(2, 4, 4, 2) % 7 - 3
        np.save(self.root / 'images.npy', images)
        run('make_references.py', self.root / 'prepared.onnx', self.root / 'images.npy',
            self.root / 'refs', '--images', 0, 1, '--output-frac', 1)
        for index in (0, 1):
            x = images[index].transpose(2, 0, 1)
            expected = np.stack([sum(WEIGHT[o, c, k, 0] * x[c, k:k + 3, :] for c in range(2)
                                     for k in range(2)) for o in range(3)])
            expected += np.array([1, -2, 3])[:, None, None]
            refs = self.root / 'refs'
            np.testing.assert_array_equal(np.load(refs / f'{index}-nchw.npy'), x[None])
            codes = np.load(refs / f'{index}-output-int32-nhwc.npy')
            self.assertEqual(codes.dtype, np.int32)
            np.testing.assert_array_equal(codes[0], expected.transpose(1, 2, 0) * 2)
            with np.load(refs / f'{index}-context.npz') as context:
                self.assertLessEqual({'x', 'conv', 'y'}, set(context))
                np.testing.assert_array_equal(context['y'], expected[None])

    def test_references_reject_outputs_off_the_frac_grid(self):
        run('prepare_graph.py', self.root / 'model.onnx', self.root / 'prepared.onnx')
        np.save(self.root / 'images.npy', np.full((1, 4, 4, 2), 0.25, np.float32))
        with self.assertRaises(subprocess.CalledProcessError):
            run('make_references.py', self.root / 'prepared.onnx', self.root / 'images.npy',
                self.root / 'refs', '--images', 0, '--output-frac', 0)


if __name__ == '__main__':
    unittest.main()
