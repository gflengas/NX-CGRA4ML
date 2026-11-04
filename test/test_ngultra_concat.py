"""Source selection and lifetime validation for retained concat tensors."""
import copy
import unittest

from cgtools.tables import build_concat_transitions, emit_concat_metadata, emit_pool_postprocess


def fixture():
    common = dict(n=1,is_dense=0,is_flatten=0,is_softmax=0,co=2,ci=2,
                  h=4,w=4,ch=4,cw=4,ph=2,pw=2,pool='POOL_MAX',
                  upsample_h=1,upsample_w=1,output_quant=dict(bits=8,frac=4),
                  input_quant=dict(bits=8,frac=4))
    skip = dict(common)
    decoder = dict(common,upsample_h=2,upsample_w=2)
    destination = dict(common,ci=4,concat_input=dict(skip_bundle=0,skip_first=False,
                      resize_quant=None,join_quant=dict(bits=8,frac=4),skip_source='prepool'))
    return [skip,decoder,destination]


class ConcatTests(unittest.TestCase):
    def test_prepool_shape_storage_and_emission(self):
        transitions,retained,sizes = build_concat_transitions(fixture())
        self.assertEqual(sizes,[32])
        self.assertEqual(retained[0]['words'],32)
        self.assertEqual(retained[0]['prepool'],1)
        self.assertEqual(transitions[0]['skip_words'],32)
        self.assertIn('.skip_prepool=1', '\n'.join(emit_concat_metadata(fixture())))

    def test_prepool_fraction_is_independent_of_pool_output(self):
        bundles = fixture()
        bundles[0]['prepool_quant'] = dict(bits=8, frac=6)
        transitions, _, _ = build_concat_transitions(bundles)
        self.assertEqual(transitions[0]['skip_quant']['frac'], 6)

    def test_pool_quantization_contract(self):
        bundle = dict(fixture()[0], add_in_buffer_idx=-1,
                      prepool_quant=dict(bits=8, frac=6))
        bundle['fixedpoint_pool_postprocess'] = dict(input_frac=6, operations=[
            dict(op='Quant', quant=dict(bits=8, frac=4, signed=True,
                 narrow=0, rounding='ROUND', zero_point=0))])
        self.assertIn('model_pool_postprocess', '\n'.join(emit_pool_postprocess([bundle])))
        for field, value in [('pool', 'POOL_NONE'), ('prepool_quant', dict(bits=8, frac=5)),
                             ('output_quant', dict(bits=8, frac=3)),
                             ('fixedpoint_pool_postprocess', None)]:
            bad = copy.deepcopy(bundle); bad[field] = value
            with self.assertRaises(ValueError): emit_pool_postprocess([bad])
        bad = copy.deepcopy(bundle)
        bad['fixedpoint_pool_postprocess']['operations'][0]['op'] = 'LeakyRelu'
        with self.assertRaises(ValueError): emit_pool_postprocess([bad])

    def test_default_output_source_remains_compatible(self):
        bundles = fixture()
        del bundles[-1]['concat_input']['skip_source']
        bundles[1].update(upsample_h=1,upsample_w=1)
        bundles[2].update(h=2,w=2)
        transitions,retained,sizes = build_concat_transitions(bundles)
        self.assertEqual(sizes,[8])
        self.assertEqual(transitions[0]['skip_prepool'],0)
        self.assertEqual(retained[0]['prepool'],0)

    def test_invalid_sources_and_shapes_rejected(self):
        for field,value in [('skip_source','unknown'),('skip_first',1)]:
            bundles = fixture();bundles[-1]['concat_input'][field]=value
            with self.assertRaises(ValueError):build_concat_transitions(bundles)
        for field,value in [('pool','POOL_NONE'),('ch',0),('ch',2),('upsample_h',2)]:
            bundles=fixture();bundles[0][field]=value
            with self.assertRaises(ValueError):build_concat_transitions(bundles)

    def test_repeated_skip_retains_until_last_consumer(self):
        bundles=fixture();bundles += [copy.deepcopy(bundles[1]),copy.deepcopy(bundles[2])]
        transitions,retained,sizes=build_concat_transitions(bundles)
        self.assertEqual(len(retained),1)
        self.assertEqual(retained[0]['last_consumer'],4)
        self.assertEqual([t['skip_slot'] for t in transitions],[0,0])

    def test_mixed_views_of_same_producer_rejected(self):
        bundles=fixture();bundles += [copy.deepcopy(bundles[1]),copy.deepcopy(bundles[2])]
        bundles[3].update(upsample_h=1,upsample_w=1)
        bundles[4].update(h=2,w=2)
        bundles[4]['concat_input']['skip_source']='output'
        with self.assertRaisesRegex(ValueError,'mix prepool'):
            build_concat_transitions(bundles)


if __name__ == '__main__':
    unittest.main()
