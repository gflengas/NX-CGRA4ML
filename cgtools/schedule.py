"""Accelerator bundle headers: the 64-bit word the controller reads per bundle."""

import math


def clog2(value):
    """Returns ceil(log2(value)) of a positive value."""
    if value <= 0:
        raise ValueError(f"clog2 requires a positive value, got {value}")
    return math.ceil(math.log2(value))


def build_header(hardware, bundle):
    """Packs a bundle's 64-bit accelerator header; each field's width follows
    the hardware's maxima.

    Args:
        hardware: hardware.json of the export.
        bundle: Bundle record.
    """
    rows = hardware["processing_elements"][0]
    max_kernel_w = hardware["max_kernel_size"]
    if isinstance(max_kernel_w, list):
        max_kernel_w = max_kernel_w[1]
    max_image = hardware["max_image_size"]
    if isinstance(max_image, list):
        max_image_h, max_image_w = max_image
    else:
        max_image_h = max_image_w = max_image
    widths = {
        "kw2": clog2((max_kernel_w + 1) / 2),
        "cols": clog2(max_image_w),
        "blocks": clog2(math.ceil(max_image_h / rows)),
        "cin": clog2(hardware["max_channels_in"]),
        "batch": clog2(hardware["max_batch_size"]),
        "weight_addr": clog2(hardware["ram_weights_depth"]),
    }
    fields = (
        (bundle["kw"] // 2, widths["kw2"]),
        (bundle["w"] - 1, widths["cols"]),
        (bundle["l"] - 1, widths["blocks"]),
        (bundle["cm_p0"] - 1, widths["cin"]),
        (bundle["cm"] - 1, widths["cin"]),
        (bundle["n"] - 1, widths["batch"]),
        (bundle["kw"] * bundle["cm_p0"] - 1, widths["weight_addr"]),
        (bundle["kw"] * bundle["cm"] - 1, widths["weight_addr"]),
    )
    header = 0
    offset = 0
    for value, width in fields:
        if value < 0 or value >= 1 << width:
            raise ValueError(f"header value {value} does not fit in {width} bits")
        header |= value << offset
        offset += width
    if offset > hardware["header_width"]:
        raise ValueError("hardware header fields exceed header_width")
    return header
