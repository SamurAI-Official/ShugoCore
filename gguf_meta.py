#!/usr/bin/env python3
"""Minimal GGUF header reader: the geometry the layer planner needs.

Three numbers decide how much memory a device must have: how many blocks the model
has, how many key/value heads each block attends with, and the head dimension. They
are already written in the file's header, and reading them costs a few hundred bytes
of I/O -- loading the model to ask is not an option, because the whole point of the
split is that this node may not have room for it.

The planner uses them for two things an operator should not have to know by heart: a
layer's cache lives on whichever device holds that layer (so a device's real cost is
weights *plus* cache, and `--parallel N` multiplies the cache per slot), and the layer
count is a property of the model rather than a flag to remember.

Deliberately small and fail-closed: anything unexpected returns ``{}`` and the caller
keeps its previous behaviour (an operator-provided layer count, a weight-only budget)
instead of a number that is quietly wrong.
"""
import struct
from typing import Any, Dict, Optional

GGUF_MAGIC = b"GGUF"
# value type -> fixed width in bytes; strings (8) and arrays (9) are variable.
_FIXED_WIDTH = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1,
                10: 8, 11: 8, 12: 8}
# Keys we want, relative to the architecture prefix. None of them is an array: the
# huge arrays in a GGUF belong to the tokenizer and come after everything we need.
WANTED_SUFFIXES = ("block_count", "attention.head_count",
                   "attention.head_count_kv", "attention.key_length",
                   "attention.value_length", "embedding_length", "context_length")
# Keys that must be present before the scan may stop. `head_count_kv` is in here
# because a grouped-query model states far fewer KV heads than query heads (measured
# here: 2 against 14), and stopping before it turns a 7x under-estimate into a
# confident one -- the arrays that follow are skipped cheaply, so reading further is
# the cheap half of that trade.
NEEDED = ("block_count", "attention.head_count", "embedding_length",
          "attention.head_count_kv")


def read_metadata(path: str, *, max_bytes: int = 4 * 1024 * 1024,
                  max_pairs: int = 4096) -> Dict[str, Any]:
    """Architecture and geometry from a GGUF header, or ``{}`` for anything odd."""
    try:
        with open(str(path), "rb") as handle:
            return _parse(handle, max_bytes=max_bytes, max_pairs=max_pairs)
    except Exception:
        return {}


def layer_count(meta: Optional[Dict[str, Any]]) -> int:
    """Blocks in the model, 0 when unknown (the caller keeps its own answer then)."""
    try:
        return max(0, int((meta or {}).get("block_count", 0)))
    except (TypeError, ValueError):
        return 0


def kv_bytes_per_token(meta: Optional[Dict[str, Any]],
                       *, dtype_bytes: int = 2) -> int:
    """Cache bytes per token per layer: K and V, each ``heads x head_dim`` wide.

    ``dtype_bytes`` is the cache element width (2 for the f16 default; the operator
    changes it with ``-ctk``/``-ctv``). Returns 0 when the geometry is unknown, so the
    caller can say "not accounted for" rather than inventing a figure.
    """
    meta = meta or {}
    try:
        heads = int(meta.get("attention.head_count_kv")
                    or meta.get("attention.head_count") or 0)
        head_dim = int(meta.get("attention.key_length") or 0)
        if not head_dim:
            # Not stated: llama.cpp derives it from the embedding width and the head
            # count, so derive it the same way instead of giving up.
            embedding = int(meta.get("embedding_length") or 0)
            total_heads = int(meta.get("attention.head_count") or 0)
            head_dim = embedding // total_heads if total_heads else 0
        width = max(1, int(dtype_bytes or 2))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0
    if heads <= 0 or head_dim <= 0:
        return 0
    return 2 * heads * head_dim * width


def slot_cache_bytes(meta: Optional[Dict[str, Any]], *, context: int,
                     slots: int = 1, layers: int = 0,
                     dtype_bytes: int = 2) -> int:
    """Cache bytes for ``layers`` of a device's own holdings across ``slots`` slots."""
    per_token = kv_bytes_per_token(meta, dtype_bytes=dtype_bytes)
    try:
        count = int(layers or 0) or layer_count(meta)
        return max(0, per_token * max(0, int(context or 0))
                   * max(1, int(slots or 1)) * max(0, count))
    except (TypeError, ValueError):
        return 0


class _StopScan(Exception):
    """Raised for a value too large to walk: the scan ends where it stands.

    A GGUF's big arrays (the tokenizer's vocabulary) are not geometry, and their items
    are variable-length strings, so there is no way to skip them without walking them.
    Stopping is safe because the keys this reader wants come before them, and it is
    honest: a value it did not read is not one it can report.
    """


# -- parsing ---------------------------------------------------------------------
def _parse(handle, *, max_bytes: int, max_pairs: int) -> Dict[str, Any]:
    if handle.read(4) != GGUF_MAGIC:
        return {}
    version = _u32(handle.read(4))
    if version not in (2, 3):
        # Version 1 wrote 32-bit counts; refusing is better than misreading them.
        return {}
    handle.read(8)                                  # tensor count (unused)
    count = _u64(handle.read(8))
    if count <= 0 or count > max_pairs:
        return {}
    out: Dict[str, Any] = {}
    arch = ""
    consumed = 0
    for _ in range(count):
        name = _read_string(handle)
        if name is None:
            break
        consumed += len(name) + 8
        if consumed > max_bytes:
            break
        try:
            value, size = _read_value(handle)
        except _StopScan:
            break                                   # a value too big to walk
        consumed += size
        if name == "general.architecture":
            arch = str(value or "")
        elif arch and name.startswith(arch + "."):
            suffix = name.split(".", 1)[1]
            if suffix in WANTED_SUFFIXES:
                out[suffix] = value
        if arch and all(key in out for key in NEEDED):
            break                                   # before the tokenizer arrays
    if arch:
        out["architecture"] = arch
    return out


def _item_size(handle, item_kind: int) -> int:
    """Bytes consumed by one array item of a declared type (0 when unreadable).

    Array items do not repeat their type -- it is declared once in the array header --
    so they cannot go through ``_read_value``, which reads a type per value. Reading a
    type per item is how the first version of this walked four bytes out of step and
    lost every key after the array.
    """
    if item_kind in _FIXED_WIDTH:
        width = _FIXED_WIDTH[item_kind]
        return width if len(handle.read(width) or b"") == width else 0
    if item_kind == 12:                             # float64: fixed, just not mapped
        return 8 if len(handle.read(8) or b"") == 8 else 0
    if item_kind == 8:                              # string
        value = _read_string(handle)
        return 0 if value is None else 8 + len(value.encode("utf-8"))
    if item_kind == 9:                              # array of arrays
        return 16 if len(handle.read(12) or b"") == 12 else 0
    return 0


def _read_string(handle) -> Optional[str]:
    raw = handle.read(8)
    if len(raw) < 8:
        return None
    length = _u64(raw)
    if length == 0:
        return ""
    if length > 4 * 1024 * 1024:                    # not a key we are looking for
        return None
    data = handle.read(length)
    if len(data) < length:
        return None
    return data.decode("utf-8", "replace")


def _read_value(handle, depth: int = 0):
    """Return ``(value, bytes_consumed)``; arrays are skipped, not collected."""
    raw = handle.read(4)
    if len(raw) < 4:
        return None, 0
    kind = _u32(raw)
    if depth > 2:
        return None, 4
    if kind in _FIXED_WIDTH:
        width = _FIXED_WIDTH[kind]
        data = handle.read(width)
        if len(data) < width:
            return None, 4
        if kind == 6:
            return struct.unpack("<f", data)[0], 4 + width
        if kind == 7:
            return bool(data[0]), 4 + width
        return (int.from_bytes(data, "little", signed=kind in (1, 3, 5, 11)),
                4 + width)
    if kind == 12:
        data = handle.read(8)
        return (struct.unpack("<d", data)[0] if len(data) == 8 else None), 12
    if kind == 8:
        value = _read_string(handle)
        size = 8 + len(value.encode("utf-8")) if value is not None else 8
        return value, 4 + size
    if kind == 9:
        head = handle.read(12)                      # declared item type + item count
        if len(head) < 12:
            return None, 4
        item_kind = _u32(head[:4])
        items = _u64(head[4:])
        if items > 4096:
            raise _StopScan()
        spent = 16
        for _ in range(items):
            size = _item_size(handle, item_kind)
            spent += size
            if size == 0:
                break
        return None, spent
    return None, 4


def _u32(data: bytes) -> int:
    return int.from_bytes(data[:4], "little") if len(data) >= 4 else 0


def _u64(data: bytes) -> int:
    return int.from_bytes(data[:8], "little") if len(data) >= 8 else 0


if __name__ == "__main__":                          # pragma: no cover
    import json
    import sys

    meta = read_metadata(sys.argv[1] if len(sys.argv) > 1 else "")
    print(json.dumps(meta, indent=2, sort_keys=True))
    print("layers:", layer_count(meta),
          "kv/token/layer:", kv_bytes_per_token(meta),
          "slots-cache:", slot_cache_bytes(meta, context=2048, slots=1))
