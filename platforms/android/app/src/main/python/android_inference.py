"""
ShugoCore Android Inference Backend

Provides OpenAI-compatible API client for the local llama.cpp
server running via JNI bindings.

This allows ShugoCore's existing backends (OllamaBackend, OpenAIBackend)
to work unchanged on Android through the local API server.
"""

import json
import logging
from urllib.request import urlopen, Request
from urllib.error import URLError
from typing import Optional, Dict, Any, List
from model_backends import BaseBackend, register_backend


def _http_post(url: str, payload: dict, timeout: float) -> dict:
    """POST JSON via stdlib urllib (``requests`` is not bundled in Chaquopy)."""
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=int(timeout)) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get(url: str, timeout: float) -> dict:
    with urlopen(url, timeout=int(timeout)) as resp:
        return json.loads(resp.read().decode("utf-8"))


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
        # CPU-only on-device generation needs ample headroom for prompt
        # eval (prefill) + decode. On a 0.5B Q4 model on Exynos 1380:
        # prefill ~280s + decode ~1.3s/token. Full 80-token generation
        # needs ~385s; 600s matches the governor deadline and absorbs
        # thermal-throttle slowdowns.
        timeout: int = 600,
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
        **kwargs,
    ) -> str:
        """Generate text using the local llama.cpp server."""
        payload = {
            "model": model_id,
            "prompt": prompt,
            "stream": False,
            "options": {
                # Structured decision JSON needs room for preamble + the JSON
                # object. On the tiny 0.5B model the preamble often consumes
                # tokens; capping too low truncates mid-JSON (parse fails ->
                # no_viable_action). No newline stop: single-line JSON is
                # preferred but a preamble + newline must NOT halt generation
                # before the JSON appears.
                "num_predict": kwargs.get("max_tokens", 128),
                "temperature": kwargs.get("temperature", 0.7),
                "top_p": kwargs.get("top_p", 0.9),
                "top_k": kwargs.get("top_k", 40),
            },
        }
        print(f"ANDROID_INFERENCE generate called: model_id={model_id} prompt_len={len(prompt)}")
        data = _http_post(
            f"{self.base_url}/api/generate", payload, timeout or self.timeout
        )
        response_text = data.get("response", "")
        if not response_text:
            logging.getLogger(__name__).warning("Empty response from model")
        print(f"MODEL_RESPONSE_text_len={len(response_text)} text={response_text!r}")
        return response_text

    def chat(
        self,
        model_id: str,
        messages: List[Dict[str, str]],
        timeout: float = 30.0,
        **kwargs,
    ) -> str:
        """Chat completion using the local llama.cpp server."""
        payload = {
            "model": model_id,
            "messages": messages,
            "stream": False,
            "options": {
                "num_predict": kwargs.get("max_tokens", 128),
                "temperature": kwargs.get("temperature", 0.7),
                "top_p": kwargs.get("top_p", 0.9),
            },
        }
        data = _http_post(
            f"{self.base_url}/api/chat", payload, timeout or self.timeout
        )
        return data.get("message", {}).get("content", "")

    def list_models(self) -> List[str]:
        """List available models via the /api/tags endpoint."""
        try:
            data = _http_get(f"{self.base_url}/api/tags", self.timeout)
            return [m["name"] for m in data.get("models", [])]
        except Exception:
            return [self.model_name]

    def get_health(self) -> bool:
        """Check if the local inference server is running."""
        try:
            _http_get(f"{self.base_url}/health", 5)
            return True
        except Exception:
            return False


# Self-register so ``create_backend({"type": "android", ...})`` works.
register_backend("android", AndroidBackend)
