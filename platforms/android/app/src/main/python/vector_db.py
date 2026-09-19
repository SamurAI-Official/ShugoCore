import hashlib
import logging
import math
import re
from datetime import datetime
try:
    import chromadb
    _HAS_CHROMA = True
except ImportError:
    _HAS_CHROMA = False
from typing import Callable, Dict, Any, List, Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Embedder protocol (v1.30.4)
# ---------------------------------------------------------------------------
@runtime_checkable
class Embedder(Protocol):
    """Pluggable embedding backend (v1.30.4).

    An Embedder converts text into a fixed-dimension vector that Tier 2
    stores index and search against. Implementations must be deterministic
    for the working set so that facts written through one backend can be
    retrieved through another; ``hashed_embedding`` provides this for
    free, and a real Sentence Transformer model satisfies it by virtue of
    being deterministic.

    Built-in implementations:

      * :class:`HashingEmbedder` — dependency-free (default). Same
        algorithm as ``hashed_embedding``.
      * :class:`SentenceTransformerEmbedder` — optional, lazily imported;
        only usable when ``sentence_transformers`` is installed.
    """

    name: str
    dimension: int

    def embed(self, text: str) -> List[float]: ...
    def embed_batch(self, texts: List[str]) -> List[List[float]]: ...


def hashed_embedding(text: str, dimension: int = 256,
                     normalize: bool = True) -> List[float]:
    """Dependency-free deterministic hashing bag-of-words embedding.

    Mirrors ``SemanticMemory._embed`` so Tier 2 vectors are consistent no
    matter which store backs them. Identical text always maps to the same
    vector, and semantically overlapping token sets land nearby in
    cosine space - a real search signal (unlike the older all-zero
    placeholder vectors).

    Args:
        text: The text to embed.
        dimension: Vector length (default 256 to match SemanticMemory).
        normalize: L2-normalize the result (enables cosine comparison).

    Returns:
        A list of floats of length ``dimension``.
    """
    vector = [0.0] * int(dimension)
    for token in re.findall(r"[a-z0-9]+", str(text).lower()):
        digest = int(hashlib.sha256(token.encode("utf-8")).hexdigest(), 16)
        index = digest % int(dimension)
        sign = 1.0 if (digest >> 128) & 1 else -1.0
        vector[index] += sign
    if normalize:
        norm = math.sqrt(sum(v * v for v in vector))
        if norm > 0:
            vector = [v / norm for v in vector]
    return vector


class HashingEmbedder:
    """Default dependency-free embedder (SHA-256 hashing bag-of-words).

    Wraps :func:`hashed_embedding` in the :class:`Embedder` protocol so
    callers can plug it into ``VectorDB(embedding=...)`` interchangeably
    with any other Embedder.
    """

    name = "hashing"
    dimension = 256

    def __init__(self, dimension: int = 256):
        self.dimension = max(16, int(dimension))

    def embed(self, text: str) -> List[float]:
        return hashed_embedding(str(text), self.dimension)

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        return [self.embed(t) for t in texts]


class SentenceTransformerEmbedder:
    """Real semantic embeddings via sentence-transformers (optional).

    The dependency is imported lazily inside ``__init__``; if the library
    is not installed, construction raises ``ImportError`` so the caller
    can fall back to :class:`HashingEmbedder`. ``model_name`` defaults to
    ``all-MiniLM-L6-v2`` (384 dims, fast, well-tested).
    """

    name = "sentence-transformer"
    dimension = 384

    def __init__(self, model_name: str = "all-MiniLM-L6-v2",
                 dimension: Optional[int] = None, **kwargs: Any):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ImportError(
                "sentence-transformers is required for the sentence-transformer "
                "embedding backend. Install it with: "
                "pip install sentence-transformers"
            ) from exc
        self._model = SentenceTransformer(model_name, **kwargs)
        if dimension is not None:
            self.dimension = int(dimension)
        else:
            getter = getattr(self._model,
                             "get_sentence_embedding_dimension", None)
            if callable(getter):
                self.dimension = int(getter())
            else:  # very old sentence-transformers fallback
                first = next(iter(getattr(self._model, "_modules",
                                          {}).values()), None)
                self.dimension = int(getattr(first, "embed_dim", 384))
        self.model_name = str(model_name)

    def embed(self, text: str) -> List[float]:
        vec = self._model.encode([str(text)], convert_to_numpy=True)[0]
        return [float(v) for v in vec]

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        vectors = self._model.encode([str(t) for t in texts],
                                     convert_to_numpy=True)
        return [[float(v) for v in vec] for vec in vectors]


def make_embedder(name: str = "hashing", dimension: int = 256,
                  **kwargs: Any) -> Embedder:
    """Build an :class:`Embedder` by name.

    Falls back to :class:`HashingEmbedder` when the requested backend is
    unavailable (e.g. ``sentence-transformer`` without the library) or
    the name is unrecognized. Never raises on missing dependencies — a
    real semantic search is strictly an upgrade on top of the
    dependency-free baseline, and a missing dependency must never block
    the engine.
    """
    key = (name or "hashing").lower()
    if key in ("hashing", "hash"):
        return HashingEmbedder(dimension=dimension)
    if key in ("sentence-transformer", "sentence_transformer", "st"):
        try:
            return SentenceTransformerEmbedder(dimension=dimension, **kwargs)
        except ImportError as exc:
            logging.getLogger(__name__).warning(
                "sentence-transformer backend unavailable (%s); "
                "falling back to HashingEmbedder", exc)
            return HashingEmbedder(dimension=dimension)
    logging.getLogger(__name__).warning(
        "unknown embedder %r; falling back to HashingEmbedder", name)
    return HashingEmbedder(dimension=dimension)


class VectorDB:
    def __init__(self, config: Dict[str, Any],
                 embedding_fn: Optional[Callable[[str], List[float]]] = None):
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.db_type = config.get("type", "unknown")
        self.dimension = int(config.get("dimension", 256))
        self.collection_name = config.get("collection_name", "default")
        # Pluggable embedding backend (P7 / v1.30.4): callers may inject an
        # Embedder instance OR a callable via ``embedding_fn``; otherwise the
        # config may name one via ``embedder`` ("hashing" default,
        # "sentence-transformer" optional). The default stays the
        # deterministic hash embedder so similarity search works out of the
        # box with zero deps.
        named = config.get("embedder")
        if embedding_fn is not None:
            self.embedding_fn = embedding_fn
        elif isinstance(named, Embedder):
            self.embedding_fn = named.embed
        elif named:
            embedder = make_embedder(str(named), dimension=self.dimension)
            self.embedding_fn = embedder.embed
        else:
            self.embedding_fn = (
                lambda text: hashed_embedding(str(text), self.dimension))

        self.client = None
        self.collection = None

        self.initialize_db()
    
    def initialize_db(self):
        if self.db_type == "chroma":
            if not _HAS_CHROMA:
                self.logger.warning("chromadb not installed; VectorDB will operate in stub mode.")
                return
            self.client = chromadb.PersistentClient(path="./chroma_db")
            self.collection = self.client.get_or_create_collection(name=self.collection_name)
            self.logger.info(f"Initialized ChromaDB with collection '{self.collection_name}'.")
        else:
            self.logger.error(f"Unsupported vector database type: {self.db_type}")
            raise ValueError("Unsupported vector database type.")
    
    def store_vector(self, id: str, vector: List[float], metadata: Dict[str, Any] = None):
        if self.collection is not None:
            self.collection.add(ids=[id], embeddings=[vector], metadatas=[metadata or {}])
            self.logger.info(f"Stored vector for ID: {id}")
    
    def retrieve_vector(self, id: str) -> Dict[str, Any]:
        if self.collection is not None:
            results = self.collection.get(ids=[id], include=["embeddings", "metadatas"])
            if results and results["ids"]:
                return {
                    "id": results["ids"][0],
                    "vector": results.get("embeddings", [None])[0] if results.get("embeddings") else None,
                    "metadata": results.get("metadatas", [None])[0] if results.get("metadatas") else None,
                }
        return {}

    def query_vectors(self, query_vector: List[float], top_k: int = 5) -> List[Dict[str, Any]]:
        if self.collection is not None:
            results = self.collection.query(query_embeddings=[query_vector], n_results=top_k)
            if results and results["ids"]:
                ids = results["ids"][0]
                distances = results["distances"][0] if results.get("distances") else [None] * len(ids)
                metadatas = results["metadatas"][0] if results.get("metadatas") else [None] * len(ids)
                return [
                    {"id": ids[i], "distance": distances[i], "metadata": metadatas[i]}
                    for i in range(len(ids))
                ]
        return []
    
    def delete_vector(self, id: str):
        if self.collection is not None:
            self.collection.delete(ids=[id])
            self.logger.info(f"Deleted vector for ID: {id}")

    def update(self, environment_data: Dict[str, Any]):
        """
        Persist environment/feedback data in the vector database (called by Autonomy.adapt_to_environment).

        Args:
        - environment_data: A dictionary of environmental data to store as metadata.
        """
        if not isinstance(environment_data, dict) or not environment_data:
            self.logger.warning("update() received empty or non-dict data; nothing stored.")
            return

        if self.collection is None:
            self.logger.warning("VectorDB not initialized (stub mode); environment update skipped.")
            return

        env_id = f"env_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        # Real deterministic hashing embedding (searchable in cosine space),
        # with the full payload carried in metadata.
        content = " ".join(f"{key} {value}" for key, value in environment_data.items())
        vector = self.embedding_fn(content)
        metadata = {key: str(value) for key, value in environment_data.items()}
        self.store_vector(id=env_id, vector=vector, metadata=metadata)
        self.logger.info(f"Stored environment update with ID: {env_id}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    config = {
        "type": "chroma",
        "dimension": 1536,
        "collection_name": "test_collection"
    }
    vector_db = VectorDB(config)
