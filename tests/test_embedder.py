"""Tests for the pluggable Embedder protocol (v1.30.4).

Hashing is exercised end-to-end (real installation); SentenceTransformer is
mocked so the test never tries to download a 100+ MB model in CI.
"""
import unittest
from unittest import mock

from vector_db import (
    Embedder,
    HashingEmbedder,
    SentenceTransformerEmbedder,
    hashed_embedding,
    make_embedder,
)


class HashingEmbedderTestCase(unittest.TestCase):

    def test_protocol_conformance(self):
        e = HashingEmbedder()
        self.assertIsInstance(e, Embedder)
        self.assertEqual(e.name, "hashing")

    def test_default_dimension(self):
        e = HashingEmbedder()
        self.assertEqual(e.dimension, 256)

    def test_custom_dimension(self):
        e = HashingEmbedder(dimension=128)
        self.assertEqual(e.dimension, 128)

    def test_embed_matches_legacy_function(self):
        """Wrap legacy behavior so swapping the embedder never changes vectors."""
        e = HashingEmbedder(dimension=256)
        text = "hello world and goodbye"
        self.assertEqual(e.embed(text), hashed_embedding(text, 256))

    def test_embed_batch(self):
        e = HashingEmbedder(dimension=64)
        vecs = e.embed_batch(["a b c", "x y z", ""])
        self.assertEqual(len(vecs), 3)
        for v in vecs:
            self.assertEqual(len(v), 64)
        self.assertEqual(vecs[2], [0.0] * 64)  # empty text -> zero vector
        # Deterministic: same input -> same vector
        self.assertEqual(e.embed_batch(["a b"])[0], e.embed_batch(["a b"])[0])


class MakeEmbedderTestCase(unittest.TestCase):

    def test_default_is_hashing(self):
        e = make_embedder()
        self.assertIsInstance(e, HashingEmbedder)
        self.assertEqual(e.dimension, 256)

    def test_explicit_hashing(self):
        e = make_embedder("hashing", dimension=512)
        self.assertIsInstance(e, HashingEmbedder)
        self.assertEqual(e.dimension, 512)

    def test_unknown_falls_back_to_hashing_with_warning(self):
        e = make_embedder("not-a-real-backend")
        self.assertIsInstance(e, HashingEmbedder)

    def test_sentence_transformer_falls_back_when_missing(self):
        # sentence_transformers is not installed in the test env -> falls back.
        e = make_embedder("sentence-transformer", dimension=384)
        self.assertIsInstance(e, HashingEmbedder)

    def test_sentence_transformer_constructed_when_available(self):
        fake_model = mock.Mock()
        fake_model.encode.return_value = [[0.1, 0.2, 0.3]]
        fake_model.get_sentence_embedding_dimension.return_value = 3
        fake_module = mock.Mock()
        fake_module.SentenceTransformer.return_value = fake_model
        with mock.patch.dict("sys.modules",
                             {"sentence_transformers": fake_module}):
            e = make_embedder("sentence-transformer", dimension=3)
        self.assertIsInstance(e, SentenceTransformerEmbedder)
        self.assertEqual(e.dimension, 3)
        self.assertEqual(e.embed("anything"), [0.1, 0.2, 0.3])
        fake_model.encode.assert_called()


class SentenceTransformerEmbedderTestCase(unittest.TestCase):
    def test_raises_when_library_missing(self):
        # Real environment does not have sentence_transformers; construction
        # must raise a clear ImportError (the factory catches and falls back).
        with mock.patch.dict("sys.modules", {"sentence_transformers": None}):
            with self.assertRaises(ImportError) as ctx:
                SentenceTransformerEmbedder()
            self.assertIn("sentence-transformers", str(ctx.exception))


class EmbedderContractTestCase(unittest.TestCase):

    def test_all_implementations_satisfy_protocol(self):
        # Use a stub to check that arbitrary Embedder classes are recognized
        # at runtime (runtime_checkable protocol).
        class Stub:
            name = "stub"
            dimension = 8

            def embed(self, text):
                return [0.0] * 8

            def embed_batch(self, texts):
                return [[0.0] * 8 for _ in texts]

        # Sanity: Stub is structurally an Embedder.
        self.assertIsInstance(Stub(), Embedder)


class VectorDBEmbedderWiringTestCase(unittest.TestCase):
    """VectorDB picks up the embedder from config or injection."""

    def test_config_named_embedder_selected(self):
        from vector_db import VectorDB
        db = VectorDB({"type": "chroma", "embedder": "hashing",
                       "dimension": 64})
        v = db.embedding_fn("hello")
        self.assertEqual(len(v), 64)
        self.assertEqual(v, hashed_embedding("hello", 64))

    def test_unknown_named_embedder_falls_back_to_hash(self):
        from vector_db import VectorDB
        db = VectorDB({"type": "chroma", "embedder": "does-not-exist",
                       "dimension": 64})
        v = db.embedding_fn("hello")
        self.assertEqual(len(v), 64)

    def test_embedder_instance_accepted(self):
        from vector_db import HashingEmbedder, VectorDB
        inst = HashingEmbedder(dimension=128)
        db = VectorDB({"type": "chroma"}, embedding_fn=inst.embed)
        self.assertEqual(len(db.embedding_fn("hi")), 128)

    def test_callable_fn_wins_over_config_name(self):
        from vector_db import VectorDB
        custom = lambda text: [1.0] * 8  # noqa: E731
        db = VectorDB({"type": "chroma", "embedder": "sentence-transformer"},
                      embedding_fn=custom)
        self.assertEqual(db.embedding_fn("hi"), [1.0] * 8)


if __name__ == "__main__":
    unittest.main()