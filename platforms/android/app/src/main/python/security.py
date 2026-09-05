"""
ShugoCore security primitives
=============================

Dependency-free building blocks used across the framework:

- ``canonical_hash`` / ``canonical_json``  - stable hashing of dicts (used to
  bind policy verdicts to the exact decision they authorize)
- ``redact`` / ``RedactionFilter``        - keep secrets out of logs
- ``sanitize_text``                       - strip control characters and cap
  length (log-injection / memory-pollution defense)
- ``SecretResolver``                      - secrets sourced from environment
  variables or an in-memory override map, injected at execution time and
  never carried through decision dicts
- ``validate_url``                        - scheme/host/userinfo validation
  against an allowlist (SSRF defense)
- ``RateLimiter``                         - per-key token bucket
- ``CircuitBreaker``                      - per-host failure isolation
- ``retry_with_backoff``                  - bounded exponential retry
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Dict, Iterable, Optional, Tuple, Type
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

MAX_TEXT_LENGTH = 2000

_SECRET_KEY_PATTERN = re.compile(
    r"(api[-_]?key|apikey|authorization|token|secret|password|credential)",
    re.IGNORECASE,
)
_SECRET_IN_TEXT_PATTERN = re.compile(
    r"(api[-_]?key|apikey|token|access_token|password|authorization)(\s*[=:]\s*)([^\s&,\"']+)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Canonical hashing
# ---------------------------------------------------------------------------
def canonical_json(obj: Any) -> str:
    """Stable JSON serialization (sorted keys, no whitespace)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def canonical_hash(obj: Any) -> str:
    """SHA-256 over the canonical JSON form; binds verdicts to decisions."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
def redact(value: Any) -> Any:
    """
    Deep-copy ``value`` with secret-looking keys and credentials embedded in
    strings masked. Applied to everything that reaches a log handler.
    """
    if isinstance(value, dict):
        out: Dict[Any, Any] = {}
        for key, val in value.items():
            if isinstance(key, str) and _SECRET_KEY_PATTERN.search(key):
                out[key] = "***REDACTED***"
            else:
                out[key] = redact(val)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        return _SECRET_IN_TEXT_PATTERN.sub(
            lambda match: f"{match.group(1)}{match.group(2)}***REDACTED***", value
        )
    return value


class RedactionFilter(logging.Filter):
    """Logging filter that redacts secrets from every emitted record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
        except Exception:
            pass  # never break logging because of redaction
        return True


# ---------------------------------------------------------------------------
# Content sanitization
# ---------------------------------------------------------------------------
def sanitize_text(text: Any, max_length: int = MAX_TEXT_LENGTH) -> str:
    """
    Collapse control characters (prevents log/event injection), collapse the
    resulting whitespace runs, and hard-cap length (prevents memory
    pollution through the storage layers).
    """
    cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", str(text))
    cleaned = re.sub(r" {2,}", " ", cleaned)
    return cleaned.strip()[: max(0, int(max_length))]


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
class SecretResolver:
    """
    Resolves secrets from an in-memory override map or environment variables
    (``SHUGOCORE_<NAME>`` then ``<NAME>``). Secrets are injected at execution
    time and must never be stored in decision dicts or logged.
    """

    def __init__(self, overrides: Optional[Dict[str, str]] = None,
                 env_prefix: str = "SHUGOCORE_"):
        self._overrides: Dict[str, str] = dict(overrides or {})
        self._env_prefix = env_prefix
        self._lock = threading.Lock()

    def set(self, name: str, value: str) -> None:
        with self._lock:
            self._overrides[str(name)] = str(value)

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            override = self._overrides.get(str(name))
        if override:
            return override
        env_value = os.environ.get(self._env_prefix + str(name).upper())
        if env_value:
            return env_value
        return os.environ.get(str(name).upper()) or default

    def require(self, name: str) -> str:
        value = self.get(name)
        if not value:
            raise KeyError(
                f"Required secret '{name}' is not configured "
                f"(set {self._env_prefix}{str(name).upper()})."
            )
        return value


# ---------------------------------------------------------------------------
# URL validation (SSRF defense)
# ---------------------------------------------------------------------------
def validate_url(url: str, allowed_hosts: Iterable[str],
                 allowed_schemes: Tuple[str, ...] = ("https",)) -> Tuple[bool, str]:
    """
    Validate a URL against scheme restrictions and a host allowlist.
    Allowlist entries are exact hostnames (``api.example.com``) or wildcard
    suffixes (``*.example.com``). Rejects embedded credentials.
    """
    try:
        parsed = urlparse(str(url))
    except ValueError:
        return False, "unparseable URL"

    if parsed.scheme not in allowed_schemes:
        return False, f"URL scheme '{parsed.scheme or 'none'}' is not allowed"

    host = parsed.hostname
    if not host:
        return False, "URL has no host"

    if parsed.username or parsed.password:
        return False, "credentials embedded in URL are not allowed"

    host_lower = host.lower()
    for entry in allowed_hosts or []:
        entry = str(entry).lower()
        if entry.startswith("*."):
            if host_lower == entry[2:] or host_lower.endswith("." + entry[2:]):
                return True, ""
        elif host_lower == entry:
            return True, ""

    return False, f"host '{host_lower}' is not in the allowlist"


# ---------------------------------------------------------------------------
# Rate limiting (per-key token bucket)
# ---------------------------------------------------------------------------
class RateLimiter:
    """Blocking token-bucket rate limiter, keyed by arbitrary string."""

    def __init__(self, calls_per_minute: float = 60.0, burst: int = 10):
        self.rate = max(0.01, float(calls_per_minute)) / 60.0  # tokens/second
        self.capacity = max(1, int(burst))
        self._buckets: Dict[str, Tuple[float, float]] = {}  # key -> (tokens, ts)
        self._lock = threading.Lock()

    def acquire(self, key: str, timeout: float = 5.0) -> bool:
        """Wait up to ``timeout`` seconds for a token; True when acquired."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._lock:
                tokens, last = self._buckets.get(key, (float(self.capacity), time.monotonic()))
                now = time.monotonic()
                tokens = min(self.capacity, tokens + (now - last) * self.rate)
                if tokens >= 1.0:
                    self._buckets[key] = (tokens - 1.0, now)
                    return True
                wait_needed = (1.0 - tokens) / self.rate
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(wait_needed, remaining, 0.5))


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------
class CircuitBreaker:
    """
    Per-host failure isolation: after ``failure_threshold`` consecutive
    failures the breaker opens for ``reset_timeout`` seconds (fail fast
    instead of hammering a struggling dependency).
    """

    def __init__(self, failure_threshold: int = 5, reset_timeout: float = 60.0):
        self.failure_threshold = max(1, int(failure_threshold))
        self.reset_timeout = max(0.0, float(reset_timeout))
        self._failures = 0
        self._opened_at: Optional[float] = None
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return True
            if time.monotonic() - self._opened_at >= self.reset_timeout:
                # Half-open: allow one attempt through.
                self._opened_at = None
                self._failures = self.failure_threshold - 1
                return True
            return False

    def is_open(self) -> bool:
        """Pure read: whether the breaker is currently in the open state."""
        with self._lock:
            if self._opened_at is None:
                return False
            return time.monotonic() - self._opened_at < self.reset_timeout

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._opened_at = time.monotonic()


# ---------------------------------------------------------------------------
# Retry with backoff
# ---------------------------------------------------------------------------
def retry_with_backoff(fn: Callable[[], Any], attempts: int = 3,
                       base_delay: float = 0.5, max_delay: float = 4.0,
                       retry_on: Tuple[Type[BaseException], ...] = (Exception,),
                       sleep: Callable[[float], None] = time.sleep) -> Any:
    """Bounded exponential-backoff retry. Re-raises the final exception."""
    last_error: Optional[BaseException] = None
    for attempt in range(max(1, attempts)):
        try:
            return fn()
        except retry_on as exc:
            last_error = exc
            if attempt == attempts - 1:
                raise
            delay = min(max_delay, base_delay * (2 ** attempt))
            sleep(delay)
    raise last_error  # pragma: no cover - defensive

