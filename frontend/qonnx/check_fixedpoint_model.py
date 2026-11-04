"""Checks the fixed-point graph of a model against independent QONNX references.

Imports and plans a shape-normalized model, runs its integer graph on each
input (float NCHW) and compares every node with the QONNX context and the
output with the expected int32 NHWC codes; writes import.json, parameters.npz,
the lowering plan and acceptance.json. Needs at least two distinct inputs.
"""
import argparse
import hashlib
import json
from pathlib import Path
import platform

import numpy as np

from .fixedpoint_graph import evaluate, parse_graph, require
from .plan_fixedpoint_graph import plan_graph


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('model', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--input', type=Path, action='append', required=True)
    parser.add_argument('--reference', type=Path, action='append', required=True)
    parser.add_argument('--expected-output', type=Path, action='append', required=True)
    parser.add_argument('--expected-nodes', type=int, required=True)
    parser.add_argument('--expected-output-words', type=int, required=True)
    parser.add_argument('--expected-output-frac', type=int, required=True)
    args = parser.parse_args()
    require(len(args.input) == len(args.reference) == len(args.expected_output),
            'each input needs one context and terminal reference')
    require(len(args.input) >= 2, 'full-model acceptance requires two inputs')
    require(not args.output.exists(), 'use a fresh acceptance directory')
    sources = [Path(__file__), Path(__file__).with_name('fixedpoint_graph.py'),
               Path(__file__).with_name('plan_fixedpoint_graph.py')]
    paths = [args.model, *args.input, *args.reference, *args.expected_output, *sources]
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    pins = {str(p.resolve()): sha(p) for p in paths}  # Missing inputs fail before execution.
    require(len({sha(p) for p in args.input}) >= 2, 'inputs must include distinct values')
    args.output.mkdir(parents=True)
    (args.output/'input-pins.json').write_text(json.dumps(pins, indent=2)+'\n')
    spec, parameters = parse_graph(args.model)
    require(len(spec['nodes']) == args.expected_nodes, 'unexpected graph node count')
    plan = plan_graph(spec, parameters)
    (args.output/'import.json').write_text(json.dumps(spec, indent=2)+'\n')
    np.savez(args.output/'parameters.npz', **parameters)
    (args.output/'lowering-plan.json').write_text(json.dumps(plan, indent=2)+'\n')
    cases = []
    for input_path, reference_path, expected_path in zip(args.input, args.reference, args.expected_output):
        context = evaluate(spec, parameters, np.load(input_path, allow_pickle=False), return_context=True)
        checks = []
        with np.load(reference_path, allow_pickle=False) as reference:
            for node in spec['nodes']:
                name = node['output']; value = context[name]
                require(value.frac == plan['tensor_fracs'][name], 'schedule fraction mismatch: '+name)
                np.testing.assert_array_equal(value.real(), reference[name], err_msg=name)
                checks.append(dict(tensor=name, elements=int(value.codes.size), frac=value.frac))
        result = context[spec['output']]
        require(result.frac == args.expected_output_frac, 'unexpected terminal fraction')
        require(result.codes.size == args.expected_output_words, 'unexpected terminal size')
        require(np.all((result.codes >= -(2**31)) & (result.codes < 2**31)), 'terminal exceeds int32')
        np.testing.assert_array_equal(result.codes.transpose(0,2,3,1),
                                      np.load(expected_path, allow_pickle=False))
        cases.append(dict(input=str(input_path), nodes=checks, output_words=int(result.codes.size)))
        print(input_path.name, 'PASS', len(checks), 'nodes;', result.codes.size, 'terminal integers', flush=True)
        del context
    require(all(sha(Path(p)) == pin for p,pin in pins.items()), 'input/source changed during execution')
    report = dict(status='passed', scope='integer graph and schedule metadata only',
                  python=platform.python_version(), numpy=np.__version__, cases=cases,
                  input_pins=pins)
    (args.output/'acceptance.json').write_text(json.dumps(report, indent=2)+'\n')


if __name__ == '__main__':
    main()
