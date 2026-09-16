"""Tests for the audit log: tamper-evident chain + remote sink shipping."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audit import (
    AuditChain,
    FileAuditSink,
    HTTPSAuditSink,
    sinks_from_env,
    verify_audit_file,
)


class _RecordingSink:
    """In-memory LogSink for tests."""

    def __init__(self, name="test", fail=False):
        self.name = name
        self.received = []
        self.submitted = 0
        self.failed = 0
        self._fail = fail
        self._lock = threading.Lock()

    def submit(self, entry):
        with self._lock:
            self.submitted += 1
        if self._fail:
            with self._lock:
                self.failed += 1
            return False
        with self._lock:
            self.received.append(dict(entry))
        return True

    def drain(self, timeout=5.0):
        return None

    def stats(self):
        with self._lock:
            return {"submitted": self.submitted, "received": len(self.received),
                    "failed": self.failed}


class LocalChainTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shugocore_audit_")
        self.path = os.path.join(self.tmp, "chain.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_append_writes_and_chains(self):
        chain = AuditChain(self.path)
        first = chain.append("decision", {"x": 1})
        second = chain.append("execution", {"y": 2})
        self.assertEqual(first["seq"], 1)
        self.assertEqual(second["seq"], 2)
        self.assertEqual(second["prev_hash"], first["hash"])
        ok, errs, n = verify_audit_file(self.path)
        self.assertTrue(ok, errs)
        self.assertEqual(n, 2)


class SinksTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shugocore_sink_")
        self.path = os.path.join(self.tmp, "chain.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_local_chain_succeeds_when_sink_raises(self):
        class BoomSink:
            name = "boom"
            def submit(self, entry):
                raise RuntimeError("intentional sink failure")
            def drain(self, timeout=5.0):
                pass
            def stats(self):
                return {"raised": 1}

        chain = AuditChain(self.path, sinks=[BoomSink()])
        entry = chain.append("decision", {"x": 1})
        self.assertEqual(entry["seq"], 1)
        ok, errs, n = verify_audit_file(self.path)
        self.assertTrue(ok, errs)
        self.assertEqual(n, 1)

    def test_recording_sink_receives_entries(self):
        sink = _RecordingSink("record")
        chain = AuditChain(self.path, sinks=[sink])
        chain.append("a", {"k": 1})
        chain.append("b", {"k": 2})
        chain.drain_sinks(timeout=2.0)
        time.sleep(0.05)
        self.assertEqual(sink.submitted, 2)
        self.assertEqual(len(sink.received), 2)
        self.assertEqual(sink.received[0]["type"], "a")
        self.assertEqual(sink.received[1]["type"], "b")

    def test_add_sink_after_construction(self):
        chain = AuditChain(self.path)
        chain.append("pre", {})
        sink = _RecordingSink("late")
        chain.add_sink(sink)
        chain.append("post", {})
        chain.drain_sinks(timeout=2.0)
        time.sleep(0.05)
        self.assertEqual(sink.submitted, 1)
        self.assertEqual(sink.received[0]["type"], "post")

    def test_sink_stats_returns_observability(self):
        sink = _RecordingSink("obs")
        chain = AuditChain(self.path, sinks=[sink])
        chain.append("a", {})
        chain.drain_sinks(timeout=2.0)
        stats = chain.sink_stats()
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["name"], "obs")
        self.assertEqual(stats[0]["submitted"], 1)

    def test_multiple_sinks_all_receive(self):
        s1, s2 = _RecordingSink("s1"), _RecordingSink("s2")
        chain = AuditChain(self.path, sinks=[s1, s2])
        chain.append("x", {})
        chain.drain_sinks(timeout=2.0)
        time.sleep(0.05)
        self.assertEqual(s1.submitted, 1)
        self.assertEqual(s2.submitted, 1)


class HTTPSAuditSinkTestCase(unittest.TestCase):
    def test_requires_https_scheme(self):
        with self.assertRaises(ValueError):
            HTTPSAuditSink("http://example.com/audit")
        with self.assertRaises(ValueError):
            HTTPSAuditSink("ftp://example.com/audit")
        with self.assertRaises(ValueError):
            HTTPSAuditSink("https://")

    def test_successful_post_marks_delivered(self):
        from unittest import mock
        sink = HTTPSAuditSink("https://example.com/audit",
                              auth_token="t0k3n",
                              flush_interval=0.05, batch_max=1,
                              max_retries=0)
        fake_response = mock.Mock(status_code=204)
        with mock.patch("requests.post", return_value=fake_response) as post:
            sink.submit({"seq": 1, "hash": "x", "payload": {"k": "v"}})
            for _ in range(60):
                if sink.stats()["delivered"] > 0:
                    break
                time.sleep(0.05)
        self.assertEqual(sink.stats()["delivered"], 1)
        self.assertEqual(post.call_count, 1)
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer t0k3n")
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
        sink.drain(timeout=2.0)

    def test_failed_post_does_not_block_submit(self):
        from unittest import mock
        sink = HTTPSAuditSink("https://example.com/audit",
                              flush_interval=0.05, batch_max=1,
                              max_retries=0)
        with mock.patch("requests.post", side_effect=RuntimeError("net")):
            ok = sink.submit({"seq": 1, "hash": "x"})
            self.assertTrue(ok)
            for _ in range(60):
                if sink.stats()["failed"] > 0:
                    break
                time.sleep(0.05)
        self.assertGreaterEqual(sink.stats()["failed"], 1)
        sink.drain(timeout=2.0)

    def test_reserved_headers_cannot_be_overridden(self):
        from unittest import mock as _mock
        sink = HTTPSAuditSink("https://example.com/audit",
                              extra_headers={"Authorization": "Bearer evil",
                                             "Content-Type": "text/plain",
                                             "X-Trace": "abc"},
                              flush_interval=0.05, batch_max=1,
                              max_retries=0)
        fake_response = _mock.Mock(status_code=200)
        with _mock.patch("requests.post", return_value=fake_response) as post:
            sink.submit({"seq": 1, "hash": "x"})
            for _ in range(60):
                if sink.stats()["delivered"] > 0:
                    break
                time.sleep(0.05)
            hdrs = post.call_args.kwargs["headers"]
        # Content-Type must be JSON (caller cannot override to text/plain).
        self.assertEqual(hdrs["Content-Type"], "application/json")
        # No Authorization header is set when no auth_token is supplied
        # AND the caller tried to inject one (which is dropped silently).
        self.assertNotIn("Bearer evil", hdrs.get("Authorization", ""))
        # Non-reserved headers pass through unchanged.
        self.assertEqual(hdrs["X-Trace"], "abc")
        sink.drain(timeout=2.0)


class FileAuditSinkTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shugocore_filesink_")
        self.path = os.path.join(self.tmp, "mirror.jsonl")
        self.chain_path = os.path.join(self.tmp, "chain.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mirrors_entries_to_second_file(self):
        sink = FileAuditSink(self.path, flush_interval=0.05, batch_max=2,
                             max_retries=0)
        chain = AuditChain(self.chain_path, sinks=[sink])
        chain.append("a", {"k": 1})
        chain.append("b", {"k": 2})
        chain.drain_sinks(timeout=2.0)
        for _ in range(60):
            if sink.stats()["delivered"] > 0 and os.path.exists(self.path):
                break
            time.sleep(0.05)
        with open(self.path) as fh:
            mirrored = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(mirrored), 2)
        self.assertEqual(mirrored[0]["type"], "a")
        self.assertEqual(mirrored[1]["type"], "b")
        sink.drain(timeout=2.0)


class SinksFromEnvTestCase(unittest.TestCase):
    def test_empty_env_returns_empty_list(self):
        self.assertEqual(sinks_from_env(env={}), [])

    def test_https_url_builds_sink(self):
        sinks = sinks_from_env(env={
            "SHUGOCORE_AUDIT_HTTPS_URL": "https://example.com/audit",
            "SHUGOCORE_AUDIT_HTTPS_TOKEN": "t0k",
        })
        self.assertEqual(len(sinks), 1)
        self.assertIsInstance(sinks[0], HTTPSAuditSink)
        self.assertEqual(sinks[0]._auth_token, "t0k")

    def test_invalid_https_url_is_skipped(self):
        sinks = sinks_from_env(env={
            "SHUGOCORE_AUDIT_HTTPS_URL": "http://not-https.example.com",
        })
        self.assertEqual(sinks, [])

    def test_file_path_builds_sink(self):
        sinks = sinks_from_env(env={
            "SHUGOCORE_AUDIT_FILE_PATH": "/tmp/shugocore_audit_mirror.jsonl",
        })
        self.assertEqual(len(sinks), 1)
        self.assertIsInstance(sinks[0], FileAuditSink)

    def test_both_env_keys_build_both(self):
        sinks = sinks_from_env(env={
            "SHUGOCORE_AUDIT_HTTPS_URL": "https://example.com/audit",
            "SHUGOCORE_AUDIT_FILE_PATH": "/tmp/shugocore_audit_mirror.jsonl",
        })
        self.assertEqual(len(sinks), 2)


if __name__ == "__main__":
    unittest.main()
