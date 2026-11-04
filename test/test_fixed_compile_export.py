"""The plan compiler of the generator equals the C reference.

cgtools/tables.py compiles every fixed-point plan at export; the runtime only
reads the records. Random plans (all shapes: no operation, one quantize,
quantize/leaky/quantize, right, left and zero shifts, narrow ranges, wide
codes that stay in the checked mode) are compiled by the generator, emitted as
C, and compared field by field and table entry by table entry with
test/fixed_compile_reference.h. The fixtures' plans are checked the same way
by test/check_fixed_exhaustive.py.
"""
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cgtools import tables  # noqa: E402

PLANS = 3000


def random_plan(rng):
    """A plan the C reference accepts (cg_fixed_prepare does not trap)."""
    count = rng.choice((0, 1, 3, 3))
    quant = lambda: dict(bits=rng.choice((2, 4, 8, 8, 8, 12, 16, 24, 32)),
                         frac=rng.randint(-31, 31), narrow=rng.randint(0, 1))
    ops = []
    if count:
        ops.append(dict(op="Quant", quant=quant()))
    if count == 3:
        shift = rng.randint(0, 16)
        ops.append(dict(op="LeakyRelu", slope=dict(numerator=rng.randint(0, 1 << shift),
                                                   shift=shift)))
        ops.append(dict(op="Quant", quant=quant()))
    return dict(input_frac=rng.randint(-62, 62), operations=ops)


def c_plan(spec):
    ops = []
    for op in spec["operations"]:
        if op["op"] == "Quant":
            q = op["quant"]
            ops.append(f"{{.kind=CG_FIXED_QUANT, .bits={q['bits']}, .frac={q['frac']}, "
                       f".narrow={q['narrow']}}}")
        else:
            ops.append(f"{{.kind=CG_FIXED_LEAKY, .numerator={op['slope']['numerator']}, "
                       f".shift={op['slope']['shift']}}}")
    return (f"  {{.enabled=1, .input_frac={spec['input_frac']}, .count={len(ops)}, "
            f".operations={{{', '.join(ops) or '{0}'}}}}},")


MAIN = r"""
static int same_step(const cg_fixed_step_t *a, const cg_fixed_step_t *b) {
  return a->shift == b->shift && a->half_minus_one == b->half_minus_one &&
         a->left_low == b->left_low && a->left_high == b->left_high &&
         a->low == b->low && a->high == b->high;
}

int main(void) {
  int failed = 0, compiled = 0, checked = 0, tabled = 0;
  for (int i = 0; i < MODEL_BUNDLE_COUNT; ++i) {
    cg_fixed_compiled_t reference;
    int32_t table[CG_FIXED_TABLE_CODES];
    const cg_fixed_compiled_t *record = &model_fixed_compiled[i];
    cg_fixed_reference(&plans[i], &reference, table);
    int same = record->mode == reference.mode && record->kind == reference.kind &&
               record->count == reference.count && record->numerator == reference.numerator &&
               record->leaky_shift == reference.leaky_shift &&
               same_step(&record->first, &reference.first) &&
               same_step(&record->last, &reference.last) &&
               (record->table == NULL) == (reference.table == NULL);
    for (int k = 0; same && record->table != NULL && k < CG_FIXED_TABLE_CODES; ++k)
      same = record->table[k] == table[k];
    if (!same && failed++ < 5) printf("MISMATCH plan %d mode %u/%u\n", i, record->mode, reference.mode);
    compiled += reference.mode == CG_FIXED_MODE_COMPILED;
    checked += reference.mode == CG_FIXED_MODE_CHECKED;
    tabled += reference.table != NULL;
  }
  printf("%s plans=%d compiled=%d checked=%d tables=%d\n", failed ? "FAIL" : "PASS",
         MODEL_BUNDLE_COUNT, compiled, checked, tabled);
  return failed != 0;
}
"""


class CompileExportTests(unittest.TestCase):
    def test_generator_matches_reference_compiler(self):
        rng = random.Random(243)
        specs = [random_plan(rng) for _ in range(PLANS)]
        bundles = [{"fixedpoint_postprocess": spec} for spec in specs]
        source = ["#include <stdio.h>", '#include "fixed_compile_reference.h"',
                  f"#define MODEL_BUNDLE_COUNT {PLANS}",
                  "static const cg_fixed_postprocess_t plans[MODEL_BUNDLE_COUNT] = {",
                  *map(c_plan, specs), "};",
                  *tables.emit_compiled_postprocess(bundles), MAIN]
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "compile_export.c"
            path.write_text("\n".join(source))
            build = subprocess.run(["gcc", "-std=gnu11", "-O1", "-Wall", "-Wextra", "-Werror",
                                    "-Wno-unused-function", "-Wno-unused-const-variable",
                                    "-I", str(ROOT / "runtime"),
                                    "-I", str(ROOT / "test"), str(path), "-o",
                                    str(Path(work) / "compile_export")],
                                   capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr[-2000:])
            run = subprocess.run([str(Path(work) / "compile_export")], capture_output=True,
                                 text=True)
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertIn("PASS", run.stdout)
        # Every mode and both table states were exercised.
        fields = dict(item.split("=") for item in run.stdout.split()[1:])
        self.assertGreater(int(fields["compiled"]), 0)
        self.assertGreater(int(fields["checked"]), 0)
        self.assertGreater(int(fields["tables"]), 0)


if __name__ == "__main__":
    unittest.main()
