"""Execute the canonical C postprocessor; no board or build fixtures required."""
import ctypes as ct
from fractions import Fraction
from pathlib import Path
import subprocess
import tempfile
import unittest

import numpy as np

from cgtools.tables import emit_fixedpoint_postprocess

ROOT = Path(__file__).resolve().parents[1]


class Operation(ct.Structure):
    _fields_ = [(key, ct.c_uint32) for key in ['kind','bits','narrow','numerator','shift']] + [('frac',ct.c_int32)]


class Plan(ct.Structure):
    _fields_ = [('enabled',ct.c_uint32),('count',ct.c_uint32),('input_frac',ct.c_int32),('operations',Operation*3)]


class CPostprocessor:
    def __init__(self, directory):
        directory = Path(directory)
        source = directory/'postprocess.c'
        source.write_text('''#include <stddef.h>
#include "cg_fixedpoint.h"
int32_t quant(int64_t x, int32_t f, uint32_t b, int32_t q, uint32_t n) {
  return cg_fixed_quantize(x,f,b,q,n);
}
void run(const int32_t *x, int32_t *y, size_t count, const cg_fixed_postprocess_t *p) {
  for (size_t i=0; i<count; ++i) y[i]=cg_fixed_postprocess(x[i],p);
}
''')
        library = directory/'postprocess.so'
        subprocess.run(['gcc','-std=c99','-O2','-shared','-fPIC','-fsanitize=undefined',
                        '-fno-sanitize-recover=all','-I'+str(ROOT/'runtime'),
                        str(source),'-o',str(library)],check=True)
        self.lib = ct.CDLL(str(library))
        self.lib.quant.argtypes = [ct.c_int64,ct.c_int32,ct.c_uint32,ct.c_int32,ct.c_uint32]
        self.lib.quant.restype = ct.c_int32
        self.lib.run.argtypes = [ct.POINTER(ct.c_int32),ct.POINTER(ct.c_int32),ct.c_size_t,ct.POINTER(Plan)]
        self.lib.run.restype = None

    def execute(self, values, contract):
        ops = contract['operations']
        plan = Plan(enabled=1,count=len(ops),input_frac=contract['input_frac'])
        for i,op in enumerate(ops):
            if op['op'] == 'Quant':
                q = op['quant']; plan.operations[i] = Operation(kind=1,bits=q['bits'],frac=q['frac'],narrow=q['narrow'])
            else:
                plan.operations[i] = Operation(kind=2,**op['slope'])
        if np.any(values < -(2**31)) or np.any(values >= 2**31):
            raise ValueError('C postprocess input exceeds int32')
        x = np.ascontiguousarray(values,dtype=np.int32)
        y = np.empty_like(x)
        self.lib.run(x.ctypes.data_as(ct.POINTER(ct.c_int32)),y.ctypes.data_as(ct.POINTER(ct.c_int32)),x.size,ct.byref(plan))
        return y


def quant(frac=0,narrow=0,bits=8):
    return dict(op='Quant',quant=dict(bits=bits,frac=frac,narrow=narrow,signed=True,rounding='ROUND',zero_point=0))


class CFixedpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.c = CPostprocessor(cls.directory.name)

    def test_signed_rounding_and_saturation_against_exact_rationals(self):
        values = list(range(-129,130)) + [-2**63,2**63-1,-2**31,2**31-1]
        for source in [-62,-31,-1,0,1,15,31,62,78]:
            for target in [-31,0,31]:
                for bits in [2,8,32]:
                    for narrow in [0,1]:
                        for value in values:
                            expected = round(Fraction(value)*Fraction(2)**(target-source))
                            expected = max(-(1<<(bits-1))+narrow,min((1<<(bits-1))-1,expected))
                            self.assertEqual(self.c.lib.quant(value,source,bits,target,narrow),expected)

    def test_double_quantization_and_leaky(self):
        values = np.arange(-4096,4097,dtype=np.int64)
        for before,after in [(0,0),(2,4),(4,2)]:
            ops = [quant(before),dict(op='LeakyRelu',slope=dict(numerator=13,shift=7)),quant(after)]
            actual = self.c.execute(values,dict(input_frac=3,operations=ops))
            expected = []
            for v in values:
                first = max(-128,min(127,round(Fraction(int(v))*Fraction(2)**(before-3))))
                real = Fraction(first)*Fraction(2)**-before
                if real < 0: real *= Fraction(13,128)
                expected.append(max(-128,min(127,round(real*Fraction(2)**after))))
            np.testing.assert_array_equal(actual,expected)

    def test_terminal_preserves_int32_extremes(self):
        values = np.array([-2**31,-21864,-129,128,23877,2**31-1],dtype=np.int64)
        np.testing.assert_array_equal(self.c.execute(values,dict(input_frac=11,operations=[])),values)

    def test_metadata_rejects_unsupported_contracts(self):
        base = dict(add_in_buffer_idx=-1,is_softmax=0,output_quant=dict(bits=8,frac=0))
        for spec in [dict(input_frac=0,operations=[dict(op='LeakyRelu',slope=dict(numerator=13,shift=7))]),
                     dict(input_frac=0,operations=[quant(0,2)]),
                     dict(input_frac=0,operations=[dict(op='Quant',quant=dict(quant()['quant'],rounding='FLOOR'))]),
                     dict(input_frac=0,operations=[quant(),dict(op='LeakyRelu',slope=dict(numerator=129,shift=7)),quant()]),
                     dict(input_frac=63,operations=[quant()])]:
            with self.assertRaises(ValueError):
                emit_fixedpoint_postprocess([dict(base,fixedpoint_postprocess=spec)])
        with self.assertRaisesRegex(ValueError,'terminal'):
            emit_fixedpoint_postprocess([dict(base,fixedpoint_postprocess=dict(input_frac=0,operations=[])),base])
        with self.assertRaisesRegex(ValueError,'metadata'):
            emit_fixedpoint_postprocess([dict(base,fixedpoint_postprocess=dict(input_frac=0,operations=[quant(1)]))])


if __name__ == '__main__':
    unittest.main()
