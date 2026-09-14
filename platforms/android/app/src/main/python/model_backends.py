"""
ShugoCore model backends
========================

Pluggable adapters that turn a prompt into model output text:

- ``OllamaBackend``           - HTTP API on 127.0.0.1:11434 (replaces the old
  per-call ``ollama`` subprocess; no argument-injection surface, timeouts)
- ``OpenAICompatibleBackend`` - any /v1/chat/completions endpoint
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
from urllib.parse import urlparse

import requests

from security import sanitize_text

logger = logging.getLogger(__name__)

# Model names must not start with '-' (argument injection) and stay bounded.
MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")

# Hard cap on any single backend response we buffer into memory.
MAX_RESPONSE_BYTES = 262_144  # 256 KiB


def validate_model_name(name: str) -> bool:
    return bool(isinstance(name, str) and MODEL_NAME_PATTERN.match(name))


class BackendError(RuntimeError):
    """Raised when a backend cannot complete a generation request."""


def _validated_base_url(url: str) -> str:
    """Normalize and validate a backend base URL.

    Only http/https is accepted, the host must be present, and embedded
    credentials are refused, so a config value cannot smuggle a ``file://``
    read or a userinfo payload into the transport.
    """
    text = str(url or "").strip()
    try:
        parsed = urlparse(text)
    except ValueError:
        raise BackendError(f"unparseable backend URL: {url!r}")
    if parsed.scheme not in ("http", "https"):
        raise BackendError(
            f"backend URL scheme '{parsed.scheme or 'none'}' is not allowed "
            f"(http/https only)")
    if not parsed.hostname:
        raise BackendError("backend URL has no host")
    if parsed.username or parsed.password:
        raise BackendError("credentials embedded in a backend URL are not allowed")
    return text.rstrip("/")


def _check_response(response: Any) -> None:
    """Reject redirects outright (a redirect could bypass an allowlist)."""
    if 300 <= response.status_code < 400:
        raise BackendError("backend returned a redirect; refused to follow it")
    response.raise_for_status()


def _read_bounded(response: Any, cap: int = MAX_RESPONSE_BYTES) -> bytes:
    """Read a streaming response, refusing to buffer more than ``cap`` bytes."""
    chunks: List[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > cap:
            raise BackendError(
                f"backend response exceeded {cap} bytes (oversized body refused)")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_json(raw: bytes) -> Dict[str, Any]:
    data = json.loads(raw.decode("utf-8"))
    return data if isinstance(data, dict) else {}


class BaseBackend:
    """Interface for all model backends."""

    name = "base"

    def generate(self, model_id: str, prompt: str, timeout: float = 30.0,
                 grammar: Optional[str] = None) -> str:
        """Generate text. ``grammar`` is an optional GBNF constraint; backends
        without native GBNF support map it to their closest structured-output
        mode (and may ignore it)."""
        raise NotImplementedError

    def list_models(self) -> List[str]:
        raise NotImplementedError


class OllamaBackend(BaseBackend):
    """Ollama HTTP API (default: loopback). Never spawns subprocesses."""

    name = "ollama"

    def __init__(self, base_url: str = "http://127.0.0.1:11434", timeout: float = 120.0):
        self.base_url = _validated_base_url(base_url)
        self.timeout = float(timeout)

    def generate(self, model_id: str, prompt: str, timeout: float = None,
                 grammar: Optional[str] = None) -> str:
        if not validate_model_name(model_id):
            raise BackendError(f"invalid model name: {model_id!r}")
        body: Dict[str, Any] = {"model": model_id, "prompt": str(prompt),
                                "stream": False}
        if grammar:
            # Ollama has no GBNF channel; its JSON mode is the closest
            # structured-output constraint.
            body["format"] = "json"
        with requests.post(
            f"{self.base_url}/api/generate",
            json=body,
            timeout=timeout or self.timeout,
            allow_redirects=False,
            stream=True,
        ) as response:
            _check_response(response)
            payload = _decode_json(_read_bounded(response))
        return str(payload.get("response", ""))

    def list_models(self) -> List[str]:
        with requests.get(
            f"{self.base_url}/api/tags",
            timeout=self.timeout,
            allow_redirects=False,
            stream=True,
        ) as response:
            _check_response(response)
            payload = _decode_json(_read_bounded(response))
        return [str(entry.get("name", ""))
                for entry in payload.get("models", [])
                if isinstance(entry, dict) and entry.get("name")]


class OpenAICompatibleBackend(BaseBackend):
    """
        Any OpenAI-compatible /v1/chat/completions endpoint. The API key is read
    from the environment at call time (``api_key_env``), never stored here.
    """

    name = "openai"

    def __init__(self, base_url: str, api_key_env: str = "OPENAI_API_KEY",
                 timeout: float = 30.0):
        self.base_url = _validated_base_url(base_url)
        self.api_key_env = str(api_key_env)
        self.timeout = float(timeout)

    def generate(self, model_id: str, prompt: str, timeout: float = None,
                 grammar: Optional[str] = None) -> str:
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise BackendError(f"environment variable {self.api_key_env} is not set")
        body: Dict[str, Any] = {
            "model": model_id, "stream": False,
            "messages": [{"role": "user", "content": str(prompt)}],
        }
        if grammar:
            # Closest OpenAI-compatible structured-output mode.
            body["response_format"] = {"type": "json_object"}
        with requests.post(
            f"{self.base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
            timeout=timeout or self.timeout,
            allow_redirects=False,
            stream=True,
        ) as response:
            _check_response(response)
            payload = _decode_json(_read_bounded(response))
        choices = payload.get("choices", [])
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

    def generate(self, model_id: str, prompt: str, timeout: float = None,
                 grammar: Optional[str] = None) -> str:
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


def register_backend(type_name: str, backend_cls: type) -> None:
    """Register a backend class under a config ``type`` name.

    Optional backends (e.g. the Android local-inference client) self-register
    on import instead of creating a hard import dependency here.
    """
    _BACKEND_TYPES[str(type_name).lower()] = backend_cls


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
