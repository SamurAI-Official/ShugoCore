"""
ShugoCore model backends
========================

Pluggable adapters that turn a prompt into model output text:

- ``OllamaBackend``           - HTTP API on 127.0.0.1:11434 (replaces the old
  per-call ``ollama`` subprocess; no argument-injection surface, timeouts)
- ``OpenAICompatibleBackend`` - any /chat/completions endpoint
- ``StubBackend``             - deterministic offline backend that emits a
  valid (null-action) proposal so the structured decision protocol works
  end-to-end without network access

Selected per model config (``model['backend']``) or a global default via
:func:`create_backend`.
"""

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import requests

from security import sanitize_text

logger = logging.getLogger(__name__)

# Model names must not start with '-' (argument injection) and stay bounded.
MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")


def validate_model_name(name: str) -> bool:
    return bool(isinstance(name, str) and MODEL_NAME_PATTERN.match(name))


class BackendError(RuntimeError):
    """Raised when a backend cannot complete a generation request."""


class BaseBackend:
    """Interface for all model backends."""

    name = "base"

    def generate(self, model_id: str, prompt: str, timeout: float = 30.0) -> str:
        raise NotImplementedError

    def list_models(self) -> List[str]:
        raise NotImplementedError


class OllamaBackend(BaseBackend):
    """Ollama HTTP API (default: loopback). Never spawns subprocesses."""

    name = "ollama"

    def __init__(self, base_url: str = "http://127.0.0.1:11434", timeout: float = 30.0):
        self.base_url = str(base_url).rstrip("/")
        self.timeout = float(timeout)

    def generate(self, model_id: str, prompt: str, timeout: float = None) -> str:
        if not validate_model_name(model_id):
            raise BackendError(f"invalid model name: {model_id!r}")
        response = requests.post(
            f"{self.base_url}/api/generate",
            json={"model": model_id, "prompt": str(prompt), "stream": False},
            timeout=timeout or self.timeout,
        )
        response.raise_for_status()
        return str(response.json().get("response", ""))

    def list_models(self) -> List[str]:
        response = requests.get(f"{self.base_url}/api/tags", timeout=self.timeout)
        response.raise_for_status()
        return [str(entry.get("name", ""))
                for entry in response.json().get("models", [])
                if isinstance(entry, dict) and entry.get("name")]


class OpenAICompatibleBackend(BaseBackend):
    """
    Any OpenAI-compatible /chat/completions endpoint. The API key is read
    from the environment at call time (``api_key_env``), never stored here.
    """

    name = "openai"

    def __init__(self, base_url: str, api_key_env: str = "OPENAI_API_KEY",
                 timeout: float = 30.0):
        self.base_url = str(base_url).rstrip("/")
        self.api_key_env = str(api_key_env)
        self.timeout = float(timeout)

    def generate(self, model_id: str, prompt: str, timeout: float = None) -> str:
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise BackendError(f"environment variable {self.api_key_env} is not set")
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model_id, "stream": False,
                  "messages": [{"role": "user", "content": str(prompt)}]},
            timeout=timeout or self.timeout,
        )
        response.raise_for_status()
        choices = response.json().get("choices", [])
        if not choices:
            return ""
        return str(choices[0].get("message", {}).get("content", ""))

    def list_models(self) -> List[str]:
        return []  # not universally supported; models come from config


class StubBackend(BaseBackend):
    """
    Deterministic offline backend. Emits a syntactically valid proposal with
    ``action_type: null`` so the decision pipeline, gating and memory flows
    can be exercised without any network access.
    """

    name = "stub"

    def generate(self, model_id: str, prompt: str, timeout: float = None) -> str:
        return json.dumps({
            "action_type": None,
            "params": {},
            "confidence": 0.0,
            "text": f"[stub:{sanitize_text(model_id, 32)}] {sanitize_text(prompt, 120)}",
        })

    def list_models(self) -> List[str]:
        return []


_BACKEND_TYPES = {
    "ollama": OllamaBackend,
    "openai": OpenAICompatibleBackend,
    "stub": StubBackend,
}


def create_backend(config: Optional[Dict[str, Any]] = None) -> BaseBackend:
    """Build a backend from a config dict (default: Ollama on loopback)."""
    config = dict(config or {})
    backend_type = str(config.get("type", "ollama")).lower()
    backend_cls = _BACKEND_TYPES.get(backend_type)
    if backend_cls is None:
        raise ValueError(f"unknown backend type '{backend_type}' "
                         f"(available: {sorted(_BACKEND_TYPES)})")
    kwargs = {k: v for k, v in config.items() if k != "type"}
    return backend_cls(**kwargs)
