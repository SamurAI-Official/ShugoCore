"""Phase 3 subsystem tests: memory, tools, fallback, dialogue."""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subsystems import (CommandExecutor, UserIntent, IntentType,
                        MemoryManager, FactMemory, TopicTracker)
from subsystems.tools import (ToolRegistry, TimerManager, Tool, ToolResult,
                              default_tools)
from subsystems.fallback import RetryPolicy, FallbackChain, retry_call


class FactMemoryTest(unittest.TestCase):
    def test_extracts_name_and_preference(self):
        td = tempfile.mkdtemp()
        fm = FactMemory(td)
        touched = fm.remember("my name is Alex and I like jazz")
        texts = [t.lower() for t in touched]
        self.assertTrue(any("alex" in t for t in texts))
        self.assertTrue(any("jazz" in t for t in texts))

    def test_over_capture_truncation(self):
        fm = FactMemory()
        fm.remember("my name is Alex and I like jazz")
        for fact in fm.recall(limit=10):
            self.assertNotIn("and I like jazz", fact["text"])

    def test_name_pattern_preferred_over_attribute(self):
        fm = FactMemory()
        fm.remember("my name is Alex")
        facts = fm.recall(limit=10)
        names = [f["text"] for f in facts if f["kind"] == "user_name"]
        self.assertTrue(any("Alex" in n for n in names))

    def test_repetition_boosts_score(self):
        fm = FactMemory()
        for _ in range(3):
            fm.remember("I love coffee")
        facts = fm.recall(limit=3)
        self.assertEqual(facts[0]["text"], "User likes coffee")
        self.assertEqual(facts[0]["count"], 3)

    def test_persists_across_reload(self):
        td = tempfile.mkdtemp()
        fm = FactMemory(td)
        fm.remember("my name is Alex")
        fm2 = FactMemory(td)
        names = [f["text"] for f in fm2.recall(limit=5)]
        self.assertTrue(any("Alex" in n for n in names))

    def test_facts_about_keyword(self):
        fm = FactMemory()
        fm.remember("I love coffee and I love tea")
        hits = fm.facts_about("coffee", limit=3)
        self.assertTrue(any("coffee" in h["text"] for h in hits))

    def test_duration_fact(self):
        fm = FactMemory()
        fm.remember("set a timer for 5 minutes")
        facts = fm.recall(limit=5)
        self.assertTrue(any("5 minute" in f["text"] for f in facts))


class TopicTrackerTest(unittest.TestCase):
    def test_detects_topic(self):
        tt = TopicTracker()
        self.assertEqual(tt.track("set a timer for 5 minutes"), "timers")
        self.assertEqual(tt.current_topic, "timers")

    def test_continuation_keeps_topic(self):
        tt = TopicTracker()
        tt.track("set a timer for 5 minutes")
        tt.track("how long?")
        self.assertEqual(tt.current_topic, "timers")

    def test_topic_switch(self):
        tt = TopicTracker()
        tt.track("set a timer for 5 minutes")
        tt.track("what's the weather like?")
        self.assertEqual(tt.current_topic, "weather")

    def test_history_bounded(self):
        tt = TopicTracker(max_history=3)
        for u in ["timer", "weather", "music", "battery"]:
            tt.track(u)
        self.assertEqual(len(tt.history()), 3)

    def test_random_speech_does_not_create_topic(self):
        tt = TopicTracker()
        for _ in range(6):
            tt.track("hello there")
        self.assertIsNone(tt.current_topic)


class MemoryManagerTest(unittest.TestCase):
    def test_on_user_input_digest(self):
        mm = MemoryManager()
        digest = mm.on_user_input("set a timer for 5 minutes")
        self.assertEqual(digest["topic"], "timers")
        self.assertTrue(digest["new_facts"])

    def test_recall_context_shape(self):
        mm = MemoryManager()
        mm.on_user_input("my name is Alex")
        ctx = mm.recall_context()
        self.assertIn("facts", ctx)
        self.assertIn("current_topic", ctx)
        self.assertIn("topic_text", ctx)

    def test_recall_fact_strings(self):
        mm = MemoryManager()
        mm.on_user_input("my name is Alex")
        strings = mm.recall_fact_strings()
        self.assertTrue(any("Alex" in s for s in strings))


if __name__ == "__main__":
    unittest.main()
class ToolRegistryTest(unittest.TestCase):
    def test_register_and_call(self):
        reg = ToolRegistry()
        reg.register_handler("double", lambda x: ToolResult.ok_result(str(x * 2)))
        r = reg.call("double", x=4)
        self.assertTrue(r.ok)
        self.assertEqual(r.output, "8")

    def test_unknown_tool_returns_error_result(self):
        reg = ToolRegistry()
        r = reg.call("nope")
        self.assertFalse(r.ok)

    def test_handler_exception_is_caught(self):
        reg = ToolRegistry()

        def boom():
            raise RuntimeError("kaput")

        reg.register_handler("boom", boom)
        r = reg.call("boom")
        self.assertFalse(r.ok)
        self.assertIn("kaput", r.error)

    def test_describe_all(self):
        reg = ToolRegistry()
        reg.register_handler("a", lambda: ToolResult.ok_result(""))
        reg.register_handler("b", lambda: ToolResult.ok_result(""))
        self.assertEqual({d["name"] for d in reg.describe_all()}, {"a", "b"})


class DefaultToolsTest(unittest.TestCase):
    def test_time_and_date(self):
        reg = default_tools()
        self.assertIn("It's", reg.call("get_time").output)
        self.assertIn("Today is", reg.call("get_date").output)

    def test_joke(self):
        reg = default_tools()
        r = reg.call("tell_joke")
        self.assertTrue(r.ok)
        self.assertTrue(len(r.output) > 10)

    def test_battery_provider(self):
        reg = default_tools(battery_provider=lambda: 87)
        self.assertIn("87%", reg.call("get_battery").output)
        reg_none = default_tools(battery_provider=lambda: None)
        self.assertFalse(reg_none.call("get_battery").ok)

    def test_sensors_provider(self):
        reg = default_tools(sensor_provider=lambda: {"temp": "24C"})
        r = reg.call("check_sensors")
        self.assertTrue(r.ok)
        self.assertIn("temp", r.output)


class TimerManagerTest(unittest.TestCase):
    def test_set_and_check_fires(self):
        fired = []
        tm = TimerManager()
        timer = tm.set(0.05, "quick", on_fire=lambda label: fired.append(label))
        time.sleep(0.1)
        fired_list = tm.check()
        self.assertEqual(len(fired_list), 1)
        self.assertEqual(fired_list[0].timer_id, timer.timer_id)
        self.assertTrue(fired)

    def test_zero_duration_fires_immediately(self):
        tm = TimerManager()
        timer = tm.set(0, "now")
        self.assertTrue(timer.fired)

    def test_set_timer_tool_wires_manager(self):
        tm = TimerManager()
        reg = default_tools(timer_manager=tm)
        r = reg.call("set_timer", duration_value=1, duration_unit="hour")
        self.assertTrue(r.ok)
        self.assertIn("timer_id", r.data)
        self.assertEqual(tm.count(), 1)

    def test_cancel(self):
        tm = TimerManager()
        timer = tm.set(60, "long")
        tm.cancel(timer.timer_id)
        self.assertEqual(tm.count(), 0)

    def test_list_pending(self):
        tm = TimerManager()
        tm.set(60, "one")
        tm.set(120, "two")
        self.assertEqual(len(tm.list_pending()), 2)
class RetryPolicyTest(unittest.TestCase):
    def test_succeeds_first_try(self):
        calls = []

        def ok():
            calls.append(1)
            return "done"

        policy = RetryPolicy(max_attempts=3, base_backoff_secs=0)
        ok_flag, result = retry_call(ok, policy)
        self.assertTrue(ok_flag)
        self.assertEqual(result, "done")
        self.assertEqual(len(calls), 1)

    def test_succeeds_after_retry(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError("temporary")
            return "recovered"

        policy = RetryPolicy(max_attempts=4, base_backoff_secs=0)
        ok_flag, result = retry_call(flaky, policy)
        self.assertTrue(ok_flag)
        self.assertEqual(result, "recovered")
        self.assertEqual(len(calls), 3)

    def test_exhausts_and_returns_last_error(self):
        calls = []

        def always_fails():
            calls.append(1)
            raise ValueError("nope")

        policy = RetryPolicy(max_attempts=2, base_backoff_secs=0)
        ok_flag, result = retry_call(always_fails, policy)
        self.assertFalse(ok_flag)
        self.assertIsInstance(result, ValueError)
        self.assertEqual(len(calls), 2)

    def test_allowed_exceptions_filter(self):
        def raises():
            raise TypeError("wrong kind")

        # Only retry on IOError — TypeError passes straight through.
        policy = RetryPolicy(max_attempts=3, base_backoff_secs=0,
                             allowed_exceptions=(IOError,))
        with self.assertRaises(TypeError):
            retry_call(raises, policy)


class FallbackChainTest(unittest.TestCase):
    def test_first_success_wins(self):
        chain = FallbackChain()
        chain.add("primary", lambda: (True, "from primary"))
        chain.add("backup", lambda: (True, "from backup"))
        name, ok, result = chain.run()
        self.assertTrue(ok)
        self.assertEqual(name, "primary")
        self.assertEqual(result, "from primary")

    def test_falls_through_to_backup(self):
        chain = FallbackChain()
        chain.add("broken", lambda: (False, None))
        chain.add("backup", lambda: (True, "saved"))
        name, ok, result = chain.run()
        self.assertEqual(name, "backup")
        self.assertEqual(result, "saved")

    def test_exception_in_strategy_is_caught(self):
        chain = FallbackChain()

        def explode():
            raise RuntimeError("boom")

        chain.add("explodes", explode)
        chain.add("safe", lambda: (True, "still fine"))
        name, ok, result = chain.run()
        self.assertEqual(name, "safe")
        self.assertTrue(ok)

    def test_all_fail_returns_last_result(self):
        chain = FallbackChain()
        chain.add("a", lambda: (False, "a failed"))
        chain.add("b", lambda: (False, "b failed"))
        name, ok, result = chain.run()
        self.assertFalse(ok)
        self.assertEqual(name, "b")
        self.assertEqual(result, "b failed")
class DialogueStateTest(unittest.TestCase):
    def test_begin_and_resolve_clarification(self):
        from subsystems.dialogue import DialogueState
        ds = DialogueState()
        ds.begin_clarification("timer_duration", "set a timer",
                               entities={"action_verb": "set"})
        self.assertTrue(ds.has_pending)
        resolved = ds.resolve_answer("5 minutes")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.entities["duration_value"], 5)
        self.assertEqual(resolved.entities["duration_unit"], "minute")
        self.assertFalse(ds.has_pending)

    def test_new_intent_does_not_resolve_pending(self):
        from subsystems.dialogue import DialogueState
        ds = DialogueState()
        ds.begin_clarification("timer_duration", "set a timer")
        resolved = ds.resolve_answer("set another timer")
        self.assertIsNone(resolved)
        self.assertTrue(ds.has_pending)

    def test_coreference_resolution(self):
        from subsystems.dialogue import DialogueState
        ds = DialogueState()
        ds.remember_entity("the lights")
        self.assertEqual(ds.resolve_coreference("turn them off"),
                         "turn the lights off")
        self.assertEqual(ds.resolve_coreference("fine, leave it"),
                         "fine, leave the lights")

    def test_coreference_no_entity_noop(self):
        from subsystems.dialogue import DialogueState
        ds = DialogueState()
        self.assertEqual(ds.resolve_coreference("turn it off"), "turn it off")

    def test_extract_named_entity(self):
        from subsystems.dialogue import extract_named_entity, DialogueState
        self.assertEqual(extract_named_entity("turn the lights off"),
                         "the lights")
        self.assertEqual(extract_named_entity("play some jazz music"), "jazz music")
        self.assertIsNone(extract_named_entity("hello there"))

    def test_entity_recency_bounded(self):
        from subsystems.dialogue import DialogueState
        ds = DialogueState(max_entities=2)
        ds.remember_entity("a")
        ds.remember_entity("b")
        ds.remember_entity("c")
        self.assertEqual(ds.last_entity, "c")
        self.assertEqual(len(ds._recent_entities), 2)

    def test_clear(self):
        from subsystems.dialogue import DialogueState
        ds = DialogueState()
        ds.begin_clarification("timer_duration", "set a timer")
        ds.remember_entity("the lights")
        ds.clear()
        self.assertFalse(ds.has_pending)
        self.assertIsNone(ds.last_entity)

class CommandRouterIntegrationTest(unittest.TestCase):
    """Phase 3 wiring: CommandExecutor + ToolRegistry + handlers."""

    def _make_executor(self, tools):
        from subsystems.command_router import default_handlers as build_handlers
        executor = CommandExecutor()
        executor.set_tool_registry(tools)
        for name, handler in build_handlers(tools).items():
            executor.register_handler(name, handler)
        return executor

    def test_time_command_uses_tool(self):
        reg = default_tools()
        ex = self._make_executor(reg)
        intent = UserIntent(IntentType.COMMAND, "what time is it")
        result = ex.execute(intent)
        self.assertTrue(result.success)
        self.assertIn("It's", result.response)
        self.assertEqual(result.action_taken, "time_report")

    def test_timer_without_duration_clarifies(self):
        reg = default_tools()
        ex = self._make_executor(reg)
        intent = UserIntent(IntentType.COMMAND, "set a timer")
        result = ex.execute(intent)
        self.assertEqual(result.action_taken, "timer_clarify")
        self.assertIn("how long", result.response.lower())

    def test_timer_with_duration_sets_real_timer(self):
        tm = TimerManager()
        reg = default_tools(timer_manager=tm)
        ex = self._make_executor(reg)
        intent = UserIntent(
            IntentType.COMMAND, "set a timer for 2 minutes",
            entities={"duration_value": 2, "duration_unit": "minute"})
        result = ex.execute(intent)
        self.assertTrue(result.success)
        self.assertEqual(result.action_taken, "timer_set")
        self.assertEqual(tm.count(), 1)

    def test_battery_command_uses_provider(self):
        reg = default_tools(battery_provider=lambda: 72)
        ex = self._make_executor(reg)
        intent = UserIntent(IntentType.COMMAND, "check my battery")
        result = ex.execute(intent)
        self.assertTrue(result.success)
        self.assertIn("72%", result.response)

    def test_handlers_work_without_tools(self):
        """Graceful degradation when no tool registry is wired."""
        from subsystems.command_router import default_handlers as build_handlers
        ex = CommandExecutor()
        for name, handler in build_handlers(None).items():
            ex.register_handler(name, handler)
        intent = UserIntent(IntentType.COMMAND, "set a timer for 2 minutes",
                            entities={"duration_value": 2,
                                      "duration_unit": "minute"})
        result = ex.execute(intent)
        self.assertFalse(result.success)  # tool kit missing

    def test_full_subsystem_import(self):
        import subsystems
        for name in ("IntentParser", "CommandExecutor", "MemoryManager",
                     "ToolRegistry", "TimerManager", "RetryPolicy",
                     "FallbackChain", "DialogueState"):
            self.assertTrue(hasattr(subsystems, name), name)

class AgentPhase3IntegrationTest(unittest.TestCase):
    """Full agent: real Phase 3 wiring through create_agent().

    Command intents should speak immediately without a model, timers should
    actually register, facts should persist, and clarification answers should
    merge back into the original request.
    """

    def setUp(self):
        from shugocore_agent import create_agent
        self._agent_data = tempfile.mkdtemp()
        self.agent = create_agent(
            device_caps="TestSoC", api_url="http://127.0.0.1:11434",
            data_dir=self._agent_data)
        self.spoken = []
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

    def test_timer_command_spoken_and_registered(self):
        self.agent._handle_conversational_input(
            {"transcript": "set a timer for 2 minutes"})
        speaker = self._listen()
        self.assertTrue(any("timer" in s.lower() for s in speaker.spoken))
        count = self.agent.tool_registry._timer_manager.count()
        self.assertEqual(count, 1)

    def test_time_command_spokens(self):
        self.agent._handle_conversational_input({"transcript": "what time is it"})
        speaker = self._listen()
        self.assertTrue(any("It's" in s for s in speaker.spoken))

    def test_fact_memory_learns_name(self):
        speaker = self.agent._speak_listener
        self.agent._handle_conversational_input({"transcript": "my name is Alex"})
        facts = self.agent.user_memory.facts.recall(limit=10)
        self.assertTrue(any("Alex" in f["text"] for f in facts))
        # The command path never needed the model; a non-command path may
        # attempt an engine call but must fail gracefully (engine is None).
        self.agent._handle_conversational_input({"transcript": "hello"})
        self.assertTrue(self.agent.intent_parser is not None)

    def test_clarification_answer_completes_timer(self):
        # Turn 1: no duration -> asks for it, opens pending clarification.
        self.agent._handle_conversational_input({"transcript": "set a timer"})
        self.assertTrue(self.agent.dialogue.has_pending)
        speaker = self._listen()
        self.assertTrue(any("how long" in s.lower() for s in speaker.spoken))
        # Turn 2: the answer merges and executes the real tool.
        self.agent._handle_conversational_input({"transcript": "5 minutes"})
        self.assertFalse(self.agent.dialogue.has_pending)
        count = self.agent.tool_registry._timer_manager.count()
        self.assertEqual(count, 1)
        self.assertTrue(any("5 minute" in s.lower() for s in speaker.spoken))

    def test_coreference_tracked_for_nodes(self):
        self.agent._handle_conversational_input(
            {"transcript": "turn the lights off"})
        self.assertEqual(self.agent.dialogue.last_entity, "the lights")

    def test_timer_polling_fires_and_speaks(self):
        tm = self.agent.tool_registry._timer_manager
        tm.set(0.05, "Eggs",
               on_fire=lambda label: self.agent._speak_listener.speak(
                   f"{label} is done!"))
        time.sleep(0.1)
        self.agent._check_timers()
        speaker = self._listen()
        self.assertTrue(any("Eggs is done" in s for s in speaker.spoken))