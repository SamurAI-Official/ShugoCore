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
        success = result.get('success', False)
        for model_id in decision:
            current_performance = self.model_manager.get_model_performance(model_id)
            if success:
                new_performance = current_performance * (1 + self.learning_rate)
            else:
                new_performance = current_performance * (1 - self.learning_rate)
            
            self.model_manager.update_model_performance(model_id, new_performance)
            self.logger.info(f"Updated performance of model {model_id}: {new_performance}")
