"""Plan graph-to-convolution lowering without emitting deployment artifacts.

Node indices refer to the fixedpoint_graph IR. Each post-operation stays
explicit, so no quantization or skip value is lost to fusion.
"""
import argparse
import json
import math
from pathlib import Path

from .fixedpoint_graph import FORMAT, require


def plan_graph(spec, parameter_names):
    require(spec.get('format') == FORMAT, 'unknown graph format')
    constants = set(parameter_names)
    known = constants | {spec['input']}
    formats = {spec['input']: None}
    shapes = {spec['input']: spec['input_shape']}
    owners = {spec['input']: -1}
    last_use = {}
    static, preamble, bundles = [], [], []
    dynamic = []
    consumers = {}
    for index, node in enumerate(spec['nodes']):
        op, names, out = node['op'], node['inputs'], node['output']
        require(out not in known, 'duplicate graph value')
        require(all(name in known for name in names if name), 'non-topological graph')
        known.add(out)
        shapes[out] = node['shape']
        if op == 'Quant' and names[0] in constants:
            constants.add(out)
            formats[out] = node['quant']['frac']
            static.append(index)
            continue
        dynamic.append(index)
        operands = names[:1] if op in ('Quant', 'Resize') else names
        if op == 'Conv':
            require(all(name in constants for name in names[1:]), 'dynamic Conv parameters')
            require(formats.get(names[0]) is not None and
                    all(name in formats for name in names[1:]), 'unquantized Conv operand')
            require(owners[names[0]] == len(bundles)-1, 'Conv input is not the preceding computed stage')
            raw_frac = formats[names[0]] + formats[names[1]]
            frac = max(raw_frac, formats[names[2]]) if len(names) == 3 else raw_frac
            bundles.append(dict(index=len(bundles), conv=index, postprocess=[],
                                transitions=[], input=names[0], output=out,
                                input_shape=shapes[names[0]], conv_shape=node['shape'],
                                raw_frac=raw_frac, biased_frac=frac))
        elif op == 'Quant':
            frac = node['quant']['frac']
        elif op == 'LeakyRelu':
            require(formats.get(names[0]) is not None, 'unquantized activation')
            frac = formats[names[0]] + node['slope']['shift']
        elif op in ('MaxPool', 'Resize'):
            require(formats.get(names[0]) is not None, 'unquantized spatial operation')
            frac = formats[names[0]]
        elif op == 'Concat':
            require(len(names) == 2 and all(formats.get(n) is not None for n in names),
                    'Concat requires two quantized branches')
            require(len(bundles) > 0 and sum(owners[n] == len(bundles)-1 for n in names) == 1,
                    'Concat requires current decoder and earlier skip')
            frac = max(formats[n] for n in names)
        else:
            raise ValueError('unsupported schedule operator: ' + op)
        owner = len(bundles)-1
        if op != 'Conv':
            if not bundles:
                require(op == 'Quant', 'only input quantization may precede the first Conv')
                preamble.append(index)
            else:
                bundle = bundles[-1]
                require(op == 'Concat' or owners[names[0]] == owner,
                        'postoperation reads a different stage')
                transition = bool(bundle['transitions']) or op in ('MaxPool', 'Resize', 'Concat')
                bundle['transitions' if transition else 'postprocess'].append(index)
                bundle['output'] = out
        for name in operands:
            if name and name not in constants:
                consumers.setdefault(name, []).append(index)
                last_use[name] = index
        formats[out] = frac
        owners[out] = owner
    require(bundles and spec['output'] == bundles[-1]['output'], 'output is not the terminal stage')
    require(preamble, 'missing hardware input quantization')
    ordered = preamble + [i for b in bundles for i in
                          [b['conv']] + b['postprocess'] + b['transitions']]
    require(ordered == dynamic, 'schedule changed dynamic node order')
    require(sorted(static + ordered) == list(range(len(spec['nodes']))), 'incomplete node coverage')
    retained = []
    for name, uses in consumers.items():
        producer = owners[name]
        if producer < 0:
            continue
        # Immediate Conv input is packed normally. Any later use or branch
        # inside a later stage must keep the exact named intermediate value.
        cross = [i for i in uses if owners[spec['nodes'][i]['output']] > producer and
                 not (spec['nodes'][i]['op'] == 'Conv' and
                      owners[spec['nodes'][i]['output']] == producer+1)]
        if cross:
            require(all(spec['nodes'][i]['op'] == 'Concat' for i in cross),
                    'only concat skip retention is supported')
            node_index = next(i for i,n in enumerate(spec['nodes']) if n['output'] == name)
            retained.append(dict(tensor=name, producer=producer, produced_at=node_index,
                                 last_use=last_use[name], consumers=cross, shape=shapes[name],
                                 words=math.prod(shapes[name]), frac=formats[name]))
    # Assign slots using exact operation lifetimes, not pooled bundle outputs.
    slots = []
    for entry in sorted(retained, key=lambda v: v['produced_at']):
        slot = next((i for i,s in enumerate(slots) if s['last_use'] < entry['produced_at']), len(slots))
        if slot == len(slots):
            slots.append(dict(words=0, last_use=-1))
        slots[slot] = dict(words=max(slots[slot]['words'], entry['words']), last_use=entry['last_use'])
        entry['slot'] = slot
    return dict(format='qonnx-convolution-plan-v1', source_sha256=spec['source_sha256'],
                parameter_nodes=static, input_nodes=preamble, bundles=bundles,
                retained=retained, retained_slot_words=[s['words'] for s in slots],
                tensor_fracs=formats, output=spec['output'], output_frac=formats[spec['output']])


def bundle_postprocess(spec, bundle):
    """Translate preserved graph nodes into the explicit C metadata contract."""
    operations = []
    frac, bits = bundle['biased_frac'], 32
    for index in bundle['postprocess']:
        node = spec['nodes'][index]
        if node['op'] == 'Quant':
            operations.append(dict(op='Quant', quant=dict(node['quant'])))
            frac, bits = node['quant']['frac'], node['quant']['bits']
        elif node['op'] == 'LeakyRelu':
            operations.append(dict(op='LeakyRelu', slope=dict(node['slope'])))
            frac += node['slope']['shift']
        else:
            raise ValueError('unsupported graph postprocess: '+node['op'])
    require([op['op'] for op in operations] in ([], ['Quant'], ['Quant','LeakyRelu','Quant']),
            'unsupported graph postprocess sequence')
    return dict(fixedpoint_postprocess=dict(input_frac=bundle['biased_frac'], operations=operations),
                output_quant=dict(bits=bits, frac=frac))


def main():
    import numpy as np
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('imported', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    spec = json.loads((args.imported/'import.json').read_text())
    with np.load(args.imported/'parameters.npz', allow_pickle=False) as parameters:
        plan = plan_graph(spec, parameters.files)
    args.output.write_text(json.dumps(plan, indent=2)+'\n')


if __name__ == '__main__':
    main()
