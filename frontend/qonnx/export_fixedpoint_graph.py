"""Exports a fixed-point graph as a CGRA4ML export.

Plans the graph into accelerator bundles, packs weights, inputs and expected
outputs with upstream's packers, and writes vectors/ in the computed reference
format: every expected value comes from the integer graph, not a simulation.
A batch of N images runs the batch-1 graph per image.
"""
import hashlib
import json
import math
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from frontend.qonnx.fixedpoint_graph import Tensor, convolution, evaluate, require
from frontend.qonnx.plan_fixedpoint_graph import plan_graph, bundle_postprocess


def export_graph(spec, parameters, input_nhwc, output, hardware_path, reference=None):
    from deepsocflow import Hardware
    from deepsocflow.py.dataflow import (get_runtime_params, create_headers,
        reorder_w_q2e_conv, reorder_b_q2e_conv, reorder_x_q2e_conv,
        reorder_y_q2e_conv, pack_words_into_bytes)
    from cgtools.tables import (build_concat_transitions, emit_fixedpoint_postprocess,
        emit_pool_postprocess)

    # A batch of N images: the batch-1 graph runs per image and the node
    # outputs are stacked on the batch axis (the hardware batches through each
    # bundle's n, sharing the weight stream).
    require(input_nhwc.dtype == np.float32 and input_nhwc.ndim == 4 and
            list(input_nhwc.transpose(0,3,1,2).shape[1:]) == spec['input_shape'][1:],
            'graph export requires float32 NHWC input')
    plan = plan_graph(spec, parameters)
    contexts = [evaluate(spec, parameters, input_nhwc[i:i+1].transpose(0,3,1,2), return_context=True)
                for i in range(input_nhwc.shape[0])]
    context = dict(contexts[0])
    dynamic = {spec['input']}  # tensors that depend on the input (not weights)
    for node in spec['nodes']:
        if any(name in dynamic for name in node['inputs']):
            dynamic.add(node['output'])
    for name in dynamic:
        values = [c[name] for c in contexts]
        if isinstance(values[0], Tensor):
            require(len({v.frac for v in values}) == 1, 'batch images differ in fraction: '+name)
            context[name] = Tensor(np.concatenate([v.codes for v in values]), values[0].frac)
    checks = []
    if reference is not None:
        # The reference context covers the first image.
        for node in spec['nodes']:
            value = contexts[0][node['output']]
            np.testing.assert_array_equal(value.real(), reference[node['output']], err_msg=node['output'])
            checks.append(dict(tensor=node['output'],elements=int(value.codes.size),frac=value.frac))
    hw = Hardware.from_json(str(hardware_path))
    require(hw.X_BITS == hw.K_BITS == 8 and hw.Y_BITS == hw.Y_OUT_BITS == hw.B_BITS == 32,
            'fixed-point graph export requires the qualified 8/8/32 profile')
    require(len(plan['bundles']) <= hw.MAX_N_BUNDLES, 'graph exceeds hardware bundle limit')
    output = Path(output)
    output.mkdir(parents=True,exist_ok=False)
    previous = Path.cwd()
    try:
        os.chdir(output)
        hw.export_json(); hw.export()
    finally:
        os.chdir(previous)
    vectors = output/'vectors'; vectors.mkdir(exist_ok=True)
    bundles, weights, biases, inputs, raw_reports = [], [], [], [], []
    bias_offset = 0
    core = SimpleNamespace(type='conv',strides=(1,1),padding='same')
    post_names = {}
    for stage in plan['bundles']:
        index = stage['index']; node = spec['nodes'][stage['conv']]
        x, w = (context[name] for name in node['inputs'][:2])
        bias = context[node['inputs'][2]] if len(node['inputs']) == 3 else None
        require(isinstance(x,Tensor) and isinstance(w,Tensor), 'unquantized hardware operand')
        require(np.all((x.codes >= -128) & (x.codes <= 127)) and
                np.all((w.codes >= -128) & (w.codes <= 127)), 'hardware operand exceeds signed byte')
        transitions = [spec['nodes'][i] for i in stage['transitions']]
        kinds = [n['op'] for n in transitions]
        require(kinds in ([],['MaxPool'],['MaxPool','Quant'],['Resize','Quant','Concat','Quant']),
                'unsupported graph transition sequence')
        post_name = spec['nodes'][stage['postprocess'][-1]]['output'] if stage['postprocess'] else node['output']
        post_names[post_name] = index
        pooled = transitions[0] if kinds and kinds[0] == 'MaxPool' else None
        value = context[transitions[-1]['output'] if pooled else post_name]
        pool = (SimpleNamespace(pool_layer=SimpleNamespace(pool_size=(2,2),strides=(2,2),padding='valid'))
                if pooled else None)
        xn,xci,xh,xw = x.codes.shape; co,_,kh,kw = w.codes.shape
        if node['attributes']['group'] == 1:
            dense = w.codes
        else:
            require(node['attributes']['group'] == xci == co, 'unsupported depthwise multiplier')
            dense = np.zeros((co,xci,kh,kw),dtype=np.int64)
            dense[np.arange(co),np.arange(co)] = w.codes[:,0]
        runtime = get_runtime_params(hw,(kh,kw,xci,co),(xn,xh,xw,xci),
                                     tuple(value.codes.transpose(0,2,3,1).shape),core,pool,False)
        runtime = create_headers(hw,runtime); r = runtime
        require(kh <= hw.KH_MAX and kw <= hw.KW_MAX and xh <= hw.XH_MAX and
                xw <= hw.XW_MAX and xn <= hw.XN_MAX and xci <= hw.CI_MAX, 'hardware shape bounds')
        # Each pass runs whole: its channels x input width fill the edge RAM.
        require(kh == 1 or (r.CM_0 if r.CP == 1 else max(r.CM_0,r.CM))*xw <= hw.RAM_EDGES_DEPTH,
                'edge RAM bound exceeded')
        require(hw.X_BITS+hw.K_BITS+math.ceil(math.log2(kh*kw*r.CM)) <= hw.Y_BITS,
                'accumulator width exceeded')
        we = reorder_w_q2e_conv(dense.transpose(2,3,1,0),hw,r)
        xe = reorder_x_q2e_conv(x.codes.transpose(0,2,3,1),hw,r)
        wb = b''.join(pack_words_into_bytes(part.flatten(),hw.K_BITS).tobytes() for part in we)
        xb = b''.join(pack_words_into_bytes(part,hw.X_BITS).tobytes() for part in xe)
        weights.append(wb); inputs.append(xb)
        be = reorder_b_q2e_conv(bias.codes,hw,r) if bias is not None else np.array([],dtype=np.int64)
        require(np.all((be >= -(2**31)) & (be < 2**31)), 'bias exceeds int32 storage')
        biases.append(be.astype('<i4').tobytes())
        metadata = bundle_postprocess(spec,stage)
        if pooled and len(transitions) == 2:
            metadata['prepool_quant'] = dict(metadata['output_quant'])
            q = transitions[1]['quant']
            metadata['output_quant'] = dict(bits=q['bits'],frac=q['frac'])
            metadata['fixedpoint_pool_postprocess'] = dict(input_frac=metadata['prepool_quant']['frac'],
                operations=[dict(op='Quant',quant=dict(q))])
        raw_frac = x.frac+w.frac
        require(0 <= stage['biased_frac']-raw_frac <= 30 and
                (bias is None or 0 <= stage['biased_frac']-bias.frac <= 30), 'unsupported bias alignment')
        b = dict(n=xn,l=r.XL,kw=kw,coe=r.CO_PRL,h=xh,w=xw,ci=xci,co=co,
                 w_kw2=xw-kw//2,t=r.IT,p=r.CP,cm=r.CM,cm_p0=r.CM_0,
                 on=r.ON,oh=r.OH,ow=r.OW,oc=r.OC,ch=r.CYH,cw=r.CYW,ph=r.PYH,pw=r.PYW,
                 pkh=r.PKH,pkw=r.PKW,psh=r.PSH,psw=r.PSW,
                 xp_words=xn*r.XL*xw*(hw.ROWS+r.X_PAD),b_offset=bias_offset,
                 w_bpt=int(we[-1][0].size),w_bpt_p0=int(we[0][0].size),
                 x_bpt=int(xe[-1].size),x_bpt_p0=int(xe[0].size),
                 ib_out=index+1 if index+1<len(plan['bundles']) else -1,
                 in_buffer_idx=-1 if index==0 else (index-1)%2,out_buffer_idx=index%2,
                 add_out_buffer_idx=-1,add_in_buffer_idx=-1,is_bias=int(bias is not None),
                 is_flatten=0,is_softmax=0,is_dense=0,upsample_h=2 if kinds and kinds[0]=='Resize' else 1,
                 upsample_w=2 if kinds and kinds[0]=='Resize' else 1,x_pad=r.X_PAD,
                 b_val_shift=stage['biased_frac']-raw_frac,
                 b_bias_shift=stage['biased_frac']-bias.frac if bias is not None else 0,
                 ca_nzero=1,ca_shift=0,ca_pl_scale=0,aa_nzero=0,aa_shift=0,aa_pl_scale=0,
                 pa_nzero=0,pa_shift=0,pa_pl_scale=0,softmax_frac=0,softmax_max_f=0,
                 csh=r.CSH,csw=r.CSW,csh_shift=r.CSH_SHIFT,csw_shift=r.CSW_SHIFT,
                 psh_shift=r.PSH_SHIFT,psw_shift=r.PSW_SHIFT,pool='POOL_MAX' if pooled else 'POOL_NONE',
                 header=int(r.header),debug_nhwc_words=int(value.codes.size),
                 input_quant=dict(bits=hw.X_BITS,frac=x.frac),**metadata)
        bias_offset += be.size
        bundles.append(b)
        require(np.all((value.codes >= -(2**31)) & (value.codes < 2**31)), 'processed output exceeds int32')
        np.savetxt(vectors/f'{index}_y_nhwc_exp.txt',value.codes.transpose(0,2,3,1).ravel(),fmt='%d')
        (vectors/f'{index}_x_exp.bin').write_bytes(xb)
        start = 0
        raw_sum = np.zeros([xn]+node['shape'][1:],dtype=np.int64)
        for ip in range(r.CP):
            count = r.CM_0 if ip == 0 else r.CM
            raw = (convolution(x,w,None,node['attributes']) if r.CP == 1 else
                   convolution(Tensor(x.codes[:,start:start+count],x.frac),
                               Tensor(dense[:,start:start+count],w.frac),None,
                               dict(node['attributes'],group=1)))
            require(np.all((raw.codes >= -(2**31)) & (raw.codes < 2**31)), 'raw pass exceeds int32')
            raw_sum += raw.codes
            packed = reorder_y_q2e_conv(raw.codes.transpose(0,2,3,1),hw,r)
            for it in range(r.IT):
                np.savetxt(vectors/f'{index}_{ip}_{it}_y_exp.txt',packed[it].ravel(),fmt='%d')
            start += count
        np.testing.assert_array_equal(raw_sum,convolution(x,w,None,node['attributes']).codes)
        raw_reports.append(dict(bundle=index,passes=r.CP,raw_words=int(raw_sum.size),fraction=raw_frac))
    # Attach each decoder's transition to the following hardware consumer.
    for stage in plan['bundles']:
        nodes = [spec['nodes'][i] for i in stage['transitions']]
        if not nodes or nodes[0]['op'] != 'Resize': continue
        index = stage['index']; require(index+1<len(bundles),'terminal resize unsupported')
        resize,rq,join,jq = nodes
        require(join['inputs'].count(rq['output']) == 1 and
                jq['output'] == plan['bundles'][index+1]['input'], 'decoder transition wiring mismatch')
        skip_name = next(name for name in join['inputs'] if name != rq['output'])
        require(skip_name in post_names,'concat skip is not a preserved postprocess output')
        producer = post_names[skip_name]
        # The runtime's transition quantizer saturates to -2^(bits-1): narrow
        # ranges are not implemented, so reject them instead of mis-rounding.
        require(rq['quant']['narrow'] == 0 and jq['quant']['narrow'] == 0,
                'narrow resize/concat quantizers are not supported')
        slim = lambda q: dict(bits=q['bits'],frac=q['frac'])
        bundles[index+1]['concat_input'] = dict(skip_bundle=producer,
            skip_first=join['inputs'][0] == skip_name,resize_quant=slim(rq['quant']),
            join_quant=slim(jq['quant']),skip_source='prepool' if bundles[producer]['pool']=='POOL_MAX' else 'output')
    for index,b in enumerate(bundles):
        b['o_words'] = len(inputs[index+1]) if index+1<len(bundles) else b['debug_nhwc_words']
        b['o_bytes'] = b['o_words'] if index+1<len(bundles) else b['o_words']*4
    build_concat_transitions(bundles); emit_fixedpoint_postprocess(bundles); emit_pool_postprocess(bundles)
    wbytes = b''.join(weights); bbytes = b''.join(biases); payload = wbytes+bbytes
    require(len(wbytes)%4 == 0,'bias payload alignment')
    defines = dict(N_BUNDLES=len(bundles),X_BITS_L2=3,W_BITS_L2=3,KH_MAX=hw.KH_MAX,
        PE_ROWS=hw.ROWS,PE_COLS=hw.COLS,N_OUT_BUF=2,N_ADD_BUF=0,WB_BYTES=len(payload),W_BYTES=len(wbytes),
        B_WORDS=int(bias_offset),X_BYTES=len(inputs[0]),X_BYTES_ALL=sum(map(len,inputs)),
        O_WORDS=bundles[-1]['debug_nhwc_words'],O_WORDS_MAX=max(b['o_words'] for b in bundles),
        O_BYTES_MAX=max(b['o_bytes'] for b in bundles),NHWC_WORDS=max(b['n']*b['h']*b['w']*b['co'] for b in bundles),
        Y_TYPE_str='int32',B_TYPE_str='int32',O_TYPE_str='int32',AXI_WIDTH=hw.AXI_WIDTH,
        CONFIG_BASEADDR=hw.CONFIG_BASEADDR,DATA_DIR='vectors')
    config = dict(defines=defines,bundles=bundles,reference_format='computed_integer_graph_v1')
    (vectors/'config.json').write_text(json.dumps(config,indent=2)+'\n')
    (vectors/'wb.bin').write_bytes(payload); (vectors/'x.bin').write_bytes(inputs[0])
    (vectors/'wbx.bin').write_bytes(payload+inputs[0]); (vectors/'x_all.bin').write_bytes(b''.join(inputs))
    (output/'input.bin').write_bytes(inputs[0])
    final = context[spec['output']].codes.transpose(0,2,3,1)
    np.savetxt(vectors/'y_exp.txt',final.ravel(),fmt='%d')
    np.save(output/'output-int32-nhwc.npy',final.astype(np.int32))
    audit = dict(model_sha256=spec['source_sha256'],nodes=len(spec['nodes']),bundles=len(bundles),
                 input_sha256=hashlib.sha256(input_nhwc.tobytes()).hexdigest(),
                 raw_pass_checks=raw_reports,independent_reference_checks=checks,
                 reference_format=config['reference_format'],full_model_runtime_validated=False)
    (output/'qonnx-source.json').write_text(json.dumps(audit,indent=2)+'\n')
    print('Graph DDR export complete:',len(bundles),'bundles;',len(payload),'parameter bytes',flush=True)
