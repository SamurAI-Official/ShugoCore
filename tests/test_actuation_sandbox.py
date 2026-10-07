"""The loopback actuation sandbox must prove containment, not assert it.

The sandbox drives the real gate and the real execution layer against a real
loopback service, so these tests check the two halves that matter: what the
engine reported, and whether anything actually arrived on the wire.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import actuation_sandbox as sb  # noqa: E402
import claim_matrix  # noqa: E402


class TargetTestCase(unittest.TestCase):
    def test_target_records_what_arrives(self):
        import requests
        target = sb.LoopbackActuationTarget(tls=False)
        try:
            response = requests.post(target.url, json={"probe": 1}, timeout=5)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(target.count, 1)
            self.assertEqual(target.last()["body"], {"probe": 1})
            self.assertEqual(target.last()["method"], "POST")
        finally:
            target.stop()

    def test_failure_path_reports_500(self):
        import requests
        target = sb.LoopbackActuationTarget(tls=False)
        try:
            response = requests.post(target.url.replace("/actuate", "/fail"),
                                     json={}, timeout=5)
            self.assertEqual(response.status_code, 500)
            self.assertEqual(target.count, 1)
        finally:
            target.stop()

    def test_tls_target_is_trusted_through_its_own_ca(self):
        """Verification stays on: the sandbox's CA is what makes it valid."""
        import requests
        with tempfile.TemporaryDirectory() as tmp:
            target = sb.LoopbackActuationTarget(tls=True, cert_dir=tmp)
            saved = os.environ.get("REQUESTS_CA_BUNDLE")
            os.environ["REQUESTS_CA_BUNDLE"] = target.ca_path
            try:
                self.assertTrue(target.url.startswith("https://"))
                response = requests.post(target.url, json={"probe": "tls"},
                                         timeout=5)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(target.count, 1)
            finally:
                if saved is None:
                    os.environ.pop("REQUESTS_CA_BUNDLE", None)
                else:
                    os.environ["REQUESTS_CA_BUNDLE"] = saved
                target.stop()

    def test_the_certificate_chain_names_its_issuer(self):
        """The CA publishes a key id, and the leaf names it back.

        Not decoration: OpenSSL 3.x refuses a leaf whose issuer has a key id that the
        leaf does not reference -- "Missing Authority Key Identifier" -- so this
        sandbox verified on Python 3.9-3.12 and failed on 3.13. Asserting the profile
        directly is what makes it checkable here: the local OpenSSL (1.1.1) is lenient
        enough to accept the certificate either way.
        """
        from cryptography import x509

        def names(cert):
            return {extension.oid._name for extension in cert.extensions}

        with tempfile.TemporaryDirectory() as tmp:
            target = sb.LoopbackActuationTarget(tls=True, cert_dir=tmp)
            try:
                ca = x509.load_pem_x509_certificate(Path(target.ca_path).read_bytes())
                leaf = x509.load_pem_x509_certificate(
                    Path(tmp, "sandbox_server.pem").read_bytes())
            finally:
                target.stop()
        self.assertIn("basicConstraints", names(ca))
        self.assertIn("subjectKeyIdentifier", names(ca))
        self.assertIn("authorityKeyIdentifier", names(leaf))
        self.assertIn("subjectAltName", names(leaf))
        self.assertIn("extendedKeyUsage", names(leaf))


class WorkspaceTestCase(unittest.TestCase):
    def test_workspace_restores_the_working_directory(self):
        before = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            with sb.sandbox_workspace(tmp) as inside:
                self.assertEqual(os.path.realpath(os.getcwd()),
                                 os.path.realpath(tmp))
                self.assertEqual(os.path.realpath(inside), os.path.realpath(tmp))
        self.assertEqual(os.getcwd(), before)


class ScenarioTestCase(unittest.TestCase):
    """Three scenarios end to end: the allowed one and two refusals."""

    @classmethod
    def setUpClass(cls):
        # A *relative* path on purpose: the workspace changes the process cwd, so
        # this is the case where a relative CA path / chain path would break.
        cls.tmp = os.path.join("runtime", "test_actuation_sandbox")
        cls.rows = {row["name"]: row for row in sb.run_scenarios(
            cls.tmp,
            only={"loopback_allowlisted_actuates", "no_consent_refused",
                  "loopback_plain_http_refused_by_scheme"})}

    def test_the_allowed_actuation_reaches_the_wire(self):
        row = self.rows["loopback_allowlisted_actuates"]
        self.assertEqual(row["observed"], "allowed")
        self.assertEqual(row["delta"], 1)       # the only scenario that may
        self.assertEqual(row["http_status"], 200)
        self.assertGreaterEqual(row["chain_executions"], 1)   # and it is audited
        self.assertTrue(row["ok"])

    def test_missing_consent_never_reaches_the_wire(self):
        row = self.rows["no_consent_refused"]
        self.assertEqual(row["observed"], "refused")
        self.assertEqual(row["delta"], 0)
        self.assertIn("consent_required", row["reason"])
        self.assertTrue(row["ok"])

    def test_plain_http_is_refused_by_the_scheme_rule(self):
        row = self.rows["loopback_plain_http_refused_by_scheme"]
        self.assertEqual(row["observed"], "refused")
        self.assertEqual(row["delta"], 0)
        self.assertIn("http", row["reason"])
        self.assertTrue(row["ok"])

    def test_render_flags_a_mismatch(self):
        rows = [{"name": "x", "expect": "allowed", "observed": "refused",
                 "delta": 0, "delta_allowed": 1, "reason": "nope", "ok": False}]
        text = sb.render(rows)
        self.assertIn("FAIL", text)
        self.assertIn("0/1 scenarios behaved as required", text)


class WorldTranscriptTestCase(unittest.TestCase):
    """The transcript `world.sandbox` is judged on, built from the run's rows.

    Judged by the real consumer (`claim_matrix.world_engagement`), because a
    transcript that its own parser reads as engagement is the only useful thing
    to assert -- and because the failure mode to guard against is a transcript
    that reads as engagement when the action never happened.
    """

    ALLOWED = {"name": sb.WORLD_SCENARIO,
               "why": "operator allowlist + consent + approval",
               "expect": "allowed", "observed": "allowed", "delta": 1,
               "http_status": 200, "chain_executions": 33, "ok": True}

    def _text(self, rows):
        return "\n".join(sb.world_transcript(rows))

    def test_an_allowlisted_run_is_proven_by_the_real_parser(self):
        ok, detail = claim_matrix.world_engagement(self._text([self.ALLOWED]))
        self.assertTrue(ok, detail)

    def test_the_reply_carries_what_the_target_answered(self):
        """The status and the chain count come from the row, not from a template."""
        text = self._text([self.ALLOWED])
        self.assertIn("HTTP 200", text)
        self.assertIn("chain=33", text)
        self.assertIn("(gated)", text)

    def test_a_refusal_is_not_dressed_up_as_an_answer(self):
        refused = dict(self.ALLOWED, observed="refused", delta=0, http_status=None,
                       chain_executions=0, reason="consent_required")
        text = self._text([refused])
        ok, detail = claim_matrix.world_engagement(text)
        self.assertIs(ok, False,
                      "a scenario that was refused must not read as engagement")
        self.assertNotIn("[REPLY", text)
        self.assertNotIn("(gated)", text)

    def test_a_run_that_omitted_the_scenario_reports_unproven(self):
        """Not a session: the row must stay unproven, not be called contradicted."""
        text = self._text([])
        self.assertIn("did not include", text)
        self.assertNotIn("[WORLD", text)
        ok, detail = claim_matrix.world_engagement(text)
        self.assertIsNone(ok, detail)


if __name__ == "__main__":
    unittest.main()
