import logging
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
        self.logger = logging.getLogger(__name__)

    def generate_task(self, task_type: str, content: str = '') -> Dict[str, Any]:
        """
        Creates a structured task dictionary with metadata, ensuring traceability and logging execution.
        """
        task = {
            'type': task_type,
            'content': content,
            'timestamp': logging.Formatter.formatTime(
                logging.LogRecord("", 0, "", 0, "", [], None), "%Y-%m-%d %H:%M:%S"
            )
        }
        self.logging_manager.log_task_execution(task, True, "Generated new task.")
        return task
    
    def execute_autonomous_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes a task autonomously, handling decision-making, execution, learning, and logging.
        """
        try:
            self.logger.info(f"Starting autonomous execution for task: {task['type']} - {task.get('content', 'N/A')}")
            decision = self.decision_engine.make_decision(task)
            self.logger.info(f"Decision made: {decision}")
            result = self.execution_layer.execute(decision)
            self.logger.info(f"Task execution result: {result}")
            self.reinforcement_learning.update_model_performance(task, decision, result)
            self.logging_manager.log_decision(task, decision, result)
            return result
        except Exception as e:
            self.logging_manager.log_error("Error during autonomous task execution", e)
            return {}

    def autonomous_learning_cycle(self, tasks: List[Dict[str, Any]]):
        """
        Runs a cycle of autonomous decision-making and execution, incorporating feedback dynamically.
        """
        for task in tasks:
            result = self.execute_autonomous_task(task)
            self.logger.info(f"Autonomous cycle completed for task: {task['type']} - Result: {result}")
            if 'feedback' in result:
                tasks.append(self.generate_task('feedback', result['feedback']))
    
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
        except Exception as e:
            self.logging_manager.log_error("Error adapting to environment", e)
