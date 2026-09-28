"""The capability-retention matrix must flag what is gone, not what is claimed.

The classifier is fed fixture listings here, including the two failures that
matter operationally: a node whose data dir is empty (a reinstall, not an
upgrade) and a node holding the wrong mesh secret.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capability_matrix as cm  # noqa: E402

LISTING = """total 120912
drwxrwxrwx 4 u0_a371 u0_a371      3452 2026-09-26 00:59 .
drwx------ 6 u0_a371 u0_a371      3452 2026-09-25 22:14 ..
drwx------ 2 u0_a371 u0_a371      3452 2026-09-26 01:46 artifacts
-rw------- 1 u0_a371 u0_a371 101837098 2026-09-26 00:59 audit_chain.jsonl
-rw------- 1 u0_a371 u0_a371    267740 2026-09-26 00:59 decision_engine.log
-rw------- 1 u0_a371 u0_a371      1313 2026-09-26 00:59 episodic_journal.jsonl
-rw-rw-rw- 1 u0_a371 u0_a371        32 2026-09-24 23:42 mesh_token.txt
-rw-rw-rw- 1 u0_a371 u0_a371       105 2026-09-24 23:43 mesh_peers.json
drwx------ 2 u0_a371 u0_a371      3452 2026-09-25 22:14 models
-rw------- 1 u0_a371 u0_a371       45082 2026-09-13 17:21 nrr_upscaler_v0.1.onnx
-rw------- 1 u0_a371 u0_a371        24 2026-09-26 00:55 profileInstalled
-rw------- 1 u0_a371 u0_a371      5545 2026-09-20 16:01 personality_model.json
-rw------- 1 u0_a371 u0_a371  21479424 2026-09-26 00:59 semantic_memory.db
drwx------ 2 u0_a371 u0_a371      3452 2026-09-26 01:46 shared
-rw------- 1 u0_a371 u0_a371         2 2026-09-20 16:39 timers.json
lrwxrwxrwx 1 u0_a371 u0_a371        11 2026-09-26 02:10 shortcut -> target
-rw------- 1 u0_a371 u0_a371      1809 2026-09-20 16:58 user_facts.json
"""


def verdicts(result):
    return {row["key"]: row["verdict"] for row in result["rows"]}


class ListingParseTestCase(unittest.TestCase):
    def test_parses_files_dirs_sizes_and_skips_noise(self):
        files = cm.parse_android_listing(LISTING)
        self.assertEqual(files["semantic_memory.db"][0], 21479424)
        self.assertFalse(files["semantic_memory.db"][1])
        self.assertTrue(files["models"][1])              # a directory
        self.assertTrue(files["artifacts"][1])
        self.assertNotIn("total", files)                 # the header line
        self.assertNotIn(".", files)
        self.assertNotIn("shortcut", files)              # a symlink is not a claim

    def test_adb_chatter_yields_no_files(self):
        self.assertEqual(cm.parse_android_listing("/system/bin/sh: not found"),
                         {})


class EvaluateTestCase(unittest.TestCase):
    def setUp(self):
        self.files = cm.parse_android_listing(LISTING)

    def test_a_healthy_phone_is_all_ok(self):
        result = cm.evaluate(self.files, platform="android",
                             expected_token="fleet-secret",
                             state={"token": "fleet-secret"})
        self.assertEqual(cm.summarize(result)["failing"], [])
        self.assertEqual(set(verdicts(result).values()), {"ok"})

    def test_a_wiped_node_reports_every_claim_missing(self):
        """What a reinstall looks like -- the upgrade path must not do this."""
        result = cm.evaluate({}, platform="android")
        summary = cm.summarize(result)
        self.assertIn("tier2_memory", summary["failing"])
        self.assertIn("timers", summary["failing"])
        self.assertEqual(verdicts(result)["tier2_memory"], "missing")

    def test_zero_byte_state_is_a_failure_not_a_pass(self):
        files = dict(self.files)
        files["timers.json"] = [0, False]
        result = cm.evaluate(files, platform="android")
        self.assertEqual(verdicts(result)["timers"], "empty")
        self.assertIn("timers", cm.summarize(result)["failing"])

    def test_a_foreign_mesh_secret_is_a_mismatch(self):
        result = cm.evaluate(self.files, platform="android",
                             expected_token="fleet-secret",
                             state={"token": "someone-elses-key"})
        self.assertEqual(verdicts(result)["mesh_identity"], "mismatch")
        self.assertIn("mesh_identity", cm.summarize(result)["failing"])

    def test_host_has_no_device_only_claims(self):
        result = cm.evaluate({"semantic_memory.db": [1024, False]},
                             platform="host")
        rows = verdicts(result)
        self.assertEqual(rows["profile"], "n/a")
        self.assertEqual(rows["mesh_peers"], "n/a")
        self.assertEqual(rows["models"], "n/a")
        self.assertEqual(rows["tier2_memory"], "ok")
        # A host only creates timers/facts when it uses them, so their absence
        # there is informational -- but on a device it is a real loss.
        self.assertEqual(rows["timers"], "not-created")
        self.assertNotIn("timers", cm.summarize(result)["failing"])
        device = cm.evaluate({}, platform="android")
        self.assertEqual(verdicts(device)["timers"], "missing")
        self.assertIn("timers", cm.summarize(device)["failing"])

    def test_render_shows_a_column_per_node_and_the_gaps(self):
        good = cm.evaluate(self.files, platform="android")
        good["name"] = "phone"
        wiped = cm.evaluate({}, platform="android")
        wiped["name"] = "reinstalled"
        text = cm.render([good, wiped])
        self.assertIn("Tier 2 semantic memory", text)
        self.assertIn("phone", text)
        self.assertIn("reinstalled", text)
        self.assertIn("needs attention: tier2_memory", text)


class HostScanTestCase(unittest.TestCase):
    def test_phone_probe_uses_an_absolute_path(self):
        """run-as has no chdir on Android 16: relative paths read as 'missing'."""
        args = []

        def _fake_adb(adb, serial, command):
            args.append(command)
            return ""

        original = cm._adb_run
        cm._adb_run = _fake_adb
        try:
            cm.probe_phone("A16", "serial", "adb", "com.samurai.shugocore")
        finally:
            cm._adb_run = original
        listing = [a for a in args if "ls -l" in a]
        self.assertTrue(listing, "the probe never listed the data dir")
        self.assertIn("/data/user/0/com.samurai.shugocore/files", listing[0])
        self.assertIn("/data/user/0/com.samurai.shugocore/files/mesh_token.txt",
                      " ".join(args))

    def test_probe_host_reads_a_real_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("semantic_memory.db", "audit_chain.jsonl",
                         "timers.json"):
                with open(os.path.join(tmp, name), "wb") as handle:
                    handle.write(b"x" * 16)
            os.makedirs(os.path.join(tmp, "shared"), exist_ok=True)
            os.makedirs(os.path.join(tmp, "artifacts"), exist_ok=True)
            with open(os.path.join(tmp, "mesh_token.txt"), "w",
                      encoding="utf-8") as handle:
                handle.write("fleet-secret")
            result = cm.probe_host("hub", tmp, expected_token="fleet-secret",
                                   events=("restart",))
            rows = verdicts(result)
            self.assertEqual(rows["tier2_memory"], "ok")
            self.assertEqual(rows["mesh_identity"], "ok")
            self.assertEqual(rows["shared"], "ok")
            self.assertEqual(result["events"], ["restart"])
            self.assertNotIn("note", result)


class BaselineTestCase(unittest.TestCase):
    """A matrix says what is true now; a baseline says what changed."""

    def _healthy(self, name="phone"):
        result = cm.evaluate(cm.parse_android_listing(LISTING), platform="android",
                             expected_token="fleet-secret",
                             state={"token": "fleet-secret"})
        result["name"] = name
        return result

    def test_a_run_compared_with_its_own_baseline_shows_no_change(self):
        # Through JSON, the way a baseline actually reaches a later run.
        baseline = json.loads(json.dumps(cm.snapshot([self._healthy()],
                                                     label="after 1.30.22")))
        report = cm.diff(baseline, [self._healthy()])
        self.assertEqual(report["regressed"], [])
        self.assertIn("no change", cm.render_diff(report, baseline))

    def test_an_upgrade_that_wipes_memory_is_a_regression(self):
        baseline = cm.snapshot([self._healthy()])
        wiped = cm.evaluate({}, platform="android")
        wiped["name"] = "phone"
        report = cm.diff(baseline, [wiped])
        self.assertIn("tier2_memory", {e["key"] for e in report["regressed"]})
        self.assertIn("phone:tier2_memory ok->missing",
                      cm.render_diff(report, baseline))

    def test_a_pre_existing_failure_is_not_blamed_on_this_run(self):
        """A baseline taken from a broken node must not make the next fix look bad."""
        already = cm.evaluate({}, platform="android")
        already["name"] = "phone"
        report = cm.diff(cm.snapshot([already]), [self._healthy("phone")])
        self.assertEqual(report["regressed"], [])
        self.assertTrue(report["recovered"])
        self.assertIn("recovered", cm.render_diff(report))

    def test_a_node_the_baseline_never_saw_is_unknown_not_damaged(self):
        baseline = cm.snapshot([self._healthy("old-phone")])
        report = cm.diff(baseline, [self._healthy("new-phone")])
        self.assertEqual(report["regressed"], [])
        self.assertEqual(report["unknown_nodes"], ["new-phone"])
        self.assertIn("no baseline entry for: new-phone", cm.render_diff(report))

    def test_a_baseline_that_cannot_be_read_is_reported_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "baseline.json")
            quiet = io.StringIO()
            with contextlib.redirect_stdout(quiet):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("{not json")
                self.assertIsNone(cm.load_baseline(path))
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("[]")
                self.assertIsNone(cm.load_baseline(path))
                self.assertIsNone(cm.load_baseline(os.path.join(tmp, "gone.json")))
            self.assertIn("unreadable", quiet.getvalue())


class BaselineCliTestCase(unittest.TestCase):
    """The comparison has to work as a gate, so its exit code matters."""

    NAMES = ("semantic_memory.db", "audit_chain.jsonl", "decision_engine.log",
             "episodic_journal.jsonl", "timers.json", "user_facts.json",
             "personality_model.json", "mesh_token.txt")

    def _host_dir(self, path):
        for name in self.NAMES:
            with open(os.path.join(path, name), "wb") as handle:
                handle.write(b"x" * 32)
        for name in ("shared", "artifacts"):
            os.makedirs(os.path.join(path, name), exist_ok=True)
        return path

    def test_saving_then_comparing_a_baseline_gates_on_a_regression(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._host_dir(tmp)
            baseline_path = os.path.join(tmp, "baseline.json")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cm.main(["--host-dir", tmp, "--host-name", "hub",
                                "--save-baseline", baseline_path,
                                "--baseline-label", "release 1.30.22"])
            self.assertEqual(code, 0, out.getvalue())
            with open(baseline_path, encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual(saved["label"], "release 1.30.22")
            self.assertEqual(saved["nodes"]["hub"]["verdicts"]["tier2_memory"], "ok")

            os.remove(os.path.join(tmp, "semantic_memory.db"))   # the upgrade
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cm.main(["--host-dir", tmp, "--host-name", "hub",
                                "--baseline", baseline_path])
            self.assertEqual(code, 1, out.getvalue())
            self.assertIn("hub:tier2_memory ok->missing", out.getvalue())

            self._host_dir(tmp)                                   # put it back
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cm.main(["--host-dir", tmp, "--host-name", "hub",
                                "--baseline", baseline_path])
            self.assertEqual(code, 0, out.getvalue())
            self.assertIn("no change against the baseline", out.getvalue())

    def test_a_missing_baseline_file_fails_rather_than_passing_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._host_dir(tmp)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cm.main(["--host-dir", tmp, "--baseline",
                                os.path.join(tmp, "never-saved.json")])
            self.assertEqual(code, 1)
            self.assertIn("unreadable", out.getvalue())


if __name__ == "__main__":
    unittest.main()
