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
import json
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from security import canonical_hash, canonical_json, sanitize_text

GENESIS_HASH = "0" * 64


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AuditChain:
    """Append-only, hash-chained audit log persisted as JSONL."""

    def __init__(self, path: str):
        self.path = str(path)
        self._lock = threading.Lock()
        self._seq = 0
        self._tail = GENESIS_HASH
        self._load_existing()

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
        return entry

    # -- public API ----------------------------------------------------------

    def append(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Append one event; returns the stored (hash-chained) entry."""
        with self._lock:
            entry = self._make_entry(event_type, dict(payload or {}))
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
            self._seq = int(entry["seq"])
            self._tail = str(entry["hash"])
            return entry

    def verify(self) -> Tuple[bool, List[str], int]:
        """Recompute the whole chain. Returns (ok, errors, entries_checked)."""
        return verify_audit_file(self.path)


def verify_audit_file(path: str) -> Tuple[bool, List[str], int]:
    """
    Verify a JSONL audit chain: sequence continuity, hash linkage and
    per-entry hash correctness. Returns (ok, errors, entries_checked).
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
                body = {k: v for k, v in entry.items() if k != "hash"}
                recomputed = hashlib.sha256(
                    (str(body.get("prev_hash")) + canonical_json(body)).encode("utf-8")
                ).hexdigest()
                if body.get("prev_hash") != expected_prev:
                    errors.append(f"line {line_number}: broken chain linkage")
                if int(body.get("seq", -1)) != expected_seq:
                    errors.append(f"line {line_number}: sequence gap (expected {expected_seq})")
                if entry_hash != recomputed:
                    errors.append(f"line {line_number}: entry hash mismatch (tampered?)")
                expected_prev = entry_hash
                expected_seq += 1
    except FileNotFoundError:
        return False, ["audit file not found"], 0
    except OSError as exc:
        return False, [f"cannot read audit file: {exc}"], 0

    return (not errors), errors, checked


def cli_main(argv: Optional[List[str]] = None) -> int:
    """
    CLI entry point: ``python3 audit.py verify <file>`` or the
    ``shugocore-verify-audit`` console script.
    """
    import sys as _sys
    args = list(argv) if argv is not None else _sys.argv[1:]
    if len(args) != 2 or args[0] != "verify":
        print("usage: shugocore-verify-audit verify <audit_file>")
        return 2
    ok, errs, count = verify_audit_file(args[1])
    if ok:
        print(f"AUDIT CHAIN OK - {count} entries verified")
        return 0
    print(f"AUDIT CHAIN BROKEN - {len(errs)} problem(s) across {count} entries:")
    for err in errs[:20]:
        print(f"  - {err}")
    return 1


if __name__ == "__main__":
    sys.exit(cli_main())
