import logging
from typing import List, Dict, Any

class ModelManager:
    def __init__(self, models: List[Dict[str, Any]]):
        """
        Initializes the ModelManager with a list of models and their metadata.
        """
        self.models = models  # List of available models with metadata
        self.model_performance = {model['id']: 1.0 for model in models}  # Track performance
        self.logger = logging.getLogger(__name__)
    
    def select_models(self, task: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Select appropriate models based on the task type.
        """
        selected_models = [model for model in self.models if model['type'] == task['type']]
        return selected_models if selected_models else self.models  # Fallback to all models if none match
    
    def aggregate_outputs(self, model_outputs: List[tuple]) -> Dict[str, Any]:
        """
        Aggregate model outputs using weighted voting based on performance.
        """
        weighted_sum = {}
        total_weight = sum(weight for _, _, weight in model_outputs)

        for model_id, output, weight in model_outputs:
            for key, value in output.items():
                weighted_sum[key] = weighted_sum.get(key, 0) + value * weight

        final_output = {key: value / total_weight for key, value in weighted_sum.items()}
        return final_output
    
    def get_model_performance(self, model_id: str) -> float:
        """
        Retrieve the performance score of a model.
        """
        return self.model_performance.get(model_id, 1.0)
    
    def update_model_performance(self, model_id: str, success: bool):
        """
        Update model performance based on task success.
        """
        if model_id in self.model_performance:
            self.model_performance[model_id] *= 1.1 if success else 0.9
            self.logger.info(f"Updated performance for {model_id}: {self.model_performance[model_id]}")
    
    def add_model(self, model: Dict[str, Any]):
        """
        Dynamically add a new model and initialize its performance tracking.
        """
        self.models.append(model)
        self.model_performance[model['id']] = 1.0  # Initialize with neutral weight
        self.logger.info(f"Added new model: {model['id']}")
