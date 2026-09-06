"""
ShugoCore telemetry hooks
=========================

Native observability across execution paths (task phases, HTTP, backend
generation, memory operations).

- When ``opentelemetry-api`` is installed, real OTel spans are emitted
  (wired to whatever exporters the host configures).
- Otherwise a built-in no-op tracer records span durations in a ring buffer
  for local introspection - same API, zero dependencies.

Usage::

    from telemetry import get_tracer
    tracer = get_tracer("decision_engine")
    with tracer.start_span("agent.task", {"task_type": "api_call"}) as span:
        ...
        span.set_attribute("status", "success")

Recent spans (no-op mode)::

    tracer.recent_spans()  -> [{"name": ..., "duration_ms": ..., ...}]
"""

import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Deque, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

try:
    from opentelemetry import trace as _otel_trace
    _HAS_OTEL = _otel_trace is not None
except Exception:  # pragma: no cover - import can fail for many reasons
    _HAS_OTEL = False

_MAX_RECENT_SPANS = 1024


class Span:
    """Minimal span interface shared by the OTel and no-op implementations."""

    def set_attribute(self, key: str, value: Any) -> None: ...

    def add_event(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> None: ...


class _NoopSpan(Span):
    __slots__ = ("name", "_started", "_attributes", "_ended", "_events")

    def __init__(self, name: str, attributes: Optional[Dict[str, Any]] = None):
        self.name = name
        self._started = time.monotonic()
        self._attributes: Dict[str, Any] = dict(attributes or {})
        self._ended: Optional[float] = None
        self._events: List[Dict[str, Any]] = []

    def set_attribute(self, key: str, value: Any) -> None:
        self._attributes[key] = value

    def add_event(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> None:
        self._events.append({"name": name, "attributes": dict(attributes or {})})

    def _finish(self) -> Dict[str, Any]:
        self._ended = time.monotonic()
        return {
            "name": self.name,
            "attributes": dict(self._attributes),
            "duration_ms": round((self._ended - self._started) * 1000.0, 3),
            "events": list(self._events),
        }


class _OtelSpan(Span):
    """Adapter over a real OpenTelemetry span (when available).

    Attribute/event calls are mirrored locally so the diagnostic
    ``recent_spans()`` buffer stays populated even when no OTel
    provider/exporter is configured (the common dev case) — previously
    the OTel path ended before recording anything, silently emptying
    the buffer on any host with ``opentelemetry-api`` installed.
    """

    def __init__(self, span: Any, name: str,
                 attributes: Optional[Dict[str, Any]] = None):
        self._span = span
        self.name = name
        self._started = time.monotonic()
        self._attributes: Dict[str, Any] = dict(attributes or {})
        self._events: List[Dict[str, Any]] = []

    def set_attribute(self, key: str, value: Any) -> None:
        self._attributes[key] = value
        try:
            self._span.set_attribute(key, value)
        except Exception:
            pass

    def add_event(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> None:
        self._events.append({"name": name, "attributes": dict(attributes or {})})
        try:
            self._span.add_event(name, attributes or {})
        except Exception:
            pass

    def _finish(self) -> Dict[str, Any]:
        self._ended = time.monotonic()
        try:
            self._span.end()   # still flows to any host-configured provider
        except Exception:
            pass
        return {
            "name": self.name,
            "attributes": dict(self._attributes),
            "duration_ms": round((self._ended - self._started) * 1000.0, 3),
            "events": list(self._events),
        }


class Tracer:
    """Span generator that prefers OTel when installed."""

    def __init__(self, name: str = "shugocore"):
        self.name = name
        self._recent: Deque[Dict[str, Any]] = deque(maxlen=_MAX_RECENT_SPANS)
        self._lock = threading.Lock()

    @contextmanager
    def start_span(self, name: str,
                   attributes: Optional[Dict[str, Any]] = None) -> Iterator[Span]:
        span: Optional[Any] = None
        if _HAS_OTEL:
            try:
                instrumentor = _otel_trace.get_tracer(self.name)
                span = instrumentor.start_span(name, attributes=attributes or {})
            except Exception:
                span = None
            if span is not None:
                adapter = _OtelSpan(span, name, attributes)
                try:
                    yield adapter
                finally:
                    record = adapter._finish()
                    with self._lock:
                        self._recent.append(record)
                return

        noop = _NoopSpan(name, attributes)
        try:
            yield noop
        finally:
            record = noop._finish()
            with self._lock:
                self._recent.append(record)

    def recent_spans(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Most recent spans recorded by the no-op tracer (for local insight)."""
        with self._lock:
            items = list(self._recent)
        return items[-limit:] if limit else items

    def clear(self) -> None:
        with self._lock:
            self._recent.clear()


_tracers: Dict[str, Tracer] = {}
_tracers_lock = threading.Lock()


def get_tracer(name: str = "shugocore") -> Tracer:
    """
    Return a process-lifetime tracer. When ``opentelemetry-api`` is
    installed, spans flow to whatever provider/exporters the host process
    configures; otherwise the built-in no-op tracer records durations.
    """
    with _tracers_lock:
        tracer = _tracers.get(name)
        if tracer is None:
            tracer = Tracer(name)
            _tracers[name] = tracer
        return tracer