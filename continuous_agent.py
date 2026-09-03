"""
ShugoCore Continuous Agent Daemon
==================================

The "press go" entry point for continuous synthetic functional agency.
Embodies the README orchestration loop as a runnable process:

    1. OBSERVE      task arrives; reasoning tokens enter Tier 0 scratchpad
    2. GATE         Tier 3 invariants check the action before anything runs
    3. DECIDE       models are selected and aggregated, enriched with Tier 2 context
    4. EXECUTE      the execution layer performs the tool / API interaction
    5. EVALUATE     reinforcement learning turns the outcome into a reward signal
    6. RECORD       the event lands in the Tier 1 episodic buffer
    7. CONSOLIDATE  a decoupled worker compresses episodes into Tier 2 facts,
                    decays stale salience and prunes forgotten knowledge

The loop is bounded in four axes (matching the architecture invariants):

- **Iterations** - ``--max-iterations`` caps the total number of loop passes
  (default: unlimited, runs forever).
- **Deadline**  - ``--max-seconds`` caps wall-clock runtime.
- **Budget**    - each task still flows through the governor's per-task step
  budget and deadline, so a single pathological task cannot stall the loop.
- **Safety**    - the fallback controller can latch PAUSED / SAFE_STATE /
  HALTED at any moment; the loop honors the governor state and stops
  generating new tasks when PAUSED.

Example::

    python3 continuous_agent.py --interval 2.0 --max-iterations 1000

Or from Python::

    from continuous_agent import ContinuousAgent
    agent = ContinuousAgent(models=[...], interval=2.0, max_iterations=100)
    agent.start()
    agent.await_stop()
"""

import argparse
import logging
import signal
import threading
import time
from typing import Any, Dict, List, Optional

from decision_engine import DecisionEngine
from state_machine import AgentState
from telemetry import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer("shugocore.continuous")


class ContinuousAgent:
    """
    Continuous observe-act loop over a policy-gated DecisionEngine.

    The loop reads from a task source (queue, iterator or autonomous
    generator), executes each task through the single gated path, records
    the outcome in Tier 1 memory, and lets the engine's decoupled
    maintenance worker consolidate Tier 1 -> Tier 2 continuously.
    """

    def __init__(self, engine: Optional[DecisionEngine] = None,
                 models: Optional[List[Dict[str, Any]]] = None,
                 vector_db_config: Optional[Dict[str, Any]] = None,
                 task_source: Optional[Any] = None,
                 interval: float = 1.0,
                 max_iterations: Optional[int] = None,
                 max_seconds: Optional[float] = None,
                 memory_db_path: Optional[str] = None,
                 audit_path: Optional[str] = None,
                 episodic_journal_path: Optional[str] = None,
                 **engine_kwargs: Any):
        # Build the engine unless one was injected (tests / embedding).
        if engine is not None:
            self.engine = engine
        else:
            self.engine = DecisionEngine(
                models or [],
                vector_db_config or {"type": "chroma"},
                memory_db_path=memory_db_path,
                audit_path=audit_path,
                episodic_journal_path=episodic_journal_path,
                **engine_kwargs,
            )
        # A task source is either an iterator/queue of task dicts or a
        # callable returning a task (or None when idle). With no source, the
        # engine's Autonomy module generates tasks autonomously.
        self.task_source = task_source
        self.interval = max(0.01, float(interval))
        self.max_iterations = (int(max_iterations) if max_iterations else None)
        self.max_seconds = (float(max_seconds) if max_seconds else None)
        if self.max_seconds is not None:
            self.max_seconds = max(0.0, self.max_seconds)

        self._stop_event = threading.Event()
        self._loops_completed = 0
        self._total_tasks = 0
        self._successes = 0
        self._failures = 0
        self._started_at = 0.0
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start the continuous loop in a background thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._run_loop, name="continuous-agent", daemon=True)
        self._thread.start()
        logger.info("Continuous agent started")

    def stop(self, timeout: float = 3.0) -> None:
        """Request a graceful stop and join the loop thread."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def await_stop(self, timeout: Optional[float] = None) -> None:
        """Block until the loop stops (or timeout elapses, if given)."""
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def status(self) -> Dict[str, Any]:
        """Current loop statistics (for dashboards / the network bridge)."""
        return {
            "running": (self._thread is not None and self._thread.is_alive()),
            "loops": self._loops_completed,
            "tasks": self._total_tasks,
            "successes": self._successes,
            "failures": self._failures,
            "governor_state": self.engine.governor.state.value,
            "fallback_mode": self.engine.fallbacks.mode,
            "uptime_seconds": round(time.monotonic() - self._started_at, 1)
                              if self._started_at else 0.0,
            "tier1_backlog": len(self.engine.memory.tier1),
            "tier2_facts": self.engine.memory.tier2.count(),
        }
    # -- the loop ------------------------------------------------------------

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            # Wall-clock bound
            if (self.max_seconds is not None
                    and time.monotonic() - self._started_at >= self.max_seconds):
                logger.info("Continuous agent reached max_seconds; stopping.")
                break
            # Iteration bound
            if (self.max_iterations is not None
                    and self._loops_completed >= self.max_iterations):
                logger.info("Continuous agent reached max_iterations; stopping.")
                break

            with tracer.start_span("continuous.loop") as span:
                # Honor governor interlocks: when PAUSED, the engine refuses
                # new tasks; the loop sleeps instead of spamming refusals.
                if self.engine.governor.state in (AgentState.PAUSED,
                                                  AgentState.HALTED):
                    span.set_attribute("status", "paused")
                    time.sleep(self.interval)
                    continue

                try:
                    self._loop_once(span)
                except Exception as exc:
                    # The loop must never die to an unexpected exception.
                    # Report to the fallback controller so it can latch a
                    # safe state deterministically.
                    self.engine.fallbacks.report_violation(
                        "continuous_loop_error", str(exc)[:200])
                    logger.error("Continuous loop error: %s", exc)
                    time.sleep(self.interval)

            self._loops_completed += 1
            time.sleep(self.interval)

    def _loop_once(self, span: Any) -> None:
        """One pass of the observe-act loop."""
        # OBSERVE: pull the next task (autonomous generation when none).
        task = self._next_task()
        if task is None:
            span.set_attribute("status", "idle")
            return

        self._total_tasks += 1
        span.set_attribute("task_type", str(task.get("type", "unknown")))

        # GATE -> DECIDE -> EXECUTE -> EVALUATE -> RECORD
        #   (all inside the engine's single gated path).
        result = self.engine.execute_task(task)
        status = result.get("status", "unknown") if isinstance(result, dict) else "unknown"
        span.set_attribute("status", status)

        if status == "success":
            self._successes += 1
        else:
            self._failures += 1

        # EVALUATE: reward signal -> model performance update.
        self.engine.autonomy.evaluate_task_performance(task, result)

        # CONSOLIDATE: the decoupled memory worker does this continuously;
        # we also nudge an explicit pass so boundedness is deterministic
        # even if the worker is delayed.
        try:
            self.engine.memory.consolidate_now()
        except Exception as exc:
            logger.warning("Consolidation pass failed: %s", exc)

        logger.debug("Continuous loop processed %s -> %s",
                     task.get("type", "unknown"), status)

    def _next_task(self) -> Optional[Dict[str, Any]]:
        """Pull a task from the source or generate one autonomously."""
        if self.task_source is not None:
            if callable(self.task_source):
                try:
                    return self.task_source()
                except Exception as exc:
                    logger.warning("Task source callable failed: %s", exc)
                    return None
            # Iterator / generator protocol
            try:
                return next(self.task_source)  # type: ignore[arg-type]
            except StopIteration:
                logger.info("Task source exhausted; idle.")
                return None
            except Exception as exc:
                logger.warning("Task source iteration failed: %s", exc)
                return None

        # Autonomous: generate a reflective self-improvement task so the
        # loop always has grounded work even with no external task feed.
        return self.engine.autonomy.generate_task(
            "text", content="continuous self-observation and improvement")


def _cli() -> int:
    parser = argparse.ArgumentParser(
        description="Run ShugoCore as a continuous agent daemon.")
    parser.add_argument("--models", nargs="+", default=[],
                        help="model ids to register (default: stub)")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="seconds between loop passes (default: 1.0)")
    parser.add_argument("--max-iterations", type=int, default=None,
                        help="stop after N loop passes (default: unlimited)")
    parser.add_argument("--max-seconds", type=float, default=None,
                        help="stop after N wall-clock seconds")
    parser.add_argument("--memory-db", default="semantic_memory.db",
                        help="Tier 2 SQLite path")
    parser.add_argument("--audit", default="audit_chain.jsonl",
                        help="audit chain path")
    parser.add_argument("--journal", default="episodic_journal.jsonl",
                        help="Tier 1 episodic journal path")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    models = (args.models or
              [{"id": "stub", "type": "text", "weight": 1.0,
                "backend": {"type": "stub"}}])

    # Allow Ctrl-C / SIGTERM to stop the loop cleanly.
    loop = ContinuousAgent(
        models=models,
        interval=args.interval,
        max_iterations=args.max_iterations,
        max_seconds=args.max_seconds,
        memory_db_path=args.memory_db,
        audit_path=args.audit,
        episodic_journal_path=args.journal,
    )

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: loop.stop())
        except (ValueError, OSError):
            pass  # not on the main thread / platform doesn't support it

    loop.start()
    logger.info("Continuous agent running; Ctrl-C to stop.")
    try:
        loop.await_stop()
    except KeyboardInterrupt:
        pass
    finally:
        # Capture stats BEFORE shutting the engine down (status touches
        # the Tier 2 DB which is closed by memory.shutdown().)
        stats = loop.status()
        loop.stop()
        loop.engine.task_manager.stop()
        loop.engine.memory.shutdown()
        logger.info("Continuous agent stopped "
                    "(loops=%d tasks=%d ok=%d fail=%d)",
                    stats["loops"], stats["tasks"],
                    stats["successes"], stats["failures"])
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())