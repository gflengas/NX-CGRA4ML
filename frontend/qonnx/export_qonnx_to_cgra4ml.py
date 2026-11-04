"""Exports an imported QONNX graph as a CGRA4ML export.

Fixed-point graphs (U-Nets with upsampling and concat) go through export_fixedpoint_graph; bundle graphs
(the Brevitas ResNet) become upstream XLayers and go through upstream's
exporter. The input .npy holds one image or a batch. Batch normalization is
the identity: the graph already has its parameters folded.
"""
import argparse
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
import numpy as np
import tensorflow as tf
from tensorflow import keras

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'CGRA4ML'))
from deepsocflow import Hardware,SYS_BITS,XModel,XBundle,XConvBN,XDense,XActivation,XPool,export_inference,verify_inference
from deepsocflow.py.utils import BUNDLES


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('imported',type=Path);p.add_argument('input',type=Path);p.add_argument('output',type=Path);p.add_argument('--hardware',type=Path,required=True);p.add_argument('--verify',action='store_true');p.add_argument('--reference-context',type=Path);a=p.parse_args()
    spec=json.loads((a.imported/'import.json').read_text());params=np.load(a.imported/'parameters.npz');x=np.load(a.input,allow_pickle=False)
    refs=np.load(a.reference_context,allow_pickle=False) if a.reference_context else None
    hwpath=a.hardware.resolve();out=a.output.resolve()
    if spec.get('format') == 'qonnx-fixedpoint-graph-v1':
        if a.verify:
            p.error('fixed-point graphs require the canonical NG-ULTRA runtime simulator; export then run python3 -m cgtools sim')
        from frontend.qonnx.export_fixedpoint_graph import export_graph
        export_graph(spec,params,x,out,hwpath,refs)
        return
    out.mkdir(parents=True,exist_ok=False)
    # N images: a batch-N export (the model is batch 1).
    assert list(x.shape[1:])==spec['input_shape'][1:] and x.dtype==np.float32
    descriptors=spec['bundles'];iq=spec['input_quant'];bits=SYS_BITS(x=iq['bits'],k=descriptors[0]['weight_bits'],b=max(b['bias_bits'] for b in descriptors if b['use_bias']))
    def act(q):
        assert q['bits']==bits.x and q['signed'] is True and q['zero_point']==0 and q['rounding']=='ROUND' and q['narrow']==0
        return XActivation(sys_bits=bits,o_int_bits=q['bits']-1-q['frac'],type=q['kind'],slope=q['slope'])
    class ImportedModel(XModel):
        def __init__(self):
            super().__init__(bits,x_int_bits=bits.x-1-iq['frac']);self.bundle_layers=[]
            for b in descriptors:
                name=f"b{b['index']}";w=params[name+'.weight'];assert b['weight_bits']==bits.k
                kw=dict(k_int_bits=bits.k-1-b['weight_frac'],b_int_bits=bits.b-1-b.get('bias_frac',bits.b-1),act=act(b['activation']),use_bias=b['use_bias'],kernel_initializer=keras.initializers.Constant(w))
                if b['use_bias']:kw['bias_initializer']=keras.initializers.Constant(params[name+'.bias'])
                if b['kind']=='conv':
                    assert b['padding']=='same';core=XConvBN(filters=w.shape[-1],kernel_size=b['kernel_shape'],strides=tuple(b['strides']),epsilon=0.0,**kw)
                else:
                    assert b['kind']=='dense';core=XDense(units=w.shape[-1],**kw)
                pool=None
                if 'pool' in b:
                    pp=b['pool'];pool=XPool(type=pp['kind'],pool_size=tuple(pp['size']),strides=tuple(pp['strides']),padding=pp['padding'],act=act(pp['activation']))
                self.bundle_layers.append(XBundle(core=core,pool=pool,add_act=act(b['add']['activation']) if 'add' in b else None,flatten=b['flatten'],softmax=b['softmax']))
        def call(self,x):
            x=self.input_quant_layer(x);outputs=[]
            for b,layer in zip(descriptors,self.bundle_layers):
                x=layer(x,outputs[b['add']['source']]) if 'add' in b else layer(x);outputs.append(x)
            return x
    inp=keras.Input(shape=tuple(spec['input_shape'][1:]));candidate=ImportedModel();model=keras.Model(inp,candidate(inp));hardware=Hardware.from_json(str(hwpath))
    os.chdir(out);hardware.export_json();hardware.export()
    with patch.object(tf.random,'uniform',return_value=tf.constant(x)) as draw:
        export_inference(model,hardware,batch_size=x.shape[0]);assert draw.call_count==1
    # Prove that identity BN did not fold the QONNX parameters a second time.
    for b,layer in zip(descriptors,BUNDLES):
        name=f"b{b['index']}"
        w=layer.core.w.ftensor.numpy();np.testing.assert_array_equal(w,params[name+'.weight'])
        if b['use_bias']:np.testing.assert_array_equal(layer.core.b.ftensor.numpy(),params[name+'.bias'])
    if refs is not None:
        checks={}
        for b,layer in zip(descriptors,BUNDLES):
            tensors=b['tensors']
            values=dict(input=layer.core.x.ftensor.numpy(),biased=layer.core.out.ftensor.numpy(),
                        activated=(layer.pre_softmax if b['softmax'] else layer.core.act.out).ftensor.numpy(),
                        output=layer.out.ftensor.numpy())
            if 'add' in b:values['added']=layer.add.act.out.ftensor.numpy()
            if 'pool' in b:values['pooled']=layer.pool.act.out.ftensor.numpy()
            raw=layer.core.y.ftensor.numpy()
            if b['kind']=='conv':
                # RTL computes stride-1 SAME; CPU selects the SAME_UPPER stride
                # positions. Compare the positions represented by the ONNX Conv.
                h,w=raw.shape[1:3];kh,kw=b['kernel_shape'];sh,sw=b['strides']
                hs=(kh-1)//2-max((sh*((h+sh-1)//sh-1)+kh-h)//2,0)
                ws=(kw-1)//2-max((sw*((w+sw-1)//sw-1)+kw-w)//2,0)
                raw=raw[:,hs::sh,ws::sw,:]
            values['raw']=raw
            for key,value in values.items():
                expected=refs[tensors[key]]
                if expected.ndim==4:expected=expected.transpose(0,2,3,1)
                name=f"b{b['index']}.{key}"
                if key=='output' and b['softmax']:
                    np.testing.assert_allclose(value,expected,rtol=0,atol=1e-5,err_msg=name)
                else:np.testing.assert_array_equal(value,expected,err_msg=name)
                checks[name]=dict(elements=int(value.size),max_error=float(np.max(np.abs(value-expected))))
        (out/'reference-comparison.json').write_text(json.dumps(checks,indent=2)+'\n')
        print('All graph-derived bundle boundaries agree with independent QONNX references',flush=True)
    (out/'qonnx-source.json').write_text(json.dumps(dict(model_sha256=spec['source_sha256'],parsed_nodes=spec['parsed_nodes'],input_shape=list(x.shape)),indent=2)+'\n')
    if a.verify:
        import shutil
        import cgtools  # noqa: F401  (puts ngultra/py on sys.path)
        from ngultra import site
        verilator=shutil.which(site.get('verilator'))
        if verilator is None:p.error('Verilator not found; set VERILATOR or verilator in site.json')
        verify_inference(model,hardware,SIM='verilator',SIM_PATH=str(Path(verilator).parent)+'/')
    # The fixture generator reads only the computed reference format.
    from frontend.convert_legacy_vectors import convert
    convert(out/'vectors')
    print('QONNX-derived XLayers export complete; folded parameters preserved exactly',flush=True)


if __name__=='__main__':main()
