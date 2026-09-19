"""
ShugoCore tamper-evident audit log
==================================

Implements the ``auditability`` invariant as a verifiable artifact: a JSONL
file where every entry carries ``prev_hash`` and ``hash = SHA-256(prev_hash
+ canonical entry)``. Any modification or deletion of a historical entry
breaks the chain and is detected by :func:`verify_audit_file`.

Usage::

    chain = AuditChain("audit_chain.jsonl")
    chain.append("decision", {"task": "..."})
    ok, errors = verify_audit_file("audit_chain.jsonl")

CLI::

    python3 audit.py verify audit_chain.jsonl
"""

import hashlib
import hmac
import json
import logging
import os
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Protocol

from security import canonical_hash, canonical_json, sanitize_text

logger = logging.getLogger(__name__)

GENESIS_HASH = "0" * 64

# Remote audit-shipping constants. The sinks are observational (the local
# JSONL chain is always the source of truth) and must be fail-safe: a sink
# failure can never block a decision. Bounded everywhere — bounded retry,
# bounded queue, bounded per-sink thread count.
LOG_SINK_QUEUE_CAP = 1024      # pending entries per sink (drop oldest on overflow)
LOG_SINK_HTTP_TIMEOUT_S = 5.0  # HTTPS sink request timeout
LOG_SINK_RETRY_BACKOFF_S = 2.0  # base backoff for failed dispatches
LOG_SINK_MAX_RETRIES = 3       # bounded retry count per entry
LOG_SINK_FLUSH_INTERVAL_S = 1.0  # background flush tick
LOG_SINK_BATCH_MAX = 50        # entries flushed per tick


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Log sinks (remote audit shipping)
# ---------------------------------------------------------------------------
class LogSink(Protocol):
    """A bounded, fail-safe destination for already-stored audit entries.

    Sinks are observational — the local hash-chained JSONL is always the
    source of truth. A sink exists to mirror that history to a second store
    (fleet operator's log aggregator, S3 bucket, syslog, etc.) so a single
    node compromise cannot rewrite history.

    Contract:
      * ``submit`` is called from the audit-append thread; it must NEVER raise
        (the caller wraps it in a blanket ``except``), and it must NEVER block
        for more than a few milliseconds. Long-running work belongs in a
        background thread that drains an internal queue.
      * ``drain`` is called by the AuditChain on shutdown to flush pending
        entries; it may block but should be bounded.
      * ``stats`` returns an observability snapshot (counts of submitted /
        delivered / failed entries) without exposing secret material.
    """

    name: str

    def submit(self, entry: Dict[str, Any]) -> bool: ...
    def drain(self, timeout: float = 5.0) -> None: ...
    def stats(self) -> Dict[str, int]: ...


class _BaseThreadedSink:
    """Common machinery for sinks that queue entries and flush them on a
    background thread: bounded queue (drop-oldest on overflow), bounded
    retries, bounded flush interval, thread-safe stats. Subclasses implement
    ``_deliver(batch)`` which must return True on success and False otherwise
    — failures trigger bounded exponential backoff up to ``max_retries``.
    """

    def __init__(self, name: str, *, queue_cap: int = LOG_SINK_QUEUE_CAP,
                 flush_interval: float = LOG_SINK_FLUSH_INTERVAL_S,
                 batch_max: int = LOG_SINK_BATCH_MAX,
                 max_retries: int = LOG_SINK_MAX_RETRIES) -> None:
        self.name = str(name)
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(
            maxsize=max(1, int(queue_cap)))
        self._flush_interval = max(0.05, float(flush_interval))
        self._batch_max = max(1, int(batch_max))
        self._max_retries = max(0, int(max_retries))
        self._stats_lock = threading.Lock()
        self._submitted = 0
        self._delivered = 0
        self._failed = 0
        self._dropped = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = threading.Thread(
            target=self._run, name=f"audit-sink-{name}",
            daemon=True)
        self._thread.start()

    def submit(self, entry: Dict[str, Any]) -> bool:
        """Enqueue an entry; drop oldest on overflow (the chain remains intact
        locally). Never raises."""
        with self._stats_lock:
            self._submitted += 1
        try:
            self._queue.put_nowait(dict(entry))
            return True
        except queue.Full:
            # Drop the oldest to make room — log + count. This is the only
            # place a sink can lose entries; the local JSONL chain still has
            # them.
            with self._stats_lock:
                self._dropped += 1
            try:
                _ = self._queue.get_nowait()
                self._queue.put_nowait(dict(entry))
                return True
            except (queue.Empty, queue.Full):
                return False

    def drain(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            if self._queue.empty():
                return
            time.sleep(0.05)

    def stats(self) -> Dict[str, int]:
        with self._stats_lock:
            return {
                "submitted": self._submitted,
                "delivered": self._delivered,
                "failed": self._failed,
                "dropped": self._dropped,
                "pending": self._queue.qsize(),
            }

    def _deliver(self, batch: List[Dict[str, Any]]) -> bool:
        """Subclasses implement actual transport. Return True on success."""
        raise NotImplementedError

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._flush_once()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("audit sink %s: flush failed: %s",
                               self.name, exc)
            self._stop.wait(self._flush_interval)
        # Final drain on stop
        try:
            self._flush_once()
        except Exception:
            pass

    def _flush_once(self) -> None:
        batch: List[Dict[str, Any]] = []
        try:
            while len(batch) < self._batch_max:
                batch.append(self._queue.get_nowait())
        except queue.Empty:
            pass
        if not batch:
            return
        for attempt in range(self._max_retries + 1):
            try:
                ok = bool(self._deliver(batch))
            except Exception as exc:
                logger.warning("audit sink %s: deliver raised: %s",
                               self.name, exc)
                ok = False
            if ok:
                with self._stats_lock:
                    self._delivered += len(batch)
                return
            if attempt < self._max_retries:
                time.sleep(LOG_SINK_RETRY_BACKOFF_S * (2 ** attempt))
        with self._stats_lock:
            self._failed += len(batch)
        logger.warning("audit sink %s: dropped %d entries after %d retries",
                       self.name, len(batch), self._max_retries)


class HTTPSAuditSink(_BaseThreadedSink):
    """POST every audit entry to a remote HTTPS endpoint as NDJSON.

    The remote endpoint receives the *same* canonical entry the local chain
    stored (sequence, hash, payload, optional HMAC). It is responsible for
    verifying the chain; ShugoCore only mirrors.

    Configuration (env-driven, with explicit overrides for tests):
      * ``url``           - required, must be ``https://...``
      * ``auth_token``    - optional bearer token sent as
                            ``Authorization: Bearer <token>``
      * ``extra_headers`` - optional dict of additional static headers
                            (Authorization and Content-Type are reserved)
      * ``timeout``       - per-request timeout (default 5.0s)
      * ``verify_tls``    - TLS verification (default True); set False ONLY
                            for development against self-signed endpoints
    """

    def __init__(self, url: str, *, auth_token: Optional[str] = None,
                 extra_headers: Optional[Dict[str, str]] = None,
                 timeout: float = LOG_SINK_HTTP_TIMEOUT_S,
                 verify_tls: bool = True, **kwargs: Any) -> None:
        super().__init__(name="https", **kwargs)
        from urllib.parse import urlparse
        parsed = urlparse(str(url))
        if parsed.scheme != "https":
            raise ValueError(
                f"HTTPSAuditSink requires https:// scheme, got {parsed.scheme!r}")
        if not parsed.netloc:
            raise ValueError("HTTPSAuditSink requires a host in the URL")
        self._url = str(url)
        self._auth_token = str(auth_token) if auth_token else None
        self._timeout = max(0.5, float(timeout))
        self._verify_tls = bool(verify_tls)
        self._extra_headers: Dict[str, str] = dict(extra_headers or {})

    def _deliver(self, batch: List[Dict[str, Any]]) -> bool:
        import requests  # local: keep audit.py import-time dep-free
        headers = {"Content-Type": "application/json"}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        for k, v in self._extra_headers.items():
            if k.lower() in ("authorization", "content-type"):
                continue  # protected; caller cannot override
            headers[str(k)] = str(v)
        # One request per batch — a single POST carrying an NDJSON body.
        body = "\n".join(json.dumps(e, sort_keys=True, default=str)
                         for e in batch).encode("utf-8")
        try:
            resp = requests.post(self._url, data=body, headers=headers,
                                 timeout=self._timeout,
                                 verify=self._verify_tls)
        except Exception:
            return False
        return 200 <= int(resp.status_code) < 300


class FileAuditSink(_BaseThreadedSink):
    """Append mirrored entries to a second local file (NDJSON, one per line).

    Useful for operators who want to rsync/copy the file to durable storage
    without running an HTTPS collector. NOT a chain — the second file is a
    flat mirror; the hash chain lives only on the primary ``AuditChain.path``.
    """

    def __init__(self, path: str, **kwargs: Any) -> None:
        super().__init__(name="file", **kwargs)
        self._path = str(path)
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._path)) or ".",
                        exist_ok=True)
        except OSError:
            pass

    def _deliver(self, batch: List[Dict[str, Any]]) -> bool:
        try:
            with open(self._path, "a", encoding="utf-8") as handle:
                for entry in batch:
                    handle.write(json.dumps(entry, sort_keys=True,
                                            default=str) + "\n")
            return True
        except OSError:
            return False


def _entry_hmac(key: str, body: Dict[str, Any]) -> str:
    """HMAC-SHA256 of the canonical body using the chain's operator key.

    Makes the audit chain authenticated, not just tamper-evident: only
    a holder of the HMAC key can legitimately produce signed entries,
    so a rewritten chain on shared storage is detectable.
    """
    return hmac.new(
        key.encode("utf-8"),
        canonical_json(body).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class AuditChain:
    """Append-only, hash-chained audit log persisted as JSONL.

    When ``hmac_key`` is provided, every appended entry also carries an
    HMAC over its canonical body (roadmap item: HMAC-signed audit chains).
    Verification of older unsigned files remains supported; supply the
    same key on :meth:`verify` to authenticate signed entries.

    Sinks (optional): pass one or more ``LogSink`` instances via
    ``sinks=[...]`` to mirror every locally-stored entry to a second store
    (HTTPS endpoint, second local file, etc.). The local chain is always the
    source of truth — sinks are observational and fail-safe.
    """

    def __init__(self, path: str, hmac_key: Optional[str] = None,
                 sinks: Optional[List["LogSink"]] = None) -> None:
        self.path = str(path)
        self.hmac_key = str(hmac_key) if hmac_key else None
        self._lock = threading.Lock()
        self._seq = 0
        self._tail = GENESIS_HASH
        self._sinks: List["LogSink"] = list(sinks or [])
        self._load_existing()

    def add_sink(self, sink: "LogSink") -> None:
        """Register a sink after construction. Safe to call at any time."""
        with self._lock:
            self._sinks.append(sink)

    def sink_stats(self) -> List[Dict[str, Any]]:
        """Snapshot of every configured sink's delivery counters."""
        out: List[Dict[str, Any]] = []
        for sink in self._sinks:
            try:
                stats = sink.stats()
            except Exception as exc:
                stats = {"error": type(exc).__name__}
            out.append({"name": getattr(sink, "name", "unknown"), **stats})
        return out

    def drain_sinks(self, timeout: float = 5.0) -> None:
        """Block (bounded) until every sink has flushed its queue."""
        for sink in self._sinks:
            try:
                sink.drain(timeout=timeout)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("audit sink drain failed: %s", exc)

    # -- internals -----------------------------------------------------------

    def _load_existing(self) -> None:
        """Adopt the tail hash of an existing chain (if any)."""
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        self._seq = int(entry.get("seq", self._seq + 1))
                        self._tail = str(entry.get("hash", self._tail))
                    except (ValueError, AttributeError):
                        logger.warning(f"AuditChain: skipping malformed line in {self.path}")
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.error(f"AuditChain: cannot read {self.path}: {exc}")

    def _make_entry(self, event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = {
            "seq": self._seq + 1,
            "timestamp": _now_iso(),
            "type": sanitize_text(event_type, 64),
            "payload": payload,
            "prev_hash": self._tail,
        }
        entry = dict(body)
        entry["hash"] = hashlib.sha256(
            (body["prev_hash"] + canonical_json(body)).encode("utf-8")
        ).hexdigest()
        if self.hmac_key:
            entry["hmac"] = _entry_hmac(self.hmac_key, entry)
        return entry

    # -- public API ----------------------------------------------------------

    def append(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Append one event; returns the stored (hash-chained) entry.

        Sinks (if any) are dispatched *after* the local append succeeds.
        A sink failure can never block a decision: dispatch is non-blocking
        and bounded, and exceptions are caught and recorded in stats.
        """
        with self._lock:
            entry = self._make_entry(event_type, dict(payload or {}))
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
            self._seq = int(entry["seq"])
            self._tail = str(entry["hash"])
            sinks_snapshot = list(self._sinks)
        # Dispatch to sinks OUTSIDE the lock so a slow sink can't serialize
        # the audit thread. Sinks themselves are fail-safe (their submit
        # never raises); this try/except is belt-and-braces.
        for sink in sinks_snapshot:
            try:
                sink.submit(entry)
            except Exception as exc:
                logger.warning("audit sink %s submit failed: %s",
                               getattr(sink, "name", "?"), exc)
        return entry

    def verify(self, hmac_key: Optional[str] = None) -> Tuple[bool, List[str], int]:
        """Recompute the whole chain. Returns (ok, errors, entries_checked).

        Pass the chain's HMAC key to also authenticate every signed entry.
        """
        return verify_audit_file(self.path, hmac_key=hmac_key)


def verify_audit_file(path: str,
                      hmac_key: Optional[str] = None) -> Tuple[bool, List[str], int]:
    """
    Verify a JSONL audit chain: sequence continuity, hash linkage and
    per-entry hash correctness. Returns (ok, errors, entries_checked).

    When ``hmac_key`` is provided, every entry carrying an ``hmac`` field
    is authenticated against it (entries from older unsigned chains are
    skipped for backward compatibility).
    """
    errors: List[str] = []
    expected_prev = GENESIS_HASH
    expected_seq = 1
    checked = 0
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    errors.append(f"line {line_number}: malformed JSON")
                    continue
                checked += 1
                entry_hash = str(entry.get("hash", ""))
                body = {k: v for k, v in entry.items() if k not in ("hash", "hmac")}
                recomputed = hashlib.sha256(
                    (str(body.get("prev_hash")) + canonical_json(body)).encode("utf-8")
                ).hexdigest()
                if body.get("prev_hash") != expected_prev:
                    errors.append(f"line {line_number}: broken chain linkage")
                if int(body.get("seq", -1)) != expected_seq:
                    errors.append(f"line {line_number}: sequence gap (expected {expected_seq})")
                if entry_hash != recomputed:
                    errors.append(f"line {line_number}: entry hash mismatch (tampered?)")
                if hmac_key:
                    stored_hmac = entry.get("hmac", "")
                    if not stored_hmac:
                        errors.append(
                            f"line {line_number}: missing HMAC (unsigned entry "
                            f"in a key-protected chain)")
                    else:
                        # The HMAC authenticates the whole stored entry
                        # (including its hash field) minus the hmac itself.
                        hmac_body = {k: v for k, v in entry.items()
                                     if k != "hmac"}
                        if _entry_hmac(hmac_key, hmac_body) != stored_hmac:
                            errors.append(
                                f"line {line_number}: HMAC mismatch (unauthorized "
                                f"modification?)")
                expected_prev = entry_hash
                expected_seq += 1
    except FileNotFoundError:
        return False, ["audit file not found"], 0
    except OSError as exc:
        return False, [f"cannot read audit file: {exc}"], 0

    return (not errors), errors, checked


def sinks_from_env(env: Optional[Dict[str, str]] = None) -> List["LogSink"]:
    """Build sinks from environment variables (one-shot helper).

    Recognized variables:
      * ``SHUGOCORE_AUDIT_HTTPS_URL``   - enable HTTPSAuditSink
      * ``SHUGOCORE_AUDIT_HTTPS_TOKEN`` - bearer token for that sink
      * ``SHUGOCORE_AUDIT_HTTPS_VERIFY_TLS`` - "false" disables TLS verify
                                                (development only)
      * ``SHUGOCORE_AUDIT_FILE_PATH``   - enable FileAuditSink

    Returns a list of successfully-constructed sinks (empty on no-op). A
    misconfiguration (e.g. a non-https URL) is logged and the sink is
    skipped — sinks are observational and must never crash the engine.
    """
    env = dict(env if env is not None else os.environ)
    sinks: List["LogSink"] = []
    https_url = env.get("SHUGOCORE_AUDIT_HTTPS_URL", "").strip()
    if https_url:
        try:
            verify_tls = env.get(
                "SHUGOCORE_AUDIT_HTTPS_VERIFY_TLS", "true").lower() != "false"
            sinks.append(HTTPSAuditSink(
                https_url,
                auth_token=env.get("SHUGOCORE_AUDIT_HTTPS_TOKEN"),
                verify_tls=verify_tls,
            ))
        except Exception as exc:
            logger.warning("audit sink env: HTTPS sink disabled: %s", exc)
    file_path = env.get("SHUGOCORE_AUDIT_FILE_PATH", "").strip()
    if file_path:
        try:
            sinks.append(FileAuditSink(file_path))
        except Exception as exc:
            logger.warning("audit sink env: file sink disabled: %s", exc)
    return sinks


def cli_main(argv: Optional[List[str]] = None) -> int:
    """
    CLI entry point: ``python3 audit.py verify <file>`` or the
    ``shugocore-verify-audit`` console script. With ``--hmac-key KEY``
    additionally authenticates every signed entry.
    """
    import sys as _sys
    args = list(argv) if argv is not None else _sys.argv[1:]
    if len(args) not in (2, 4):
        print("usage: shugocore-verify-audit verify <audit_file> [--hmac-key KEY]")
        return 2
    if args[0] != "verify":
        print("usage: shugocore-verify-audit verify <audit_file> [--hmac-key KEY]")
        return 2
    hmac_key = None
    if len(args) == 4:
        if args[2] != "--hmac-key":
            print("usage: shugocore-verify-audit verify <audit_file> [--hmac-key KEY]")
            return 2
        hmac_key = args[3]
    ok, errs, count = verify_audit_file(args[1], hmac_key=hmac_key)
    if ok:
        print(f"AUDIT CHAIN OK - {count} entries verified")
        return 0
    print(f"AUDIT CHAIN BROKEN - {len(errs)} problem(s) across {count} entries:")
    for err in errs[:20]:
        print(f"  - {err}")
    return 1


if __name__ == "__main__":
    sys.exit(cli_main())
