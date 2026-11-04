"""Strict graph import and integer semantics for the supported U-Net operators.

A frontend IR, not a hardware model: execution reads only the graph's
parameters and the input. Only parse_graph needs ONNX.
"""
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import math
from pathlib import Path

import numpy as np

FORMAT = 'qonnx-fixedpoint-graph-v1'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def parse_graph(path):
    import onnx
    from onnx import helper, numpy_helper
    model = onnx.load(path)
    graph = model.graph
    constants = {v.name: numpy_helper.to_array(v) for v in graph.initializer}
    require(len(constants) == len(graph.initializer), 'duplicate initializer')
    require(len(graph.input) == len(graph.output) == 1, 'normalize the deployment interface first')
    shapes = {}
    for value in list(graph.input) + list(graph.output) + list(graph.value_info):
        dims = [d.dim_value for d in value.type.tensor_type.shape.dim]
        require(all(d > 0 for d in dims), 'missing or dynamic shape: ' + value.name)
        shapes[value.name] = dims
    for name, value in constants.items():
        require(np.isfinite(value).all(), 'nonfinite initializer: ' + name)
        shapes[name] = list(value.shape)
    input_name, output_name = graph.input[0].name, graph.output[0].name
    require(len(shapes[input_name]) == 4 and shapes[input_name][0] == 1,
            'expected batch-1 NCHW input')
    known = set(constants) | {input_name}
    descriptors = []
    for node in graph.node:
        require(len(node.output) == 1 and node.output[0] not in known, 'duplicate/multiple node output')
        require(all(v in known for v in node.input if v), 'graph is not topologically ordered')
        output = node.output[0]
        require(output in shapes, 'infer every output shape before import: ' + output)
        attrs = {a.name: helper.get_attribute_value(a) for a in node.attribute}
        require(len(attrs) == len(node.attribute), 'duplicate attribute')
        op, inputs = node.op_type, list(node.input)
        desc = dict(op=op, inputs=inputs, output=output, shape=shapes[output])
        if op == 'Quant':
            require(node.domain in ('onnx.brevitas', 'qonnx.custom_op.general'), 'unsupported Quant domain')
            require(len(inputs) == 4 and all(v in constants for v in inputs[1:]), 'dynamic Quant parameters')
            require(set(attrs) == {'signed', 'narrow', 'rounding_mode'} and
                    attrs['signed'] == 1 and attrs['narrow'] in (0, 1) and
                    attrs['rounding_mode'] == b'ROUND', 'unsupported Quant semantics')
            values = [constants[v] for v in inputs[1:]]
            require(all(v.size == 1 for v in values), 'per-channel Quant is unsupported')
            scale, zero, bits = [float(v.item()) for v in values]
            require(scale > 0 and zero == 0 and bits.is_integer() and 2 <= bits <= 32,
                    'invalid fixed-point Quant parameters')
            frac = -math.log2(scale)
            require(frac.is_integer() and -31 <= frac <= 31, 'Quant scale must be a bounded power of two')
            require(shapes[inputs[0]] == shapes[output], 'Quant shape mismatch')
            desc['quant'] = dict(bits=int(bits), frac=int(frac), signed=True,
                                 narrow=attrs['narrow'], rounding='ROUND', zero_point=0)
        else:
            require(node.domain == '', 'unsupported operator domain')
            if op == 'Conv':
                require(len(inputs) in (2, 3), 'invalid Conv inputs')
                require(set(attrs) == {'dilations', 'group', 'kernel_shape', 'pads', 'strides'},
                        'unsupported Conv attributes')
                x, w, y = shapes[inputs[0]], shapes[inputs[1]], shapes[output]
                require(len(x) == len(w) == len(y) == 4 and x[0] == y[0] == 1,
                        'Conv requires batch-1 rank four')
                group = attrs['group']; kernel = attrs['kernel_shape']
                require(group == 1 or group == x[1] == w[0], 'only dense or multiplier-one depthwise Conv')
                require(group > 0 and x[1] == w[1] * group and w[0] % group == 0,
                        'inconsistent Conv groups')
                require(kernel == w[2:] and kernel in ([1, 1], [3, 3]), 'unsupported Conv kernel')
                require(attrs['strides'] == attrs['dilations'] == [1, 1], 'unsupported Conv stride/dilation')
                require(attrs['pads'] == [kernel[0]//2, kernel[1]//2]*2,
                        'Conv requires symmetric SAME padding')
                require(y == [1, w[0], x[2], x[3]], 'Conv output shape mismatch')
                if len(inputs) == 3:
                    require(shapes[inputs[2]] == [w[0]], 'Conv bias shape mismatch')
                desc['attributes'] = attrs
            elif op == 'LeakyRelu':
                require(len(inputs) == 1 and set(attrs) == {'alpha'}, 'unsupported LeakyRelu attributes')
                alpha = float(attrs['alpha']); require(math.isfinite(alpha) and 0 <= alpha <= 1, 'invalid slope')
                ratio = Fraction(alpha)
                denominator = ratio.denominator
                require(denominator <= 65536 and denominator & (denominator-1) == 0,
                        'LeakyRelu slope must be an exact bounded binary rational')
                require(shapes[inputs[0]] == shapes[output], 'LeakyRelu shape mismatch')
                desc['slope'] = dict(numerator=ratio.numerator, shift=denominator.bit_length()-1)
            elif op == 'MaxPool':
                require(len(inputs) == 1 and attrs == dict(ceil_mode=0, dilations=[1, 1],
                        kernel_shape=[2, 2], pads=[0, 0, 0, 0], strides=[2, 2]), 'unsupported MaxPool')
                x = shapes[inputs[0]]
                require(len(x) == 4 and x[2] % 2 == x[3] % 2 == 0 and
                        shapes[output] == x[:2]+[x[2]//2, x[3]//2], 'MaxPool shape mismatch')
            elif op == 'Resize':
                require(len(inputs) == 3 and inputs[1] == '' and inputs[2] in constants,
                        'Resize requires constant scales and absent ROI')
                require(attrs == dict(coordinate_transformation_mode=b'asymmetric',
                        cubic_coeff_a=-0.75, mode=b'nearest', nearest_mode=b'floor'), 'unsupported Resize sampling')
                scales = constants[inputs[2]]
                require(scales.shape == (4,) and np.array_equal(scales, [1, 1, 2, 2]), 'unsupported Resize scale')
                x = shapes[inputs[0]]
                require(len(x) == 4 and shapes[output] == x[:2]+[2*x[2], 2*x[3]], 'Resize shape mismatch')
                desc['factors'] = [2, 2]
            elif op == 'Concat':
                require(len(inputs) == 2 and attrs == {'axis': 1}, 'unsupported Concat')
                x, y = [shapes[v] for v in inputs]
                require(len(x) == len(y) == 4 and x[:1]+x[2:] == y[:1]+y[2:] and
                        shapes[output] == [x[0], x[1]+y[1], *x[2:]], 'Concat shape mismatch')
            else:
                raise ValueError('unsupported graph operator: ' + op)
        descriptors.append(desc); known.add(output)
    require(output_name in known and descriptors[-1]['output'] == output_name, 'missing terminal output')
    # Every constant remains explicit; no reference context participates in import.
    return dict(format=FORMAT, source_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                input=input_name, output=output_name, input_shape=shapes[input_name],
                output_shape=shapes[output_name], nodes=descriptors), constants


def bounded(values, multiplier=1):
    maximum = max(abs(int(values.min(initial=0))), abs(int(values.max(initial=0))))
    require(maximum * multiplier < 2**62, 'integer operation exceeds the frontend safety bound')


def shift_round(values, shift):
    """Exact signed round-to-nearest, ties-to-even, without a floating cast."""
    values = np.asarray(values, dtype=np.int64)
    require(-61 <= shift <= 61, 'unsupported rescaling shift')
    if shift <= 0:
        bounded(values, 1 << -shift)
        return values << -shift
    denominator = 1 << shift
    quotient, remainder = np.divmod(values, denominator)
    halfway = denominator // 2
    return quotient + ((remainder > halfway) | ((remainder == halfway) & ((quotient & 1) != 0)))


@dataclass
class Tensor:
    codes: np.ndarray
    frac: int

    def real(self):
        return np.ldexp(self.codes.astype(np.float64), -self.frac)


def quantize(value, quant):
    bits, frac = quant['bits'], quant['frac']
    low, high = -(1 << (bits-1)) + quant['narrow'], (1 << (bits-1))-1
    if isinstance(value, Tensor):
        codes = shift_round(value.codes, value.frac-frac)
    else:
        array = np.asarray(value)
        require(np.isfinite(array).all(), 'nonfinite quantizer input')
        # Float input/parameter values enter fixed-point exactly once. Clipping
        # before int64 conversion also avoids overflow on large finite inputs.
        codes = np.clip(np.rint(np.ldexp(array.astype(np.float64), frac)), low, high).astype(np.int64)
    return Tensor(np.clip(codes, low, high), frac)


def convolution(x, weight, bias, attributes):
    require(isinstance(x, Tensor) and isinstance(weight, Tensor), 'Conv operands must be quantized')
    data, kernel = x.codes, weight.codes
    groups = attributes['group']; kh, kw = kernel.shape[2:]
    bounded(data); bounded(kernel)
    limit = max(abs(int(data.min())), abs(int(data.max()))) * max(abs(int(kernel.min())), abs(int(kernel.max()))) * int(np.prod(kernel.shape[1:]))
    require(limit < 2**62, 'Conv accumulation can overflow')
    padded = np.pad(data, ((0,0), (0,0), (kh//2,kh//2), (kw//2,kw//2)))
    windows = np.lib.stride_tricks.sliding_window_view(padded, (kh,kw), axis=(2,3))
    if groups == 1:
        codes = np.einsum('nchwkl,ockl->nohw', windows, kernel, dtype=np.int64, optimize=True)
    else:
        codes = np.einsum('nchwkl,ckl->nchw', windows, kernel[:,0], dtype=np.int64, optimize=True)
    frac = x.frac + weight.frac
    if bias is not None:
        require(isinstance(bias, Tensor), 'Conv bias must be quantized')
        target = max(frac, bias.frac)
        codes = shift_round(codes, frac-target)
        aligned = shift_round(bias.codes, bias.frac-target).reshape(1,-1,1,1)
        bounded(codes, 2); bounded(aligned, 2)
        codes = codes + aligned; frac = target
    return Tensor(codes, frac)


def evaluate(spec, parameters, value, return_context=False):
    require(spec.get('format') == FORMAT, 'unknown graph format')
    require(list(value.shape) == spec['input_shape'], 'input shape mismatch')
    context = dict(parameters); context[spec['input']] = value
    for node in spec['nodes']:
        op, names = node['op'], node['inputs']
        operands = [context[v] for v in names if v]
        if op == 'Quant':
            result = quantize(operands[0], node['quant'])
        elif op == 'Conv':
            result = convolution(operands[0], operands[1], operands[2] if len(operands)==3 else None, node['attributes'])
        else:
            x = operands[0]; require(isinstance(x, Tensor), op+' input must be fixed-point')
            if op == 'LeakyRelu':
                numerator, shift = node['slope']['numerator'], node['slope']['shift']
                bounded(x.codes, max(numerator, 1 << shift))
                result = Tensor(np.where(x.codes < 0, x.codes*numerator, x.codes << shift), x.frac+shift)
            elif op == 'MaxPool':
                n,c,h,w = x.codes.shape
                result = Tensor(x.codes.reshape(n,c,h//2,2,w//2,2).max(axis=(3,5)), x.frac)
            elif op == 'Resize':
                result = Tensor(x.codes.repeat(2,axis=2).repeat(2,axis=3), x.frac)
            elif op == 'Concat':
                y = operands[1]; require(isinstance(y, Tensor), 'Concat input must be fixed-point')
                frac = max(x.frac, y.frac)
                result = Tensor(np.concatenate((shift_round(x.codes,x.frac-frac),
                                                shift_round(y.codes,y.frac-frac)),axis=1), frac)
            else:
                raise ValueError('unsupported graph operator: '+op)
        require(list(result.codes.shape) == node['shape'], 'execution shape mismatch: '+node['output'])
        context[node['output']] = result
    return context if return_context else context[spec['output']]
