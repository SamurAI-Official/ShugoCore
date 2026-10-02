"""A proposer that keeps failing is not asked every cycle (v1.30.23).

The rule fallback already existed for a model that never proposes an executable action.
What it did not do was stop *calling* that model: every cycle paid the round-trip again,
and the failure counter kept climbing whether or not the model was the reason. These tests
pin the three properties that make the backoff honest -- the model is skipped, the skip is
reported as a skip rather than as garbage, and the streak that caused it does not grow
while we are the ones not asking.
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shugocore_agent import create_agent  # noqa: E402
import decision_engine  # noqa: E402

_NULL_PROPOSAL = '{"action_type": null, "params": {}, "confidence": 0.0}'
_USABLE_PROPOSAL = ('{"action_type": "record_observation", '
                    '"params": {"text": "noticed"}, "confidence": 0.9}')


class ProposalBackoffTestCase(unittest.TestCase):
    """Each agent gets its own data dir, so nothing here touches the repo root."""

    def setUp(self):
        # The agent chdirs into its data dir, so put that back afterwards: a
        # temp dir must not outlive the test as everyone else's working directory.
        self._origin = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(prefix="shugocore_backoff_")
        self.data_dir = self._tmp.name
        self.agents = []

    def tearDown(self):
        for agent in self.agents:
            try:
                agent.cleanup()
            except Exception:
                pass
        try:
            os.chdir(self._origin)
        except OSError:
            pass
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def _agent(self):
        agent = create_agent(device_caps="Exynos-1380",
                             api_url="http://127.0.0.1:11434",
                             data_dir=self.data_dir)
        self.agents.append(agent)
        engine = agent.engine
        # One model, so "was it asked?" is a question with one answer.
        engine.select_models = lambda task: [{"id": "shugocore-local",
                                              "weight": 1.0}]
        return engine

    def _cycles(self, engine, count, output=_NULL_PROPOSAL):
        calls = []
        with mock.patch.object(engine.subconscious, "get_model_output",
                               side_effect=lambda *a, **k: calls.append(1) or output):
            decisions = [engine.make_decision({"type": "t", "content": "x"})
                         for _ in range(count)]
        return calls, decisions

    def test_a_failing_proposer_stops_being_asked(self):
        engine = self._agent()
        calls, _ = self._cycles(engine, 3)
        self.assertEqual(len(calls), 3, "the first three cycles must ask the model")
        calls, decisions = self._cycles(engine, 1)
        self.assertEqual(calls, [], "the fourth cycle must not ask the model again")
        decision = decisions[0]
        self.assertEqual(decision["action_type"], "record_observation")
        self.assertEqual(decision["proposal_source"], "rule_fallback")
        self.assertEqual(decision["params"]["reason"], "proposer_backoff")
        self.assertGreater(decision["params"]["retry_in_secs"], 0)

    def test_a_skip_is_reported_as_a_skip_not_as_model_failure(self):
        """Nothing may read as "the model produced garbage" when it was not asked."""
        engine = self._agent()
        self._cycles(engine, 3)
        self.assertEqual(engine._model_failures, 3)
        _, decisions = self._cycles(engine, 5)
        self.assertEqual(engine._model_failures, 3,
                         "the streak must not grow while we are not asking")
        self.assertEqual(decisions[-1]["params"]["failures"], 3)
        self.assertEqual(decisions[-1]["params"]["reason"], "proposer_backoff")

    def test_the_wait_grows_with_the_streak_and_is_capped(self):
        engine = self._agent()
        self._cycles(engine, 3)
        first = engine._proposer_ready_at - decision_engine.time.monotonic()
        self.assertAlmostEqual(first, decision_engine.PROPOSAL_BACKOFF_BASE_SECS,
                               delta=1.0)
        # Force the window open: another real failure must wait longer, not the same.
        engine._proposer_ready_at = 0.0
        self._cycles(engine, 1)
        second = engine._proposer_ready_at - decision_engine.time.monotonic()
        self.assertAlmostEqual(second, decision_engine.PROPOSAL_BACKOFF_BASE_SECS * 2,
                               delta=1.0)
        self.assertLessEqual(second, decision_engine.PROPOSAL_BACKOFF_MAX_SECS)

    def test_a_usable_proposal_clears_the_backoff(self):
        engine = self._agent()
        self._cycles(engine, 3)
        self.assertGreater(engine._proposer_ready_at, 0.0)
        engine._proposer_ready_at = 0.0            # the window elapsed
        _, decisions = self._cycles(engine, 1, output=_USABLE_PROPOSAL)
        self.assertEqual(decisions[0]["proposal_source"], "shugocore-local")
        self.assertEqual(engine._model_failures, 0)
        self.assertEqual(engine._proposer_ready_at, 0.0)

    # -- a backend that is DOWN is not a model that proposed nothing ------

    def _cycles_raising(self, engine, count, exc=None):
        calls = []

        def boom(*args, **kwargs):
            calls.append(1)
            raise (exc or RuntimeError("backend is loading its weights"))

        with mock.patch.object(engine.subconscious, "get_model_output", side_effect=boom):
            decisions = [engine.make_decision({"type": "t", "content": "x"})
                         for _ in range(count)]
        return calls, decisions

    def test_an_outage_is_not_charged_to_the_model(self):
        """Three refused connections must not read as \"the model proposed nothing\"."""
        engine = self._agent()
        self._cycles_raising(engine, 5)
        self.assertEqual(engine._model_failures, 0,
                         "a backend outage was counted against the model")
        self.assertEqual(engine._proposer_ready_at, 0.0,
                         "a backend outage started the proposer backoff")
        self.assertGreater(engine._backend_failures, 0,
                           "the outage was not tracked at all")

    def test_an_outage_keeps_asking_so_recovery_is_immediate(self):
        """The node must not go on answering with rules after the model came back.

        This is the measured live failure: a cold LM Studio (loading a 27B) answered with an
        error, the node started a 30s+ backoff, and the model-backed claim went rule-backed.
        """
        engine = self._agent()
        calls, decisions = self._cycles_raising(engine, 8)
        self.assertEqual(len(calls), 8, "the model was skipped while it was only down")
        # And the cycle right after the backend answers is model-backed again.
        _, decisions = self._cycles(engine, 1, output=_USABLE_PROPOSAL)
        self.assertEqual(decisions[0]["proposal_source"], "shugocore-local")

    def test_the_first_unreachable_cycles_are_honest_and_the_loop_stays_productive(self):
        """The substitution is unchanged: no action twice (the shell calls that
        BACKEND_FAILURE), then the safe rule action -- so an outage never stalls the loop."""
        engine = self._agent()
        _, decisions = self._cycles_raising(engine, 1)
        self.assertIsNone(decisions[0]["action_type"])
        self.assertIsNone(decisions[0]["proposal_source"])
        _, decisions = self._cycles_raising(engine, 2)      # cycles 2 and 3
        self.assertIsNone(decisions[0]["action_type"])
        self.assertEqual(decisions[1]["action_type"], "record_observation")
        self.assertEqual(decisions[1]["proposal_source"], "rule_fallback")
        self.assertEqual(decisions[1]["params"]["reason"], "backend_unavailable")

    def test_an_answer_clears_the_outage_streak(self):
        engine = self._agent()
        self._cycles_raising(engine, 3)
        self.assertGreater(engine._backend_failures, 0)
        self._cycles(engine, 1, output=_USABLE_PROPOSAL)
        self.assertEqual(engine._backend_failures, 0)

    def test_a_model_that_answers_but_proposes_nothing_still_counts(self):
        """The distinction is \"reached\" vs \"answered\", not an amnesty for bad output."""
        engine = self._agent()
        self._cycles(engine, 3, output=_NULL_PROPOSAL)
        self.assertEqual(engine._model_failures, 3)
        self.assertGreater(engine._proposer_ready_at, 0.0)


class ModelWarmUpTestCase(unittest.TestCase):
    """The model scenario establishes the precondition it measures, bounded and reported."""

    def _session(self):
        spec = importlib.util.spec_from_file_location(
            "agency_session_under_test",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "scripts", "agency_session.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_a_dead_endpoint_is_reported_as_not_warm_rather_than_waited_on(self):
        report = self._session().warm_model("http://127.0.0.1:1", "x", budget=0.0)
        self.assertTrue(report.startswith("NOT warm"), report)
        self.assertIn("running anyway", report)

    def test_an_unloadable_model_does_not_make_the_endpoint_look_cold(self):
        """The measured bug: the first listed model asked for 64.74 GB and answered 400.

        The listing order is not stable, so warming only ``models[0]`` reported the endpoint
        cold while a model that works sat next to it.
        """
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = _json.dumps({"data": [{"id": "unloadable-27b"},
                                             {"id": "good-27b"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                request = _json.loads(self.rfile.read(length) or b"{}")
                name = str(request.get("model") or "")
                if name == "good-27b":
                    body, code = b'{"choices": [{"message": {"content": "ok"}}]}', 200
                else:
                    body = _json.dumps(
                        {"error": {"message": "requires approximately 64.74 GB"}}).encode()
                    code = 400
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            report = self._session().warm_model(
                f"http://127.0.0.1:{server.server_address[1]}", "", budget=5.0)
        finally:
            server.shutdown()
            thread.join(timeout=5)
        self.assertTrue(report.startswith("warm"), report)
        self.assertIn("good-27b", report)

    def test_the_scenario_announces_its_warm_up_before_it_measures(self):
        source = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "agency_session.py"), encoding="utf-8").read()
        self.assertIn("warming the model endpoint", source)
        self.assertIn("--warm-seconds", source)
        # Announced before the terminal it is warming for is spawned, so the transcript reads
        # in the order things happened. Scoped to the model scenario: other scenarios spawn
        # subprocesses earlier in the file.
        start = source.index("def scenario_model")
        self.assertLess(source.index("warming the model endpoint", start),
                        source.index("subprocess.run(command", start))


if __name__ == "__main__":
    unittest.main()