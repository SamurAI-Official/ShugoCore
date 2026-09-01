import logging
import threading
from typing import List, Dict, Any

class ModelManager:
    def __init__(self, models: List[Dict[str, Any]]):
        """
        Initializes the ModelManager with a list of models and their metadata.
        """
        self.models = models  # List of available models with metadata
        self.model_performance = {model['id']: 1.0 for model in models}  # Track performance
        self._lock = threading.RLock()  # shared with RL / task threads
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
            if isinstance(output, dict):
                for key, value in output.items():
                    weighted_sum[key] = weighted_sum.get(key, 0) + (value * weight if isinstance(value, (int, float)) else weight)
            else:
                # Handle non-dict outputs (e.g. strings) by treating as a single 'output' key
                weighted_sum['output'] = weighted_sum.get('output', 0) + weight

        if total_weight == 0:
            return {'outputs': [output for model_id, output, weight in model_outputs]}
        final_output = {key: value / total_weight for key, value in weighted_sum.items()}
        return final_output
    
    def get_model_performance(self, model_id: str) -> float:
        """
        Retrieve the performance score of a model.
        """
        with self._lock:
            return self.model_performance.get(model_id, 1.0)
    
    def update_model_performance(self, model_id: str, success: bool):
        """
        Update model performance based on task success (multiplicative).
        """
        with self._lock:
            if model_id in self.model_performance:
                self.model_performance[model_id] *= 1.1 if success else 0.9
                self.logger.info(f"Updated performance for {model_id}: {self.model_performance[model_id]}")

    def set_model_performance(self, model_id: str, performance: float):
        """
        Set model performance to an explicit value (used by reinforcement learning).
        """
        with self._lock:
            if model_id in self.model_performance:
                self.model_performance[model_id] = max(0.0, performance)
                self.logger.info(f"Set performance for {model_id}: {self.model_performance[model_id]}")
    
    def add_model(self, model: Dict[str, Any]):
        """
        Dynamically add a new model and initialize its performance tracking.
        """
        with self._lock:
            self.models.append(model)
            self.model_performance[model['id']] = 1.0  # Initialize with neutral weight
        self.logger.info(f"Added new model: {model['id']}")

    def update_models(self, environment_data: Dict[str, Any]):
        """
        Update model metadata/performance based on new environmental data
        (called by Autonomy.adapt_to_environment).

        Supported keys in environment_data:
        - 'model_performances': {model_id: score} explicit performance updates
        - 'new_models': list of model dicts ({'id': ..., 'type': ..., 'weight': ...}) to register
        Any other keys are logged and ignored.
        """
        if not isinstance(environment_data, dict):
            self.logger.warning("update_models received non-dict data; ignoring.")
            return

        for model_id, performance in environment_data.get('model_performances', {}).items():
            self.set_model_performance(model_id, float(performance))

        for model in environment_data.get('new_models', []):
            if isinstance(model, dict) and 'id' in model:
                self.add_model(model)
            else:
                self.logger.warning(f"Skipping invalid model entry: {model}")

        ignored_keys = set(environment_data.keys()) - {'model_performances', 'new_models'}
        if ignored_keys:
            self.logger.info(f"update_models ignored keys: {sorted(ignored_keys)}")
        self.logger.info("Model manager updated with environment data.")
