#!/usr/bin/env python3
"""
ShugoCore Desktop Server
=======================

A small, **stdlib-only** HTTP server that lets low-end/older devices and
desktop browsers talk to a full ShugoCore instance running on this machine.

It speaks two wire contracts on one port:

1. **Ollama wire contract** (``/api/generate``, ``/api/chat``, ``/api/tags``,
   ``/health``) so the Android app's ``AndroidBackend`` (which is
   Ollama-compatible) can point at ``http://<desktop-ip>:<port>`` with **zero
   client changes** -- the phone stays a sensor/operator node while the desktop
   provides the ``full_agent`` brain.

2. **Engine API** (``/api/v1/status``, ``POST /api/v1/task``) so the engine's
   policy-gated ``execute_task`` path is reachable over the network. The task
   endpoint goes through the **same governor/fallback/memory pipeline** as a
   local call -- it never bypasses policy.

The model backend is pluggable via ``--backend``:
  - ``ollama`` (default) - a local Ollama instance (macOS/Linux/Windows)
  - ``llamacpp``          - a llama.cpp ``llama-server`` instance
  - ``openai``            - any OpenAI-compatible ``/v1/chat/completions`` endpoint
  - ``stub``              - deterministic offline stub (tests / dry runs)

Example::

    shugocore-server --backend ollama --model qwen3.5:latest --host 0.0.0.0 --port 11435

The phone then pairs by entering ``http://<desktop-ip>:<port>`` in the app.

Notes
-----
* The server is intentionally **loopback-safe by default** (``--host 127.0.0.1``).
  To let a phone reach it, pass ``--host 0.0.0.0`` (and open the firewall port).
  If a local Ollama already owns ``:11434``, use ``--port 11435`` etc.
* ``POST /api/v1/task`` bodies are capped (1 MB) and every string field is
  length-checked; responses never echo raw task content back.
* **Authentication (fail-closed).** Set ``SHUGOCORE_SERVER_TOKEN`` to require a
  bearer token (``Authorization: Bearer <token>`` or ``X-ShugoCore-Token``)
  on every route except ``/health``. Binding to a non-loopback host without a
  token is refused unless ``--allow-unauthenticated`` is passed explicitly, so
  exposing the engine/`generate` endpoints to a LAN is an opt-in decision.
* **Abuse controls.** Requests are token-bucket rate limited per client
  address (``--rate-limit-per-minute`` / ``--rate-limit-burst``) and CORS
  preflight is answered only for loopback origins.
"""

import argparse
import hmac
import json
import logging
import os
import sys
import threading
import time
from http import server as http_server
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from security import RateLimiter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("shugocore.server")

# ---------------------------------------------------------------------------
# Wire-contract constants
# ---------------------------------------------------------------------------
MAX_JSON_BODY = 1_048_576  # 1 MB task/generate bodies
MAX_STRING_FIELD = 10_000  # per-field cap for prompt/message content
SERVER_TOKEN_ENV = "SHUGOCORE_SERVER_TOKEN"
DEFAULT_RATE_LIMIT_PER_MINUTE = 240.0
DEFAULT_RATE_LIMIT_BURST = 120


# ---------------------------------------------------------------------------
# Bind / origin helpers
# ---------------------------------------------------------------------------
def _is_loopback_host(host: str) -> bool:
    """True for loopback bind addresses (only these may run unauthenticated)."""
    candidate = str(host or "").strip().strip("[]").lower()
    return candidate in ("localhost", "::1") or candidate.startswith("127.")


def _is_loopback_origin(origin: str) -> bool:
    """True when a CORS ``Origin`` header points at a loopback host."""
    try:
        parsed = urlparse(str(origin))
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and _is_loopback_host(parsed.hostname or "")


def _extract_token(handler: "http_server.BaseHTTPRequestHandler") -> Optional[str]:
    """Bearer token from ``Authorization`` (or ``X-ShugoCore-Token``)."""
    header = str(handler.headers.get("Authorization", "") or "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    alt = handler.headers.get("X-ShugoCore-Token")
    return str(alt).strip() if alt else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _safe_text(value: Any, cap: int = MAX_STRING_FIELD) -> str:
    """Coerce to a bounded string (never echo unbounded request data)."""
    text = str(value if value is not None else "")
    return text[:cap]


def _read_json_body(handler: "http_server.BaseHTTPRequestHandler") -> Dict[str, Any]:
    """Read and parse a JSON body with strict size guards.

    A negative or oversized ``Content-Length`` is rejected outright: reading a
    negative length drains the socket to EOF (unbounded), and an oversized one
    is refused before allocating. Any malformed body yields ``{}``.
    """
    try:
        length = int(handler.headers.get("Content-Length", "0") or 0)
    except (TypeError, ValueError):
        return {}
    if length <= 0 or length > MAX_JSON_BODY:
        return {}
    try:
        raw = handler.rfile.read(length).decode("utf-8")
    except Exception:  # malformed encoding / truncated body
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _send_json(handler: "http_server.BaseHTTPRequestHandler", status: int,
               payload: Dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _send_ndjson(handler: "http_server.BaseHTTPRequestHandler",
                 chunks: List[Dict[str, Any]]) -> None:
    """Send a length-prefixed NDJSON (Ollama streaming) body."""
    body = b"".join(json.dumps(c).encode("utf-8") + b"\n" for c in chunks)
    handler.send_response(200)
    handler.send_header("Content-Type", "application/x-ndjson")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _safe_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    """Trim a task result to safe, serializable fields."""
    safe: Dict[str, Any] = {"status": _safe_text(result.get("status"), 32)}
    for key in ("reason", "message", "result", "action_type", "summary"):
        if result.get(key) is not None:
            val = result[key]
            if isinstance(val, (dict, list)):
                safe[key] = val
            else:
                safe[key] = _safe_text(val, 2000)
    return safe
# ---------------------------------------------------------------------------
# ShugoCoreServer
# ---------------------------------------------------------------------------
class ShugoCoreServer:
    """HTTP facade over a DecisionEngine + pluggable model backend."""

    def __init__(self, engine, backend, model: str = "qwen3.5:latest",
                 auth_token: Optional[str] = None,
                 rate_limit_per_minute: Optional[float] = DEFAULT_RATE_LIMIT_PER_MINUTE,
                 rate_limit_burst: int = DEFAULT_RATE_LIMIT_BURST):
        self.engine = engine
        self.backend = backend
        self.model = model
        self.auth_token = str(auth_token) if auth_token else None
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._limiter: Optional[RateLimiter] = None
        if rate_limit_per_minute and float(rate_limit_per_minute) > 0:
            self._limiter = RateLimiter(
                calls_per_minute=float(rate_limit_per_minute),
                burst=int(rate_limit_burst),
            )
        from version import __version__
        self._version = __version__

    # -- request admission ---------------------------------------------------

    def authorize(self, token: Optional[str]) -> bool:
        """Constant-time bearer-token check; open when no token is configured."""
        if not self.auth_token:
            return True
        return bool(token) and hmac.compare_digest(str(token), self.auth_token)

    def allow_request(self, client_key: str) -> bool:
        """Non-blocking per-client token-bucket check (always True when off)."""
        if self._limiter is None:
            return True
        return self._limiter.acquire(str(client_key), timeout=0.0)

    # -- Ollama wire contract ------------------------------------------------

    def handle_generate(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """POST /api/generate -> backend.generate(); returns (status, payload)."""
        model = _safe_text(body.get("model") or self.model, 128)
        prompt = _safe_text(body.get("prompt") or "")
        stream = bool(body.get("stream", False))
        try:
            output = self.backend.generate(model, prompt, timeout=120.0)
        except Exception as exc:
            return 502, {"error": f"backend error: {type(exc).__name__}"}
        if stream:
            chunks: List[Dict[str, Any]] = []
            if output:
                chunks.append({"response": _safe_text(output, 4000), "done": False})
            chunks.append({"response": "", "done": True,
                           "model": model, "eval_count": 0,
                           "total_duration": int((time.monotonic() - self._started) * 1e9)})
            # Distinguish the NDJSON response for the handler.
            return 200, {"__ndjson__": chunks}
        return 200, {"model": model, "response": _safe_text(output, 4000), "done": True}

    def handle_chat(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """POST /api/chat -> build a prompt and run backend.generate()."""
        model = _safe_text(body.get("model") or self.model, 128)
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return 400, {"error": "messages is required (non-empty array)"}
        prompt = self._messages_to_prompt(messages)
        try:
            output = self.backend.generate(model, prompt, timeout=120.0)
        except Exception as exc:
            return 502, {"error": f"backend error: {type(exc).__name__}"}
        return 200, {"message": {"role": "assistant",
                                 "content": _safe_text(output, 4000)},
                     "model": model, "done": True}

    @staticmethod
    def _messages_to_prompt(messages: List[Dict[str, Any]]) -> str:
        parts: List[str] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = _safe_text(msg.get("role"), 32)
            content = _safe_text(msg.get("content"))
            if role in ("system", "user", "assistant"):
                parts.append(f"{role.title()}: {content}")
        if not parts:
            return ""
        return "\n".join(parts) + "\nAssistant:"

    def handle_tags(self) -> Tuple[int, Dict[str, Any]]:
        """GET /api/tags -> list model names."""
        names: List[str] = []
        try:
            names = self.backend.list_models()
        except Exception as exc:
            logger.warning("list_models failed: %s", type(exc).__name__)
        if not names:
            names = [self.model]
        return 200, {"models": [{"name": n, "size": 0} for n in names]}

    def handle_health(self) -> Tuple[int, Dict[str, Any]]:
        """GET /health -> basic liveness + readiness."""
        return 200, {"status": "ok", "model": self.model,
                     "version": self._version,
                     "uptime_s": int(time.monotonic() - self._started)}

    # -- Engine API ----------------------------------------------------------

    def handle_status(self) -> Tuple[int, Dict[str, Any]]:
        """GET /api/v1/status -> engine/governor/memory state snapshot."""
        try:
            fallbacks = getattr(self.engine, "fallbacks", None)
            fallback_state = (
                fallbacks.status()
                if fallbacks is not None and hasattr(fallbacks, "status")
                else {"mode": "unknown"}
            )
            memory_unavailable: bool = False
            try:
                from memory_system import memory_system_available
                memory_unavailable = not memory_system_available()
            except Exception:
                pass
            governor_state = getattr(
                getattr(self.engine, "governor", None), "state", None
            )
            state = {
                "version": self._version,
                "model": self.model,
                "backend": getattr(self.backend, "name", "unknown"),
                "governor_state": (
                    str(governor_state) if governor_state is not None else "unknown"
                ),
                "fallbacks": fallback_state,
                "memory_unavailable": memory_unavailable,
                "vector_db_stub": bool(getattr(self.engine.vector_db, "stub", False)),
                "uptime_s": int(time.monotonic() - self._started),
            }
            return 200, state
        except Exception as exc:
            logger.exception("status endpoint failed")
            return 500, {"error": f"status failed: {type(exc).__name__}"}

    def handle_task(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """POST /api/v1/task -> engine.execute_task (policy-gated)."""
        if not isinstance(body, dict) or not body:
            return 400, {"error": "task body required"}
        task = {
            "type": _safe_text(body.get("type", "user"), 64),
            "params": body.get("params")
            if isinstance(body.get("params"), dict) else {},
        }
        if body.get("content") is not None:
            task["content"] = _safe_text(body.get("content"))
        try:
            result = self.engine.execute_task(task)
            return 200, _safe_payload(result)
        except Exception as exc:
            logger.exception("execute_task failed")
            return 500, {"error": f"task failed: {type(exc).__name__}"}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class ShugoCoreHandler(http_server.BaseHTTPRequestHandler):
    """Threaded HTTP/1.1 handler dispatching the wire-contract routes."""

    server_version = "ShugoCoreServer/1.8"
    protocol_version = "HTTP/1.1"

    @property
    def core(self) -> ShugoCoreServer:
        """The ShugoCoreServer instance injected via ``core`` on the server."""
        server = self.server
        core = getattr(server, "core", None)
        if core is None:
            core = getattr(server, "__core", None)
        return core

    # -- routing -------------------------------------------------------------

    def _dispatch(self, method: str, path: str) -> None:
        core = self.core
        if core is None:
            _send_json(self, 503, {"error": "server not initialized"})
            return
        # /health stays open for liveness probes; every other route is subject
        # to the per-client rate limit and (when configured) bearer auth.
        if not path.endswith("/health"):
            client_ip = self.client_address[0] if self.client_address else "unknown"
            if not core.allow_request(client_ip):
                _send_json(self, 429, {"error": "rate limit exceeded"})
                return
            if not core.authorize(_extract_token(self)):
                _send_json(self, 401, {"error": "unauthorized"})
                return
        status, payload = 404, {"error": "not found"}
        if path.endswith("/health") and method == "GET":
            status, payload = core.handle_health()
        elif path.endswith("/api/tags") and method == "GET":
            status, payload = core.handle_tags()
        elif path.endswith("/api/generate") and method == "POST":
            status, payload = core.handle_generate(_read_json_body(self))
        elif path.endswith("/api/chat") and method == "POST":
            status, payload = core.handle_chat(_read_json_body(self))
        elif path.endswith("/api/v1/status") and method == "GET":
            status, payload = core.handle_status()
        elif path.endswith("/api/v1/task") and method == "POST":
            status, payload = core.handle_task(_read_json_body(self))

        # Ollama-style streaming generate responses are sent as NDJSON.
        if payload.get("__ndjson__") is not None and status == 200:
            _send_ndjson(self, payload["__ndjson__"])
            return
        _send_json(self, status, payload)

    # -- HTTP verbs ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET", self.path)

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST", self.path)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        origin = str(self.headers.get("Origin", "") or "")
        if _is_loopback_origin(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, X-ShugoCore-Token")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s %s", self.address_string(), fmt % args)


# ---------------------------------------------------------------------------
# Backend config + wiring
# ---------------------------------------------------------------------------
def _backend_config(backend: str, backend_url: str = "") -> Dict[str, Any]:
    """Return the shared backend config used both by the server and engine."""
    backend = str(backend).lower()
    url = str(backend_url or "").strip()
    if backend == "llamacpp":
        # llama.cpp llama-server exposes an OpenAI-compatible /v1 endpoint.
        return {"type": "openai", "base_url": url or "http://127.0.0.1:8080"}
    if backend == "openai":
        return {"type": "openai", "base_url": url or "https://api.openai.com/v1"}
    if backend == "stub":
        return {"type": "stub"}
    if backend == "ollama":
        return {"type": "ollama", "base_url": url or "http://127.0.0.1:11434"}
    raise ValueError(f"unknown backend: {backend}")


def _build_backend(backend: str, backend_url: str = "") -> Any:
    """Construct a model backend from CLI flags (imported lazily)."""
    from model_backends import create_backend
    config = _backend_config(backend, backend_url)
    return create_backend(config)


def build_engine(models: Optional[List[Dict[str, Any]]] = None,
                 memory_db_path: Optional[str] = "semantic_memory.db",
                 audit_path: Optional[str] = "audit_chain.jsonl",
                 episodic_journal_path: Optional[str] = None,
                 vector_db_config: Optional[Dict[str, Any]] = None,
                 **kwargs: Any) -> Any:
    """Build a DecisionEngine (imported lazily so stdlib-only tests stay fast)."""
    from decision_engine import DecisionEngine

    if getattr(vector_db_config, "get", None) is None:
        vector_db_config = {"type": "chroma"}
    if not models:
        models = [{"id": "shugocore-local", "type": "text",
                   "backend": {"type": "stub"}}]
    return DecisionEngine(
        models,
        vector_db_config,
        memory_db_path=memory_db_path,
        audit_path=audit_path,
        episodic_journal_path=episodic_journal_path,
        **kwargs,
    )


def build_server(engine=None, backend=None, model: str = "qwen3.5:latest",
                 host: str = "127.0.0.1",
                 port: int = 11434,
                 auth_token: Optional[str] = None,
                 rate_limit_per_minute: Optional[float] = DEFAULT_RATE_LIMIT_PER_MINUTE,
                 rate_limit_burst: int = DEFAULT_RATE_LIMIT_BURST,
                 ) -> http_server.ThreadingHTTPServer:
    """Create and bind the HTTPServer with the injected core."""
    if engine is None:
        engine = build_engine()
    if backend is None:
        backend = _build_backend("stub")
    core = ShugoCoreServer(engine, backend, model=model,
                           auth_token=auth_token,
                           rate_limit_per_minute=rate_limit_per_minute,
                           rate_limit_burst=rate_limit_burst)

    class _BoundHandler(ShugoCoreHandler):
        pass

    server = http_server.ThreadingHTTPServer((host, port), _BoundHandler)
    server.core = core  # type: ignore[attr-defined]
    return server


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="shugocore-server",
        description="ShugoCore desktop server (Ollama wire contract + engine API).",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: 127.0.0.1; use 0.0.0.0 for LAN)")
    parser.add_argument("--port", type=int, default=11434,
                        help="bind port (default: 11434; use 11435 if Ollama owns 11434)")
    parser.add_argument("--backend", default="ollama",
                        choices=["ollama", "llamacpp", "openai", "stub"],
                        help="model backend (default: ollama)")
    parser.add_argument("--backend-url", default="",
                        help="backend base URL (defaults per backend type)")
    parser.add_argument("--model", default="qwen3.5:latest",
                        help="model id reported by /api/tags and /api/generate")
    parser.add_argument("--memory-db-path", default="semantic_memory.db")
    parser.add_argument("--audit-path", default="audit_chain.jsonl")
    parser.add_argument("--episodic-journal-path", default=None)
    parser.add_argument("--auth-token-env", default=SERVER_TOKEN_ENV,
                        help=f"env var holding the bearer token "
                             f"(default: {SERVER_TOKEN_ENV})")
    parser.add_argument("--allow-unauthenticated", action="store_true",
                        help="permit a non-loopback bind with no auth token (dangerous)")
    parser.add_argument("--rate-limit-per-minute", type=float,
                        default=DEFAULT_RATE_LIMIT_PER_MINUTE,
                        help="per-client request rate, 0 disables "
                             "(default: 240)")
    parser.add_argument("--rate-limit-burst", type=int,
                        default=DEFAULT_RATE_LIMIT_BURST,
                        help="per-client burst capacity (default: 120)")
    args = parser.parse_args(argv)

    # Fail-closed exposure: a non-loopback bind must be authenticated unless
    # the operator explicitly opts out.
    token = os.environ.get(args.auth_token_env) or None
    if not _is_loopback_host(args.host):
        if not token and not args.allow_unauthenticated:
            print(f"error: refusing to bind to {args.host} without authentication.",
                  file=sys.stderr)
            print(f"  Set {args.auth_token_env}=<secret> to require a bearer token, "
                  f"or pass", file=sys.stderr)
            print("  --allow-unauthenticated to expose the engine endpoints openly.",
                  file=sys.stderr)
            return 2
        if not token:
            logger.warning("binding to %s with NO authentication "
                           "(--allow-unauthenticated)", args.host)

    try:
        backend = _build_backend(args.backend, args.backend_url)
        backend_config = _backend_config(args.backend, args.backend_url)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    engine_kwargs: Dict[str, Any] = {
        "memory_db_path": args.memory_db_path,
        "audit_path": args.audit_path,
    }
    if args.episodic_journal_path:
        engine_kwargs["episodic_journal_path"] = args.episodic_journal_path

    try:
        engine = build_engine(
            models=[{"id": args.model, "type": "text", "backend": backend_config}],
            **engine_kwargs,
        )
        server = build_server(engine=engine, backend=backend,
                              model=args.model, host=args.host, port=args.port,
                              auth_token=token,
                              rate_limit_per_minute=args.rate_limit_per_minute,
                              rate_limit_burst=args.rate_limit_burst)
    except OSError as exc:
        print(f"error: cannot bind {args.host}:{args.port}: {exc}", file=sys.stderr)
        print("  If you are using the ollama backend, a local Ollama already owns :11434.",
              file=sys.stderr)
        print("  Pick a different port:  shugocore-server --port 11435", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: failed to initialize engine: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2

    logger.info("ShugoCore server listening on http://%s:%d (backend=%s, model=%s)",
                args.host, args.port, args.backend, args.model)
    logger.info("Auth: %s", "bearer token required"
                if token else "disabled (loopback or --allow-unauthenticated)")
    logger.info("Ollama wire contract: /api/generate /api/chat /api/tags /health")
    logger.info("Engine API:           /api/v1/status  POST /api/v1/task")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())