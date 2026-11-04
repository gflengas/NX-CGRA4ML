"""Model identity and the payload manifest the host link checks.

The identity binds the runtime build to one export, hardware and parameter
set; the board prints it and the host refuses to load a payload
whose manifest names another.
"""

import hashlib
import json
import zlib

from ngultra import ddr_layout

PROTOCOL_VERSION = 2
SOFTMAX_TOLERANCE = 0.00001


def sha256(data):
    """Returns the SHA-256 hex digest of bytes."""
    return hashlib.sha256(data).hexdigest()


def config_sha256(config_path):
    """Hashes config.json without softmax_max_f: upstream's exporter takes it
    from the sample input and the runtime does not use it, so the identity does
    not depend on the input the model was exported with."""
    config = json.loads(config_path.read_text())
    for bundle in config["bundles"]:
        bundle.pop("softmax_max_f", None)
    return sha256(json.dumps(config, sort_keys=True).encode())


def identity(config_sha, hardware_sha, parameters, bundle_count):
    """Returns the model identity (SHA-256 hex) of one deployment."""
    return sha256(json.dumps({
        "config": config_sha, "hardware": hardware_sha,
        "parameters": sha256(parameters), "bundles": bundle_count,
        "protocol_version": PROTOCOL_VERSION,
    }, sort_keys=True).encode())


def reservations(logical_bytes):
    """Returns the DDR reservations after checking that the payload fits."""
    layout = ddr_layout.read()
    capacity = layout["mailbox_base"] - layout["payload_base"]
    if logical_bytes > capacity:
        raise ValueError("payload exceeds its DDR reservation")
    if (logical_bytes + 3) & ~3 > capacity:
        raise ValueError("scheduled payload exceeds its DDR reservation")
    return layout


def build(*, ident, hashes, payload, payload_name, defines, bias_bytes, layout,
          validation_data, last_bundle, final_words, output_quant):
    """Returns the payload manifest written next to the scheduled W/B file.

    Args:
        ident: Model identity.
        hashes: config, hardware and source payload SHA-256.
        payload: Scheduled W/B bytes (the file the host loads first).
        payload_name: File name of the payload, relative to the manifest.
        defines: The export's config.json defines.
        bias_bytes: Length of the bias range.
        layout: DDR reservations.
        validation_data: Whether the tables embed goldens.
        last_bundle: Final bundle record (result type and shape).
        final_words: Words of the final result.
        output_quant: Validated result quantization.
    """
    logical_bytes = defines["WB_BYTES"] + defines["X_BYTES"]
    parameters = payload[:defines["WB_BYTES"]]
    return {
        "schema": 2, "identity_sha256": ident,
        "protocol_version": PROTOCOL_VERSION,
        "config_sha256": hashes["config"], "hardware_sha256": hashes["hardware"],
        "source_payload_sha256": hashes["source_payload"],
        "scheduled_payload_sha256": sha256(payload),
        "parameters_sha256": sha256(parameters),
        "payload_file": payload_name,
        "payload_kind": "parameters",
        "file_bytes": len(payload),
        "logical_bytes": logical_bytes, "transfer_bytes": (logical_bytes + 3) & ~3,
        "crc32": zlib.crc32(payload), "parameters_crc32": zlib.crc32(parameters),
        "bias_type": defines["B_TYPE_str"], "byte_order": "little",
        "reservations": layout,
        "ranges": {
            "weights": {"offset": 0, "bytes": defines["W_BYTES"]},
            "biases": {"offset": defines["W_BYTES"], "bytes": bias_bytes},
            "input": {"offset": defines["WB_BYTES"], "bytes": defines["X_BYTES"]},
        },
        "validation_data": bool(validation_data),
        "result_words": final_words,
        "result_type": "float32" if last_bundle["is_softmax"] else "int32",
        "result_shape": [last_bundle[key] for key in ("on", "oh", "ow", "oc")],
        "result_layout": "NHWC",
        "result_quantization": output_quant,
        "softmax_tolerance": SOFTMAX_TOLERANCE,
    }
