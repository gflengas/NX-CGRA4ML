"""Host side of the DDR mailbox protocol, and the result format.

The board announces each request on UART ("external: ready ..." to receive an
input, "external: result ..." to release a result). For a ready line the host
writes BEGIN, loads the scheduled W/B and input (the first request) or only the
input (later requests), reads every byte back and publishes completion with a
CRC. For a result line it reads the tensor, saves it with its metadata,
optionally compares it with a golden, checks the resident W/B is unchanged and
acknowledges. The mailbox is signalled only after an exact readback.
--saved-result applies the same result processing to a simulator's output.

Board access uses ngultra.memory (an OpenOCD must be attached).
"""

import argparse
import hashlib
import json
import math
import re
import struct
import time
import zlib
from pathlib import Path

from ngultra import ddr_layout


def validate_result_quantization(kind, quant):
    """Checks the quantization metadata of a result type.

    Returns:
        A copy of `quant`: {"kind": "probability"} for float32 results, a
        signed zero-point-zero fixed-point description for int32 results.
    """
    if kind == "float32":
        if quant != {"kind": "probability"}:
            raise ValueError("float32 results require probability metadata")
        return dict(quant)
    if kind != "int32" or not isinstance(quant, dict) or set(quant) != {
            "kind", "signed", "bits", "fractional_bits", "scale", "zero_point"}:
        raise ValueError("integer result requires complete quantization metadata")
    if (quant["kind"] != "fixed_point" or quant["signed"] is not True or
            type(quant["bits"]) is not int or not (2 <= quant["bits"] <= 8 or quant["bits"] == 32) or
            type(quant["fractional_bits"]) is not int or
            not -15 <= quant["fractional_bits"] <= 15 or
            type(quant["zero_point"]) is not int or quant["zero_point"] != 0 or
            type(quant["scale"]) not in (int, float) or
            quant["scale"] != 2.0 ** -quant["fractional_bits"]):
        raise ValueError("invalid or unsupported signed fixed-point quantization")
    return dict(quant)


def result_quantization(is_softmax, exported):
    """Describes how the host interprets the final tensor.

    Args:
        is_softmax: The last bundle outputs float probabilities.
        exported: The export's output_quant ({"bits", "frac"}) for integer results.

    Returns:
        Validated result metadata for the payload manifest.
    """
    if is_softmax:
        return {"kind": "probability"}
    if not isinstance(exported, dict) or set(exported) != {"bits", "frac"}:
        raise ValueError("missing/invalid output_quant in the export")
    if type(exported["frac"]) is not int or not -15 <= exported["frac"] <= 15:
        raise ValueError("invalid fractional bits")
    return validate_result_quantization("int32", {
        "kind": "fixed_point", "signed": True, "bits": exported["bits"],
        "fractional_bits": exported["frac"], "scale": 2.0 ** -exported["frac"],
        "zero_point": 0,
    })


READY = re.compile(
    r"external: ready identity=([0-9a-f]{64}) address=([0-9]+) bytes=([0-9]+) "
    r"mailbox=([0-9]+) sequence=([0-9]+)"
)
RESULT = re.compile(
    r"external: result identity=([0-9a-f]{64}) sequence=([0-9]+) address=([0-9]+) "
    r"words=([0-9]+) float=([01]) mailbox=([0-9]+)"
)
CHUNK_BYTES = 16384


def sha256(data):
    """Returns the SHA-256 hex digest of bytes."""
    return hashlib.sha256(data).hexdigest()


def assemble_input(parameters, manifest, input_path, first=False):
    """Combine fixed scheduled parameters with an explicit scheduled first X."""
    if input_path is None:
        raise ValueError("an explicit packed input is required")
    inputs = manifest["ranges"]["input"]
    if (len(parameters) != inputs["offset"] or
            sha256(parameters) != manifest["parameters_sha256"]):
        raise ValueError("scheduled parameter identity/length mismatch")
    data = input_path.read_bytes()
    if len(data) != inputs["bytes"]:
        raise ValueError("replacement input length differs from compiled input")
    return parameters + data if first else data


def save_result(result, manifest, sequence, destination, golden=None, requests=None):
    """Save a released tensor before optional comparison or acknowledgement."""
    words = manifest["result_words"]
    kind = manifest["result_type"]
    shape = manifest["result_shape"]
    if (sequence < 1 or kind not in ("int32", "float32") or
            len(result) != words * 4 or math.prod(shape) != words or
            any(d <= 0 for d in shape) or manifest["result_layout"] != "NHWC"):
        raise ValueError("invalid result type/shape/length/sequence")
    quantization = validate_result_quantization(kind, manifest.get("result_quantization"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(result)
    metadata = {"sequence": sequence, "identity_sha256": manifest["identity_sha256"],
                "type": kind, "quantization": quantization, "shape": shape, "layout": manifest["result_layout"],
                "byte_order": "little", "bytes": len(result), "sha256": sha256(result),
                "comparison": "not_requested"}
    if requests is not None:
        if not sequence <= requests <= 0xffffffff:
            raise ValueError("invalid result session count")
        metadata["session_requests"] = requests
    metadata_path = destination.with_suffix(destination.suffix + ".json")
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
    if golden is not None:
        values = struct.unpack("<" + ("f" if kind == "float32" else "i") * words, result)
        expected = [float(x) if kind == "float32" else int(x)
                    for x in golden.read_text().split()]
        tolerance = manifest["softmax_tolerance"] if kind == "float32" else 0
        passed = len(expected) == words and all(abs(a-b) <= tolerance
                                              for a,b in zip(values, expected))
        metadata["comparison"] = "passed" if passed else "failed"
        metadata["golden_sha256"] = sha256(golden.read_bytes())
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
        if not passed:
            raise RuntimeError("result differs from independent golden tensor")
    return metadata


def main(argv=None):
    """`cgtools load`: serves one ready or result line of a board session."""
    parser = argparse.ArgumentParser(prog="cgtools load", description=__doc__)
    phase = parser.add_mutually_exclusive_group(required=True)
    phase.add_argument("--ready-line", help="complete readiness line from this boot")
    phase.add_argument("--result-line", help="read result, compare golden and acknowledge consumption")
    phase.add_argument("--saved-result", type=Path,
                       help="process simulator result bytes without board access")
    parser.add_argument("--sequence", type=int, help="sequence of --saved-result")
    parser.add_argument("--requests", type=int,
                        help="finite session length; required for first inference BEGIN")
    parser.add_argument("--input", type=Path, help="explicit packed X for every inference, including the first")
    parser.add_argument("--golden", type=Path, help="optional host tensor oracle, checked before acknowledgement")
    parser.add_argument("--result-output", type=Path, help="save exact result bytes and metadata; required for board results")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--payload", type=Path)
    parser.add_argument("--app-bin", type=Path)
    parser.add_argument("--bitstream", type=Path)
    parser.add_argument("--expected-app-sha256")
    parser.add_argument("--expected-bitstream-sha256")
    parser.add_argument("--app-code-bytes", type=lambda s: int(s, 0),
                        help="immutable prefix ending before .data, measured from selected ELF")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    if args.requests is not None and not 1 <= args.requests <= 0xffffffff:
        parser.error("--requests must be a positive uint32 count")
    manifest = json.loads(args.manifest.read_text())
    validate_result_quantization(manifest["result_type"], manifest.get("result_quantization"))
    if args.saved_result:
        if args.sequence is None or args.check_only or args.input:
            parser.error("saved result needs --sequence and cannot load input or use --check-only")
        save_result(args.saved_result.read_bytes(), manifest, args.sequence,
                    args.result_output or args.saved_result, args.golden, args.requests)
        print("saved result: " + ("numerical comparison passed" if args.golden else
                                 "inference complete; numerical comparison not requested"))
        return
    if not all((args.app_bin, args.bitstream, args.expected_app_sha256,
                args.expected_bitstream_sha256, args.app_code_bytes)):
        parser.error("board access requires app/bitstream paths, expected hashes and app code bytes")
    reservation = manifest["reservations"]
    canonical = ddr_layout.read()
    if (reservation != canonical or manifest["schema"] != 2 or
            manifest.get("protocol_version") != 2):
        parser.error("manifest DDR reservations/schema differ from canonical runtime")
    if (manifest["logical_bytes"] <= 0 or
            manifest["transfer_bytes"] != ((manifest["logical_bytes"] + 3) & ~3) or
            manifest["transfer_bytes"] > reservation["mailbox_base"] - reservation["payload_base"]):
        parser.error("manifest transfer length exceeds or disagrees with reservation")
    if args.result_line:
        match = RESULT.fullmatch(args.result_line.strip())
        if not match or args.input or not args.result_output:
            parser.error("result acknowledgement needs a complete result line and --result-output")
        identity = match[1]
        sequence, address, words, is_float, mailbox = map(int, match.groups()[1:])
        length = words * 4
        if (words != manifest["result_words"] or
                is_float != (manifest["result_type"] == "float32") or address % 4 or
                address < reservation["workspace_base"] or
                address + length > reservation["payload_base"]):
            parser.error("result is outside the compiled tensor/workspace contract")
    else:
        match = READY.fullmatch(args.ready_line.strip())
        if not match or args.golden:
            parser.error("load needs a complete readiness line")
        identity = match[1]
        address, length, mailbox, sequence = map(int, match.groups()[1:])
        if sequence == 1:
            wanted_address = reservation["payload_base"]
            wanted_bytes = manifest["logical_bytes"]
            if not args.input:
                parser.error("first inference requires an explicit --input")
            elif args.requests is None:
                parser.error("first inference requires a finite --requests count")
        else:
            if not args.input:
                parser.error("later requests need an explicit --input for inference mode")
            wanted_address = reservation["payload_base"] + manifest["ranges"]["input"]["offset"]
            wanted_bytes = manifest["ranges"]["input"]["bytes"]
        if address != wanted_address or length != wanted_bytes:
            parser.error("readiness range differs from selected deployment")
        if address % 4 or address + ((length + 3) & ~3) > mailbox:
            parser.error("unaligned, overlapping or oversized transfer")
    if (sequence < 1 or identity != manifest["identity_sha256"] or
            mailbox != reservation["mailbox_base"] or
            mailbox + 256 > reservation["reserved_end"]):
        parser.error("identity/mailbox/sequence differs from selected deployment")
    transfer = (length + 3) & ~3
    payload_path = args.payload or args.manifest.parent / manifest["payload_file"]
    payload = payload_path.read_bytes()
    app = args.app_bin.read_bytes()
    bitstream = args.bitstream.read_bytes()
    if (len(payload) != manifest["file_bytes"] or sha256(payload) != manifest["scheduled_payload_sha256"] or
            zlib.crc32(payload) != manifest["crc32"]):
        parser.error("payload length/hash/CRC differs from selected deployment")
    if sha256(app) != args.expected_app_sha256.lower():
        parser.error("app image SHA-256 differs from selected artifact")
    if sha256(bitstream) != args.expected_bitstream_sha256.lower():
        parser.error("bitstream SHA-256 differs from selected artifact")
    if not (0 < args.app_code_bytes <= len(app) <= 131072) or args.app_code_bytes % 4:
        parser.error("invalid immutable app prefix or boot image size")
    print(f"app SHA-256: {sha256(app)}", flush=True)
    print(f"bitstream SHA-256: {sha256(bitstream)}", flush=True)
    print(f"payload SHA-256: {sha256(payload)}", flush=True)
    ranges = manifest["ranges"]
    weights, biases, inputs = (ranges[name] for name in ("weights", "biases", "input"))
    bias_width = {"int16": 2, "int32": 4}.get(manifest["bias_type"])
    if bias_width is None:
        parser.error("unsupported scheduled bias type")
    if (weights["offset"] != 0 or weights["bytes"] != biases["offset"] or
            biases["offset"] + biases["bytes"] != inputs["offset"] or
            inputs["offset"] + inputs["bytes"] != manifest["logical_bytes"] or
            manifest["payload_kind"] != "parameters" or len(payload) != inputs["offset"] or
            inputs["offset"] % 4 or biases["offset"] % bias_width or
            biases["bytes"] % bias_width or manifest["byte_order"] != "little"):
        parser.error("invalid scheduled W/B/X ranges or types")
    parameters = payload[:inputs["offset"]]
    if sha256(parameters) != manifest["parameters_sha256"]:
        parser.error("parameter identity mismatch")
    if args.input:
        payload = assemble_input(parameters, manifest, args.input, first=sequence == 1)
        print(f"input SHA-256: {sha256(args.input.read_bytes())}", flush=True)
    if args.check_only:
        print(f"local contract exact: {length} logical / {transfer} transfer bytes at {hex(address)}")
        return
    from ngultra.memory import Memory
    with Memory() as memory:
        # Attest live immutable application bytes, not merely local hashes.
        for offset in range(0, args.app_code_bytes, CHUNK_BYTES):
            expected = app[offset:min(offset + CHUNK_BYTES, args.app_code_bytes)]
            if memory.read(0x80000000 + offset, len(expected)) != expected:
                raise RuntimeError(f"live application code mismatch at {offset}")
        status = struct.unpack("<32I", memory.read(mailbox, 128))
        identity_words = [int(identity[i:i+8], 16) for i in range(0, 64, 8)]
        if status[1] != sequence or list(status[4:12]) != identity_words:
            raise RuntimeError("live mailbox identity/sequence differs from this request")
        requests = args.requests if sequence == 1 and not args.result_line else status[16]
        if (requests is None or not sequence <= requests <= 0xffffffff or
                (args.requests is not None and requests != args.requests)):
            raise RuntimeError("invalid or inconsistent session count")
        if args.result_line:
            if status[0] != 4 or tuple(status[12:15]) != (address, words, is_float):
                raise RuntimeError("result is not released for host consumption")
            if status[15] != len(parameters):
                raise RuntimeError("live parameter size differs from deployment")
            result = memory.read(address, length)
            save_result(result, manifest, sequence, args.result_output, args.golden, requests)
            for offset in range(0, len(parameters), CHUNK_BYTES):
                expected_chunk = parameters[offset:offset + CHUNK_BYTES]
                if memory.read(reservation["payload_base"] + offset, len(expected_chunk)) != expected_chunk:
                    raise RuntimeError("resident W/B changed")
            if struct.unpack("<32I", memory.read(mailbox, 128)) != status:
                raise RuntimeError("result ownership changed before acknowledgement")
            if memory.read(mailbox + 128, 128) != bytes(128):
                raise RuntimeError("request is not clear before result acknowledgement")
            request = struct.pack("<32I", 0, sequence, 0, 0, 0, *identity_words, requests, *([0] * 18))
            memory.write(mailbox + 128, request)
            if memory.read(mailbox + 128, 128) != request:
                raise RuntimeError("result acknowledgement metadata mismatch")
            memory.write_word(mailbox + 128, 5)
            comparison = "numerical comparison passed" if args.golden else "numerical comparison not requested"
            print(f"inference complete: sequence={sequence} words={words}; {comparison}; "
                  "resident W/B unchanged; consumed", flush=True)
            return
        if list(status[:4]) != [1, sequence, address, length]:
            raise RuntimeError("live mailbox does not release this input")
        if memory.read(mailbox + 128, 128) != bytes(128):
            raise RuntimeError("request is not clear before loading")
        begin = struct.pack("<32I", 0, sequence, address, length, 0,
                            *identity_words, requests, *([0] * 18))
        memory.write(mailbox + 128, begin)
        if memory.read(mailbox + 128, 128) != begin:
            raise RuntimeError("BEGIN metadata readback mismatch")
        memory.write_word(mailbox + 128, 6)
        receiving = list(status)
        receiving[0], receiving[16] = 7, requests
        deadline = time.monotonic() + 10
        while True:
            current = struct.unpack("<32I", memory.read(mailbox, 128))
            if current == tuple(receiving):
                break
            if (current[0] != 1 or current[1:16] != status[1:16] or
                    current[16] not in (status[16], requests) or current[17:] != status[17:]):
                raise RuntimeError("unexpected ownership after BEGIN")
            if time.monotonic() >= deadline:
                raise RuntimeError("BEGIN acknowledgement timeout")
        status = tuple(receiving)
        if memory.read(mailbox + 128, 128) != bytes(128):
            raise RuntimeError("request is not clear after BEGIN")
        padded = payload + bytes(transfer - length)
        for offset in range(0, transfer, CHUNK_BYTES):
            expected = padded[offset:offset + CHUNK_BYTES]
            memory.write(address + offset, expected)
            actual = memory.read(address + offset, len(expected))
            if actual != expected:
                raise RuntimeError(f"DAP-AXI readback mismatch at offset {offset}: "
                                   f"{sha256(actual)} != {sha256(expected)}")
        print(f"DAP-AXI readback exact: {length} logical / {transfer} transfer bytes", flush=True)
        if struct.unpack("<32I", memory.read(mailbox, 128)) != status:
            raise RuntimeError("CPU ownership changed during loading")
        request = struct.pack("<32I", 0, sequence, address, length, zlib.crc32(payload),
                              *identity_words, requests, *([0] * 18))
        memory.write(mailbox + 128, request)
        if memory.read(mailbox + 128, 128) != request:
            raise RuntimeError("request metadata readback mismatch")
        memory.write_word(mailbox + 128, 2)
        print(f"completion published: sequence={sequence}", flush=True)

