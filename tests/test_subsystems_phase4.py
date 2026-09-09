"""Phase 4 subsystem tests: timer persistence, durable-memory tools,
full agent teardown."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subsystems import CommandExecutor, IntentType, MemoryManager
from subsystems.intent import IntentParser
from subsystems.command_router import default_handlers
from subsystems.tools import TimerManager, default_tools


class TimerPersistenceTest(unittest.TestCase):
    """TimerManager with a persist_path mirrors state to disk."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.path = os.path.join(self._tmp, "timers.json")

    def test_no_path_by_default(self):
        mgr = TimerManager()
        self.assertIsNone(mgr.persist_path)
        mgr.set(60)
        self.assertFalse(os.path.exists(self.path))

    def test_set_persists(self):
        mgr = TimerManager(persist_path=self.path)
        mgr.set(60, "Tea")
        self.assertTrue(os.path.exists(self.path))
        data = json.load(open(self.path))
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["label"], "Tea")
        self.assertIn("due_at", data[0])

    def test_round_trip_preserves_remaining(self):
        mgr = TimerManager(persist_path=self.path)
        mgr.set(60, "Tea", timer_id="t1")
        mgr2 = TimerManager(persist_path=self.path)
        self.assertEqual(mgr2.count(), 1)
        timer = mgr2.get("t1")
        self.assertIsNotNone(timer)
        self.assertLessEqual(timer.remaining_seconds, 60.0)
        self.assertGreater(timer.remaining_seconds, 55.0)

    def test_cancel_updates_file(self):
        mgr = TimerManager(persist_path=self.path)
        timer = mgr.set(60, "Tea")
        mgr.cancel(timer.timer_id)
        self.assertEqual(json.load(open(self.path)), [])

    def test_clear_all_persists_empty(self):
        mgr = TimerManager(persist_path=self.path)
        mgr.set(60)
        mgr.clear_all()
        self.assertEqual(json.load(open(self.path)), [])

    def test_zero_duration_not_persisted(self):
        mgr = TimerManager(persist_path=self.path)
        mgr.set(0, "Instant")
        self.assertEqual(json.load(open(self.path)), [])

    def test_expired_while_away(self):
        # Seed the file with an already-expired timer.
        past = time.time() - 120
        with open(self.path, "w") as fh:
            json.dump([{"timer_id": "t-old", "label": "Oven",
                        "duration_seconds": 60, "due_at": past}], fh)
        mgr = TimerManager(persist_path=self.path)
        missed = mgr.restore()
        self.assertEqual(len(missed), 1)
        self.assertTrue(missed[0].fired_while_away)
        self.assertEqual(missed[0].label, "Oven")
        # Next check() fires it and clears it from disk.
        fired = mgr.check()
        self.assertEqual(len(fired), 1)
        self.assertTrue(fired[0].fired_while_away)
        self.assertEqual(json.load(open(self.path)), [])

    def test_pending_survives_alongside_missed(self):
        past = time.time() - 120
        with open(self.path, "w") as fh:
            json.dump([{"timer_id": "t-old", "label": "Oven",
                        "duration_seconds": 60, "due_at": past},
                       {"timer_id": "t-new", "label": "Tea",
                        "duration_seconds": 600,
                        "due_at": time.time() + 600}], fh)
        mgr = TimerManager(persist_path=self.path)
        missed = mgr.restore()
        self.assertEqual([t.timer_id for t in missed], ["t-old"])
        self.assertIsNotNone(mgr.get("t-new"))

    def test_corrupt_file_ignored(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        mgr = TimerManager(persist_path=self.path)
        self.assertEqual(mgr.count(), 0)
        mgr.set(30)
        self.assertEqual(mgr.count(), 1)

    def test_malformed_entries_skipped(self):
        with open(self.path, "w") as fh:
            json.dump([{"label": "no id"}, "junk", {}], fh)
        mgr = TimerManager(persist_path=self.path)
        self.assertEqual(mgr.count(), 0)


class FactMemoryPhase4Test(unittest.TestCase):
    """Explicit verbatim facts + forgetting."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.mem = MemoryManager(self._tmp)

    def test_remember_explicit_verbatim(self):
        stored = self.mem.facts.remember_explicit("my sister is Ana")
        self.assertEqual(stored, "my sister is Ana")
        facts = self.mem.facts.recall(limit=10)
        self.assertTrue(any("Ana" in f["text"] for f in facts))

    def test_remember_explicit_dedupes(self):
        self.mem.facts.remember_explicit("my sister is Ana")
        self.mem.facts.remember_explicit("my sister is Ana")
        facts = self.mem.facts.recall(limit=10)
        self.assertEqual(len([f for f in facts if "Ana" in f["text"]]), 1)

    def test_remember_explicit_rejects_short(self):
        self.assertIsNone(self.mem.facts.remember_explicit("a"))
        self.assertIsNone(self.mem.facts.remember_explicit(""))
        self.assertEqual(len(self.mem.facts), 0)

    def test_forget_removes_matching(self):
        self.mem.facts.remember_explicit("my sister is Ana")
        self.mem.facts.remember_explicit("my brother is Bob")
        removed = self.mem.facts.forget("sister")
        self.assertEqual(removed, 1)
        facts = self.mem.facts.recall(limit=10)
        self.assertFalse(any("Ana" in f["text"] for f in facts))
        self.assertTrue(any("Bob" in f["text"] for f in facts))

    def test_forget_no_match_returns_zero(self):
        self.assertEqual(self.mem.facts.forget("nothing-matches"), 0)
        self.assertEqual(self.mem.facts.forget(""), 0)

    def test_forget_persists(self):
        self.mem.facts.remember_explicit("my sister is Ana")
        self.mem.facts.forget("sister")
        fresh = MemoryManager(self._tmp)
        self.assertEqual(len(fresh.facts), 0)


class MemoryToolsTest(unittest.TestCase):
    """remember/recall/forget tools bridged through the registry."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.mem = MemoryManager(self._tmp)
        self.provider = self.mem.facts  # tools bind to the FactMemory layer

    def test_memory_tools_registered(self):
        registry = default_tools(memory_provider=self.provider)
        for name in ("remember_fact", "recall_facts", "forget_facts"):
            self.assertTrue(registry.has(name), name)

    def test_not_registered_without_provider(self):
        self.assertFalse(default_tools().has("remember_fact"))

    def test_remember_recall_forget_flow(self):
        registry = default_tools(memory_provider=self.provider)
        r = registry.call("remember_fact", text="my sister is Ana")
        self.assertTrue(r.ok)
        r = registry.call("recall_facts", keyword="sister")
        self.assertTrue(r.ok)
        self.assertIn("Ana", r.output)
        r = registry.call("forget_facts", keyword="sister")
        self.assertTrue(r.ok)
        self.assertEqual(r.data.get("removed"), 1)
        r = registry.call("recall_facts", keyword="sister")
        self.assertTrue(r.ok)
        self.assertIn("Nothing", r.output)

    def test_remember_rejects_empty(self):
        registry = default_tools(memory_provider=self.provider)
        self.assertFalse(registry.call("remember_fact", text="a").ok)

    def test_recall_empty_memory(self):
        registry = default_tools(memory_provider=self.provider)
        r = registry.call("recall_facts")
        self.assertTrue(r.ok)
        self.assertIn("Nothing", r.output)


class MemoryIntentTest(unittest.TestCase):
    """Intent classification + routing for memory commands."""

    def setUp(self):
        self.parser = IntentParser()
        self.executor = CommandExecutor()

    def test_remember_is_command(self):
        intent = self.parser.classify("remember that my sister is Ana")
        self.assertEqual(intent.intent_type, IntentType.COMMAND)
        self.assertEqual(intent.entities.get("memory_text"),
                         "my sister is Ana")

    def test_forget_is_command(self):
        intent = self.parser.classify("forget about my sister")
        self.assertEqual(intent.intent_type, IntentType.COMMAND)
        self.assertEqual(intent.entities.get("memory_text"), "my sister")

    def test_recall_is_command(self):
        intent = self.parser.classify("recall my favorite color")
        self.assertEqual(intent.intent_type, IntentType.COMMAND)
        self.assertEqual(intent.entities.get("action_verb"), "recall")

    def test_do_you_remember_stays_question(self):
        intent = self.parser.classify("do you remember my name")
        self.assertEqual(intent.intent_type, IntentType.QUESTION)

    def test_what_do_you_remember_stays_question(self):
        intent = self.parser.classify("what do you remember about me?")
        self.assertEqual(intent.intent_type, IntentType.QUESTION)

    def test_remember_timer_goes_to_timer(self):
        intent = self.parser.classify("remember to set a timer for 5 minutes")
        self.assertEqual(self.executor._categorize(
            intent.entities.get("action_verb", ""), intent.transcript),
            "timer")


class MemoryRouterTest(unittest.TestCase):
    """End-to-end: parse -> categorize -> handle_memory with real tools."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.mem = MemoryManager(self._tmp)
        self.registry = default_tools(memory_provider=self.mem.facts)
        self.executor = CommandExecutor()
        self.executor.set_tool_registry(self.registry)
        for name, handler in default_handlers(self.registry).items():
            self.executor.register_handler(name, handler)

    def _run(self, transcript):
        intent = IntentParser().classify(transcript)
        return self.executor.execute(intent)

    def test_remember_stores_and_speaks(self):
        result = self._run("remember that my sister is Ana")
        self.assertTrue(result.success)
        self.assertEqual(result.action_taken, "memory_store")
        facts = self.mem.facts.recall(limit=10)
        self.assertTrue(any("Ana" in f["text"] for f in facts))

    def test_recall_speaks_facts(self):
        self._run("remember that my sister is Ana")
        result = self._run("recall my sister")
        self.assertTrue(result.success)
        self.assertEqual(result.action_taken, "memory_recall")
        self.assertIn("Ana", result.response)

    def test_forget_removes(self):
        self._run("remember that my sister is Ana")
        result = self._run("forget about my sister")
        self.assertTrue(result.success)
        self.assertEqual(result.action_taken, "memory_forget")
        self.assertFalse(self.mem.facts.facts_about("Ana"))

    def test_bare_remember_asks_clarify(self):
        result = self._run("remember")
        self.assertTrue(result.success)
        self.assertEqual(result.action_taken, "memory_clarify")

    def test_bare_forget_asks_clarify(self):
        result = self._run("forget")
        self.assertTrue(result.success)
        self.assertEqual(result.action_taken, "memory_clarify")

    def test_without_tools_degrades(self):
        executor = CommandExecutor()
        for name, handler in default_handlers(None).items():
            executor.register_handler(name, handler)
        intent = IntentParser().classify("remember that my sister is Ana")
        result = executor.execute(intent)
        self.assertFalse(result.success)
        self.assertIn("unavailable", result.action_taken)


class _FakeEngine:
    """Records shutdown() calls for teardown tests."""

    def __init__(self):
        self.shutdown_calls = 0

    def shutdown(self):
        self.shutdown_calls += 1


class AgentPhase4IntegrationTest(unittest.TestCase):
    """Full agent: Phase 4 wiring through create_agent()."""

    def setUp(self):
        from shugocore_agent import create_agent
        self._agent_data = tempfile.mkdtemp()
        self.agent = create_agent(
            device_caps="TestSoC", api_url="http://127.0.0.1:11434",
            data_dir=self._agent_data)
        self.agent.register_speak_listener(self._FakeSpeaker())

    def tearDown(self):
        try:
            self.agent.cleanup()
        except Exception:
            pass

    class _FakeSpeaker:
        """Minimal speak listener contract: .speak(text) -> bool."""

        def __init__(self):
            self.spoken = []

        def speak(self, text):
            self.spoken.append(text)
            return True

        def register_hook(self, hook):
            return None

    def _listen(self):
        return self.agent._speak_listener

    # -- timer persistence ----------------------------------------------
    def test_timer_manager_persists_to_data_dir(self):
        mgr = self.agent.tool_registry._timer_manager
        self.assertEqual(mgr.persist_path,
                         os.path.join(self._agent_data, "timers.json"))
        mgr.set(60, "Tea")
        self.assertTrue(os.path.exists(mgr.persist_path))

    def test_timer_survives_manager_restart(self):
        mgr = self.agent.tool_registry._timer_manager
        mgr.set(30, "Tea", timer_id="t-restart")
        fresh = TimerManager(persist_path=mgr.persist_path)
        self.assertIsNotNone(fresh.get("t-restart"))

    def test_missed_timer_announced_while_away(self):
        # Seed an expired timer, then bootstrap a fresh agent on the same
        # data dir: the next tick must announce the miss.
        self.agent.cleanup()
        past = time.time() - 120
        path = os.path.join(self._agent_data, "timers.json")
        with open(path, "w") as fh:
            json.dump([{"timer_id": "t-old", "label": "Oven",
                        "duration_seconds": 60, "due_at": past}], fh)
        from shugocore_agent import create_agent
        agent2 = create_agent(
            device_caps="TestSoC", api_url="http://127.0.0.1:11434",
            data_dir=self._agent_data)
        try:
            agent2.register_speak_listener(self._FakeSpeaker())
            agent2._check_timers()
            speaker = agent2._speak_listener
            self.assertTrue(any("While you were away" in s
                                for s in speaker.spoken), speaker.spoken)
        finally:
            agent2.cleanup()

    # -- memory commands --------------------------------------------------
    def test_remember_command_persists_fact(self):
        self.agent._handle_conversational_input(
            {"transcript": "remember that my sister is Ana"})
        facts = self.agent.user_memory.facts.recall(limit=10)
        self.assertTrue(any("Ana" in f["text"] for f in facts))
        speaker = self._listen()
        self.assertTrue(any("remember" in s.lower() for s in speaker.spoken))

    def test_forget_command_removes_fact(self):
        self.agent._handle_conversational_input(
            {"transcript": "remember that my sister is Ana"})
        self.agent._handle_conversational_input(
            {"transcript": "forget about my sister"})
        facts = self.agent.user_memory.facts.recall(limit=10)
        self.assertFalse(any("Ana" in f["text"] for f in facts))

    def test_question_recall_spoken(self):
        self.agent._handle_conversational_input(
            {"transcript": "remember that my sister is Ana"})
        self.agent._handle_conversational_input(
            {"transcript": "what do you remember about me"})
        speaker = self._listen()
        self.assertTrue(any("Ana" in s for s in speaker.spoken),
                        speaker.spoken)

    def test_facts_survive_agent_restart(self):
        self.agent._handle_conversational_input(
            {"transcript": "remember that my sister is Ana"})
        self.agent.cleanup()
        from shugocore_agent import create_agent
        agent2 = create_agent(
            device_caps="TestSoC", api_url="http://127.0.0.1:11434",
            data_dir=self._agent_data)
        try:
            facts = agent2.user_memory.facts.recall(limit=10)
            self.assertTrue(any("Ana" in f["text"] for f in facts))
        finally:
            agent2.cleanup()

    # -- full teardown ------------------------------------------------------
    def test_cleanup_does_not_shutdown_engine(self):
        fake = _FakeEngine()
        self.agent._real_engine = fake
        self.agent.cleanup()
        self.assertEqual(fake.shutdown_calls, 0)

    def test_shutdown_flushes_engine_once(self):
        fake = _FakeEngine()
        self.agent._real_engine = fake
        self.agent.shutdown()
        self.assertEqual(fake.shutdown_calls, 1)
        self.assertTrue(getattr(self.agent, "_shutdown_complete", False))

    def test_shutdown_is_idempotent(self):
        fake = _FakeEngine()
        self.agent._real_engine = fake
        self.agent.shutdown()
        self.agent.shutdown()
        self.assertEqual(fake.shutdown_calls, 1)

    def test_shutdown_without_engine_still_cleans(self):
        self.agent._real_engine = None
        self.agent.engine = None
        self.agent.shutdown()  # must not raise
        self.assertTrue(getattr(self.agent, "_shutdown_complete", False))

    def test_shutdown_survives_engine_error(self):
        class _Boom:
            def shutdown(self):
                raise RuntimeError("boom")
        self.agent._real_engine = _Boom()
        self.agent.shutdown()  # must not raise
        self.assertTrue(getattr(self.agent, "_shutdown_complete", False))


if __name__ == "__main__":
    unittest.main()

