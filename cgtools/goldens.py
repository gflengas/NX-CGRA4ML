"""Reference vectors of an export, for debug builds that validate on target.

The export's vectors/ hold the computed integer graph (format
computed_integer_graph_v1): raw accelerator words per pass and transfer, each
bundle's NHWC output, each bundle's packed input and the final softmax.
"""


def read_ints(path):
    """Returns the whitespace-separated integers of a text file."""
    return [int(value) for value in path.read_text().split()]


def compact_valid_words(values, bundle, pe_rows, it):
    """Keeps the raw accelerator words that land inside the output tensor.

    Args:
        values: Raw words of one pass and transfer, in packet order.
        bundle: Bundle record.
        pe_rows: Processing-element rows.
        it: Transfer (output channel block) index.

    Returns:
        The words whose (row, column, channel) is inside the bundle's output,
        in packet order; padding rows, columns and lanes are dropped.
    """
    compact = []
    offset = 0
    for _ in range(bundle["n"]):
        for il in range(bundle["l"]):
            for iw_kw2 in range(bundle["w_kw2"]):
                w_last = bundle["kw"] // 2 + 1 if iw_kw2 == bundle["w_kw2"] - 1 else 1
                words = pe_rows * bundle["coe"] * w_last
                packet = values[offset : offset + words]
                if len(packet) != words:
                    raise ValueError("raw vector ended inside a packet")
                for word, value in enumerate(packet):
                    ir = word % pe_rows
                    iw_last = (word // pe_rows) % w_last
                    icoe = word // (pe_rows * w_last)
                    y_h = il * pe_rows + ir
                    y_w = iw_kw2 + iw_last
                    y_c = bundle["coe"] * it + icoe
                    if y_h < bundle["h"] and y_w < bundle["w"] and y_c < bundle["co"]:
                        compact.append(value)
                offset += words
    if offset != len(values):
        raise ValueError(f"raw vector has {len(values) - offset} trailing words")
    return compact


def collect(vectors, bundles, pe_rows, residual_sources, residual_words,
            output_words, final_words):
    """Reads every golden the debug runtime compares on target.

    Args:
        vectors: The export's vectors/ directory.
        bundles: Bundle records.
        pe_rows: Processing-element rows.
        residual_sources: {residual slot: producer bundle}.
        residual_words: Words per residual slot.
        output_words: Words of one item's (image's) output in the output table.
        final_words: Words of the final result (the whole batch).

    Returns:
        Dict of tables: raw (valid raw words of every pass and transfer),
        output (each bundle's NHWC output, each image padded to
        output_words), intermediate (each bundle's packed input), residual
        (int8 slot contents, each image padded) and softmax.
    """
    raw, output = [], []
    for ib, bundle in enumerate(bundles):
        raw_words = bundle["n"] * bundle["h"] * bundle["w"] * bundle["co"] * bundle["p"]
        bundle_raw = []
        for ip in range(bundle["p"]):
            for it in range(bundle["t"]):
                expected_path = vectors / f"{ib}_{ip}_{it}_y_exp.txt"
                bundle_raw.extend(compact_valid_words(read_ints(expected_path), bundle, pe_rows, it))
        if len(bundle_raw) != raw_words:
            raise ValueError(f"bundle {ib} raw length disagrees with execution metadata")
        raw.extend(bundle_raw)
        values = read_ints(vectors / f"{ib}_y_nhwc_exp.txt")
        if len(values) != bundle["debug_nhwc_words"]:
            raise ValueError(f"bundle {ib} output length does not match config.json")
        image = len(values) // bundle["n"]  # one item of the bundle
        output.append([word for n in range(bundle["n"])
                       for word in values[n * image:(n + 1) * image] + [0] * (output_words - image)])

    intermediate = []
    for ib in range(1, len(bundles)):
        bundle = bundles[ib]
        input_bytes = (vectors / f"{ib}_x_exp.bin").read_bytes()
        if len(input_bytes) != bundle["x_bpt_p0"] + (bundle["p"] - 1) * bundle["x_bpt"]:
            raise ValueError(f"bundle {ib} input size disagrees with execution metadata")
        intermediate.extend(input_bytes)

    batch = bundles[0]["n"]
    residual = [[0] * (batch * residual_words)
                for _ in range(max(residual_sources, default=0) + 1)]
    for slot, ib in residual_sources.items():
        words = bundles[ib]["debug_nhwc_words"] // batch
        for n in range(batch):
            values = output[ib][n * output_words:n * output_words + words]
            if any(value < -128 or value > 127 for value in values):
                raise ValueError(f"residual slot {slot} contains a value outside int8")
            residual[slot][n * residual_words:n * residual_words + words] = values
    softmax = ([float(value) for value in (vectors / "y_exp.txt").read_text().split()]
               if bundles[-1]["is_softmax"] else [0.0] * final_words)
    if len(softmax) != final_words:
        raise ValueError("final output length does not match config.json")
    return {"raw": raw, "output": output, "intermediate": intermediate or [0],
            "residual": residual, "softmax": softmax}
