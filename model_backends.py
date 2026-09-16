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

v1.30.4 — :class:`BackendPool` keeps a set of backends for the same
``model.id`` and routes each request to the healthiest one (fewest recent
failures, bounded circuit-breaker isolation). Failures bump the pool; a
healthy recent success resets to the default order. Deterministic
(score-based), never random, so a soak reproduces.
"""

import json
import logging
import math
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
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
    """Build a backend from a config dict (default: Ollama on loopback).

    Supports the ``pool`` type (v1.30.4): a list of backend configs under
    ``backends`` is wrapped in a :class:`BackendPool` that routes each call
    to the healthiest backend. Each pool entry may carry an optional
    ``priority`` (higher = preferred while healthy). Pool-level options
    ``max_failures`` (default 5) and ``cooldown_s`` (default 30) bound the
    per-backend circuit breakers.

    Example::

        {"type": "pool", "backends": [
            {"type": "ollama", "base_url": "http://host-a:11434",
             "priority": 2.0},
            {"type": "ollama", "base_url": "http://host-b:11434",
             "priority": 1.0},
        ]}
    """
    config = dict(config or {})
    backend_type = str(config.get("type", "ollama")).lower()
    if backend_type == "pool":
        pool_entries = config.get("backends") or []
        if not isinstance(pool_entries, list):
            raise ValueError("pool type requires a 'backends' list")
        built: List[Dict[str, Any]] = []
        for entry in pool_entries:
            if not isinstance(entry, dict):
                raise ValueError("pool backends entries must be dicts")
            entry_config = dict(entry)
            priority = entry_config.pop("priority", 1.0)
            built.append({"backend": create_backend(entry_config),
                          "priority": priority})
        return BackendPool(
            built,
            max_failures=config.get("max_failures", 5),
            cooldown_s=config.get("cooldown_s", 30.0),
        )
    backend_cls = _BACKEND_TYPES.get(backend_type)
    if backend_cls is None:
        raise ValueError(f"unknown backend type '{backend_type}' "
                         f"(available: {sorted(_BACKEND_TYPES)})")
    kwargs = {k: v for k, v in config.items() if k != "type"}
    return backend_cls(**kwargs)


# ---------------------------------------------------------------------------
# BackendPool (v1.30.4) — health-based routing across backends for one
# model id. Lower score wins; circuit-open backends are +inf and skipped.
# Deterministic (ties broken by declared order) so soaks reproduce.
# ---------------------------------------------------------------------------
class BackendPool:
    """A small pool of :class:`BaseBackend` instances for one ``model.id``.

    Health score per backend: ``failures / priority``. A backend with
    ``failures >= max_failures`` opens its circuit for ``cooldown_s``
    seconds (then half-opens for one trial). The pool is deterministic so
    soaks reproduce — it never randomizes between candidates.

    Use::

        pool = BackendPool(backends=[
            {"backend": OllamaBackend(...), "priority": 1.0},
            {"backend": OllamaBackend(...), "priority": 0.5},
        ])
        pool.call("generate", model_id="x", prompt="...", timeout=10)
    """

    name = "pool"

    def __init__(self, backends: List[Dict[str, Any]],
                 *, max_failures: int = 5, cooldown_s: float = 30.0):
        if not backends:
            raise ValueError("BackendPool requires at least one backend")
        self._backends = list(backends)
        self._max_failures = max(1, int(max_failures))
        self._cooldown_s = max(0.0, float(cooldown_s))
        self._lock = threading.Lock()
        self._failures: Dict[int, int] = {}
        self._open_until: Dict[int, float] = {}
        self._successes: Dict[int, int] = {}
        for entry in self._backends:
            if "backend" not in entry:
                raise ValueError(
                    "BackendPool entries must have a 'backend' key")
            entry.setdefault("priority", 1.0)
            priority = float(entry["priority"])
            if not math.isfinite(priority) or priority <= 0:
                raise ValueError(
                    f"BackendPool priority must be a positive finite "
                    f"number, got {priority!r}")
            entry["priority"] = priority
            bid = id(entry["backend"])
            self._failures[bid] = 0
            self._successes[bid] = 0

    def __len__(self) -> int:
        return len(self._backends)

    def backends(self) -> List[Dict[str, Any]]:
        """Snapshot the pool entries (backends + priorities)."""
        with self._lock:
            return [{"backend": e["backend"], "priority": e["priority"]}
                    for e in self._backends]

    def stats(self) -> List[Dict[str, Any]]:
        """Per-backend delivery counters (observational)."""
        with self._lock:
            now = time.monotonic()
            out: List[Dict[str, Any]] = []
            for entry in self._backends:
                backend = entry["backend"]
                bid = id(backend)
                open_at = self._open_until.get(bid, 0.0)
                remaining = max(0.0, open_at - now)
                out.append({
                    "name": getattr(backend, "name",
                                    type(backend).__name__),
                    "priority": entry["priority"],
                    "failures": self._failures.get(bid, 0),
                    "successes": self._successes.get(bid, 0),
                    "circuit_open": remaining > 0,
                    "circuit_open_s": round(remaining, 1),
                })
            return out

    def _is_circuit_open(self, bid: int) -> bool:
        until = self._open_until.get(bid, 0.0)
        if until <= 0:
            return False
        if time.monotonic() >= until:
            # Cooldown elapsed — half-open: allow one trial; reduce the
            # failure count so a single success closes it.
            self._open_until[bid] = 0.0
            self._failures[bid] = max(0, self._failures[bid] - 1)
            return False
        return True

    def _score(self, bid: int, priority: float) -> float:
        # Lower is better. Circuit-open backends score +inf and are skipped.
        if self._is_circuit_open(bid):
            return float("inf")
        return float(self._failures.get(bid, 0)) / max(1.0, priority)

    def _choose(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            best: Optional[Tuple[float, int, Dict[str, Any]]] = None
            for index, entry in enumerate(self._backends):
                bid = id(entry["backend"])
                score = self._score(bid, entry["priority"])
                # Lower score wins; ties broken by LOWER index (declared
                # order) so the pool never shuffles between calls.
                key = (score, index)
                if best is None or key < (best[0], best[1]):
                    best = (score, index, entry)
            if best is None or best[0] == float("inf"):
                return None
            return best[2]

    def _record_success(self, backend: BaseBackend) -> None:
        bid = id(backend)
        with self._lock:
            self._successes[bid] = self._successes.get(bid, 0) + 1
            self._failures[bid] = 0
            self._open_until[bid] = 0.0

    def _record_failure(self, backend: BaseBackend) -> None:
        bid = id(backend)
        with self._lock:
            count = self._failures.get(bid, 0) + 1
            self._failures[bid] = count
            if count >= self._max_failures:
                self._open_until[bid] = time.monotonic() + self._cooldown_s

    def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Route one backend call through the pool.

        Walks every backend in health order; the first one that succeeds
        wins, a failure is recorded and the call retries on the next
        healthiest backend. If every backend fails, the last exception
        is re-raised.
        """
        last_error: Optional[BaseException] = None
        attempted: List[int] = []
        while True:
            entry = self._choose()
            if entry is None or id(entry["backend"]) in attempted:
                break  # no candidate (circuits all open) or tried them all
            backend = entry["backend"]
            attempted.append(id(backend))
            fn = getattr(backend, method, None)
            if fn is None or not callable(fn):
                last_error = AttributeError(
                    f"backend {getattr(backend, 'name', '?')} has no "
                    f"method {method!r}")
                continue
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                last_error = exc
                self._record_failure(backend)
                logger.warning(
                    "BackendPool: %s.%s failed (%s); trying next",
                    getattr(backend, "name", "?"), method,
                    type(exc).__name__)
                continue
            self._record_success(backend)
            return result
        if last_error is not None:
            raise last_error
        raise BackendError(
            "BackendPool: no backend available (all circuits open)")

    # -- BaseBackend-shaped proxy methods -------------------------------------
    # Make the pool usable anywhere a backend is expected.

    def generate(self, model_id: str, prompt: str,
                 timeout: float = 30.0,
                 grammar: Optional[str] = None) -> str:
        return self.call("generate", model_id, prompt, timeout=timeout,
                         grammar=grammar)

    def chat(self, model_id: str, messages: List[Dict[str, str]],
             timeout: float = 30.0,
             grammar: Optional[str] = None) -> str:
        return self.call("chat", model_id, messages, timeout=timeout,
                         grammar=grammar)

    def list_models(self) -> List[str]:
        # Aggregated union; duplicates removed.
        seen: List[str] = []
        for entry in self.backends():
            try:
                for m in entry["backend"].list_models():
                    if m not in seen:
                        seen.append(m)
            except Exception:
                continue
        return seen

    def get_health(self) -> bool:
        """True iff at least one backend is healthy (not circuit-open)."""
        return any(
            not self._is_circuit_open(id(e["backend"]))
            for e in self._backends
        )
