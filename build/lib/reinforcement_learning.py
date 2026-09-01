import logging
from typing import Dict, Any
from model_manager import ModelManager

class ReinforcementLearning:
    def __init__(self, model_manager: ModelManager, learning_rate: float = 0.1):
        self.model_manager = model_manager
        self.learning_rate = learning_rate
        self.logger = logging.getLogger(__name__)
    
    def update_model_performance(self, task: Dict[str, Any], decision: Dict[str, Any], result: Dict[str, Any]):
        """
        Adjusts model weights based on task outcomes.
        """
        if isinstance(result, dict):
            success = (result.get('success')
                       if result.get('success') is not None
                       else result.get('status') == 'success')
        else:
            success = False
        # Iterate over the model IDs in the model_outputs dict
        for model_id in decision.get('model_outputs', {}):
            current_performance = self.model_manager.get_model_performance(model_id)
            if success:
                new_performance = current_performance * (1 + self.learning_rate)
            else:
                new_performance = current_performance * (1 - self.learning_rate)
            
            self.model_manager.set_model_performance(model_id, new_performance)
            self.logger.info(f"Updated performance of model {model_id}: {new_performance}")

    def evaluate_performance(self, task: Dict[str, Any], result: Dict[str, Any]) -> float:
        """
        Evaluate the performance of a task execution and return a reward signal
        (used by autonomy.evaluate_task_performance).

        Args:
        - task: The task that was executed.
        - result: The result of the execution.

        Returns:
        - A float reward: 1.0 (success), 0.0 (failure), 0.5 (indeterminate).
        """
        if not isinstance(result, dict):
            return 0.5

        if result.get('status') == 'success' or result.get('success') is True:
            reward = 1.0
        elif result.get('status') == 'error' or 'error' in result:
            reward = 0.0
        else:
            reward = 0.5  # Neutral for indeterminate outcomes

        self.logger.info(f"Evaluated performance for task {task.get('type', 'unknown')}: {reward}")
        return reward
