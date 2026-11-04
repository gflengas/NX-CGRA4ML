"""Host result interpretation and rejection before saving/ACK."""
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cgtools.host_link import result_quantization, save_result, validate_result_quantization


class ResultMetadataTests(unittest.TestCase):
    def test_export_scale(self):
        self.assertEqual(result_quantization(False, {"bits": 4, "frac": 3})["scale"], 0.125)

    def test_missing_and_invalid_settings(self):
        for exported in [None, {}, {"bits": 4}, {"bits": True, "frac": 3}, {"bits": 4, "frac": True}, {"bits": 4, "frac": 1000}]:
            with self.subTest(exported=exported), self.assertRaises(ValueError):
                result_quantization(False, exported)
        for bits in (33, 16):
            with self.subTest(bits=bits), self.assertRaises(ValueError):
                result_quantization(False, {"bits": bits, "frac": 3})

    def test_terminal_int32_preserves_full_range_and_scale(self):
        quant=result_quantization(False,{'bits':32,'frac':11})
        self.assertEqual(quant['scale'],2**-11)
        values=[-2**31,-21864,-129,128,23877,2**31-1]
        raw=struct.pack('<6i',*values)
        with tempfile.TemporaryDirectory() as tmp:
            dest=Path(tmp)/'result.bin';golden=Path(tmp)/'golden.txt'
            golden.write_text('\n'.join(map(str,values)))
            manifest=dict(result_words=6,result_type='int32',result_shape=[1,1,1,6],
                          result_layout='NHWC',identity_sha256='a'*64,result_quantization=quant)
            save_result(raw,manifest,1,dest,golden,1)
            self.assertEqual(dest.read_bytes(),raw)
            self.assertEqual(json.loads(dest.with_suffix('.bin.json').read_text())['comparison'],'passed')

    def test_round_trip_with_and_without_goldens(self):
        for kind, raw, quant in [("int32", struct.pack('<i', 7), result_quantization(False, {"bits": 4, "frac": 3})),
                                  ("float32", struct.pack('<f', 0.875), result_quantization(True, None))]:
            for checked in [False, True]:
                with self.subTest(kind=kind, checked=checked), tempfile.TemporaryDirectory() as tmp:
                    dest = Path(tmp)/'result.bin'; golden = Path(tmp)/'golden.txt'
                    golden.write_text('7' if kind == 'int32' else '0.875')
                    manifest = dict(result_words=1, result_type=kind, result_shape=[1,1,1,1], result_layout='NHWC', identity_sha256='a'*64, result_quantization=quant, softmax_tolerance=1e-5)
                    save_result(raw, manifest, 1, dest, golden if checked else None, 1)
                    stored=json.loads(dest.with_suffix('.bin.json').read_text())
                    self.assertEqual(dest.read_bytes(), raw)
                    self.assertEqual(stored['quantization'], quant)
                    self.assertEqual(stored['comparison'], 'passed' if checked else 'not_requested')
                    if kind == 'int32':
                        self.assertEqual((struct.unpack('<i', raw)[0]-quant['zero_point'])*quant['scale'], 0.875)

    def test_invalid_manifest_rejected_before_save(self):
        quant=result_quantization(False, {'bits':4,'frac':3})
        bad=[None, {}, dict(quant, scale=0.25), dict(quant, scale=True), dict(quant, signed=False), dict(quant, fractional_bits=True), dict(quant, zero_point=False), dict(quant, extra=0)]
        for q in bad:
            with self.subTest(q=q), tempfile.TemporaryDirectory() as tmp:
                dest=Path(tmp)/'result.bin'
                manifest=dict(result_words=1, result_type='int32', result_shape=[1,1,1,1], result_layout='NHWC', identity_sha256='a'*64, result_quantization=q)
                with self.assertRaises(ValueError): save_result(struct.pack('<i',7),manifest,1,dest)
                self.assertFalse(dest.exists())
        with self.assertRaises(ValueError): validate_result_quantization('float32', quant)

    def test_wrong_golden_preserves_raw_result_and_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest=Path(tmp)/'result.bin'; golden=Path(tmp)/'golden'; golden.write_text('6')
            manifest=dict(result_words=1,result_type='int32',result_shape=[1,1,1,1],result_layout='NHWC',identity_sha256='a'*64,result_quantization=result_quantization(False,{'bits':4,'frac':3}))
            with self.assertRaisesRegex(RuntimeError,'independent golden'): save_result(struct.pack('<i',7),manifest,1,dest,golden)
            self.assertEqual(dest.read_bytes(),struct.pack('<i',7))
            self.assertEqual(json.loads(dest.with_suffix('.bin.json').read_text())['comparison'],'failed')


if __name__ == '__main__': unittest.main()
