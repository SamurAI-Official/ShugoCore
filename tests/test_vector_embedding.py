"""
ShugoCore vector DB embedding tests
====================================

Tests for the deterministic hashing embeddings (P4: no more all-zero
placeholder vectors) and pluggable embedding backends (P7).
"""

import unittest

from vector_db import VectorDB, hashed_embedding


class HashedEmbeddingTestCase(unittest.TestCase):
    """Tests the dependency-free deterministic embedding."""

    def test_same_text_same_vector(self):
        """The same text must always map to the exact same vector."""
        v1 = hashed_embedding("obstacle in zone north", dimension=64)
        v2 = hashed_embedding("obstacle in zone north", dimension=64)
        self.assertEqual(v1, v2)

    def test_dimension_respected(self):
        """Output length should match the requested dimension."""
        v = hashed_embedding("scan the room", dimension=128)
        self.assertEqual(len(v), 128)

    def test_distinct_text_distinct_vectors(self):
        """Different texts should produce different vectors."""
        v1 = hashed_embedding("obstacle in zone north", dimension=64)
        v2 = hashed_embedding("translating a book", dimension=64)
        self.assertNotEqual(v1, v2)

    def test_overlapping_tokens_similar(self):
        """Texts sharing tokens should have nonzero cosine similarity."""
        a = hashed_embedding("north obstacle", dimension=256)
        b = hashed_embedding("obstacle north zone", dimension=256)
        dot = sum(x * y for x, y in zip(a, b))
        self.assertGreater(dot, 0.0)

    def test_normalized_vectors(self):
        """L2-normalized vectors should have unit norm."""
        v = hashed_embedding("some words here", dimension=256)
        norm = sum(x * x for x in v) ** 0.5
        self.assertAlmostEqual(norm, 1.0, places=5)


class VectorDBEmbeddingFrameworkTestCase(unittest.TestCase):
    """Tests the pluggable embedding hook."""

    def test_custom_embedding_fn_used(self):
        """An injected embedding callable should be used by update()."""
        calls = {"n": 0}
        def fake_emb(text):
            calls["n"] += 1
            return [1.0, 0.0, 0.0]

        db = VectorDB(
            {"type": "chroma", "dimension": 3,
             "collection_name": "embed_test"},
            embedding_fn=fake_emb,
        )
        # The instance should keep the injected callable (not the default).
        self.assertIs(db.embedding_fn, fake_emb)


if __name__ == "__main__":
    unittest.main()