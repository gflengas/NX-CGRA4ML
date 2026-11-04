"""C emission helpers for the generated model tables (model.h and model.c)."""


def c_float(value):
    """Returns a C float literal that round-trips the value."""
    text = f"{value:.9g}"
    if "." not in text and "e" not in text:
        text += ".0"
    return text + "f"


def rows(values, width, indent="  "):
    """Returns `values` as C initializer lines, `width` per line."""
    lines = []
    for offset in range(0, len(values), width):
        lines.append(indent + ", ".join(values[offset : offset + width]) + ",")
    return lines


def split_c_tables(lines):
    """Moves every `static const` table into a source file.

    Args:
        lines: Generated C lines of one combined header.

    Returns:
        (header, source): the header keeps types and constants and declares
        each table `extern const`; the source defines every table once.
    """
    header, source = [], ['#include "model.h"', ""]
    index = 0
    while index < len(lines):
        if not lines[index].startswith("static const "):
            header.append(lines[index])
            index += 1
            continue
        start = index
        while "= {" not in lines[index]:
            index += 1
        declaration = " ".join(line.strip() for line in lines[start:index + 1])
        declaration = declaration[len("static const "):declaration.index(" = {")]
        while lines[index] != "};" and not lines[index].endswith("};"):
            index += 1
        header.append(f"extern const {declaration};")
        source.extend(["const " + lines[start][len("static const "):], *lines[start + 1:index + 1], ""])
        index += 1
    return header, source


def u32(value):
    """Returns a UINT32_C hexadecimal literal."""
    return f"UINT32_C(0x{value:08x})"


def initializer(fields, per_line=4):
    """Returns the lines of one designated struct initializer.

    Args:
        fields: (name, C value text) pairs in declaration order.
        per_line: Fields per output line.
    """
    items = [f".{name}={value}" for name, value in fields]
    return (["  {"] +
            ["    " + ", ".join(items[i:i + per_line]) + ","
             for i in range(0, len(items), per_line)] +
            ["  },"])


def table(type_name, declaration, records):
    """Returns a `static const` table of struct initializers.

    Args:
        type_name: C struct type.
        declaration: Table name and size, e.g. "model_bundles[MODEL_BUNDLE_COUNT]".
        records: One list of (name, value) fields per entry; an empty table
            gets one zero entry so the C array is never empty.
    """
    lines = ["static const " + type_name, "    " + declaration + " = {"]
    for fields in records:
        lines.extend(initializer(fields))
    if not records:
        lines.append("  {0},")
    return lines + ["};", ""]


def array(declaration, values, width=12):
    """Returns a `static const` one-dimensional array, `width` values per line."""
    return ["static const " + declaration + " = {", *rows(values, width), "};", ""]


def array2(declaration, tables, width=12):
    """Returns a `static const` two-dimensional array of integers."""
    lines = ["static const " + declaration + " = {"]
    for values in tables:
        lines.append("  {")
        lines.extend(rows([str(value) for value in values], width, "    "))
        lines.append("  },")
    return lines + ["};", ""]
