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
import ipaddress
import json
import logging
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http import server as http_server
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlsplit

from security import RateLimiter

# Track 2: security inventory & baseline (observational, optional).
try:
    from security_inventory import (collect_agent_inventory,
                                    collect_server_inventory,
                                    evaluate_baseline)
    from security_inventory import SERVER_BASELINE as _SERVER_BASELINE
    _HAS_SECURITY_INVENTORY = True
except Exception:
    _HAS_SECURITY_INVENTORY = False

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

# Activity accounting bounds (the repo's no-unbounded-growth principle):
# latency samples and the request-per-minute window are both capped.
ACTIVITY_LATENCY_RING = 100      # latency samples kept per endpoint
ACTIVITY_RPM_WINDOW = 60.0       # seconds in the requests/minute window
ACTIVITY_RPM_CAP = 2000          # max timestamps retained across endpoints

# Bounds for the human-approval, fleet, and sensor-stream surfaces.
APPROVALS_MAX_PENDING = 200      # pending approval requests surfaced per call
FLEET_MAX_NODES = 200            # paired mobile nodes surfaced per call
SENSOR_STREAM_RING = 100         # bounded sensor samples retained (drop-oldest)


# ---------------------------------------------------------------------------
# Bind / origin helpers
# ---------------------------------------------------------------------------
def _is_loopback_host(host: str) -> bool:
    """True for loopback bind addresses (only these may run unauthenticated).

    Strict: ``localhost`` (exact, case-insensitive), the IPv6 loopback
    ``::1``, or a numeric IPv4 address inside 127.0.0.0/8. The old
    ``startswith("127.")`` prefix test also matched attacker-controlled DNS
    names such as ``127.evil.com`` or ``127.0.0.1.nip.io`` -- those resolve
    to non-loopback addresses and must NOT count as loopback.
    """
    candidate = str(host or "").strip().strip("[]").lower().rstrip(".")
    if candidate in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _normalize_request_path(raw_path: str) -> str:
    """Return the decoded path component of a request-target.

    Strips query/fragment, percent-decodes, collapses a single trailing
    slash (except the root), so ``/health?x=1`` and ``/health/`` route like
    ``/health`` while ``/evil/health`` never does.
    """
    try:
        path = urlsplit(str(raw_path or "")).path or "/"
    except ValueError:
        return "/"
    from urllib.parse import unquote
    path = unquote(path)
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path or "/"


def _approval_request_id(path: str) -> Optional[str]:
    """Extract ``<id>`` from ``/api/v1/approvals/<id>/approve|deny``.

    Returns None unless the normalized path has exactly that shape and the
    id is non-empty without embedded slashes.
    """
    parts = path.split("/")
    # ['', 'api', 'v1', 'approvals', '<id>', 'approve'|'deny']
    if (len(parts) == 6 and parts[1] == "api" and parts[2] == "v1"
            and parts[3] == "approvals" and parts[5] in ("approve", "deny")
            and parts[4]):
        return parts[4]
    return None


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


def _safe_value(value: Any, depth: int = 0) -> Any:
    """Recursively bound a payload value (depth + breadth + string caps).

    Scalars are coerced via ``_safe_text``; containers are walked with a
    small max-depth (deeper levels collapse to ``"[truncated]"``), a
    max-item cap (extras dropped), and a running char budget so a 5 MB
    nested blob cannot ride through the ``result`` field uncapped.
    """
    if depth > 4:
        return "[truncated]"
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= 50:
                break
            out[_safe_text(k, 64)] = _safe_value(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_safe_value(v, depth + 1) for v in list(value)[:50]]
    return _safe_text(value, 2000)


def _safe_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    """Trim a task result to safe, serializable fields.

    Every value -- including nested ``dict``/``list`` results -- is
    recursively capped and passed through ``redact`` so secrets or
    unbounded memory echoes from ``engine.execute_task`` never reach
    the wire verbatim.
    """
    from security import redact as _redact
    safe: Dict[str, Any] = {"status": _safe_text(result.get("status"), 32)}
    for key in ("reason", "message", "result", "action_type", "summary"):
        if result.get(key) is not None:
            safe[key] = _redact(_safe_value(result[key]))
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
        self._started_wall = time.time()
        self._limiter: Optional[RateLimiter] = None
        if rate_limit_per_minute and float(rate_limit_per_minute) > 0:
            self._limiter = RateLimiter(
                calls_per_minute=float(rate_limit_per_minute),
                burst=int(rate_limit_burst),
            )
        # -- activity accounting (bounded; honest; observational only) --------
        # Per-endpoint outcome counters. Every dispatched wire request lands
        # here exactly once: 2xx -> ok, 4xx -> client_error, 5xx -> server_error.
        self._activity_lock = threading.Lock()
        self._stats: Dict[str, Dict[str, int]] = {}
        # Per-endpoint latency ring (most recent last), capped.
        self._latency: Dict[str, deque] = {}
        # Shared request-timestamp window for requests/minute, pruned by time.
        self._rpm: deque = deque(maxlen=ACTIVITY_RPM_CAP)
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

    # -- activity accounting ---------------------------------------------------

    @staticmethod
    def route_name(method: str, path: str) -> Optional[str]:
        """Canonical activity-bucket name for a wire request (None = unknown).

        ``path`` must already be normalized via ``_normalize_request_path``
        (no query string); matching is exact so ``/evil/health`` never
        buckets as ``health``.
        """
        if method == "GET":
            if path == "/health":
                return "health"
            if path == "/api/tags":
                return "tags"
            if path == "/api/v1/status":
                return "status"
            if path == "/api/v1/activity":
                return "activity"
            if path == "/api/v1/uptime":
                return "uptime"
            if path == "/api/v1/approvals":
                return "approvals"
            if path == "/api/v1/fleet":
                return "fleet"
            if path == "/api/v1/sensors":
                return "sensors"
            if path == "/api/v1/security":
                return "security"
        elif method == "POST":
            if path == "/api/generate":
                return "generate"
            if path == "/api/chat":
                return "chat"
            if path == "/api/v1/task":
                return "task"
            if path == "/api/v1/fleet":
                return "fleet_pair"
            if _approval_request_id(path) is not None:
                return ("approve" if path.endswith("/approve") else "deny")
        return None

    def record(self, route: Optional[str], http_status: int,
               latency_ms: Optional[float] = None) -> None:
        """Record one dispatched request (outcome bucket + latency + window)."""
        bucket = ("ok" if http_status < 400
                  else "client_error" if http_status < 500
                  else "server_error")
        name = route or "other"
        now = time.monotonic()
        with self._activity_lock:
            stats = self._stats.setdefault(
                name, {"requests": 0, "ok": 0,
                       "client_error": 0, "server_error": 0})
            stats["requests"] += 1
            stats[bucket] += 1
            if latency_ms is not None:
                ring = self._latency.setdefault(
                    name, deque(maxlen=ACTIVITY_LATENCY_RING))
                ring.append(float(latency_ms))
            self._rpm.append((now, name))

    def _rpm_counts(self, now: float) -> Dict[str, float]:
        """Requests/minute per endpoint over the trailing window (pruned)."""
        cutoff = now - ACTIVITY_RPM_WINDOW
        counts: Dict[str, int] = {}
        for ts, name in self._rpm:
            if ts >= cutoff:
                counts[name] = counts.get(name, 0) + 1
        return {name: round(n * 60.0 / ACTIVITY_RPM_WINDOW, 2)
                for name, n in counts.items()}

    def _agent_activity(self) -> Optional[Dict[str, Any]]:
        """Phase-A loop status from a hosted agent, verbatim (None if absent).

        The desktop server usually fronts a bare DecisionEngine (no loop); a
        deployment that hosts the full agent exposes get_status() with the
        instrumented loop/loop_stages/mesh_activity keys. Only real keys are
        surfaced — an absent surface is omitted, never fabricated.
        """
        get_status = getattr(self.engine, "get_status", None)
        if not callable(get_status):
            return None
        try:
            snap = get_status()
        except Exception as exc:
            logger.warning("agent get_status failed: %s", type(exc).__name__)
            return None
        if not isinstance(snap, dict):
            return None
        agent = {key: snap[key] for key in
                 ("loop", "loop_stages", "mesh_activity", "uptime_seconds",
                  "mesh_role", "mesh_primary")
                 if key in snap}
        return agent or None

    def activity_summary(self) -> Dict[str, Any]:
        """Compact totals for GET /api/v1/status (additive, backward-safe)."""
        now = time.monotonic()
        with self._activity_lock:
            requests = sum(s["requests"] for s in self._stats.values())
            ok = sum(s["ok"] for s in self._stats.values())
            client = sum(s["client_error"] for s in self._stats.values())
            server = sum(s["server_error"] for s in self._stats.values())
            total_rpm = round(sum(self._rpm_counts(now).values()), 2)
        return {"requests_total": requests, "ok_total": ok,
                "client_error_total": client, "server_error_total": server,
                "requests_per_minute": total_rpm}

    def handle_activity(self) -> Tuple[int, Dict[str, Any]]:
        """GET /api/v1/activity -> full activity snapshot."""
        now = time.monotonic()
        with self._activity_lock:
            rpm = self._rpm_counts(now)
            endpoints: Dict[str, Dict[str, Any]] = {}
            for name, stats in self._stats.items():
                entry: Dict[str, Any] = dict(stats)
                entry["requests_per_minute"] = rpm.get(name, 0.0)
                ring = self._latency.get(name)
                if ring:
                    recent = list(ring)[-25:]
                    entry["latency_ms"] = {
                        "avg_recent": round(sum(recent) / len(recent), 1),
                        "max": round(max(ring), 1),
                        "last": round(ring[-1], 1),
                    }
                endpoints[name] = entry
        body: Dict[str, Any] = {
            "version": self._version,
            "model": self.model,
            "backend": getattr(self.backend, "name", "unknown"),
            "uptime_seconds": round(now - self._started, 3),
            "requests": endpoints,
        }
        agent = self._agent_activity()
        if agent is not None:
            body["agent"] = agent
        return 200, body

    def handle_security(self) -> Tuple[int, Dict[str, Any]]:
        """GET /api/v1/security -> Track 2 security inventory + baseline.

        Wire-facing server controls (token, rate limiting) and the hosted
        engine's audit chain are always inventoried when the module is
        importable. Honest and additive: absent surfaces are omitted;
        unverifiable baseline controls are violations, never assumed safe.
        """
        body: Dict[str, Any] = {"version": self._version}
        if not _HAS_SECURITY_INVENTORY:
            body["security_inventory"] = None
            body["security_baseline"] = None
            body["security_module"] = "unavailable"
            return 200, body
        inventory = {"server": collect_server_inventory(self)}
        # Flatten the hosted engine's controls to the top level so the
        # baseline evaluator sees audit/policy sections directly.
        for key, value in collect_agent_inventory(self.engine).items():
            inventory[key] = value
        body["security_inventory"] = inventory
        body["security_baseline"] = evaluate_baseline(
            inventory, baseline=_SERVER_BASELINE)
        return 200, body

    def handle_uptime(self) -> Tuple[int, Dict[str, Any]]:
        """GET /api/v1/uptime -> server uptime + start time."""
        return 200, {
            "uptime_seconds": round(time.monotonic() - self._started, 3),
            "started_at": datetime.fromtimestamp(
                self._started_wall, tz=timezone.utc).isoformat(),
        }

    # -- C1: human-approval surface -------------------------------------------

    def handle_approvals(self) -> Tuple[int, Dict[str, Any]]:
        """GET /api/v1/approvals -> pending side-effecting action approvals.

        The ApprovalBroker is fail-closed (no attached channel -> immediate
        denial); when the engine hosts a broker, this endpoint lets an
        operator console poll the pending queue and resolve each request
        explicitly instead of relying on the TTL default-deny. Bounded
        (``APPROVALS_MAX_PENDING``) and sanitized: descriptions are
        length-capped strings, never raw request payloads.
        """
        broker = getattr(self.engine, "approvals", None)
        if broker is None or not hasattr(broker, "list_pending"):
            return 200, {"approvals": [], "count": 0, "enabled": False}
        try:
            pending = broker.list_pending()
        except Exception as exc:
            logger.warning("approvals list failed: %s", type(exc).__name__)
            return 500, {"error": f"approval list failed: {type(exc).__name__}"}
        out: List[Dict[str, Any]] = []
        for req in pending[:APPROVALS_MAX_PENDING]:
            if not isinstance(req, dict):
                continue
            try:
                description = req.get("description", {})
                if isinstance(description, dict):
                    safe_desc = {_safe_text(k, 64): _safe_text(v, 500)
                                 for k, v in description.items()}
                else:
                    safe_desc = {"summary": _safe_text(description, 500)}
                out.append({
                    "request_id": _safe_text(req.get("request_id"), 64),
                    "description": safe_desc,
                    "requested_at": round(float(req.get("requested_at", 0.0)), 3),
                })
            except (TypeError, ValueError):
                # One malformed broker entry must not 500 the whole listing
                # (or drop the handler thread's connection).
                logger.warning("skipping malformed approval entry: %s",
                               type(req).__name__)
                continue
        return 200, {
            "approvals": out,
            "count": len(out),
            "pending_total": len(pending),
            "enabled": True,
            "ttl_seconds": getattr(broker, "ttl_seconds", None),
        }

    def resolve_approval(self, request_id: str,
                         approved: bool) -> Tuple[int, Dict[str, Any]]:
        """POST /api/v1/approvals/<id>/approve|deny -> operator resolution."""
        broker = getattr(self.engine, "approvals", None)
        if broker is None or not hasattr(broker, "approve"):
            return 503, {"error": "no approval broker on this engine"}
        rid = _safe_text(request_id, 64)
        if not rid:
            return 400, {"error": "request_id required"}
        try:
            if approved:
                ok = bool(broker.approve(rid))
            else:
                ok = bool(broker.deny(rid))
        except Exception as exc:
            logger.warning("approval resolve failed: %s", type(exc).__name__)
            return 500, {"error": f"approval resolve failed: {type(exc).__name__}"}
        return 200, {"request_id": rid, "decision": "approve" if approved
                     else "deny", "resolved": ok}

    # -- C2: fleet dashboard --------------------------------------------------

    def handle_fleet(self) -> Tuple[int, Dict[str, Any]]:
        """GET /api/v1/fleet -> bounded snapshot of paired mobile nodes."""
        handler = getattr(self.engine, "mobile_handler", None)
        registry = None
        if handler is not None:
            registry = getattr(handler, "registry", None)
            if registry is None:
                registry = getattr(handler, "node_registry", None)
            if callable(registry):
                try:
                    registry = registry()
                except Exception:
                    registry = None
        if registry is None or not hasattr(registry, "list_nodes"):
            return 200, {"nodes": [], "count": 0, "enabled": False}
        try:
            nodes = registry.list_nodes()
        except Exception as exc:
            logger.warning("fleet listing failed: %s", type(exc).__name__)
            return 500, {"error": f"fleet listing failed: {type(exc).__name__}"}
        out: List[Dict[str, Any]] = []
        for node in nodes[:FLEET_MAX_NODES]:
            if not isinstance(node, dict):
                continue
            manifest = node.get("manifest", {})
            if not isinstance(manifest, dict):
                manifest = {}
            out.append({
                "device_id": _safe_text(node.get("device_id"), 128),
                "alive": bool(node.get("alive")),
                "paired_by": _safe_text(node.get("paired_by"), 64),
                "expires_at": node.get("expires_at"),
                "manifest": {_safe_text(k, 64): _safe_text(v, 200)
                             for k, v in manifest.items()},
            })
        return 200, {"nodes": out, "count": len(out), "enabled": True}

    def handle_fleet_pair(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """POST /api/v1/fleet -> operator pairing/unpairing of mobile nodes.

        Pairing is consent (docs/android_integration.md): it allowlists the
        device in the registry, keeps the ingest topic-ACL allowlist in sync,
        and subscribes the device's contract topics. Body:
        ``{"device_id": "...", "action": "pair"|"unpair", "manifest": {...}}``.
        Bearer auth and rate limiting already ran in ``_dispatch``.
        """
        if not isinstance(body, dict):
            return 400, {"error": "body must be a JSON object"}
        raw_id = body.get("device_id")
        device_id = (str(raw_id).strip().strip("/")
                     if isinstance(raw_id, (str, int)) else "")
        if not device_id or len(device_id) > 48 or "/" in device_id \
                or device_id in (".", ".."):
            return 400, {"error": "device_id required (max 48 chars)"}
        raw_action = body.get("action")
        action = (raw_action.strip().lower()
                  if isinstance(raw_action, str) and raw_action.strip()
                  else "pair")
        if action not in ("pair", "unpair"):
            return 400, {"error": "action must be 'pair' or 'unpair'"}
        handler = getattr(self.engine, "mobile_handler", None)
        if handler is None:
            return 503, {"error": "mobile fleet not enabled"}
        registry = getattr(handler, "registry", None)
        if registry is None:
            registry = getattr(handler, "node_registry", None)
        if callable(registry):
            try:
                registry = registry()
            except Exception:
                registry = None
        if registry is None or not hasattr(registry, "pair"):
            return 503, {"error": "mobile fleet not enabled"}
        if action == "unpair":
            removed = bool(registry.unpair(device_id))
            # Revoke consent symmetrically: drop the device from the
            # ingest topic-ACL allowlist too (set in policy.CapabilityRegistry).
            try:
                manager = getattr(handler, "manager", None)
                caps = getattr(manager, "capabilities", None)
                allow = getattr(caps, "mobile_devices_allowlist", None)
                if isinstance(allow, set):
                    allow.discard(device_id)
                elif isinstance(allow, list) and device_id in allow:
                    allow.remove(device_id)
            except Exception as exc:
                logger.warning("fleet unpair allowlist sync failed for %s: %s",
                               device_id, type(exc).__name__)
            return 200, {"enabled": True, "action": "unpair",
                         "device_id": device_id, "removed": removed}
        manifest = body.get("manifest")
        if not isinstance(manifest, dict):
            manifest = {}
        paired_by = body.get("paired_by")
        paired_by = (str(paired_by).strip()
                     if isinstance(paired_by, str) and paired_by.strip()
                     else "operator")
        try:
            entry = registry.pair(device_id, manifest, paired_by=paired_by)
        except ValueError as exc:
            return 400, {"error": str(exc)}
        # Pairing == consent: keep the topic-ACL allowlist in sync and
        # subscribe the contract topics (best-effort, never fatal).
        try:
            manager = getattr(handler, "manager", None)
            caps = getattr(manager, "capabilities", None)
            allow = getattr(caps, "mobile_devices_allowlist", None)
            if isinstance(allow, set):
                allow.add(device_id)
            elif isinstance(allow, list) and device_id not in allow:
                allow.append(device_id)
            if callable(getattr(manager, "subscribe_device", None)):
                manager.subscribe_device(device_id)
        except Exception as exc:
            logger.warning("fleet pair post-steps failed for %s: %s",
                           device_id, type(exc).__name__)
        return 200, {"enabled": True, "action": "pair",
                     "device_id": device_id, "paired": True,
                     "alive": bool(registry.alive(device_id)),
                     "expires_at": entry.get("expires_at")}

    # -- C3: sensor live-stream (poll-based, bounded) -------------------------

    def handle_sensors(self) -> Tuple[int, Dict[str, Any]]:
        """GET /api/v1/sensors -> bounded live sensor-stream window.

        Each poll records ONE bounded sample (the hosted agent's current
        telemetry snapshot — thermal, power, capabilities, mesh peers) into
        a fixed-capacity ring and returns the ring so a dashboard can render
        a compact live stream. The ring never grows past
        ``SENSOR_STREAM_RING`` samples. A bare engine (no hosted agent)
        returns ``enabled: False`` — never fabricated telemetry.
        """
        get_status = getattr(self.engine, "get_status", None)
        status: Dict[str, Any] = {}
        if callable(get_status):
            try:
                status = get_status()
            except Exception as exc:
                logger.warning("sensor status failed: %s", type(exc).__name__)
                return 500, {"error": f"sensor status failed: {type(exc).__name__}"}
        raw_telemetry = getattr(self.engine, "telemetry", None)
        telemetry: Dict[str, Any] = {}
        if isinstance(raw_telemetry, dict):
            telemetry = raw_telemetry
        if not status and not telemetry:
            return 200, {"stream": [], "enabled": False}
        sample: Dict[str, Any] = {
            "ts": round(time.time(), 3),
            "telemetry": {_safe_text(k, 64): _safe_text(v, 200)
                          for k, v in telemetry.items()},
        }
        for key in ("memory_usage_mb", "mesh_peer_count", "tick_count",
                    "telemetry_received"):
            if key in status:
                sample[key] = status[key]
        if "mesh_peers" in status and isinstance(status["mesh_peers"], list):
            sample["mesh_peers"] = status["mesh_peers"][:20]
        if "capabilities" in status and isinstance(status["capabilities"], dict):
            sample["capabilities"] = {
                _safe_text(k, 64): _safe_text(v, 120)
                for k, v in status["capabilities"].items()}
        with self._activity_lock:
            if not hasattr(self, "_sensor_ring"):
                self._sensor_ring = deque(maxlen=SENSOR_STREAM_RING)
            self._sensor_ring.append(sample)
            ring_snapshot = list(self._sensor_ring)
        return 200, {"stream": ring_snapshot, "enabled": True,
                     "count": len(ring_snapshot),
                     "capacity": SENSOR_STREAM_RING}

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
                "uptime_seconds": round(time.monotonic() - self._started, 3),
                "activity": self.activity_summary(),
            }
            return 200, state
        except Exception as exc:
            logger.exception("status endpoint failed")
            return 500, {"error": f"status failed: {type(exc).__name__}"}

    def handle_task(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """POST /api/v1/task -> engine.execute_task (policy-gated).

        ``params`` is deep-capped at the boundary (nested strings sanitized
        and length-capped, containers bounded in depth/breadth) so a hostile
        caller cannot smuggle an unbounded blob or control characters past
        the ``type``/``content`` caps into the policy/execution pipeline;
        ``execute_task`` still re-gates everything downstream.
        """
        if not isinstance(body, dict) or not body:
            return 400, {"error": "task body required"}
        task = {
            "type": _safe_text(body.get("type", "user"), 64),
            "params": _safe_value(body.get("params")
                                  if isinstance(body.get("params"), dict) else {}),
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
        # Normalize once: strip query/fragment, percent-decode, collapse a
        # trailing slash. Exact-match routing below then guarantees
        # ``/evil/health`` or ``/evil/api/tags`` never hit real handlers,
        # while ``/health?x=1`` still serves liveness probes.
        norm_path = _normalize_request_path(path)
        # /health stays open for liveness probes; every other route is subject
        # to the per-client rate limit and (when configured) bearer auth.
        # Auth/rate-limit run before the existence check so unauthenticated
        # probes cannot distinguish "unknown path" (404) from "known but
        # unauthorized" (401) -- no route oracle for scanners.
        route = core.route_name(method, norm_path)
        if norm_path != "/health":
            client_ip = self.client_address[0] if self.client_address else "unknown"
            if not core.allow_request(client_ip):
                core.record(route, 429)
                _send_json(self, 429, {"error": "rate limit exceeded"})
                return
            if not core.authorize(_extract_token(self)):
                core.record(route, 401)
                _send_json(self, 401, {"error": "unauthorized"})
                return
        started = time.monotonic()
        status, payload = 404, {"error": "not found"}
        if norm_path == "/health" and method == "GET":
            status, payload = core.handle_health()
        elif norm_path == "/api/tags" and method == "GET":
            status, payload = core.handle_tags()
        elif norm_path == "/api/generate" and method == "POST":
            status, payload = core.handle_generate(_read_json_body(self))
        elif norm_path == "/api/chat" and method == "POST":
            status, payload = core.handle_chat(_read_json_body(self))
        elif norm_path == "/api/v1/status" and method == "GET":
            status, payload = core.handle_status()
        elif norm_path == "/api/v1/activity" and method == "GET":
            status, payload = core.handle_activity()
        elif norm_path == "/api/v1/uptime" and method == "GET":
            status, payload = core.handle_uptime()
        elif norm_path == "/api/v1/approvals" and method == "GET":
            status, payload = core.handle_approvals()
        elif norm_path == "/api/v1/fleet" and method == "GET":
            status, payload = core.handle_fleet()
        elif norm_path == "/api/v1/fleet" and method == "POST":
            status, payload = core.handle_fleet_pair(_read_json_body(self))
        elif norm_path == "/api/v1/sensors" and method == "GET":
            status, payload = core.handle_sensors()
        elif norm_path == "/api/v1/security" and method == "GET":
            status, payload = core.handle_security()
        elif method == "POST" and norm_path.endswith("/approve"):
            # Path shape: /api/v1/approvals/<id>/approve
            rid = _approval_request_id(norm_path)
            if rid is None:
                status, payload = 404, {"error": "not found"}
            else:
                status, payload = core.resolve_approval(rid, approved=True)
        elif method == "POST" and norm_path.endswith("/deny"):
            rid = _approval_request_id(norm_path)
            if rid is None:
                status, payload = 404, {"error": "not found"}
            else:
                status, payload = core.resolve_approval(rid, approved=False)
        elif norm_path == "/api/v1/task" and method == "POST":
            status, payload = core.handle_task(_read_json_body(self))
        core.record(route, status, (time.monotonic() - started) * 1000.0)

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


# socketserver.TCPServer defaults request_queue_size to 5, which is smaller than
# the concurrency the fleet surfaces invite (an approvals console plus N paired
# phones calling /api/v1/fleet and /api/v1/status). On macOS a SYN that arrives
# while the accept queue is full is RESET, so a client sees ECONNRESET before
# the handler — or even the rate limiter — ever runs; Linux drops the SYN and
# the retry succeeds, which is why this only bites on a Mac. Measured: 16
# simultaneous loopback connects -> 8-9 resets at the default 5, 0 at 128.
_LISTEN_BACKLOG = 128


class _BoundHTTPServer(http_server.ThreadingHTTPServer):
    """Threaded server whose accept queue matches a fleet, not a demo."""

    daemon_threads = True
    request_queue_size = _LISTEN_BACKLOG


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

    server = _BoundHTTPServer((host, port), _BoundHandler)
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
    parser.add_argument("--no-mobile", action="store_true",
                        help="disable the mobile fleet/sensor subsystem "
                             "(enabled by default)")
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

    # Mobile fleet/sensor subsystem (enabled by default; --no-mobile opts out).
    # Construction mirrors tests/test_mobile.py: registry + capabilities ->
    # manager + broker -> handler, handed to the engine as mobile_handler so
    # /api/v1/fleet and /api/v1/sensors report enabled=true. Any failure here
    # degrades to the historical behavior (enabled=false) instead of refusing
    # to start the server.
    if not args.no_mobile:
        try:
            from mobile_nodes import (MobileNodeManager, MobileComputeBroker,
                                      MobileExecutionHandler,
                                      MobileNodeRegistry)
            from policy import CapabilityRegistry
            from ros2_interface import create_ros2_interface

            _ros2 = create_ros2_interface(node_name="shugocore_server")
            _caps = CapabilityRegistry()
            _mregistry = MobileNodeRegistry()
            _manager = MobileNodeManager(_ros2, _mregistry, _caps)
            _broker = MobileComputeBroker(_ros2, _mregistry, _caps)
            engine_kwargs["mobile_handler"] = MobileExecutionHandler(
                _manager, _broker)
            logger.info("mobile fleet: enabled "
                        "(nodes appear in /api/v1/fleet once paired)")
        except Exception as exc:
            logger.warning("mobile fleet disabled (%s: %s)",
                           type(exc).__name__, exc)

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
    logger.info("Engine API:           /api/v1/status  /api/v1/security  POST /api/v1/task")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())