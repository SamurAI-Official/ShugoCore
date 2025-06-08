import logging
import chromadb
from chromadb.config import Settings
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
            results = self.collection.get(ids=[id])
            if results and results["ids"]:
                return {
                    "id": results["ids"][0],
                    "vector": results["embeddings"][0],
                    "metadata": results["metadatas"][0],
                }
        return {}

    def query_vectors(self, query_vector: List[float], top_k: int = 5) -> List[Dict[str, Any]]:
        if self.collection is not None:
            results = self.collection.query(query_embeddings=[query_vector], n_results=top_k)
            if results and results["ids"]:
                return [
                    {"id": results["ids"][i][0], "distance": results["distances"][i][0], "metadata": results["metadatas"][i][0]}
                    for i in range(len(results["ids"]))
                ]
        return []
    
    def delete_vector(self, id: str):
        if self.collection is not None:
            self.collection.delete(ids=[id])
            self.logger.info(f"Deleted vector for ID: {id}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    config = {
        "type": "chroma",
        "dimension": 1536,
        "collection_name": "test_collection"
    }
    vector_db = VectorDB(config)
