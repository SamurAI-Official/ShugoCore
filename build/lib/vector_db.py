import logging
from datetime import datetime
try:
    import chromadb
    _HAS_CHROMA = True
except ImportError:
    _HAS_CHROMA = False
from typing import Dict, Any, List

class VectorDB:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.db_type = config.get("type", "unknown")
        self.dimension = config.get("dimension", 1536)
        self.collection_name = config.get("collection_name", "default")

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
        # Placeholder embedding; the actual payload is carried in metadata
        vector = [0.0] * self.dimension
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
