"""The interactive turn pipeline: a real node answers the operator in front of it.

Three defects, all found by *driving the real loop against a real model* rather
than by reading it, and all invisible to a green suite -- which is why they are
pinned here.

1. ``create_agent(data_dir="relative/path")`` booted a node with no engine.
   ``__init__`` stored the path verbatim, then ``_bootstrap`` chdir'd into it,
   so every state path was resolved a second time against the new cwd
   (``runtime/node/runtime/node/...``), the decision log could not be created
   and ``DecisionEngine`` construction raised ``FileNotFoundError``. The node
   reported itself "ready" and could never decide anything.

2. A conversational turn talked to the wrong endpoint. The engine's registry
   is what the operator re-points when they choose a backend, but
   ``_make_conversational_decision`` asked ``_backend_for({"id": name})`` -- a
   synthesized dict with no ``"backend"`` key -- so ``_backend_for`` returned
   ``None`` and the call fell through to the subconscious's *global* backend.
   Measured live: LM Studio on :1234 had a working model, the terminal was
   pointed at it, and every reply still failed against Ollama on :11434.

3. A node that had lost the mesh lease went mute to the operator sitting at it.
   The follower refusal is deliberate and correct for ambient speech, but it
   also swallowed the reply to a turn the node had just been handed -- 5 of 5
   typed turns answered with no words at all.
"""
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shugocore_agent import (  # noqa: E402
    DELEGATE_TOPIC,
    TURN_RESULT_TOPIC,
    TURN_TOPIC,
    AndroidAgent,
    create_agent,
)
import shugocore_agent  # noqa: E402  (patched: the forwarded-turn wait)


class _AgentFixture(unittest.TestCase):
    """One temp data dir per test, restored cwd, every agent cleaned up."""

    def setUp(self):
        self._origin = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(prefix="shugocore_interactive_")
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

    def _agent(self, **kwargs):
        kwargs.setdefault("data_dir", self._tmp.name)
        agent = create_agent(device_caps="Desktop", **kwargs)
        self.agents.append(agent)
        return agent


class RelativeDataDirTestCase(_AgentFixture):
    """A relative data dir must produce a working node, not a zombie."""

    def test_a_relative_data_dir_is_resolved_before_the_chdir(self):
        os.chdir(self._tmp.name)
        agent = self._agent(data_dir=os.path.join("nested", "node"))
        # Absolute, and resolved against the caller's cwd -- not the dir the
        # agent moved into during boot.
        expected = os.path.join(self._tmp.name, "nested", "node")
        self.assertEqual(os.path.abspath(agent.data_dir), expected)
        self.assertIn(self._tmp.name, agent.data_dir)

    def test_a_relative_data_dir_still_boots_a_working_engine(self):
        """The whole point: the node can actually decide something."""
        os.chdir(self._tmp.name)
        agent = self._agent(data_dir=os.path.join("nested", "node"))
        self.assertIsNotNone(agent.engine,
                             "engine failed to initialize: %s"
                             % str(getattr(agent, "engine_error", ""))[:300])
        # The per-node state landed beside the data dir, not in a doubled path
        # (`nested/node/nested/node/...`), which is what the bug produced.
        self.assertTrue(
            os.path.isfile(os.path.join(agent.data_dir, "semantic_memory.db")),
            "node state is not in the data dir: %s" % agent.memory_db_path)
        self.assertFalse(
            os.path.isdir(os.path.join(agent.data_dir, "nested")),
            "the data dir was joined onto itself")

    def test_an_absolute_data_dir_is_unchanged(self):
        agent = self._agent()
        self.assertEqual(os.path.abspath(agent.data_dir),
                         os.path.abspath(self._tmp.name))

    def test_no_data_dir_is_still_none(self):
        """On Android there is always one; a bare call must not invent one."""
        agent = self._agent(data_dir="")
        self.assertIsNone(agent.data_dir)


class _RecordingBackend:
    """A backend that records which endpoint it was asked to talk to."""

    def __init__(self, label, output=""):
        self.label = label
        self.output = output
        self.calls = []

    def generate(self, model_id, prompt, timeout=None, **kwargs):
        self.calls.append({"model": model_id, "prompt": prompt})
        return self.output

    def list_models(self):
        return ["configured-model"]


class ConversationalBackendTestCase(_AgentFixture):
    """A conversational turn goes where the registry says, or nowhere."""

    def _engine(self, output=""):
        agent = self._agent(api_url=None, local_model=False)
        engine = agent.engine
        self.assertIsNotNone(engine)

        configured = _RecordingBackend("configured", output=output)
        # The registry says "use MY backend". Give the entry a real backend
        # config so `_backend_for` has something to resolve -- exactly what the
        # desktop terminal writes when the operator picks an endpoint.
        engine.models = [{"id": "configured-model", "type": "text", "weight": 1.0,
                          "backend": {"type": "stub"}}]
        engine.model_manager.models = engine.models
        engine.model_manager.model_performance = {"configured-model": 1.0}
        engine._backend_cache.clear()
        engine._backend_for = lambda model, delegation_url=None: (
            configured if model.get("backend") else None)
        # The global fallback is what the bug silently reached for.
        global_backend = _RecordingBackend("global", output=output)
        engine.subconscious.backend = global_backend
        engine.select_models = lambda task: engine.models
        return engine, configured, global_backend

    def test_a_conversational_turn_uses_the_registry_backend(self):
        engine, configured, global_backend = self._engine(
            output='{"action_type": "speak", "params": {"text": "Hello."}, '
                   '"confidence": 0.9}')
        decision = engine.make_decision({"id": "c1", "type": "conversation",
                                         "prompt": "say hello",
                                         "model": "configured-model"})
        self.assertEqual(len(configured.calls), 1,
                         "the configured backend was never called; the turn "
                         "fell through to another endpoint")
        self.assertEqual(len(global_backend.calls), 0,
                         "the conversational turn bypassed the configured "
                         "backend and used the global one")
        # And the configured backend was actually asked for the configured model.
        self.assertEqual(configured.calls[0]["model"], "configured-model")
        self.assertEqual(decision.get("action_type"), "speak")
        self.assertEqual(decision.get("params", {}).get("text"), "Hello.")

    def test_the_model_entry_is_the_registry_row_not_a_bare_id(self):
        engine, _configured, _global = self._engine()
        entry = engine._model_entry("configured-model")
        self.assertIn("backend", entry,
                      "_model_entry returned a synthesized dict, which makes "
                      "_backend_for resolve no backend at all")
        self.assertEqual(entry["id"], "configured-model")

    def test_an_unknown_model_still_falls_back_without_raising(self):
        engine, _configured, _global = self._engine()
        self.assertEqual(engine._model_entry("not-registered"),
                         {"id": "not-registered"})

    def test_the_model_that_answered_is_named_on_the_decision(self):
        """A person-facing reply must say what backed it, not report "none"."""
        engine, _configured, _global = self._engine(
            output='{"action_type": "speak", "params": {"text": "Hello."}, '
                   '"confidence": 0.9}')
        decision = engine.make_decision({"id": "c3", "type": "conversation",
                                         "prompt": "say hello",
                                         "model": "configured-model"})
        self.assertEqual(decision.get("proposal_source"), "configured-model",
                         "a conversational decision carried no source, so the "
                         "node reports 'backed by none' after a model answered")

    def test_without_a_task_model_the_registry_default_wins(self):
        engine, configured, global_backend = self._engine(
            output='{"action_type": "speak", "params": {"text": "Hi."}, '
                   '"confidence": 0.9}')
        engine.make_decision({"id": "c2", "type": "conversation",
                              "prompt": "hi"})
        self.assertEqual(len(configured.calls), 1)
        self.assertEqual(len(global_backend.calls), 0)


class _FakeElection:
    """An election whose verdict the test sets directly."""

    def __init__(self, node_id="shugo-follower", primary="shugo-primary"):
        self.node_id = node_id
        self._primary = primary

    def tick(self):
        return {"primary": self._primary, "role": "follower"
                if self._primary else "standalone"}

    def primary(self):
        return self._primary


class _FakeRuntime:
    """Stands in for ShugonetAgentRuntime: records frames instead of sending."""

    def __init__(self, status="success", on_send=None):
        self.sent = []
        self._status = status
        # Called after a successful send: how a test simulates the primary's
        # answer arriving back over the mesh.
        self.on_send = on_send

    def send(self, peer, topic, payload):
        self.sent.append({"peer": peer, "topic": topic, "payload": payload})
        if self._status != "success":
            return {"status": "refused", "reason": self._status}
        if self.on_send is not None:
            self.on_send(peer, topic, payload)
        return {"status": "success"}


def _bare_agent(node_id="shugo-follower", primary="shugo-primary",
                runtime=None):
    """An agent with just the mesh surface: no engine, no data dir."""
    agent = AndroidAgent.__new__(AndroidAgent)
    agent.node_id = node_id
    agent.device_caps = "desktop"
    agent.tick_count = 0
    agent.mesh_election = _FakeElection(node_id=node_id, primary=primary)
    agent.shugonet_runtime = runtime
    agent.telemetry = {}
    agent.policy = {}
    agent.last_observation = {}
    agent.log = lambda *args, **kwargs: None
    agent._delegated_from = None
    agent._speak_listener = None
    agent._peer_tts = {}
    agent.interaction = None
    agent.conversation = None
    agent.attention = None
    agent.engine = None
    agent._open_question = None
    return agent


class FollowerForwardsTheTurnTestCase(unittest.TestCase):
    """A node without the lease sends the turn on; it does not answer it.

    Track 1 in one line: one decision, one mouth. The node that holds the lease
    decides; the node the operator used gets the reply back as a *delegated*
    action.
    """

    def setUp(self):
        # The hand-off wait is a real pause on a real mesh; these tests drive the
        # send, so they must not spend it.
        self._wait = shugocore_agent._FORWARD_WAIT_S
        shugocore_agent._FORWARD_WAIT_S = 0.05

    def tearDown(self):
        shugocore_agent._FORWARD_WAIT_S = self._wait

    def test_the_primary_is_named_when_a_peer_holds_the_lease(self):
        agent = _bare_agent(primary="shugo-primary")
        self.assertEqual(agent._turn_primary(), "shugo-primary")

    def test_no_primary_means_decide_here(self):
        for primary in (None, "shugo-follower"):
            agent = _bare_agent(primary=primary)
            self.assertIsNone(agent._turn_primary(), primary)

    def test_a_missing_election_means_decide_here(self):
        agent = _bare_agent()
        agent.mesh_election = None
        self.assertIsNone(agent._turn_primary())

    def test_a_turn_is_forwarded_with_the_facts_only_this_node_has(self):
        runtime = _FakeRuntime()
        agent = _bare_agent(runtime=runtime)
        # The primary's confirmation arrives over the mesh while we wait.
        runtime.on_send = lambda _peer, _topic, payload: (
            agent._record_turn_result("shugo-primary",
                                      {"id": payload["id"], "status": "answered"}))
        forwarded = agent._forward_turn("what time is it?",
                                        {"source": "terminal"}, None,
                                        "It's 4pm.", None)
        self.assertTrue(forwarded)
        self.assertEqual(len(runtime.sent), 1)
        frame = runtime.sent[0]
        self.assertEqual(frame["topic"], TURN_TOPIC)
        self.assertEqual(frame["peer"], "shugo-primary")
        payload = frame["payload"]
        self.assertEqual(payload["transcript"], "what time is it?")
        self.assertEqual(payload["reply_to"], "shugo-follower")
        self.assertEqual(payload["proposed_text"], "It's 4pm.")
        self.assertIn("facts", payload)
        self.assertIn("can_speak", payload)

    def test_a_primary_that_never_answers_leaves_us_to_answer(self):
        """A hand-off nobody confirms must not become silence."""
        agent = _bare_agent(runtime=_FakeRuntime())
        self.assertFalse(agent._forward_turn("hello", {}, None, "Hi.", None))

    def test_a_primary_that_reports_failure_leaves_us_to_answer(self):
        runtime = _FakeRuntime()
        agent = _bare_agent(runtime=runtime)
        runtime.on_send = lambda _peer, _topic, payload: (
            agent._record_turn_result(
                "shugo-primary",
                {"id": payload["id"], "status": "error",
                 "detail": "not delivered"}))
        self.assertFalse(agent._forward_turn("hello", {}, None, "Hi.", None))

    def test_a_refused_forward_falls_back_to_answering_here(self):
        """The fallback is the point: an unreachable primary must not mute us."""
        runtime = _FakeRuntime(status="peer 'shugo-primary' not reachable")
        agent = _bare_agent(runtime=runtime)
        self.assertFalse(agent._forward_turn("hello", {}, None, "", None))

    def test_no_runtime_means_answer_here(self):
        agent = _bare_agent(runtime=None)
        self.assertFalse(agent._forward_turn("hello", {}, None, "", None))

    def test_the_primary_does_not_forward_its_own_turn(self):
        runtime = _FakeRuntime()
        agent = _bare_agent(node_id="shugo-primary", primary="shugo-primary",
                            runtime=runtime)
        self.assertFalse(agent._forward_turn("hello", {}, None, "", None))
        self.assertEqual(runtime.sent, [])


class PrimaryAnswersForwardedTurnsTestCase(unittest.TestCase):
    """The lease holder decides, places the reply, and never loops."""

    def _primary(self, node_id="shugo-primary", runtime=None):
        """A primary that can actually speak through a recording listener."""
        agent = _bare_agent(node_id=node_id, primary=node_id, runtime=runtime)
        spoken = []
        agent._speak_listener = type(
            "S", (), {"speak": lambda _self, text: (spoken.append(text), True)[1]})()
        agent._spoken = spoken
        agent.conversation = None
        agent.interaction = None
        # The node that took the turn is a reachable peer that can speak, which
        # is what makes the reply delegatable rather than local.
        agent._response_candidates = lambda: [
            {"device_id": node_id, "facts": {}, "can_speak": True,
             "is_self": True, "priority": 10},
            {"device_id": "shugo-phone", "facts": {"terminal_active": True},
             "can_speak": True, "is_self": False, "priority": 500},
        ]
        return agent

    def test_a_non_primary_refuses_a_forwarded_turn(self):
        """Two followers must not both decide the same turn."""
        runtime = _FakeRuntime()
        agent = _bare_agent(node_id="shugo-follower",
                            primary="shugo-primary", runtime=runtime)
        agent._handle_forwarded_turn(
            "shugo-other", {"id": "t1", "transcript": "hello"})
        self.assertEqual(len(runtime.sent), 1, "a refusal must be reported")
        self.assertEqual(runtime.sent[0]["topic"], TURN_RESULT_TOPIC)
        self.assertEqual(runtime.sent[0]["payload"]["status"], "refused")
        self.assertIn("not the primary", runtime.sent[0]["payload"]["reason"])

    def test_the_primary_relays_a_proposal_back_to_the_node_that_asked(self):
        runtime = _FakeRuntime()
        agent = self._primary(runtime=runtime)
        agent._answer_forwarded_turn(
            "shugo-phone", {"id": "t2", "transcript": "how's your battery?",
                            "reply_to": "shugo-phone",
                            "proposed_text": "Battery is at 80%."})
        # The answer is delegated to the node that took the turn...
        frames = {f["topic"]: f for f in runtime.sent}
        self.assertIn(DELEGATE_TOPIC, frames)
        delegate = frames[DELEGATE_TOPIC]
        self.assertEqual(delegate["peer"], "shugo-phone")
        self.assertEqual(delegate["payload"]["params"]["text"],
                         "Battery is at 80%.")
        # ...and not spoken on the primary itself...
        self.assertEqual(agent._spoken, [])
        # ...and the originator is told what happened to its hand-off.
        self.assertIn(TURN_RESULT_TOPIC, frames)
        self.assertEqual(frames[TURN_RESULT_TOPIC]["payload"]["status"],
                         "answered")

    def test_an_empty_transcript_is_refused_rather_than_answered(self):
        runtime = _FakeRuntime()
        agent = self._primary(runtime=runtime)
        agent._answer_forwarded_turn("shugo-phone", {"id": "t3",
                                                     "transcript": "   "})
        self.assertEqual(runtime.sent[0]["payload"]["status"], "refused")

    def test_a_forwarded_reply_is_not_forwarded_again(self):
        """The loop guard: the answer must not ping-pong between the nodes.

        The reply travels as a *delegated action*, which
        `_handle_delegated_action` executes directly -- it never re-enters the
        turn pipeline, so a forwarded turn cannot become an infinite forward.
        """
        runtime = _FakeRuntime()
        agent = self._primary(runtime=runtime)
        agent._answer_forwarded_turn(
            "shugo-phone", {"id": "t4", "transcript": "hello",
                            "reply_to": "shugo-phone",
                            "proposed_text": "Hi there."})
        topics = [f["topic"] for f in runtime.sent]
        self.assertNotIn(TURN_TOPIC, topics,
                         "the primary forwarded a turn it was asked to answer")

    def test_reply_prefer_travels_in_the_decision_params(self):
        decision = {"action_type": "speak", "params": {"text": "hi"}}
        out = AndroidAgent._with_reply_prefer(decision, "shugo-phone")
        self.assertEqual(out["params"]["reply_prefer"], "shugo-phone")
        self.assertEqual(out["params"]["text"], "hi")
        # The original is untouched: this must not mutate a shared decision.
        self.assertNotIn("reply_prefer", decision["params"])

    def test_the_turn_origin_is_preferred_over_proximity(self):
        agent = _bare_agent(node_id="shugo-primary", primary="shugo-primary")
        agent._response_candidates = lambda: [
            {"device_id": "shugo-phone", "facts": {"face_present": True},
             "can_speak": True, "is_self": False, "priority": 500},
            {"device_id": "shugo-primary", "facts": {}, "can_speak": True,
             "is_self": True, "priority": 10},
        ]
        chosen, _why = agent.select_response_node(prefer="shugo-phone")
        self.assertEqual(chosen["device_id"], "shugo-phone")
        self.assertIn("turn-origin", chosen["why"])

    def test_an_unreachable_origin_falls_back_to_proximity(self):
        """A preferred node that cannot speak must not mute the answer."""
        agent = _bare_agent(node_id="shugo-primary", primary="shugo-primary")
        agent._response_candidates = lambda: [
            {"device_id": "shugo-phone", "facts": {"face_present": True},
             "can_speak": False, "is_self": False, "priority": 500},
            {"device_id": "shugo-desktop", "facts": {"face_present": True},
             "can_speak": True, "is_self": False, "priority": 10},
        ]
        chosen, _why = agent.select_response_node(prefer="shugo-phone")
        self.assertEqual(chosen["device_id"], "shugo-desktop")

    def test_a_failed_delegation_speaks_locally_instead_of_dropping_it(self):
        """The chosen device can be unreachable; the reply must still be heard.

        Found live: a peer advertising a face outscored this node's terminal, the
        route delegated to a node that was not in the mesh peer map, the send was
        refused -- and the utterance was dropped. The operator heard nothing even
        though the reply had already been decided.
        """
        agent = _bare_agent(node_id="shugo-primary", primary="shugo-primary",
                            runtime=_FakeRuntime(status="unknown peer"))
        said = []
        agent._speak_listener = type(
            "S", (), {"speak": lambda _self, text: (said.append(text), True)[1]})()
        agent._response_candidates = lambda: [
            {"device_id": "shugo-mac", "facts": {"face_present": True},
             "can_speak": True, "is_self": False, "priority": 10},
            {"device_id": "shugo-primary", "facts": {}, "can_speak": True,
             "is_self": True, "priority": 10},
        ]
        result = agent._execute_speak({"action_type": "speak",
                                       "params": {"text": "Hello there."}})
        self.assertEqual(said, ["Hello there."],
                         f"the reply was dropped; result was {result}")
        self.assertEqual(result.get("status"), "success")


class UnansweredHandoffLetsTheNodeAnswerTestCase(unittest.TestCase):
    """A hand-off nobody took leaves this node as the only mouth left.

    The narrow exception to "a follower never speaks": the primary provably did
    not answer, so answering here duplicates nothing -- and the alternative is
    the mute terminal this whole path exists to remove.
    """

    def setUp(self):
        self._wait = shugocore_agent._FORWARD_WAIT_S
        shugocore_agent._FORWARD_WAIT_S = 0.05

    def tearDown(self):
        shugocore_agent._FORWARD_WAIT_S = self._wait

    def _follower(self, runtime):
        agent = _bare_agent(runtime=runtime)
        agent._speak_listener = type(
            "S", (), {"speak": lambda _self, text: True})()
        return agent

    def test_an_unreachable_primary_permits_a_local_answer(self):
        agent = self._follower(_FakeRuntime(status="peer not reachable"))
        self.assertFalse(agent._mesh_may_act("speak_direct"))
        self.assertTrue(agent._forward_turn("hello", {}, None, "Hi.", None) is False)
        # The hand-off was attempted and failed, so this node may answer.
        self.assertTrue(shugocore_agent._turn_fallback_ok(agent))
        self.assertTrue(agent._speak_direct("Hi."))

    def test_the_permission_does_not_leak_into_the_next_turn(self):
        agent = self._follower(_FakeRuntime(status="peer not reachable"))
        agent._forward_turn("hello", {}, None, "Hi.", None)
        self.assertTrue(shugocore_agent._turn_fallback_ok(agent))

        # A new turn starts clean -- including one whose body fails, because the
        # grant is per-turn and the wrapper clears it in a finally.
        def boom(*_args, **_kwargs):
            raise RuntimeError("turn blew up")

        agent._conversational_turn = boom
        with self.assertRaises(RuntimeError):
            agent._handle_conversational_input({"transcript": "hello again"})
        self.assertFalse(shugocore_agent._turn_fallback_ok(agent))

    def test_a_standalone_node_needs_no_permission(self):
        """With no primary there is no hand-off, and the guard already allows."""
        agent = self._follower(_FakeRuntime())
        agent.mesh_election = _FakeElection(node_id="shugo-follower",
                                            primary=None)
        self.assertFalse(agent._forward_turn("hello", {}, None, "Hi.", None))
        self.assertFalse(shugocore_agent._turn_fallback_ok(agent))
        self.assertTrue(agent._speak_direct("Hi."))

    def test_the_permission_is_thread_scoped(self):
        """A concurrent tick must not inherit it and speak on a follower."""
        agent = self._follower(_FakeRuntime(status="peer not reachable"))
        agent._forward_turn("hello", {}, None, "Hi.", None)
        seen = {}
        thread = threading.Thread(
            target=lambda: seen.update(ok=shugocore_agent._turn_fallback_ok(agent)))
        thread.start()
        thread.join()
        self.assertTrue(shugocore_agent._turn_fallback_ok(agent),
                        "the turn thread lost its own permission")
        self.assertFalse(seen["ok"],
                         "another thread inherited the fallback permission")


class ProximityIsAdvertisedTestCase(unittest.TestCase):
    """A keyboard operator must be visible to the primary that places replies."""

    def _agent(self, recent):
        agent = _bare_agent()
        agent.last_observation = {}
        agent.interaction = None
        agent.attention = None
        agent.terminal_input_recent = lambda window_s=60.0: recent
        return agent

    def test_typed_input_is_advertised_while_recent(self):
        facts = self._agent(True)._mesh_perception_facts()
        self.assertTrue(facts.get("remote_terminal_active"))

    def test_a_quiet_terminal_advertises_nothing(self):
        facts = self._agent(False)._mesh_perception_facts()
        self.assertNotIn("remote_terminal_active", facts)

    def test_the_router_scores_the_remote_spelling(self):
        from response_routing import proximity_score, DEFAULT_FLOOR
        local, _ = proximity_score({"terminal_active": True})
        remote, why = proximity_score({"remote_terminal_active": True})
        self.assertEqual(local, remote,
                         "the remote spelling must score like the local one")
        self.assertGreaterEqual(remote, DEFAULT_FLOOR)
        self.assertIn("terminal-input", why)


if __name__ == "__main__":
    unittest.main()



