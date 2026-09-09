"""
Tool registry & execution (Phase 3.2).

Gives Shugo real capabilities beyond speech: time/date, background timers
(polled each tick — no fragile sleep threads), jokes, plus agent-injected
closures for device state (battery, sensors).

    Tool         — name + handler + short description (for future LLM tool-use)
    ToolResult   — ok / output / structured data
    ToolRegistry — register, call, describe
    TimerManager — fire-and-check timers for the agent tick loop

Pure stdlib; the agent shell injects any callbacks that need agent state.
"""
import json
import logging
import os
import random
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

HandlerT = Callable[..., "ToolResult"]


class ToolResult:
    """Outcome of calling a tool."""

    def __init__(self, ok: bool, output: str = "",
                 data: Optional[Dict[str, Any]] = None,
                 error: Optional[str] = None):
        self.ok = ok
        self.output = output
        self.data = data or {}
        self.error = error

    @classmethod
    def ok_result(cls, output: str = "",
                  data: Optional[Dict[str, Any]] = None) -> "ToolResult":
        return cls(ok=True, output=output, data=data)

    @classmethod
    def err_result(cls, message: str = "") -> "ToolResult":
        return cls(ok=False, output=message or "That didn't work.", error=message)

    def __repr__(self) -> str:
        return f"ToolResult(ok={self.ok}, out={self.output[:40]!r})"


class Tool:
    """A named, callable capability."""

    def __init__(self, name: str, handler: HandlerT,
                 description: str = "",
                 parameters: Optional[Dict[str, str]] = None):
        self.name = name
        self.handler = handler
        self.description = description
        self.parameters = parameters or {}

    def call(self, **kwargs: Any) -> ToolResult:
        try:
            return self.handler(**kwargs)
        except Exception as exc:  # keep the registry honest
            logger.error("Tool '%s' raised: %s", self.name, exc)
            return ToolResult.err_result(f"{self.name} failed: {exc}")

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "parameters": self.parameters}

    def __repr__(self) -> str:
        return f"Tool({self.name})"


class ToolRegistry:
    """Holds tools and dispatches calls to them."""

    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def register_handler(self, name: str, handler: HandlerT,
                         description: str = "",
                         parameters: Optional[Dict[str, str]] = None) -> None:
        self.register(Tool(name=name, handler=handler,
                           description=description, parameters=parameters))

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def call(self, name: str, **kwargs: Any) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.err_result(f"I don't have a {name} tool yet.")
        return tool.call(**kwargs)

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> List[str]:
        return sorted(self._tools)

    def describe_all(self) -> List[Dict[str, Any]]:
        return [t.describe() for t in self._tools.values()]

    def __len__(self) -> int:
        return len(self._tools)


class Timer:
    """A background timer the agent polls via TimerManager.check()."""

    def __init__(self, duration_seconds: float, label: str = "Timer",
                 timer_id: Optional[str] = None,
                 on_fire: Optional[Callable[[str], None]] = None):
        self.timer_id = timer_id or uuid.uuid4().hex[:8]
        self.duration_seconds = float(duration_seconds)
        self.label = label or "Timer"
        self.on_fire = on_fire
        self.created_at = time.time()
        self.due_at = self.created_at + self.duration_seconds
        self.fired_at: Optional[float] = None
        # Phase 4: set when a timer is found expired after a restart.
        self.fired_while_away: bool = False

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.due_at - time.time())

    @property
    def fired(self) -> bool:
        return self.fired_at is not None

    def fire(self) -> None:
        self.fired_at = time.time()
        if self.on_fire:
            try:
                self.on_fire(self.label)
            except Exception as exc:
                logger.error("Timer on_fire callback failed: %s", exc)

    def to_dict(self) -> Dict[str, Any]:
        return {"timer_id": self.timer_id, "label": self.label,
                "remaining_seconds": round(self.remaining_seconds, 1),
                "duration_seconds": self.duration_seconds,
                "fired": self.fired}

    def to_persist_dict(self) -> Dict[str, Any]:
        """Phase 4: disk form — uses absolute epochs so a restart can
        reconstruct exact remaining time (and detect expiry)."""
        return {"timer_id": self.timer_id, "label": self.label,
                "duration_seconds": self.duration_seconds,
                "due_at": self.due_at}

    @classmethod
    def from_persist_dict(cls, data: Dict[str, Any]) -> Optional["Timer"]:
        """Rebuild a timer from its persisted form. Returns None for
        malformed entries. Expired-at-load timers load fine — restore()
        detects and flags them as fired_while_away."""
        try:
            timer_id = data.get("timer_id")
            label = data.get("label") or "Timer"
            duration = float(data.get("duration_seconds", 0))
            due_at = float(data.get("due_at", 0))
            if not timer_id or due_at <= 0:
                return None
            timer = cls(duration, label, timer_id=str(timer_id))
            timer.due_at = due_at
            return timer
        except (TypeError, ValueError):
            return None

    def __repr__(self) -> str:
        return (f"Timer({self.label!r}, {self.remaining_seconds:.0f}s "
                f"{'fired' if self.fired else 'remaining'})")


class TimerManager:
    """Owns active timers. The agent calls check() each tick; any timer that
    is due fires (optionally invoking its on_fire callback) and is removed.

    No sleep threads — the agent loop is the clock. This keeps shutdowns
    clean and avoids thread leaks on devices.

    Phase 4: optional disk persistence. When ``persist_path`` is set, every
    mutation is mirrored to that file and ``restore()`` reloads pending
    timers after a process restart. Timers that expired while the process
    was away are reported by restore() and re-inserted with
    ``fired_while_away=True`` so the next check() announces them instead of
    silently dropping them.
    """

    def __init__(self, persist_path: Optional[str] = None):
        self._timers: Dict[str, Timer] = {}
        self.persist_path = persist_path
        self._load()

    # -- persistence (Phase 4) --------------------------------------------
    def save(self) -> None:
        """Mirror pending timers to disk (best-effort, never raises)."""
        path = self.persist_path
        if not path:
            return
        try:
            payload = [t.to_persist_dict() for t in self._timers.values()
                       if not t.fired]
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, path)
        except Exception as exc:
            logger.warning("TimerManager save failed: %s", exc)

    def _load(self) -> None:
        """Load persisted timers into memory at construction time."""
        path = self.persist_path
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for item in data if isinstance(data, list) else []:
                timer = Timer.from_persist_dict(item)
                if timer is not None and not timer.fired:
                    self._timers[timer.timer_id] = timer
        except Exception as exc:
            logger.warning("TimerManager load failed: %s", exc)

    def restore(self) -> List[Timer]:
        """Post-load pass: report timers that expired while away.

        Expired timers are re-inserted with ``fired_while_away=True`` so the
        agent's next check() fires them (and can announce the miss); they
        are returned here for logging. Call once at bootstrap, after the
        manager was constructed and any ``on_fire`` plumbing is in place.
        """
        missed: List[Timer] = []
        for timer in self._timers.values():
            if not timer.fired and timer.remaining_seconds <= 0:
                timer.fired_while_away = True
                missed.append(timer)
        return missed

    def _persist(self) -> None:
        if self.persist_path:
            self.save()

    # -- timer operations ---------------------------------------------------
    def set(self, duration_seconds: float, label: str = "Timer",
            timer_id: Optional[str] = None,
            on_fire: Optional[Callable[[str], None]] = None) -> Timer:
        seconds = max(0.0, float(duration_seconds))
        timer = Timer(seconds, label, timer_id=timer_id, on_fire=on_fire)
        if seconds > 0:
            self._timers[timer.timer_id] = timer
        else:
            # Zero-duration: fire immediately (and keep a record for the ack).
            timer.fire()
            self._timers[timer.timer_id] = timer
        self._persist()
        return timer

    def cancel(self, timer_id: str) -> Optional[Timer]:
        timer = self._timers.pop(timer_id, None)
        self._persist()
        return timer

    def get(self, timer_id: str) -> Optional[Timer]:
        return self._timers.get(timer_id)

    def list_pending(self) -> List[Timer]:
        return [t for t in self._timers.values() if not t.fired]

    def check(self) -> List[Timer]:
        """Fire and remove any due timers; return them in creation order."""
        fired: List[Timer] = []
        for timer_id in list(self._timers):
            timer = self._timers[timer_id]
            if timer.fired:
                # An immediately-fired (zero-duration) timer — reap it.
                fired.append(timer)
                self._timers.pop(timer_id, None)
            elif timer.remaining_seconds <= 0:
                timer.fire()
                fired.append(timer)
                self._timers.pop(timer_id, None)
        if fired:
            self._persist()
        return fired

    def count(self) -> int:
        return len(self._timers)

    def clear_all(self) -> None:
        self._timers.clear()
        self._persist()
_JOKES = [
    "Why did the robot go to art school? It wanted to draw better circuits.",
    "I told my phone a joke. It sent it to a group chat. We don't speak anymore.",
    "Why don't robots step on LEGOs? Because they have sensitive feet — well, wheels.",
    "What's a computer's favorite snack? Microchips.",
    "Why was the math book sad? Too many problems — I can relate.",
    "I would tell you a UDP joke, but you might not get it.",
]


def default_tools(timer_manager: Optional[TimerManager] = None,
                  on_timer_fire: Optional[Callable[[str], None]] = None,
                  battery_provider: Optional[Callable[[], Any]] = None,
                  sensor_provider: Optional[Callable[[], Any]] = None,
                  memory_provider: Optional[Any] = None) -> ToolRegistry:
    """Build the standard tool set.

    Pure tools are created here. Device-bound tools (battery/sensors) take
    provider callables so the agent can feed in live state without the tools
    module importing the shell. Phase 4: ``memory_provider`` enables the
    durable-memory tools (remember/recall/forget) backed by FactMemory.
    """
    timers = timer_manager or TimerManager()
    registry = ToolRegistry()

    def _now_hms() -> str:
        return time.strftime("%I:%M %p")

    def _today() -> str:
        return time.strftime("%A, %B %d")

    registry.register_handler(
        "get_time", lambda: ToolResult.ok_result(f"It's {_now_hms()}."),
        description="Current local time",
    )
    registry.register_handler(
        "get_date", lambda: ToolResult.ok_result(f"Today is {_today()}."),
        description="Current local date",
    )

    def _set_timer(duration_value: float, duration_unit: str = "minute",
                   label: str = "Timer") -> ToolResult:
        unit_seconds = {"second": 1, "sec": 1, "minute": 60, "min": 60,
                        "hour": 3600, "hr": 3600}.get(str(duration_unit).lower(), 60)
        seconds = max(1, int(float(duration_value)) * unit_seconds)
        timer = timers.set(seconds, label, on_fire=on_timer_fire)
        return ToolResult.ok_result(
            f"Got it — {label.lower()} set for {duration_value} "
            f"{duration_unit}{'s' if float(duration_value) != 1 else ''}.",
            data={"timer_id": timer.timer_id, "seconds": seconds})

    registry.register_handler(
        "set_timer", _set_timer,
        description="Set a background timer",
        parameters={"duration_value": "number", "duration_unit": "str"},
    )
    registry.register_handler(
        "list_timers", lambda: ToolResult.ok_result(
            "No timers running."
            if not timers.list_pending()
            else "Alive: " + ", ".join(
                f"{t.label} ({t.remaining_seconds:.0f}s left)"
                for t in timers.list_pending()),
            data={"count": len(timers.list_pending())}),
        description="List active timers",
    )

    def _cancel_timer(timer_id: str) -> ToolResult:
        timer = timers.cancel(timer_id)
        if timer is None:
            return ToolResult.err_result("I couldn't find that timer.")
        return ToolResult.ok_result(f"Cancelled {timer.label}.",
                                    data={"timer_id": timer_id})

    registry.register_handler("cancel_timer", _cancel_timer,
                              description="Cancel an active timer by id")

    def _joke() -> ToolResult:
        return ToolResult.ok_result(random.choice(_JOKES))

    registry.register_handler("tell_joke", _joke, description="Tell a joke")

    if battery_provider is not None:
        def _battery() -> ToolResult:
            try:
                level = battery_provider()
                if level is None:
                    return ToolResult.err_result(
                        "Battery data isn't available right now.")
                return ToolResult.ok_result(
                    f"Your battery is at {level}%.", data={"battery": level})
            except Exception as exc:
                return ToolResult.err_result(f"Battery check failed: {exc}")

        registry.register_handler("get_battery", _battery,
                                  description="Read battery level")

    if sensor_provider is not None:
        def _sensors() -> ToolResult:
            try:
                data = sensor_provider()
                if not data:
                    return ToolResult.err_result(
                        "Sensor data isn't available right now.")
                parts = ", ".join(f"{k}: {v}" for k, v in data.items())
                return ToolResult.ok_result(f"Sensors — {parts}.", data=data)
            except Exception as exc:
                return ToolResult.err_result(f"Sensor check failed: {exc}")

        registry.register_handler("check_sensors", _sensors,
                                  description="Read current sensor readings")

    # Phase 4 — durable memory tools, backed by the agent's FactMemory.
    if memory_provider is not None:
        def _remember(text: str) -> ToolResult:
            try:
                stored = memory_provider.remember_explicit(text)
                if not stored:
                    return ToolResult.err_result(
                        "I didn't catch what to remember — say it again?")
                return ToolResult.ok_result(
                    f"Got it — I'll remember that.",
                    data={"fact": stored})
            except Exception as exc:
                return ToolResult.err_result(f"Remember failed: {exc}")

        def _recall(keyword: str = "") -> ToolResult:
            try:
                kw = (keyword or "").strip()
                rows = (memory_provider.facts_about(kw, limit=3)
                        if kw else memory_provider.recall(limit=4))
                if not rows:
                    return ToolResult.ok_result(
                        "Nothing in my memory matches that yet.")
                text = "; ".join(r["text"] for r in rows)
                return ToolResult.ok_result(
                    f"Here's what I remember: {text}.",
                    data={"count": len(rows)})
            except Exception as exc:
                return ToolResult.err_result(f"Recall failed: {exc}")

        def _forget(keyword: str) -> ToolResult:
            try:
                removed = memory_provider.forget(keyword)
                if not removed:
                    return ToolResult.ok_result(
                        f"Nothing matched {keyword!r} — memory untouched.")
                return ToolResult.ok_result(
                    "Done — forgotten.",
                    data={"removed": removed})
            except Exception as exc:
                return ToolResult.err_result(f"Forget failed: {exc}")

        registry.register_handler(
            "remember_fact", _remember,
            description="Store a fact the user asked to remember",
            parameters={"text": "str"})
        registry.register_handler(
            "recall_facts", _recall,
            description="Recall stored facts, optionally about a keyword",
            parameters={"keyword": "str"})
        registry.register_handler(
            "forget_facts", _forget,
            description="Forget facts matching a keyword",
            parameters={"keyword": "str"})

    # Expose the timer manager for agent tick-loop polling.
    registry._timer_manager = timers  # type: ignore[attr-defined]
    return registry