"""
ShugoCore task manager
======================

Bounded work queue with a single worker thread. Tasks are executed through a
gated executor injected by the decision engine (``set_executor``); without an
executor the queue refuses tasks instead of faking success. Callback
exceptions are contained so the worker loop can never die.
"""

import logging
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import Any, Callable, Dict, Optional


class TaskManager:
    """Bounded task queue executing through an injected, policy-gated executor."""

    def __init__(self, max_queue_size: int = 100, poll_timeout: float = 1.0):
        self.task_queue: Queue = Queue(maxsize=max(1, int(max_queue_size)))
        self.poll_timeout = max(0.1, float(poll_timeout))
        self._executor: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None
        self._stop_event = Event()
        self.worker_thread = Thread(target=self.process_tasks,
                                    name="task-manager-worker", daemon=True)
        self.worker_thread.start()
        self.logger = logging.getLogger(__name__)

    def set_executor(self, executor: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
        """Wire the gated execution path (typically DecisionEngine.execute_task)."""
        self._executor = executor

    def add_task(self, task: Dict[str, Any],
                 callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> bool:
        """
        Enqueue a task. Returns False when the bounded queue is full (the
        queue must never grow without bound in a continuous system).
        """
        try:
            self.task_queue.put_nowait((task, callback))
        except Full:
            self.logger.warning("Task queue is full; rejecting task.")
            return False
        self.logger.info(f"Task added to queue: {task}")
        return True

    def process_tasks(self) -> None:
        """Worker loop: executes queued tasks through the gated executor."""
        while not self._stop_event.is_set():
            try:
                task, callback = self.task_queue.get(timeout=self.poll_timeout)
            except Empty:
                continue
            try:
                result = self.execute_task(task)
                if callback:
                    try:
                        callback(result)
                    except Exception as exc:
                        self.logger.error(f"Task callback failed: {exc}")
            except Exception as exc:
                self.logger.error(f"Error processing task: {exc}")
            finally:
                self.task_queue.task_done()

    def execute_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Execute via the injected executor; refuse when none is wired."""
        if self._executor is None:
            self.logger.warning("No executor configured; refusing task "
                                "(never simulate success).")
            return {"status": "refused",
                    "reason": "no executor configured for the task queue"}
        return self._executor(task)

    def stop(self) -> None:
        """Stop the worker (safe to call multiple times)."""
        self._stop_event.set()
        if self.worker_thread.is_alive():
            self.worker_thread.join(timeout=3.0)
