"""Model tables for the runtime: model.h, model.c, scheduled W/B and manifest.

Reads one CGRA4ML export (vectors/config.json, wb.bin and, for debug
validation, the reference vectors) and its hardware.json and writes:

    model.h               limits, types and extern declarations
    model.c               bundle, concat and postprocess tables
    <payload>             W/B, loaded into DDR by the host
    <payload>.json        manifest: identity, reservations, ranges, result

One launch runs the whole model; each bundle runs in one pass over all its
input channels, for each of its n images (sizes in the tables are per image).
The runtime trusts these tables: every check of the export is made here.
"""

import json

from . import emit_c, goldens, manifest, schedule, tables
from .emit_c import u32
from .host_link import result_quantization

POOL_IDS = {"POOL_NONE": 0, "POOL_MAX": 1}
# runtime/cg_postprocess.h: CG_FUSED_CHANNELS and CG_MAX_COE.
FUSED_CHANNELS = 1024
MAX_COE = 64

TYPES = [
    "typedef struct {",
    "  uint32_t n, p, t, l, w_kw2, kw, coe;",
    "  uint32_t h, w, ci, co, ch, cw;",
    "  uint32_t csh, csw, csh_shift, csw_shift;",
    "  uint32_t is_bias, is_flatten, is_softmax;",
    "  uint32_t bias_offset, b_val_shift, b_bias_shift;",
    "  uint32_t ca_nzero, ca_shift, ca_pl_scale;",
    "  int32_t add_out_buffer_idx, add_in_buffer_idx;",
    "  uint32_t aa_nzero, aa_shift, aa_pl_scale;",
    "  uint32_t pool, ph, pw, pkh, pkw, psh, psw;",
    "  uint32_t upsample_h, upsample_w;",
    "  uint32_t softmax_frac;",
    "  uint32_t cm, cm_p0, xp_words, x_pad, output_words;",
    "  uint32_t x_bytes, x_bytes_first_pass;",
    "  uint32_t w_bytes, w_bytes_first_pass;",
    "  uint32_t transfers_and_passes, header_lo, header_hi;",
    "} model_bundle_t;",
    "",
]


def fuses(bundles, index, pe_rows, retained):
    """True when the runtime's fused pass (cg_fused_begin) takes bundle
    `index`: its packets go straight to the next input, so it needs no
    accumulation buffer. Mirrors cg_fused_begin's kinds."""
    b = bundles[index]
    save = next((r for r in retained if r["producer"] == index), None)
    if (b["p"] != 1 or b["csh"] != 1 or b["csw"] != 1 or b["csh_shift"] or b["csw_shift"] or
            b["ch"] != b["h"] or b["cw"] != b["w"] or b["add_in_buffer_idx"] != -1 or
            b["add_out_buffer_idx"] != -1 or b["is_softmax"] or b["is_flatten"] or
            b["co"] > FUSED_CHANNELS or b["coe"] > MAX_COE):
        return False
    upsampled = (b["upsample_h"], b["upsample_w"]) != (1, 1)
    pooled = b["pool"] != "POOL_NONE"
    if index + 1 == len(bundles):
        return (not pooled and not upsampled and save is None and
                b["debug_nhwc_words"] // b["n"] == b["h"] * b["w"] * b["co"])
    d = bundles[index + 1]
    if d.get("concat_input") is not None:
        return (not pooled and save is None and (b["upsample_h"], b["upsample_w"]) == (2, 2) and
                d["h"] == 2 * b["h"] and d["w"] == 2 * b["w"] and
                d["l"] == (2 * b["h"] + pe_rows - 1) // pe_rows)
    if upsampled or d["ci"] != b["co"]:
        return False
    if not pooled:
        return save is None and (d["h"], d["w"], d["l"]) == (b["h"], b["w"], b["l"])
    return (b["pool"] == "POOL_MAX" and (b["pkh"], b["pkw"], b["psh"], b["psw"]) == (2, 2, 2, 2) and
            b["h"] % 2 == 0 and b["w"] % 2 == 0 and
            (b["ph"] * 2, b["pw"] * 2) == (b["h"], b["w"]) and (d["h"], d["w"]) == (b["ph"], b["pw"]) and
            d["l"] == (b["ph"] + pe_rows - 1) // pe_rows and (save is None or save["prepool"] == 1))


def generate(vectors, output, hardware_path, payload_output,
             validation_data=0, bundle_count=None):
    """Checks one export against the hardware and writes model.h/.c, the
    payload and its manifest.

    Args:
        vectors: The export's vectors/ directory.
        output: Path of model.h; model.c is written beside it.
        hardware_path: The export's hardware.json.
        payload_output: Path of the W/B file; the manifest is
            <payload_output>.json.
        validation_data: 1 embeds the goldens (debug validation on target).
        bundle_count: Use only the first bundles (bring-up), default all.

    Returns:
        The manifest.
    """
    config_path = vectors / "config.json"
    config = json.loads(config_path.read_text())
    hardware = json.loads(hardware_path.read_text())
    defines = config["defines"]
    all_bundles = config["bundles"]
    bundle_count = len(all_bundles) if bundle_count is None else bundle_count
    if bundle_count < 1 or bundle_count > len(all_bundles):
        raise ValueError(f"bundle count must be between 1 and {len(all_bundles)}, got {bundle_count}")
    bundles = all_bundles[:bundle_count]
    pe_rows = defines["PE_ROWS"]

    # The export: what the runtime can run, and headers that round trip.
    tables.reject_unsupported_activations(bundles)
    concat_transitions, retained, _ = tables.build_concat_transitions(bundles)
    if any(plan["join_quant"]["bits"] != hardware["bits_input"]
           for plan in concat_transitions):
        raise ValueError("concat join width does not match hardware input width")
    tables.validate_upsampling(bundles)
    if hardware["processing_elements"][0] != pe_rows:
        raise ValueError("hardware.json PE rows do not match config.json")
    if hardware["bits_input"] != 1 << defines["X_BITS_L2"]:
        raise ValueError("hardware.json input width does not match config.json")
    for ib, bundle in enumerate(bundles):
        rebuilt = schedule.build_header(hardware, bundle)
        if rebuilt != bundle["header"]:
            raise ValueError(
                f"bundle {ib} header round trip failed: "
                f"0x{rebuilt:016x} != 0x{bundle['header']:016x}"
            )
        # A pass's channels x input width must fit the edge RAM, or its
        # address wraps (a layer wider than the edge RAM does not fit).
        edge_words = (bundle["cm_p0"] if bundle["p"] == 1 else
                      max(bundle["cm_p0"], bundle["cm"])) * bundle["w"]
        if bundle["kw"] > 1 and edge_words > hardware["ram_edges_depth"]:
            raise ValueError(f"bundle {ib} needs {edge_words} edge-RAM words, "
                             f"hardware has {hardware['ram_edges_depth']}")
    # One launch holds every bundle: the controller's table depth.
    if len(bundles) > hardware["max_n_bundles"]:
        raise ValueError(f"{len(bundles)} bundles, the hardware table holds "
                         f"{hardware['max_n_bundles']}")
    # Batch N (upstream's n): every bundle runs the N images, except a dense
    # bundle after a flatten, which runs them as the N rows of one item
    # (n = 1, h = N). A batched bundle's images are contiguous only when it
    # runs a single pass.
    batch = bundles[0]["n"]
    if not 1 <= batch <= hardware["max_batch_size"]:
        raise ValueError(f"batch {batch} outside 1..{hardware['max_batch_size']}")
    for ib, bundle in enumerate(bundles):
        rows = ib > 0 and bundles[ib - 1]["is_flatten"] and bundle["n"] == 1 and bundle["h"] == batch
        if bundle["n"] != batch and not rows:
            raise ValueError(f"bundle {ib} has batch {bundle['n']}, the model {batch}")
        if bundle["n"] > 1 and bundle["p"] != 1:
            raise ValueError(f"bundle {ib}: a batched bundle must run a single pass")
        if any(bundle[key] % batch for key in ("x_bpt", "x_bpt_p0", "xp_words", "debug_nhwc_words")):
            raise ValueError(f"bundle {ib} sizes are not a whole number of images")
        if bundle["pool"] not in POOL_IDS:
            raise ValueError(f"unsupported pool type: {bundle['pool']}")
    if defines["Y_TYPE_str"] not in ("int16", "int32"):
        raise ValueError(f"unsupported exported OCM type: {defines['Y_TYPE_str']}")
    if config.get("reference_format") != "computed_integer_graph_v1":
        raise ValueError("export uses the legacy reference format; "
                         "convert it with frontend/convert_legacy_vectors.py")
    output_quant = result_quantization(bundles[-1]["is_softmax"], bundles[-1].get("output_quant"))

    # The payload: W/B size, bias layout and the input boundary (input
    # replacement must not share a programmer word with persistent W/B).
    payload = (vectors / "wb.bin").read_bytes()
    if len(payload) != defines["WB_BYTES"]:
        raise ValueError("wb.bin size does not match exported WB_BYTES")
    bias_widths = {"int16": 2, "int32": 4}
    if defines["B_TYPE_str"] not in bias_widths:
        raise ValueError(f"unsupported exported bias type: {defines['B_TYPE_str']}")
    bias_width = bias_widths[defines["B_TYPE_str"]]
    bias_bytes = len(payload) - defines["W_BYTES"]
    if (hardware["bits_bias"] < bias_width * 8 or
            defines["W_BYTES"] % bias_width or
            bias_bytes != defines["B_WORDS"] * bias_width):
        raise ValueError("exported bias width, alignment or byte count is invalid")
    for ib, bundle in enumerate(bundles):
        if bundle["is_bias"] and not (
                0 <= bundle["b_offset"] <= defines["B_WORDS"] - bundle["co"]):
            raise ValueError(f"bundle {ib} bias range exceeds payload")
    if defines["WB_BYTES"] % 4:
        raise ValueError("external input boundary must be four-byte aligned")

    # Golden offsets per bundle; the intermediate ones also size the
    # runtime's packed-input buffer (x_bpt covers the whole batch).
    raw_offsets = [0]
    for bundle in bundles:
        raw_offsets.append(raw_offsets[-1] +
                           bundle["n"] * bundle["h"] * bundle["w"] * bundle["co"] * bundle["p"])
    intermediate_offsets = [0]
    for bundle in bundles[1:]:
        intermediate_offsets.append(intermediate_offsets[-1] +
            bundle["x_bpt_p0"] + (bundle["p"] - 1) * bundle["x_bpt"])

    # Residual slots {slot: producer}, written before read, one writer each,
    # contiguous from zero; one zero-filled slot keeps valid C storage when the
    # model has no skips. Words per slot are per image.
    residual_sources = {}
    for ib, bundle in enumerate(bundles):
        input_slot = bundle["add_in_buffer_idx"]
        output_slot = bundle["add_out_buffer_idx"]
        if input_slot >= 0 and input_slot not in residual_sources:
            raise ValueError(f"bundle {ib} reads residual slot {input_slot} before it is written")
        if output_slot >= 0:
            if output_slot in residual_sources:
                raise ValueError(f"residual slot {output_slot} has multiple writers")
            residual_sources[output_slot] = ib
    residual_slots = max(residual_sources, default=0) + 1
    if residual_sources and set(residual_sources) != set(range(residual_slots)):
        raise ValueError("residual slots must be contiguous from zero")
    residual_words = max([bundles[ib]["debug_nhwc_words"] // bundles[ib]["n"]
                          for ib in residual_sources.values()] + [1])
    output_words = max(bundle["debug_nhwc_words"] // bundle["n"] for bundle in bundles)
    final_words = bundles[-1]["debug_nhwc_words"]  # the whole batch's result

    hashes = {"config": manifest.config_sha256(config_path),
              "hardware": manifest.sha256(hardware_path.read_bytes()),
              "source_payload": manifest.sha256(payload)}
    layout = manifest.reservations(defines["WB_BYTES"] + defines["X_BYTES"])
    ident = manifest.identity(hashes["config"], hashes["hardware"],
                              payload[:defines["WB_BYTES"]], bundle_count)
    record = manifest.build(
        ident=ident, hashes=hashes, payload=payload, payload_name=payload_output.name,
        defines=defines, bias_bytes=bias_bytes, layout=layout,
        validation_data=validation_data, last_bundle=bundles[-1],
        final_words=final_words, output_quant=output_quant)
    payload_output.parent.mkdir(parents=True, exist_ok=True)
    payload_output.write_bytes(payload)
    payload_output.with_suffix(payload_output.suffix + ".json").write_text(
        json.dumps(record, indent=2) + "\n")

    limits = {
        "MODEL_BUNDLE_COUNT": len(bundles),
        "MODEL_BATCH": batch,
        "MODEL_WB_BYTES": defines["WB_BYTES"],
        "MODEL_W_BYTES": defines["W_BYTES"],
        "MODEL_PAYLOAD_BYTES": defines["WB_BYTES"] + defines["X_BYTES"],
        "MODEL_INPUT_BYTES": defines["X_BYTES"],
        "MODEL_PE_ROWS": pe_rows,
        "MODEL_OCM_WORDS": max(pe_rows * b["coe"] * (b["kw"] // 2 + 1) for b in bundles),
        # Per image; only bundles that do not run fused accumulate.
        "MODEL_ACCUMULATION_WORDS": max(
            [b["t"] * b["l"] * b["w"] * pe_rows * b["coe"]
             for i, b in enumerate(bundles) if not fuses(bundles, i, pe_rows, retained)] + [1]),
        "MODEL_VALID_RAW_WORDS": raw_offsets[-1],
        "MODEL_MAX_CONV_WORDS": max(b["ch"] * b["cw"] * b["co"] for b in bundles),
        "MODEL_MAX_OUTPUT_WORDS": output_words,
        "MODEL_FINAL_WORDS": final_words // batch,
        "MODEL_SOFTMAX_WORDS": final_words // batch if bundles[-1]["is_softmax"] else 1,
        "MODEL_INTERMEDIATE_BYTES": max(
            [end - begin for begin, end in zip(intermediate_offsets, intermediate_offsets[1:])] + [1]),
        "MODEL_INTERMEDIATE_EXPECTED_BYTES": max(1, intermediate_offsets[-1]),
        "MODEL_RESIDUAL_SLOTS": residual_slots,
        "MODEL_RESIDUAL_WORDS": residual_words,
        "MODEL_X_BITS": 1 << defines["X_BITS_L2"],
    }
    # model_bundle_t initializers; sizes per item, one of the bundle's n (the
    # launch scales the input sizes by n).
    fields = []
    for bundle in bundles:
        header = bundle["header"]
        n = bundle["n"]
        fields.append([
            *((name, bundle[name]) for name in (
                "n", "p", "t", "l", "w_kw2", "kw", "coe", "h", "w", "ci", "co", "ch", "cw",
                "csh", "csw", "csh_shift", "csw_shift", "is_bias", "is_flatten", "is_softmax")),
            ("bias_offset", bundle["b_offset"]),
            *((name, bundle[name]) for name in (
                "b_val_shift", "b_bias_shift", "ca_nzero", "ca_shift", "ca_pl_scale",
                "add_out_buffer_idx", "add_in_buffer_idx", "aa_nzero", "aa_shift", "aa_pl_scale")),
            ("pool", POOL_IDS[bundle["pool"]]),
            *((name, bundle[name]) for name in (
                "ph", "pw", "pkh", "pkw", "psh", "psw", "upsample_h", "upsample_w",
                "softmax_frac", "cm", "cm_p0")),
            ("xp_words", bundle["xp_words"] // n), ("x_pad", bundle["x_pad"]),
            ("output_words", bundle["debug_nhwc_words"] // n),
            ("x_bytes", bundle["x_bpt"] // n), ("x_bytes_first_pass", bundle["x_bpt_p0"] // n),
            ("w_bytes", bundle["w_bpt"]), ("w_bytes_first_pass", bundle["w_bpt_p0"]),
            ("transfers_and_passes", u32((bundle["t"] << 16) | bundle["p"])),
            ("header_lo", u32(header & 0xFFFFFFFF)), ("header_hi", u32(header >> 32)),
        ])
    out = [
        "#ifndef MODEL_H",
        "#define MODEL_H",
        "",
        "#include <stdint.h>",
        "",
        f"/* config.json SHA-256: {hashes['config']} */",
        f"/* wb.bin SHA-256: {hashes['source_payload']} */",
        f"/* hardware.json SHA-256: {hashes['hardware']} */",
        f"#define MODEL_HAS_VALIDATION_DATA {validation_data}",
        f"#define MODEL_DDR_PROTOCOL_VERSION {manifest.PROTOCOL_VERSION}",
        f'#define MODEL_IDENTITY_SHA256 "{ident}"',
        f"#define MODEL_PARAMETERS_CRC32 {u32(record['parameters_crc32'])}",
        "enum {",
        ",\n".join(f"  {name} = {value}" for name, value in limits.items()),
        "};",
        "",
        f"typedef {defines['Y_TYPE_str']}_t model_ocm_word_t;",
        f"typedef {defines['B_TYPE_str']}_t model_bias_word_t;",
        f'#define MODEL_PASS_MARKER "TARGET PREFIX {len(bundles)} PASS\\r\\n"',
        "",
        *TYPES,
        *emit_c.table("model_bundle_t", "model_bundles[MODEL_BUNDLE_COUNT]", fields),
        *emit_c.array("uint32_t model_identity[8]",
                      [u32(int(ident[i:i + 8], 16)) for i in range(0, 64, 8)]),
    ]
    if validation_data:
        golden = goldens.collect(vectors, bundles, pe_rows, residual_sources,
                                 residual_words, output_words, final_words)
        out += [
            *emit_c.array("uint32_t model_expected_raw_offsets[MODEL_BUNDLE_COUNT + 1]",
                          [str(value) for value in raw_offsets]),
            *emit_c.array("uint32_t model_expected_intermediate_offsets[MODEL_BUNDLE_COUNT]",
                          [str(value) for value in intermediate_offsets]),
            *emit_c.array("model_ocm_word_t model_expected_raw[MODEL_VALID_RAW_WORDS]",
                          [str(value) for value in golden["raw"]]),
            *emit_c.array2("int32_t model_expected_output"
                           "[MODEL_BUNDLE_COUNT][MODEL_BATCH * MODEL_MAX_OUTPUT_WORDS]",
                           golden["output"]),
            *emit_c.array2("int8_t model_expected_residual"
                           "[MODEL_RESIDUAL_SLOTS][MODEL_BATCH * MODEL_RESIDUAL_WORDS]",
                           golden["residual"]),
            f"#define MODEL_SOFTMAX_TOLERANCE {manifest.SOFTMAX_TOLERANCE:.5f}f",
            *emit_c.array("float model_expected_softmax[MODEL_BATCH * MODEL_FINAL_WORDS]",
                          [emit_c.c_float(value) for value in golden["softmax"]], 8),
            *emit_c.array("uint8_t model_expected_intermediate"
                          "[MODEL_INTERMEDIATE_EXPECTED_BYTES]",
                          [f"0x{value:02x}" for value in golden["intermediate"]]),
        ]
    out += [*tables.emit_concat_metadata(bundles),
            *tables.emit_fixedpoint_postprocess(bundles),
            *tables.emit_pool_postprocess(bundles),
            *tables.emit_compiled_postprocess(bundles),
            *tables.emit_concat_quant(bundles), "", "#endif", ""]
    header, source = emit_c.split_c_tables(out)
    output.write_text("\n".join(header))
    output.with_suffix(".c").write_text("\n".join(source))
    return record
