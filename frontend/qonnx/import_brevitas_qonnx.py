"""Parse the observed Brevitas graph patterns from edges, with strict semantics.

Reads only a shape-normalized QONNX graph (no checkpoint, goldens, node names
or bundle count).
"""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from onnx import helper, numpy_helper
from qonnx.core.modelwrapper import ModelWrapper
from qonnx.custom_op.general.quant import quant


def parse(path):
    model = ModelWrapper(str(path))
    graph = model.graph
    if any(n.op_type in ('Resize', 'Concat') or
           (n.op_type == 'Conv' and any(a.name == 'group' and a.i != 1 for a in n.attribute))
           for n in graph.node):
        if __package__:
            from .fixedpoint_graph import parse_graph
        else:
            from fixedpoint_graph import parse_graph
        return parse_graph(path)
    assert len(graph.input) == len(graph.output) == 1
    nodes = {n.output[0]: n for n in graph.node}
    assert len(nodes) == len(graph.node) and all(len(n.output) == 1 for n in graph.node)
    constants = {t.name: numpy_helper.to_array(t) for t in graph.initializer}
    consumers = {}
    for node in graph.node:
        for value in node.input:
            consumers.setdefault(value, []).append(node)
    used, parameters, bundles, outputs = set(), {}, [], []

    def attrs(node):
        used.add(node.output[0])
        assert node.domain in ('', 'qonnx.custom_op.general')
        return {a.name: helper.get_attribute_value(a) for a in node.attribute}

    def next_node(value, kinds, required=True):
        found = [n for n in consumers.get(value, [])
                 if n.op_type in kinds and n.output[0] not in used]
        assert len(found) <= 1, (value, kinds, [n.name for n in found])
        if required:
            assert len(found) == 1, (value, kinds)
        return found[0] if found else None

    def qinfo(node):
        assert node.op_type == 'Quant' and node.domain == 'qonnx.custom_op.general'
        assert attrs(node) == dict(narrow=0, signed=1, rounding_mode=b'ROUND')
        assert len(node.input) == 4
        scale, zero, bits = [constants[v] for v in node.input[1:]]
        assert all(v.size == 1 for v in (scale, zero, bits))
        scale, zero, bits = float(scale.item()), float(zero.item()), float(bits.item())
        assert scale > 0 and zero == 0 and 2 <= bits <= 32 and int(bits) == bits
        frac = -np.log2(scale)
        assert np.isfinite(frac) and int(frac) == frac
        return dict(bits=int(bits), frac=int(frac), signed=True, zero_point=0,
                    rounding='ROUND', narrow=0)

    def parameter(value):
        node = nodes[value]
        if node.op_type == 'Quant':
            q = qinfo(node)
            assert node.input[0] in constants
            arr = quant(*[constants[v] for v in node.input], signed=1,
                        narrow=0, rounding_mode='ROUND')
            np.testing.assert_array_equal(arr * 2.**q['frac'], np.rint(arr * 2.**q['frac']))
            return q, arr
        at = attrs(node)
        q, arr = parameter(node.input[0])
        if node.op_type == 'Transpose':
            assert len(node.input) == 1 and at == dict(perm=[1, 0]) and arr.ndim == 2
            return q, arr.T
        assert node.op_type == 'Unsqueeze' and not at and len(node.input) == 2
        axes = constants[node.input[1]]
        assert axes.dtype.kind in 'iu'
        return q, np.expand_dims(arr, tuple(int(v) for v in axes))

    def activation(source):
        node = next_node(source, ('Quant', 'Relu', 'LeakyRelu'))
        kind, slope = None, 1.0
        if node.op_type != 'Quant':
            at = attrs(node)
            assert list(node.input) == [source]
            kind = 'relu'
            if node.op_type == 'Relu':
                assert not at
                slope = 0.0
            else:
                assert at == dict(alpha=0.125)
                slope = 0.125
            source = node.output[0]
            node = next_node(source, ('Quant',))
        assert node.input[0] == source
        q = qinfo(node)
        result = node.output[0]
        clip = next_node(result, ('Clip',), required=False)
        if slope == 0.125:
            assert clip is not None and not attrs(clip)
            assert len(clip.input) in (2, 3)
            low = float(constants[clip.input[1]].item())
            assert low == -2**(q['bits']-1) * slope * 2.**-q['frac']
            if len(clip.input) == 3 and clip.input[2]:
                assert float(constants[clip.input[2]].item()) == (2**(q['bits']-1)-1)*2.**-q['frac']
            result = clip.output[0]
        else:
            assert clip is None
        return dict(q, kind=kind, slope=slope), result

    input_name = graph.input[0].name
    input_shape = model.get_tensor_shape(input_name)
    assert len(input_shape) == 4 and input_shape[0] == 1 and all(v > 0 for v in input_shape)
    iq_node = next_node(input_name, ('Quant',))
    iq = qinfo(iq_node)
    previous = iq_node.output[0]
    while previous != graph.output[0].name:
        begin = previous
        pad = next_node(previous, ('Pad',), required=False)
        if pad is not None:
            assert attrs(pad) == dict(mode=b'constant')
            assert len(pad.input) in (2, 3) and pad.input[0] == previous
            if len(pad.input) == 3 and pad.input[2]:
                assert float(constants[pad.input[2]].item()) == 0
            padding = constants[pad.input[1]]
            assert padding.dtype.kind in 'iu' and padding.shape == (8,)
            previous = pad.output[0]
        core = next_node(previous, ('Conv', 'MatMul'))
        assert len(core.input) == 2 and core.input[0] == previous
        at = attrs(core)
        wq, weight = parameter(core.input[1])
        index = len(bundles)
        cfg = dict(index=index, kind='conv' if core.op_type == 'Conv' else 'dense',
                   use_bias=False, weight_bits=wq['bits'], weight_frac=wq['frac'],
                   flatten=False, softmax=False, tensors=dict(input=begin, raw=core.output[0]))
        if core.op_type == 'Conv':
            assert pad is not None and weight.ndim == 4
            assert set(at) == {'dilations', 'group', 'kernel_shape', 'pads', 'strides'}
            assert at['dilations'] == [1, 1] and at['group'] == 1 and at['pads'] == [0]*4
            assert at['kernel_shape'] == list(weight.shape[2:])
            shape = model.get_tensor_shape(begin)
            assert shape[1] == weight.shape[1] and len(shape) == 4
            spatial = []
            for size, kernel, stride in zip(shape[2:], at['kernel_shape'], at['strides']):
                assert stride > 0
                total = max(((size + stride - 1)//stride - 1)*stride + kernel - size, 0)
                spatial.append((total//2, total-total//2))
            assert list(padding) == [0, 0, spatial[0][0], spatial[1][0], 0, 0, spatial[0][1], spatial[1][1]]
            cfg.update(kernel_shape=at['kernel_shape'], strides=at['strides'], padding='same')
            parameters[f'b{index}.weight'] = weight.transpose(2, 3, 1, 0)
        else:
            assert pad is None and not at and weight.ndim == 2
            assert model.get_tensor_shape(begin) == [1, weight.shape[0]]
            parameters[f'b{index}.weight'] = weight
        previous = core.output[0]
        bias_add = next_node(previous, ('Add',), required=False)
        if bias_add is not None:
            assert not attrs(bias_add) and len(bias_add.input) == 2
            other = next(v for v in bias_add.input if v != previous)
            bq, bias = parameter(other)
            assert cfg['kind'] == 'conv' and bias.shape == (1, weight.shape[0], 1, 1)
            cfg.update(use_bias=True, bias_bits=bq['bits'], bias_frac=bq['frac'])
            parameters[f'b{index}.bias'] = bias.reshape(-1)
            previous = bias_add.output[0]
        cfg['tensors']['biased'] = previous
        cfg['activation'], previous = activation(previous)
        cfg['tensors']['activated'] = previous
        # A residual Add immediately following this core uses an earlier bundle
        # output. Other consumers are skipped branches and remain for later cores.
        residuals = [n for n in consumers.get(previous, []) if n.op_type == 'Add'
                     and n.output[0] not in used and any(v in outputs for v in n.input)]
        assert len(residuals) <= 1
        if residuals:
            add = residuals[0]
            assert not attrs(add) and len(add.input) == 2
            other = next(v for v in add.input if v != previous)
            assert model.get_tensor_shape(other) == model.get_tensor_shape(previous)
            aq, previous = activation(add.output[0])
            cfg['add'] = dict(source=outputs.index(other), activation=aq)
            cfg['tensors']['added'] = previous
        pool = next_node(previous, ('MaxPool',), required=False)
        if pool is not None:
            at = attrs(pool)
            assert list(pool.input) == [previous]
            assert set(at) == {'ceil_mode', 'dilations', 'kernel_shape', 'pads', 'strides'}
            assert at['ceil_mode'] == 0 and at['dilations'] == [1, 1] and at['pads'] == [0]*4
            pq, previous = activation(pool.output[0])
            cfg['pool'] = dict(kind='max', size=at['kernel_shape'], strides=at['strides'],
                               padding='valid', activation=pq)
            cfg['tensors']['pooled'] = previous
        transpose = next_node(previous, ('Transpose',), required=False)
        if transpose is not None:
            assert attrs(transpose) == dict(perm=[0, 2, 3, 1])
            assert list(transpose.input) == [previous]
            reshape = next_node(transpose.output[0], ('Reshape',))
            assert attrs(reshape) == dict(allowzero=0) and len(reshape.input) == 2
            target = constants[reshape.input[1]]
            shape = model.get_tensor_shape(previous)
            assert list(target) == [1, int(np.prod(shape[1:]))]
            cfg['flatten'] = True
            previous = reshape.output[0]
        softmax = next_node(previous, ('Softmax',), required=False)
        if softmax is not None:
            assert attrs(softmax) == dict(axis=1) and list(softmax.input) == [previous]
            assert len(model.get_tensor_shape(previous)) == 2
            cfg['softmax'] = True
            previous = softmax.output[0]
        cfg['tensors']['output'] = previous
        outputs.append(previous)
        bundles.append(cfg)
    assert used == set(nodes), [(nodes[v].op_type, v) for v in set(nodes)-used]
    spec = dict(source_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                graph_input_shape=input_shape, graph_input_layout='NCHW',
                input_shape=[input_shape[i] for i in (0, 2, 3, 1)],
                input_quant=iq, bundles=bundles, parsed_nodes=len(used))
    return spec, parameters


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('model', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    spec, parameters = parse(args.model)
    args.output.mkdir(parents=True, exist_ok=False)
    np.savez(args.output / 'parameters.npz', **parameters)
    (args.output / 'import.json').write_text(json.dumps(spec, indent=2) + '\n')
    if spec.get('format') == 'qonnx-fixedpoint-graph-v1':
        print(f"Imported {len(spec['nodes'])} nodes with explicit fixed-point graph edges")
    else:
        print(f"Imported {spec['parsed_nodes']} nodes into {len(spec['bundles'])} graph-derived bundles")


if __name__ == '__main__':
    main()
