"""
Command execution router.

When the intent parser detects a COMMAND, this module figures out which tool
or action to invoke. It bridges the gap between "the user wants something
done" and "the right executor runs."
"""
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional

from subsystems.fallback import RetryPolicy, retry_call
from subsystems.intent import IntentType, UserIntent

logger = logging.getLogger(__name__)


class CommandResult:
    """Result of attempting to execute a command."""

    def __init__(self, success: bool, response: str,
                 action_taken: Optional[str] = None,
                 data: Optional[Dict[str, Any]] = None):
        self.success = success
        self.response = response
        self.action_taken = action_taken
        self.data = data or {}

    def __repr__(self) -> str:
        return f"CommandResult(ok={self.success}, resp={self.response[:50]!r})"


class CommandExecutor:
    """Routes classified commands to the right handler."""

    def __init__(self):
        self._handlers: Dict[str, Callable[[UserIntent], CommandResult]] = {}
        self._fallback: Optional[Callable[[UserIntent], CommandResult]] = None
        self._tools: Any = None
        self._retry_policy = RetryPolicy(max_attempts=2, base_backoff_secs=0.1)

    def set_tool_registry(self, registry: Any) -> None:
        """Give handlers access to real tools (Phase 3.2)."""
        self._tools = registry

    @property
    def tools(self) -> Any:
        return self._tools

    def register_handler(self, name: str,
                         handler: Callable[[UserIntent], CommandResult]) -> None:
        self._handlers[name] = handler

    def set_fallback(self, handler: Callable[[UserIntent], CommandResult]) -> None:
        self._fallback = handler

    def execute(self, intent: UserIntent) -> CommandResult:
        """Execute a classified intent."""
        if intent.intent_type == IntentType.COMMAND:
            return self._execute_command(intent)
        elif intent.intent_type == IntentType.QUESTION:
            return CommandResult(success=True, response="", action_taken="question")
        elif intent.intent_type == IntentType.GREETING:
            return CommandResult(success=True, response="", action_taken="greeting")
        elif intent.intent_type == IntentType.FAREWELL:
            return CommandResult(success=True, response="", action_taken="farewell")
        elif intent.intent_type == IntentType.CHITCHAT:
            return CommandResult(success=True, response="", action_taken="chitchat")
        else:
            return CommandResult(
                success=False,
                response="I'm not sure what you mean. Could you rephrase that?",
                action_taken="confused")

    def _execute_command(self, intent: UserIntent) -> CommandResult:
        """Route a command to the right handler."""
        entities = intent.entities
        verb = entities.get("action_verb", "")
        transcript_lower = intent.transcript.lower()
        category = self._categorize(verb, transcript_lower,
                                    entities.get("tool_topic"))
        handler = self._handlers.get(category)
        if handler:
            # Phase 3.3: retry transient failures once with backoff, then
            # degrade to a graceful error instead of surfacing a crash.
            ok, result = retry_call(handler, self._retry_policy, intent)
            if ok:
                return result
            logger.error("Command handler '%s' failed after retry: %r",
                         category, result)
            return CommandResult(
                success=False,
                response="I had trouble with that. Could you try again?",
                action_taken=f"{category}_error")
        if self._fallback:
            return self._fallback(intent)
        return CommandResult(
            success=False,
            response="I can't do that yet, but I'm learning!",
            action_taken="unhandled_command")

    # v1.30.5: phrasing-independent topic keywords, used for command forms
    # (where the intent layer supplies no topic) so a natural re-wording can no
    # longer miss its handler — "tell me the time" used to route to "search".
    _TOPIC_KEYWORDS = (
        ("temperature", ("temperature", "how hot", "how cold", "thermometer",
                          "degrees", "hot outside", "cold outside",
                          "is it hot", "is it cold")),
        ("weather", ("weather", "forecast", "rain", "snow")),
        ("date", ("what's the date", "what is the date", "what day is it",
                   "todays date", "today's date")),
        ("time", ("the time", "time is it", "current time", "clock")),
        ("battery", ("battery", "charge level")),
        ("sensors", ("sensor", "telemetry", "accelerometer", "gyroscope")),
    )

    def _categorize(self, verb: str, transcript: str,
                    tool_topic: Optional[str] = None) -> str:
        """Determine the command category.

        v1.30.5: a topic supplied by the intent layer (`tool_topic`) wins, so
        the phrasing of a question can no longer decide whether a tool is
        reached. Keyword rules remain for the command forms the parser already
        promotes; the explicit verbs (timer, mesh, memory) are checked first so
        a topic keyword inside them cannot shadow them.
        """
        if "timer" in transcript or "remind" in transcript:
            return "timer"
        # Fleet memory mesh: "sync your memory with your peer", "mesh status".
        # Checked before memory so "sync your memory" is not read as a fact op.
        if ("mesh" in transcript
                or "sync your memory" in transcript
                or "sync memories" in transcript
                or "share what you know" in transcript
                or "share your memory" in transcript
                or ("sync" in transcript and "peer" in transcript)):
            return "mesh"
        # Phase 4: durable-memory commands. Checked before the topic keywords
        # so "remember the time of the meeting" stays a memory command.
        if ("remember" in transcript or "forget" in transcript
                or verb in ("remember", "recall", "forget")):
            return "memory"
        if tool_topic:
            return tool_topic
        for category, keywords in self._TOPIC_KEYWORDS:
            if any(keyword in transcript for keyword in keywords):
                return category
        if verb in ("search", "find", "show", "tell"):
            return "search"
        if verb in ("turn", "open", "close", "play", "pause", "start", "stop"):
            return "device"
        return "unknown"


def default_handlers(tools: Any = None) -> Dict[str, Callable[[UserIntent], CommandResult]]:
    """Build the default set of command handlers.

    Pass a ToolRegistry (Phase 3.2) to enable real actions: timer setup,
    clock time, battery level, jokes. Without tools, handlers degrade to
    friendly placeholder responses.
    """
    handlers: Dict[str, Callable[[UserIntent], CommandResult]] = {}

    def _tool(name: str, **kwargs: Any) -> Optional[CommandResult]:
        """Call a tool; returns None when the tool produced usable output,
        or a CommandResult carrying the tool's failure/absence response."""
        if tools is None:
            return CommandResult(
                success=False,
                response="I need my tool kit for that.",
                action_taken=f"{name}_unavailable")
        result = tools.call(name, **kwargs)
        if result.ok:
            return None
        return CommandResult(success=False, response=result.output,
                             action_taken=f"{name}_unavailable")

    def handle_timer(intent: UserIntent) -> CommandResult:
        entities = intent.entities
        value = entities.get("duration_value")
        unit = entities.get("duration_unit", "minute")
        if value:
            failed = _tool("set_timer", duration_value=value,
                           duration_unit=unit)
            if failed:
                return failed
            return CommandResult(
                success=True,
                response=f"Got it — timer set for {value} {unit}"
                         f"{'s' if value != 1 else ''}.",
                action_taken="timer_set",
                data={"duration_value": value, "duration_unit": unit})
        # v1.28.1: number words ("two seconds", "a minute", "an hour") so
        # headless debug injection reaches the timer handler like digits do.
        word_match = re.search(
            r"(zero|one|two|three|four|five|six|seven|eight|nine|ten|"
            r"eleven|twelve|fifteen|twenty|thirty|a|an)"
            r"\s*(minute|min|second|sec|hour|hr)s?",
            intent.transcript, re.IGNORECASE)
        if word_match:
            word = word_match.group(1).lower()
            if word in ("a", "an"):
                wvalue: Any = 1
            else:
                wvalue = {
                    "zero": 0, "one": 1, "two": 2, "three": 3,
                    "four": 4, "five": 5, "six": 6, "seven": 7,
                    "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
                    "twelve": 12, "fifteen": 15, "twenty": 20,
                    "thirty": 30,
                }.get(word, 1)
            wunit = word_match.group(2).lower()
            failed = _tool("set_timer", duration_value=wvalue,
                           duration_unit=wunit)
            if failed:
                return failed
            return CommandResult(
                success=True,
                response=(f"Got it — timer set for {wvalue} {wunit}"
                          f"{'s' if wvalue != 1 else ''}."),
                action_taken="timer_set",
                data={"duration_value": wvalue, "duration_unit": wunit})
        # Phase 3.4: multi-turn — ask for the missing slot.
        return CommandResult(
            success=True,
            response="Timer for how long?",
            action_taken="timer_clarify")

    def handle_weather(intent: UserIntent) -> CommandResult:
        return CommandResult(
            success=True,
            response=("I don't have weather data yet, but I'm working on "
                      "connecting to a service."),
            action_taken="weather_unavailable")

    def handle_time(intent: UserIntent) -> CommandResult:
        # v1.30.5: ONE tool call. The old probe-then-call shape executed
        # get_time twice per query (harmless for a clock read, wrong for
        # anything with a side effect).
        if tools is not None and tools.has("get_time"):
            result = tools.call("get_time")
            if result.ok:
                return CommandResult(success=True, response=result.output,
                                     action_taken="time_report")
        return CommandResult(
            success=True,
            response=f"It's {time.strftime('%I:%M %p')}.",
            action_taken="time_report")

    def handle_date(intent: UserIntent) -> CommandResult:
        """v1.30.5: get_date existed but no category could reach it."""
        if tools is not None and tools.has("get_date"):
            result = tools.call("get_date")
            if result.ok:
                return CommandResult(success=True, response=result.output,
                                     action_taken="date_report")
        return CommandResult(
            success=True,
            response=f"Today is {time.strftime('%A, %B %d')}.",
            action_taken="date_report")

    def handle_temperature(intent: UserIntent) -> CommandResult:
        """v1.30.5: a REAL reading or an honest refusal — never a guess.

        Mirrors handle_battery: the registry is called once, and an
        unavailable reading is reported as unavailable rather than letting the
        language model invent a number (which is what the device log shows
        happening: "The temperature outside is currently 20 degrees Celsius").
        """
        if tools is not None and tools.has("get_temperature"):
            result = tools.call("get_temperature")
            return CommandResult(
                success=result.ok, response=result.output,
                action_taken=("temperature_report" if result.ok
                              else "temperature_unavailable"),
                data=result.data or {})
        return CommandResult(
            success=True,
            response=("I can't measure a temperature here — this device has no "
                      "ambient sensor and no weather service is connected."),
            action_taken="temperature_unavailable")

    def handle_sensors(intent: UserIntent) -> CommandResult:
        if tools is not None and tools.has("check_sensors"):
            result = tools.call("check_sensors")
            return CommandResult(
                success=result.ok, response=result.output,
                action_taken=("sensor_report" if result.ok
                              else "sensor_unavailable"),
                data=result.data or {})
        return CommandResult(
            success=True,
            response="I can't read the sensors right now.",
            action_taken="sensor_unavailable")

    def handle_battery(intent: UserIntent) -> CommandResult:
        if tools is not None and tools.has("get_battery"):
            result = tools.call("get_battery")
            return CommandResult(success=result.ok, response=result.output,
                                 action_taken="battery_check" if result.ok
                                 else "battery_unavailable")
        return CommandResult(
            success=True,
            response=("I need my tool kit to read the battery level."),
            action_taken="battery_unavailable")

    def handle_search(intent: UserIntent) -> CommandResult:
        """v1.30.5: the placeholder used to promise "I can search for that"
        with no search backend wired. A "tell/show" request that is not a
        lookup is left to the conversational path (an empty response falls
        through), and an explicit lookup is answered honestly."""
        lowered = intent.transcript.lower()
        if not any(word in lowered for word in ("search", "find", "look up",
                                                "google")):
            return CommandResult(success=True, response="",
                                 action_taken="not_a_search")
        return CommandResult(
            success=True,
            response=("I don't have a search service connected, so I can't "
                      "look that up."),
            action_taken="search_unavailable")

    def handle_device(intent: UserIntent) -> CommandResult:
        verb = intent.entities.get("action_verb", "do that")
        return CommandResult(
            success=False,
            response=(f"I can't {verb} anything yet, but I'm learning new "
                      "tricks every day!"),
            action_taken="device_unavailable")

    def handle_memory(intent: UserIntent) -> CommandResult:
        """Phase 4: remember / recall / forget, backed by FactMemory."""
        entities = intent.entities
        text = (entities.get("memory_text") or "").strip()
        transcript_l = intent.transcript.lower()

        if "forget" in transcript_l or entities.get("action_verb") == "forget":
            if len(text) < 2:
                return CommandResult(
                    success=True, response="Forget what?",
                    action_taken="memory_clarify")
            if tools is None or not tools.has("forget_facts"):
                return CommandResult(
                    success=False,
                    response="I need my memory module for that.",
                    action_taken="forget_facts_unavailable")
            result = tools.call("forget_facts", keyword=text)
            return CommandResult(success=result.ok, response=result.output,
                                 action_taken="memory_forget" if result.ok
                                 else "memory_forget_failed",
                                 data={"keyword": text})

        if ("recall" in transcript_l
                or entities.get("action_verb") == "recall"
                or "what do you remember" in transcript_l
                or "what do you know about me" in transcript_l):
            keyword = text if len(text) >= 2 else ""
            if tools is None or not tools.has("recall_facts"):
                return CommandResult(
                    success=False,
                    response="I need my memory module for that.",
                    action_taken="recall_facts_unavailable")
            result = tools.call("recall_facts", keyword=keyword)
            return CommandResult(success=result.ok, response=result.output,
                                 action_taken="memory_recall" if result.ok
                                 else "memory_recall_failed",
                                 data={"keyword": keyword})

        # Store.
        if len(text) < 2:
            return CommandResult(
                success=True, response="Remember what?",
                action_taken="memory_clarify")
        if tools is None or not tools.has("remember_fact"):
            return CommandResult(
                success=False,
                response="I need my memory module for that.",
                action_taken="remember_fact_unavailable")
        result = tools.call("remember_fact", text=text)
        return CommandResult(success=result.ok, response=result.output,
                             action_taken="memory_store" if result.ok
                             else "memory_store_failed",
                             data={"fact": text})

    def handle_mesh(intent: UserIntent) -> CommandResult:
        """Fleet memory mesh: report peers or pull a peer's Tier 2 memory.

        Backed by the ``mesh_status`` / ``mesh_sync`` tools, which the agent
        wires to its live ShugoNet runtime. ``mesh_sync`` merges the peer's
        Tier 2 facts into this agent's own memory (idempotent, provenance
        tagged), so the combined knowledge is immediately recallable here.
        """
        if tools is None:
            return CommandResult(
                success=False,
                response="I need my mesh module for that.",
                action_taken="mesh_unavailable")
        transcript_l = intent.transcript.lower()
        if ("status" in transcript_l or "who" in transcript_l
                or "peers" in transcript_l):
            if not tools.has("mesh_status"):
                return CommandResult(
                    success=False,
                    response="I need my mesh module for that.",
                    action_taken="mesh_status_unavailable")
            result = tools.call("mesh_status")
            return CommandResult(
                success=result.ok, response=result.output,
                action_taken="mesh_status" if result.ok else "mesh_status_failed",
                data=result.data or {})
        if not tools.has("mesh_sync"):
            return CommandResult(
                success=False,
                response="I need my mesh module for that.",
                action_taken="mesh_sync_unavailable")
        peer = str(intent.entities.get("peer_id") or "").strip() or None
        result = tools.call("mesh_sync", peer=peer)
        return CommandResult(
            success=result.ok, response=result.output,
            action_taken="mesh_sync" if result.ok else "mesh_sync_failed",
            data=result.data or {})

    handlers["timer"] = handle_timer
    handlers["weather"] = handle_weather
    handlers["time"] = handle_time
    handlers["date"] = handle_date
    handlers["temperature"] = handle_temperature
    handlers["sensors"] = handle_sensors
    handlers["battery"] = handle_battery
    handlers["search"] = handle_search
    handlers["device"] = handle_device
    handlers["memory"] = handle_memory
    handlers["mesh"] = handle_mesh
    return handlers
