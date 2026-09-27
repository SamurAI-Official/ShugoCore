"""GGUF header reader: the geometry the budget depends on.

The planner needs the model's block count and its cache width, and the file states
both. These tests build a header by hand so the reader can be checked without a
multi-gigabyte model, including the case that made it wrong once already: a
grouped-query model states far fewer KV heads than query heads (2 against 14 in the
model on this bench), and stopping the scan before that key turns a 7x under-estimate
into a confident one.
"""
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gguf_meta as gm  # noqa: E402

MIB = 1024 * 1024


def _string(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def _value(value) -> bytes:
    if isinstance(value, str):
        return struct.pack("<I", 8) + _string(value)
    if isinstance(value, bool):
        return struct.pack("<I", 7) + bytes([1 if value else 0])
    if isinstance(value, list):
        out = struct.pack("<I", 9) + struct.pack("<I", 8)      # array of strings
        out += struct.pack("<Q", len(value))
        for item in value:
            out += _string(str(item))
        return out
    if isinstance(value, int):
        return struct.pack("<I", 4) + struct.pack("<I", value)  # uint32
    raise AssertionError(f"unsupported test value: {value!r}")


def _gguf(pairs, *, version: int = 3) -> bytes:
    body = b"".join(_string(key) + _value(value) for key, value in pairs)
    return (b"GGUF" + struct.pack("<I", version)
            + struct.pack("<Q", 0)                             # no tensors
            + struct.pack("<Q", len(pairs)) + body)


class GgufMetaTestCase(unittest.TestCase):
    LLAMA = [("general.architecture", "llama"),
             ("llama.block_count", 32),
             ("llama.embedding_length", 4096),
             ("llama.attention.head_count", 32),
             ("llama.attention.head_count_kv", 8)]

    def _write(self, payload: bytes) -> str:
        handle, path = tempfile.mkstemp(suffix=".gguf")
        os.close(handle)
        with open(path, "wb") as fh:
            fh.write(payload)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_it_reads_the_geometry_the_planner_needs(self):
        meta = gm.read_metadata(self._write(_gguf(self.LLAMA)))
        self.assertEqual(meta["architecture"], "llama")
        self.assertEqual(gm.layer_count(meta), 32)
        self.assertEqual(meta["attention.head_count"], 32)
        self.assertEqual(meta["attention.head_count_kv"], 8)

    def test_the_cache_width_uses_the_kv_heads_and_derives_the_head_dimension(self):
        """head_dim is derived as llama.cpp derives it: embedding / query heads."""
        meta = gm.read_metadata(self._write(_gguf(self.LLAMA)))
        self.assertEqual(gm.kv_bytes_per_token(meta), 2 * 8 * 128 * 2)

    def test_without_a_kv_head_count_the_query_heads_are_the_conservative_answer(self):
        meta = gm.read_metadata(self._write(_gguf(self.LLAMA[:-1])))
        self.assertEqual(gm.kv_bytes_per_token(meta), 2 * 32 * 128 * 2)

    def test_a_quantised_cache_is_accounted_at_the_wider_of_the_two_halves(self):
        meta = gm.read_metadata(self._write(_gguf(self.LLAMA)))
        self.assertEqual(gm.kv_bytes_per_token(meta, dtype_bytes=1), 2 * 8 * 128)

    def test_slot_cache_scales_with_layers_and_slots(self):
        meta = gm.read_metadata(self._write(_gguf(self.LLAMA)))
        one = gm.slot_cache_bytes(meta, context=2048, slots=1, layers=4)
        self.assertEqual(one, 4096 * 2048 * 4)
        self.assertEqual(
            gm.slot_cache_bytes(meta, context=2048, slots=4, layers=4), one * 4)

    def test_an_array_between_the_keys_is_skipped_not_misread(self):
        pairs = [("general.architecture", "llama"),
                 ("llama.block_count", 32),
                 ("llama.embedding_length", 4096),
                 ("general.tags", ["a", "bbb", "cc"]),
                 ("llama.attention.head_count", 32),
                 ("llama.attention.head_count_kv", 8)]
        meta = gm.read_metadata(self._write(_gguf(pairs)))
        self.assertEqual(meta["attention.head_count_kv"], 8)
        self.assertEqual(gm.layer_count(meta), 32)

    def test_anything_unexpected_reports_nothing(self):
        """The caller keeps its old behaviour rather than a number that is wrong."""
        self.assertEqual(gm.read_metadata(self._write(b"not a gguf file")), {})
        self.assertEqual(gm.read_metadata(self._write(b"")), {})
        self.assertEqual(gm.read_metadata(self._write(b"GGUF")), {})
        # Version 1 wrote 32-bit counts, so it is refused rather than misread.
        self.assertEqual(gm.read_metadata(self._write(_gguf(self.LLAMA,
                                                            version=1))), {})
        self.assertEqual(gm.read_metadata(self._write(
            _gguf(self.LLAMA)[:24])), {})                      # truncated header
        self.assertEqual(gm.read_metadata(""), {})
        self.assertEqual(gm.read_metadata(os.path.join(tempfile.gettempdir(),
                                                       "no-such-model.gguf")), {})

    def test_unknown_geometry_is_zero_not_a_guess(self):
        self.assertEqual(gm.layer_count({}), 0)
        self.assertEqual(gm.layer_count(None), 0)
        self.assertEqual(gm.kv_bytes_per_token({}), 0)
        self.assertEqual(gm.kv_bytes_per_token({"embedding_length": 896}), 0)
        self.assertEqual(gm.slot_cache_bytes({}, context=2048), 0)


if __name__ == "__main__":
    unittest.main()
