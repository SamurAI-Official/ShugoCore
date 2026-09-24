"""v1.30.5 tool routing: a question's phrasing must not decide the answer.

Phase 0 (2026-09-20, on the A51 + Tab S9 FE) measured the failure on real
hardware: "what time is it" reached the `get_time` tool while "what's the time"
and "tell me the time" did not — the first only because it is a literal string
in `_COMMAND_PATTERNS`, the second because its verb ("tell") categorised as
"search". Questions about the temperature had no route at all, so the language
model answered them, and the device log shows it inventing readings ("The
temperature outside is currently 20 degrees Celsius."). These tests pin the
deterministic routes down.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shugocore_agent import create_agent  # noqa: E402
from subsystems import CommandExecutor, IntentParser, MemoryManager  # noqa: E402
from subsystems.command_router import (CommandResult,  # noqa: E402
                                       default_handlers)
from subsystems.dialogue import DialogueState  # noqa: E402
from subsystems.intent import IntentType, UserIntent  # noqa: E402
from subsystems.tools import default_tools, TimerManager  # noqa: E402

# Phrasings that must reach a tool. The expected action_taken is exact: it is
# what the on-device smoke probes assert.
TOOLABLE = [
    ("what time is it", "time_report"),
    ("what's the time", "time_report"),
    ("tell me the time", "time_report"),
    ("do you have the time", "time_report"),
    ("current time", "time_report"),
    ("what's the date", "date_report"),
    ("what day is it", "date_report"),
    ("what is the temperature", "temperature_report"),
    ("how hot is it", "temperature_report"),
    ("is it hot outside", "temperature_report"),
    ("what's the weather", "weather_unavailable"),
    ("what's my battery", "battery_check"),
    ("check my battery", "battery_check"),
]

# Every phrasing the parser promotes to a COMMAND must land on a REGISTERED
# handler. "I can't do that yet, but I'm learning!" used to be the reward for a
# re-worded command; this invariant is what prevents that class of dead end.
COMMAND_PHRASES = [
    "set a timer for two seconds",
    "set a timer",
    "remind me to call Sam",
    "turn off the lights",
    "play some music",
    "what time is it",
    "what's the weather",
    "check my battery",
    "what's my battery",
    "what's the time",
    "tell me the time",
    "what is the temperature",
    "what's the date",
    "remember that I like tea",
    "forget about the tea",
    "recall my favorite color",
    "mesh status",
    "sync your memory with your peer",
]


def build_executor(calls, sensor=None):
    """A CommandExecutor wired exactly like the agent wires it."""
    tmp = tempfile.mkdtemp(prefix="routing_")
    memory = MemoryManager(tmp)
    registry = default_tools(
        timer_manager=TimerManager(persist_path=None),
        on_timer_fire=lambda label: None,
        battery_provider=lambda: 87.0,
        sensor_provider=lambda: (sensor if sensor is not None
                                 else {"cpu_temp_c": 41.2}),
        memory_provider=memory.facts,
    )
    real_call = registry.call

    def traced(name, **kwargs):
        calls.append(name)
        return real_call(name, **kwargs)

    registry.call = traced
    executor = CommandExecutor()
    executor.set_tool_registry(registry)
    for name, handler in default_handlers(registry).items():
        executor.register_handler(name, handler)
    return executor, registry


def routed_intent(parser, phrase):
    """Mirror the agent: a toolable question is re-routed as a command."""
    intent = parser.classify(phrase)
    topic = None
    if intent.intent_type.value in ("question", "chitchat"):
        topic = parser.tool_topic(phrase)
        if topic:
            intent = UserIntent(IntentType.COMMAND, phrase, confidence=0.9,
                                entities={"tool_topic": topic})
    return intent, topic


class TestToolableQuestionRouting(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.executor, self.registry = build_executor(self.calls)
        self.parser = IntentParser()

    def test_every_phrasing_reaches_its_handler(self):
        for phrase, expected in TOOLABLE:
            with self.subTest(phrase=phrase):
                intent, _ = routed_intent(self.parser, phrase)
                self.calls.clear()
                result = self.executor.execute(intent)
                self.assertEqual(result.action_taken, expected,
                                 f"{phrase!r} -> {result.action_taken}"
                                 f" ({result.response!r})")

    def test_measurement_questions_call_a_tool(self):
        for phrase, _ in TOOLABLE:
            if phrase.startswith("what's the weather"):
                continue  # no weather service is wired anywhere yet
            with self.subTest(phrase=phrase):
                intent, _ = routed_intent(self.parser, phrase)
                self.calls.clear()
                self.executor.execute(intent)
                self.assertTrue(self.calls, f"{phrase!r} called no tool")

    def test_time_is_read_once_per_query(self):
        intent, _ = routed_intent(self.parser, "what time is it")
        self.calls.clear()
        self.executor.execute(intent)
        self.assertEqual(self.calls.count("get_time"), 1)

    def test_every_promoted_command_lands_on_a_registered_handler(self):
        handlers = set(self.registry.names()) | {
            "timer", "weather", "time", "date", "temperature", "sensors",
            "battery", "search", "device", "memory", "mesh"}
        for phrase in COMMAND_PHRASES:
            with self.subTest(phrase=phrase):
                intent = self.parser.classify(phrase)
                verb = intent.entities.get("action_verb", "")
                category = self.executor._categorize(
                    verb, phrase.lower(), intent.entities.get("tool_topic"))
                self.assertIn(category, handlers,
                              f"{phrase!r} categorised as {category!r}")

    def test_search_is_honest_or_falls_through(self):
        found = self.executor.execute(
            self.parser.classify("search for the nearest cafe"))
        self.assertEqual(found.action_taken, "search_unavailable")
        self.assertIn("don't have a search service", found.response)
        story = self.executor.execute(
            self.parser.classify("tell me a story about the sea"))
        self.assertEqual(story.response, "")

    def test_unknown_command_falls_through_to_the_model(self):
        # The agent registers an empty-response fallback: the user no longer
        # hears "I can't do that yet, but I'm learning!".
        executor, _ = build_executor([])
        executor.set_fallback(lambda intent: CommandResult(
            success=True, response="", action_taken="unhandled_command"))
        result = executor.execute(
            self.parser.classify("send a postcard to the moon"))
        self.assertEqual(result.action_taken, "unhandled_command")
        self.assertEqual(result.response, "")


class TestTemperatureHonesty(unittest.TestCase):
    """A reading or an honest refusal — never a guess."""

    def _response(self, sensor):
        calls = []
        executor, _ = build_executor(calls, sensor=sensor)
        intent, _ = routed_intent(IntentParser(), "what is the temperature")
        return executor.execute(intent)

    def test_zero_reading_is_unavailable_not_zero_degrees(self):
        # The A51's ThermalMonitor reports 0 when it has nothing: that is "no
        # reading", not a measurement of 0 C.
        result = self._response({"cpu_temp_c": 0})
        self.assertEqual(result.action_taken, "temperature_unavailable")
        self.assertNotIn("0.0 degrees", result.response)

    def test_missing_sensor_is_unavailable(self):
        result = self._response({"mem_avail_mb": 1200})
        self.assertEqual(result.action_taken, "temperature_unavailable")
        self.assertIn("can't measure", result.response)

    def test_device_temperature_is_reported_as_device_temperature(self):
        result = self._response({"cpu_temp_c": 41.2})
        self.assertEqual(result.action_taken, "temperature_report")
        self.assertIn("41.2", result.response)
        self.assertIn("not the room", result.response)

    def test_ambient_temperature_is_reported_plainly(self):
        result = self._response({"ambient_temp_c": 21.5})
        self.assertEqual(result.action_taken, "temperature_report")
        self.assertIn("21.5", result.response)

class TestNoFabricatedMeasurement(unittest.TestCase):
    """The model may never supply a reading that no tool produced."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="guard_")
        cls.agent = create_agent(device_caps="Exynos-1380",
                                 api_url="http://127.0.0.1:11434",
                                 data_dir=cls._tmp)

    def guard(self, text):
        return self.agent._guard_fabricated_measurement(text)

    def test_invented_temperature_is_refused(self):
        out = self.guard("The temperature outside is currently 20 degrees "
                         "Celsius.")
        self.assertNotIn("20 degrees", out)
        self.assertIn("can't measure", out)

    def test_invented_temperature_variant_is_refused(self):
        self.assertIn("can't measure", self.guard("It's 75 degrees outside."))
        self.assertIn("can't measure",
                      self.guard("The temperature is 12 C right now."))

    def test_invented_battery_reading_is_refused(self):
        self.assertIn("can't measure", self.guard("Your battery is at 87%."))

    def test_ordinary_text_passes_through(self):
        text = "I'd rather tell you a story about a lighthouse."
        self.assertEqual(self.guard(text), text)

    def test_empty_text_yields_empty(self):
        self.assertEqual(self.guard(""), "")


class TestClarifyKeepsWordNumbers(unittest.TestCase):
    """The timer clarify loop must accept "five minutes", not only "5"."""

    def test_word_duration_resolves(self):
        state = DialogueState()
        state.begin_clarification("timer_duration", "set a timer", {})
        intent = state.resolve_answer("five minutes")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.entities.get("duration_value"), 5)
        self.assertEqual(intent.entities.get("duration_unit"), "minute")

    def test_digit_duration_still_resolves(self):
        state = DialogueState()
        state.begin_clarification("timer_duration", "set a timer", {})
        intent = state.resolve_answer("5 minutes")
        self.assertEqual(intent.entities.get("duration_value"), 5)

    def test_article_duration_resolves_to_one(self):
        state = DialogueState()
        state.begin_clarification("timer_duration", "set a timer", {})
        intent = state.resolve_answer("a minute")
        self.assertEqual(intent.entities.get("duration_value"), 1)

    def test_unrelated_sentence_is_not_an_answer(self):
        state = DialogueState()
        state.begin_clarification("timer_duration", "set a timer", {})
        self.assertIsNone(state.resolve_answer("what's the weather"))


class TestToolTopicDetection(unittest.TestCase):
    def test_topics_are_recognised(self):
        parser = IntentParser()
        cases = {
            "what's the time": "time",
            "tell me the time": "time",
            "do you have the time": "time",
            "what day is it": "date",
            "how hot is it": "temperature",
            "what's the weather like": "weather",
            "how much battery do I have": "battery",
        }
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(parser.tool_topic(phrase), expected)

    def test_non_measurements_are_not_hijacked(self):
        parser = IntentParser()
        for phrase in ("I feel cold today", "tell me a story about the sea",
                       "what do you think about jazz music",
                       "remember that the meeting time is noon"):
            with self.subTest(phrase=phrase):
                self.assertIsNone(parser.tool_topic(phrase))


if __name__ == "__main__":
    unittest.main()
