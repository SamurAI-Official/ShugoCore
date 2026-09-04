"""
ShugoCore Android Inference Backend

Provides OpenAI-compatible API client for the local llama.cpp
server running via JNI bindings.

This allows ShugoCore's existing backends (OllamaBackend, OpenAIBackend)
to work unchanged on Android through the local API server.
"""

import requests
from typing import Optional, Dict, Any, List
from model_backends import BaseBackend, register_backend


class AndroidBackend(BaseBackend):
    """Model backend for Android local inference server.
    
    Connects to LocalApiServer running llama.cpp via JNI.
    Compatible with OllamaBackend interface (same API format).
    """
    name = "android"
    
    def __init__(
        self,
        api_url: str = "http://127.0.0.1:11434",
        model_name: str = "shugocore",
        device_caps: Optional[Dict[str, Any]] = None,
        timeout: int = 30
    ):
        self.base_url = api_url.rstrip("/")
        self.model_name = model_name
        self.device_caps = device_caps or {}
        self.timeout = timeout
    
    def generate(
        self,
        model_id: str,
        prompt: str,
        timeout: float = 30.0,
        **kwargs
    ) -> str:
        """Generate text using the local llama.cpp server.
        
        Uses Ollama-compatible API format.
        """
        response = requests.post(
            f"{self.base_url}/api/generate",
            json={
                "model": model_id,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "num_predict": kwargs.get("max_tokens", 128),
                    "temperature": kwargs.get("temperature", 0.7),
                    "top_p": kwargs.get("top_p", 0.9),
                    "top_k": kwargs.get("top_k", 40)
                }
            },
            timeout=timeout or self.timeout
        )
        response.raise_for_status()
        data = response.json()
        return data.get("response", "")
    
    def chat(
        self,
        model_id: str,
        messages: List[Dict[str, str]],
        timeout: float = 30.0,
        **kwargs
    ) -> str:
        """Chat completion using the local llama.cpp server."""
        response = requests.post(
            f"{self.base_url}/api/chat",
            json={
                "model": model_id,
                "messages": messages,
                "stream": False,
                "options": {
                    "num_predict": kwargs.get("max_tokens", 128),
                    "temperature": kwargs.get("temperature", 0.7),
                    "top_p": kwargs.get("top_p", 0.9)
                }
            },
            timeout=timeout or self.timeout
        )
        response.raise_for_status()
        data = response.json()
        return data.get("message", {}).get("content", "")
    
    def list_models(self) -> List[str]:
        """List available models via the /api/tags endpoint."""
        try:
            response = requests.get(
                f"{self.base_url}/api/tags",
                timeout=self.timeout
            )
            response.raise_for_status()
            data = response.json()
            return [m["name"] for m in data.get("models", [])]
        except Exception:
            return [self.model_name]
    
    def get_health(self) -> bool:
        """Check if the local inference server is running."""
        try:
            response = requests.get(
                f"{self.base_url}/health",
                timeout=5
            )
            return response.status_code == 200
        except Exception:
            return False


# Self-register so ``create_backend({"type": "android", ...})`` works.
register_backend("android", AndroidBackend)
