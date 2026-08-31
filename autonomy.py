import logging
from datetime import datetime
from typing import TYPE_CHECKING, List, Dict, Any

if TYPE_CHECKING:
    from decision_engine import DecisionEngine  # Prevents circular imports at runtime

from model_manager import ModelManager
from reinforcement_learning import ReinforcementLearning
from task_manager import TaskManager
from vector_db import VectorDB
from logging_manager import LoggingManager
from execution_layer import ExecutionLayer
from subconscious import SubconsciousModel

class Autonomy:
    def __init__(self, decision_engine: "DecisionEngine"):
        """
        Initializes the Autonomy module, integrating with various components for decision-making and execution.
        """
        self.decision_engine = decision_engine
        self.logging_manager = decision_engine.logging_manager
        self.model_manager = decision_engine.model_manager
        self.reinforcement_learning = decision_engine.reinforcement_learning
        self.task_manager = decision_engine.task_manager
        self.vector_db = decision_engine.vector_db
        self.execution_layer = decision_engine.execution_layer
        self.subconscious = decision_engine.subconscious
        self.memory = decision_engine.memory
        self.logger = logging.getLogger(__name__)

    def generate_task(self, task_type: str, content: str = '') -> Dict[str, Any]:
        """
        Creates a structured task dictionary with metadata, ensuring traceability and logging execution.
        """
        task = {
            'type': task_type,
            'content': content,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        self.logging_manager.log_task_execution(task, True, "Generated new task.")
        return task
    
    def execute_autonomous_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes a task autonomously, handling decision-making, execution, learning, and logging.
        """
        try:
            self.memory.note_step(f"autonomous task: {task.get('type', 'unknown')}")
            self.logger.info(f"Starting autonomous execution for task: {task['type']} - {task.get('content', 'N/A')}")
            decision = self.decision_engine.make_decision(task)
            self.logger.info(f"Decision made: {decision}")
            result = self.execution_layer.execute(decision)
            self.logger.info(f"Task execution result: {result}")
            self.reinforcement_learning.update_model_performance(task, decision, result)
            self.logging_manager.log_decision(task, decision, result)
            # Tier 1: episodic outcome record; consolidation/promotion into
            # Tier 2 (and eventually Tier 3) happens in the memory worker.
            self.memory.record_event(
                "task_result",
                {"task_type": task.get("type", "unknown"),
                 "status": str(result.get("status", "unknown")) if isinstance(result, dict) else "unknown"},
            )
            self.memory.resolve_step()
            return result
        except Exception as e:
            self.memory.record_event(
                "task_failure",
                {"task_type": task.get("type", "unknown"), "error": str(e)[:200]},
            )
            self.memory.resolve_step()
            self.logging_manager.log_error("Error during autonomous task execution", e)
            return {'status': 'error', 'message': str(e)}

    def autonomous_learning_cycle(self, tasks: List[Dict[str, Any]], max_iterations: int = 50):
        """
        Runs a cycle of autonomous decision-making and execution, incorporating feedback dynamically.
        
        Args:
        - tasks: List of tasks to execute.
        - max_iterations: Maximum number of tasks to process (prevents infinite feedback loops).
        """
        iteration = 0
        self.memory.record_event("cycle_started", {"planned_tasks": len(list(tasks))})
        for task in list(tasks):
            if iteration >= max_iterations:
                self.logger.warning(f"Reached max_iterations ({max_iterations}); stopping autonomous cycle.")
                break
            result = self.execute_autonomous_task(task)
            self.logger.info(f"Autonomous cycle completed for task: {task['type']} - Result: {result}")
            if 'feedback' in result:
                tasks.append(self.generate_task('feedback', result['feedback']))
            iteration += 1

        # End-of-cycle consolidation: compress this cycle's episodic events
        # into Tier 2 facts so long-run state stays bounded. The background
        # memory worker also does this periodically; doing it here makes the
        # bound deterministic per cycle.
        try:
            stats = self.memory.consolidate_now()
            self.logger.info(f"Memory consolidation after cycle: {stats}")
        except Exception as e:
            self.logging_manager.log_error("Memory consolidation failed after cycle", e)
    
    def evaluate_task_performance(self, task: Dict[str, Any], result: Dict[str, Any]) -> float:
        """
        Assesses task execution performance, contributing to system learning and adaptation.
        """
        try:
            performance = self.reinforcement_learning.evaluate_performance(task, result)
            self.logger.info(f"Task {task['type']} performance evaluated: {performance}")
            return performance
        except Exception as e:
            self.logging_manager.log_error(f"Error evaluating performance for task: {task['type']}", e)
            return 0.0
    
    def adapt_to_environment(self, environment_data: Dict[str, Any]):
        """
        Adjusts decision-making processes based on external environmental data, improving response capabilities.
        """
        self.logger.info(f"Adapting to new environment data: {environment_data}")
        try:
            self.vector_db.update(environment_data)
            self.model_manager.update_models(environment_data)
            # Persist the observation in long-term memory (Tier 2) so future
            # decisions can retrieve it via similarity search.
            self.memory.tier2.store_fact(
                content=f"Environment update: {str(environment_data)[:300]}",
                kind="environment",
                salience=1.2,
                metadata={"source": "adapt_to_environment"},
            )
        except Exception as e:
            self.logging_manager.log_error("Error adapting to environment", e)
