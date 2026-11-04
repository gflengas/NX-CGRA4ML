"""Per-bundle operation tables: fixed-point postprocess, pooling, upsampling
and concat transitions, validated and emitted as C initializers.
"""

UPSAMPLE_KEYS = {
    "is_dense",
    "upsample_h",
    "upsample_w",
}


def reject_unsupported_activations(bundles):
    """Do not silently discard operations emitted by older export adapters."""
    for index, bundle in enumerate(bundles):
        for field in ("output_activation", "prepool_activation"):
            if field in bundle:
                raise ValueError(
                    f"bundle {index}: unsupported {field}; "
                    "activation is not implemented by the current runtime contract"
                )


def emit_fixedpoint_postprocess(bundles):
    """Validate explicit graph operations; without them a bundle uses upstream's
    quant_lrelu activation (cg_activate)."""
    lines = ['#include "cg_fixed_types.h"',
             'static const cg_fixed_postprocess_t model_fixed_postprocess[MODEL_BUNDLE_COUNT] = {']
    for index, bundle in enumerate(bundles):
        spec = bundle.get('fixedpoint_postprocess')
        if spec is None:
            lines.append('  {.enabled=0},')
            continue
        if (not isinstance(spec, dict) or set(spec) != {'input_frac', 'operations'} or
                type(spec['input_frac']) is not int or not -62 <= spec['input_frac'] <= 62 or
                not isinstance(spec['operations'], list)):
            raise ValueError('invalid fixedpoint_postprocess fields')
        if bundle['add_in_buffer_idx'] != -1 or bundle['is_softmax']:
            raise ValueError('explicit fixedpoint postprocess cannot combine with residual/softmax')
        ops = spec['operations']
        kinds = [op.get('op') if isinstance(op, dict) else None for op in ops]
        if kinds not in ([], ['Quant'], ['Quant', 'LeakyRelu', 'Quant']):
            raise ValueError('unsupported fixedpoint operation sequence')
        if not ops and index != len(bundles)-1:
            raise ValueError('bare int32 output is supported only at the terminal bundle')
        encoded = []
        frac, bits = spec['input_frac'], 32
        for op in ops:
            if op['op'] == 'Quant':
                q = op.get('quant')
                if (set(op) != {'op', 'quant'} or not isinstance(q, dict) or
                        set(q) != {'bits','frac','signed','narrow','rounding','zero_point'} or
                        any(type(q[k]) is not int for k in ('bits','frac','narrow','zero_point')) or
                        q['signed'] is not True or q['zero_point'] != 0 or q['rounding'] != 'ROUND' or
                        not 2 <= q['bits'] <= 32 or not -31 <= q['frac'] <= 31 or q['narrow'] not in (0,1)):
                    raise ValueError('unsupported fixedpoint quantizer')
                frac, bits = q['frac'], q['bits']
                encoded.append('{.kind=CG_FIXED_QUANT, '+
                               f'.bits={bits}, .frac={frac}, .narrow={q["narrow"]}'+'}')
            else:
                slope = op.get('slope')
                if (set(op) != {'op', 'slope'} or not isinstance(slope, dict) or
                        set(slope) != {'numerator','shift'} or
                        any(type(v) is not int for v in slope.values()) or
                        not 0 <= slope['shift'] <= 16 or
                        not 0 <= slope['numerator'] <= 1 << slope['shift']):
                    raise ValueError('unsupported fixedpoint LeakyReLU')
                frac += slope['shift']
                encoded.append('{.kind=CG_FIXED_LEAKY, '+
                               f'.numerator={slope["numerator"]}, .shift={slope["shift"]}'+'}')
        if index < len(bundles)-1 and bits > 8:
            raise ValueError('intermediate fixedpoint output exceeds byte storage')
        if bundle.get('prepool_quant', bundle.get('output_quant')) != {'bits': bits, 'frac': frac}:
            raise ValueError('fixedpoint output metadata does not match operations')
        lines.append('  {.enabled=1, '+f'.input_frac={spec["input_frac"]}, .count={len(ops)}, '+
                     '.operations={'+(', '.join(encoded) if encoded else '{0}')+'}},')
    return lines + ['};']


def emit_pool_postprocess(bundles):
    """Validates and emits the post-pool fixed-point quantization table."""
    shadow = []
    for bundle in bundles:
        spec = bundle.get('fixedpoint_pool_postprocess')
        if spec is not None:
            if (bundle['pool'] != 'POOL_MAX' or not isinstance(spec, dict) or
                    not isinstance(spec.get('operations'), list) or
                    len(spec['operations']) != 1 or not isinstance(spec['operations'][0], dict) or
                    spec['operations'][0].get('op') != 'Quant' or
                    spec.get('input_frac') != bundle.get('prepool_quant', {}).get('frac')):
                raise ValueError('invalid explicit postpool quantization')
        elif 'prepool_quant' in bundle:
            raise ValueError('prepool_quant requires explicit postpool quantization')
        item = dict(bundle, fixedpoint_postprocess=spec)
        item.pop('prepool_quant', None)
        shadow.append(item)
    return [line.replace('model_fixed_postprocess[', 'model_pool_postprocess[')
            for line in emit_fixedpoint_postprocess(shadow)]


def _reference_quantize(value, source_frac, bits, target_frac, narrow):
    """cg_fixed_quantize (runtime/cg_fixedpoint.h): rescale the magnitude,
    round ties to even, saturate."""
    magnitude = -value if value < 0 else value
    limit = (1 << (bits - 1)) - (narrow if value < 0 else 1)
    shift = source_frac - target_frac
    if shift > 0:
        if shift >= 64:
            magnitude = 0
        else:
            quotient, remainder = magnitude >> shift, magnitude & ((1 << shift) - 1)
            half = 1 << (shift - 1)
            magnitude = quotient + (remainder > half or (remainder == half and quotient & 1))
    elif shift < 0:
        left = -shift
        if magnitude and (left >= 64 or magnitude > (limit >> left)):
            magnitude = limit
        elif left < 64:
            magnitude <<= left
    magnitude = min(magnitude, limit)
    return -magnitude if value < 0 else magnitude


def _compile_step(shift, quant):
    """cg_fixed_compile_step: one quantize as a shift and a clamp."""
    positive = (1 << (quant["bits"] - 1)) - 1
    negative = (1 << (quant["bits"] - 1)) - quant["narrow"]
    step = dict(shift=shift, half_minus_one=0, left_low=0, left_high=0,
                low=-negative, high=positive)
    if 0 < shift < 32:
        step["half_minus_one"] = (1 << (shift - 1)) - 1
    elif shift < 0:
        left = -shift
        step["left_high"] = 0 if left >= 32 else positive >> left
        step["left_low"] = 0 if left >= 32 else -(negative >> left)
    return step


def compile_fixedpoint(spec):
    """Compiles a validated plan at export, with the rules of the C
    reference compiler (test/fixed_compile_reference.h): mode, shape, constants and the 256-entry
    table of a quantize/leaky/quantize plan whose first codes have at most 8
    bits.

    Returns:
        dict with mode (0 none, 1 checked, 2 compiled), kind, count,
        numerator, leaky_shift, first, last (step dicts) and table (list of
        256 ints or None).
    """
    plan = dict(mode=0, kind=0, count=0, numerator=0, leaky_shift=0,
                first=dict(ZERO_STEP), last=dict(ZERO_STEP), table=None)
    if spec is None:
        return plan
    ops = spec["operations"]
    if len(ops) == 3 and ops[0]["quant"]["bits"] - 1 + ops[1]["slope"]["shift"] > 30:
        plan["mode"] = 1
        return plan
    plan.update(mode=2, count=len(ops))
    frac = spec["input_frac"]
    for index, op in enumerate(ops):
        if op["op"] == "Quant":
            plan["first" if index == 0 else "last"] = _compile_step(frac - op["quant"]["frac"],
                                                                    op["quant"])
            frac = op["quant"]["frac"]
        else:
            plan["numerator"] = op["slope"]["numerator"]
            plan["leaky_shift"] = op["slope"]["shift"]
            frac += op["slope"]["shift"]
    if len(ops) == 3 and ops[0]["quant"]["bits"] <= 8:
        slope, last = ops[1]["slope"], ops[2]["quant"]
        code_frac = ops[0]["quant"]["frac"] + slope["shift"]
        plan["table"] = [
            _reference_quantize(code * (slope["numerator"] if code < 0 else 1 << slope["shift"]),
                                code_frac, last["bits"], last["frac"], last["narrow"])
            for code in range(-128, 128)]
    right = 0 < plan["first"]["shift"] < 32
    if plan["count"] == 1 and right:
        plan["kind"] = 1
    elif plan["count"] == 3 and plan["table"] is not None and right:
        plan["kind"] = 2
    return plan


def _c_int(value):
    return "(-2147483647 - 1)" if value == -(1 << 31) else str(value)


ZERO_STEP = dict(shift=0, half_minus_one=0, left_low=0, left_high=0, low=0, high=0)


def _c_step(step):
    """A cg_fixed_step_t initializer; None is the zero step."""
    return "{" + ", ".join(f".{k}={_c_int((step or ZERO_STEP)[k])}" for k in ZERO_STEP) + "}"


def emit_compiled_postprocess(bundles):
    """Emits every bundle's convolution and pool plan compiled at export
    (model_fixed_compiled, model_pool_compiled) and their distinct tables
    (model_fixed_tables). Call after the emit_*_postprocess validation."""
    compiled = {"model_fixed_compiled": [], "model_pool_compiled": []}
    tables = []
    for bundle in bundles:
        for name, spec in (("model_fixed_compiled", bundle.get("fixedpoint_postprocess")),
                           ("model_pool_compiled", bundle.get("fixedpoint_pool_postprocess"))):
            plan = compile_fixedpoint(spec)
            if plan["table"] is not None and plan["table"] not in tables:
                tables.append(plan["table"])
            compiled[name].append(plan)
    lines = []
    if tables:
        lines.append(f"static const int32_t model_fixed_tables[{len(tables)}][CG_FIXED_TABLE_CODES] = {{")
        for table in tables:
            lines.append("  {" + ", ".join(_c_int(value) for value in table) + "},")
        lines += ["};", ""]
    for name, plans in compiled.items():
        lines.append(f"static const cg_fixed_compiled_t {name}[MODEL_BUNDLE_COUNT] = {{")
        for plan in plans:
            table = "0" if plan["table"] is None else f"model_fixed_tables[{tables.index(plan['table'])}]"
            lines.append(f"  {{.mode={plan['mode']}, .kind={plan['kind']}, .count={plan['count']}, "
                         f".numerator={plan['numerator']}, .leaky_shift={plan['leaky_shift']}, "
                         f".first={_c_step(plan['first'])}, .last={_c_step(plan['last'])}, .table={table}}},")
        lines += ["};", ""]
    return lines


def emit_concat_quant(bundles):
    """Emits each concat consumer's decoder and skip quantizers compiled at
    export (model_concat_decoder_quant, model_concat_skip_quant; zero records
    for the other bundles)."""
    transitions, _, _ = build_concat_transitions(bundles)
    by_consumer = {entry["consumer"]: entry for entry in transitions}
    lines = []
    for name, part in (("model_concat_decoder_quant", "decoder"), ("model_concat_skip_quant", "skip")):
        lines.append(f"static const cg_transition_quant_t {name}[MODEL_BUNDLE_COUNT] = {{")
        for index in range(len(bundles)):
            entry = by_consumer.get(index)
            if entry is None:
                lines.append("  {.has_resize=0},")
                continue
            # The decoder's optional resize step, then the join step.
            source_frac = entry[f"{part}_quant"]["frac"]
            resize = entry["resize_quant"] if part == "decoder" else None
            resize_step = None
            if resize is not None:
                resize_step = _compile_step(source_frac - resize["frac"],
                                            dict(bits=resize["bits"], narrow=0))
                source_frac = resize["frac"]
            join = entry["join_quant"]
            join_step = _compile_step(source_frac - join["frac"], dict(bits=join["bits"], narrow=0))
            lines.append(f"  {{.has_resize={int(resize is not None)}, .resize={_c_step(resize_step)}, "
                         f".join={_c_step(join_step)}}},")
        lines += ["};", ""]
    return lines


def validate_upsampling(bundles):
    """Validate the source-side nearest-neighbor handoff contract."""
    for index, bundle in enumerate(bundles):
        missing = UPSAMPLE_KEYS.difference(bundle)
        if missing:
            raise ValueError(
                f"bundle {index} is missing upsample metadata: {sorted(missing)}"
            )

        factors = (bundle["upsample_h"], bundle["upsample_w"])
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
               for value in factors):
            raise ValueError(
                f"bundle {index} upsample factors must be positive integers"
            )
        if factors == (1, 1):
            continue
        if bundle["is_dense"] != 0:
            raise ValueError(f"bundle {index} cannot upsample a dense output")
        if bundle["is_flatten"] != 0 or bundle["is_softmax"] != 0:
            raise ValueError(f"bundle {index} cannot upsample a terminal output")
        if bundle["add_out_buffer_idx"] != -1:
            raise ValueError(f"bundle {index} cannot upsample a residual source")
        if index + 1 >= len(bundles):
            raise ValueError(f"bundle {index} cannot upsample without a destination")

        destination = bundles[index + 1]
        channels = bundle["co"]
        if destination.get("concat_input") is not None:
            channels += bundles[destination["concat_input"]["skip_bundle"]]["co"]
        if (bundle["ph"] * factors[0] != destination["h"] or
                bundle["pw"] * factors[1] != destination["w"] or
                channels != destination["ci"]):
            raise ValueError(
                f"bundle {index} upsample shape does not match bundle {index + 1} input"
            )


def validate_transition_quantizer(value):
    """Signed, zero-point-zero, power-of-two quantization, ties-to-even.

    More general QONNX semantics must be explicitly implemented, not inferred
    from a nominal bit width. A value represents integer * 2**(-frac).
    """
    if not isinstance(value, dict) or set(value) != {"bits", "frac"}:
        raise ValueError("transition quantizer requires exactly bits and frac")
    if any(type(value[key]) is not int for key in value):
        raise ValueError("transition quantizer fields must be integers")
    if not 2 <= value["bits"] <= 8 or not -15 <= value["frac"] <= 15:
        raise ValueError("unsupported transition quantizer bits or frac")
    return dict(value)


def build_concat_transitions(bundles):
    """Validate concat inputs and allocate retained encoder slots.

    concat_input belongs to the consumer. Resize and join quantizers stay
    separate because double rounding and intermediate saturation are observable.
    Slot lifetimes end when packing the last consumer's input, before it runs.
    """
    transitions = []
    retained = {}
    for consumer, destination in enumerate(bundles):
        spec = destination.get("concat_input")
        if spec is None:
            continue
        required = {"skip_bundle", "skip_first", "resize_quant", "join_quant"}
        if not isinstance(spec, dict) or not required.issubset(spec) or set(spec)-required-{'skip_source'}:
            raise ValueError("concat_input has missing or unknown fields")
        producer = spec["skip_bundle"]
        if type(producer) is not int or not 0 <= producer < consumer - 1:
            raise ValueError("concat skip must precede the immediate decoder source")
        if type(spec["skip_first"]) is not bool:
            raise ValueError("concat skip_first must be boolean")
        decoder = bundles[consumer - 1]
        skip = bundles[producer]
        source = spec.get('skip_source', 'output')
        if source not in ('output', 'prepool'):
            raise ValueError('concat skip_source must be output or prepool')
        prepool = source == 'prepool'
        if prepool and (skip['pool'] != 'POOL_MAX' or
                        any(type(skip[k]) is not int or skip[k] <= 0 for k in ('ch','cw'))):
            raise ValueError('prepool skip requires a valid pooled convolution')
        skip_h, skip_w = (skip['ch'], skip['cw']) if prepool else (skip['ph'], skip['pw'])
        # Sizes here are per image: the runtime keeps one skip per image.
        for bundle in (skip, decoder, destination):
            if bundle["n"] != skip["n"] or bundle["is_dense"]:
                raise ValueError("concat requires convolution bundles of one batch size")
        for bundle in (skip, decoder):
            if bundle["is_flatten"] or bundle["is_softmax"]:
                raise ValueError("concat source cannot be flattened or softmax")
            if any(type(bundle[key]) is not int or bundle[key] <= 0
                   for key in ("ph", "pw", "co", "upsample_h", "upsample_w")):
                raise ValueError("concat source dimensions and scales must be positive integers")
        if (skip["upsample_h"], skip["upsample_w"]) != (1, 1):
            raise ValueError("retained concat skip must not be upsampled")
        if (decoder["ph"] * decoder["upsample_h"] != destination["h"] or
                decoder["pw"] * decoder["upsample_w"] != destination["w"] or
                skip_h != destination["h"] or skip_w != destination["w"] or
                decoder["co"] + skip["co"] != destination["ci"]):
            raise ValueError("concat spatial shape or channel count mismatch")
        decoder_quant = validate_transition_quantizer(decoder["output_quant"])
        skip_quant = validate_transition_quantizer(skip.get('prepool_quant', skip['output_quant'])
                                                    if prepool else skip['output_quant'])
        resize_quant = (None if spec["resize_quant"] is None else
                        validate_transition_quantizer(spec["resize_quant"]))
        join_quant = validate_transition_quantizer(spec["join_quant"])
        if join_quant != validate_transition_quantizer(destination["input_quant"]):
            raise ValueError("concat join quantizer does not match consumer input")
        transitions.append({
            "consumer": consumer, "skip_bundle": producer,
            "decoder_channel_offset": skip["co"] if spec["skip_first"] else 0,
            "skip_channel_offset": 0 if spec["skip_first"] else decoder["co"],
            "decoder_quant": decoder_quant, "skip_quant": skip_quant,
            "resize_quant": resize_quant, "join_quant": join_quant,
            "skip_prepool": int(prepool), "skip_words": skip_h * skip_w * skip['co'],
        })
        if producer in retained and retained[producer]['prepool'] != int(prepool):
            raise ValueError('one retained producer cannot mix prepool and output views')
        retained[producer] = {
            "producer": producer, "last_consumer": consumer,
            "words": skip_h * skip_w * skip["co"], "prepool": int(prepool),
        }

    slots = []
    for producer, lifetime in sorted(retained.items()):
        slot = next((i for i, state in enumerate(slots)
                     if state["last_consumer"] <= producer), len(slots))
        if slot == len(slots):
            slots.append({"words": 0, "last_consumer": -1})
        slots[slot]["words"] = max(slots[slot]["words"], lifetime["words"])
        slots[slot]["last_consumer"] = lifetime["last_consumer"]
        lifetime["slot"] = slot
    for transition in transitions:
        transition["skip_slot"] = retained[transition["skip_bundle"]]["slot"]
    return transitions, list(retained.values()), [slot["words"] for slot in slots]


def emit_concat_metadata(bundles):
    """Emits the concat tables: retained-output slots and per-consumer joins.

    Returns:
        C lines of model_concat_saves (what each producer keeps) and
        model_concat_plans (how each consumer joins decoder and skip).
    """
    transitions, retained, sizes = build_concat_transitions(bundles)
    lines = []
    offsets = []
    total = 0
    for size in sizes:
        offsets.append(total)
        total += size
    lines += [
        f"#define MODEL_CONCAT_WORDS {max(1, total)}u",
        "typedef struct { int32_t slot; uint32_t offset, words, prepool; } model_concat_save_t;",
        "typedef struct {",
        "  int32_t skip_bundle;",
        "  uint32_t slot, offset, last_consumer, decoder_channel_offset, skip_channel_offset;",
        "  int32_t decoder_frac, skip_frac, resize_frac, join_frac;",
        "  uint32_t resize_bits, join_bits, skip_prepool, skip_words;",
        "} model_concat_plan_t;",
        "static const model_concat_save_t model_concat_saves[MODEL_BUNDLE_COUNT] = {",
    ]
    saves = {entry["producer"]: entry for entry in retained}
    for index in range(len(bundles)):
        entry = saves.get(index)
        lines.append("  {.slot=-1}," if entry is None else
                     f"  {{.slot={entry['slot']}, .offset={offsets[entry['slot']]}, .words={entry['words']}, .prepool={entry['prepool']}}},")
    lines += ["};", "static const model_concat_plan_t model_concat_plans[MODEL_BUNDLE_COUNT] = {"]
    plans = {entry["consumer"]: entry for entry in transitions}
    for index in range(len(bundles)):
        entry = plans.get(index)
        if entry is None:
            lines.append("  {.skip_bundle=-1},")
            continue
        resize = entry["resize_quant"]
        fields = {
            "skip_bundle": entry["skip_bundle"], "slot": entry["skip_slot"],
            "offset": offsets[entry["skip_slot"]],
            "last_consumer": saves[entry["skip_bundle"]]["last_consumer"],
            "decoder_channel_offset": entry["decoder_channel_offset"],
            "skip_channel_offset": entry["skip_channel_offset"],
            "decoder_frac": entry["decoder_quant"]["frac"],
            "skip_frac": entry["skip_quant"]["frac"],
            "resize_frac": 0 if resize is None else resize["frac"],
            "resize_bits": 0 if resize is None else resize["bits"],
            "join_frac": entry["join_quant"]["frac"],
            "join_bits": entry["join_quant"]["bits"],
            "skip_prepool": entry['skip_prepool'], "skip_words": entry['skip_words'],
        }
        lines.append("  {" + ", ".join(f".{key}={value}" for key, value in fields.items()) + "},")
    return lines + ["};"]
