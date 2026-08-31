import logging
import json
import subprocess
from typing import List, Dict, Any
from collections import defaultdict

try:
    import torch
    _HAS_TORCH = True
except ImportError:
    _HAS_TORCH = False

class SubconsciousModel:
    def __init__(self, vector_db: Any):
        self.vector_db = vector_db  # Interface to vector database for storing and retrieving past interactions
        self.past_decisions = defaultdict(list)  # Store decisions with task types as keys
        self.model_success_history = defaultdict(lambda: {'successes': 0, 'failures': 0})  # Track model success rates
        self.logger = logging.getLogger(__name__)

        # Check if CUDA is available (requires torch)
        if _HAS_TORCH:
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            self.logger.info(f"Using device: {self.device}")
        else:
            self.device = "cpu"
            self.logger.info("torch not available; using CPU-only fallback.")

    def store_decision(self, task: Dict[str, Any], decision: Dict[str, Any], result: Any):
        """
        Store the past decision and its effectiveness (result).
        
        Args:
        - task: The task that was executed (input to the AI).
        - decision: The decision made by the system.
        - result: The outcome of the decision (success or failure).
        """
        task_type = task.get('type')
        decision_record = {'decision': decision, 'result': result}
        self.past_decisions[task_type].append(decision_record)

        # Track model performance based on success/failure
        # Iterate over model IDs from the model_outputs dict (decision has aggregated_output + model_outputs)
        for model_id in decision.get('model_outputs', {}):
            is_success = result.get('status') == 'success'
            if is_success:
                self.model_success_history[model_id]['successes'] += 1
            else:
                self.model_success_history[model_id]['failures'] += 1

        # Optionally, store data in the vector database
        if self.vector_db is not None:
            self.vector_db.store_vector(
                id=task_type,
                vector=[0.0] * self.vector_db.dimension if hasattr(self.vector_db, 'dimension') else [0.0],
                metadata={'decision': str(decision), 'result': str(result)}
            )

    def retrieve_past_decision(self, task: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Retrieve past decisions related to a specific task type.
        
        Args:
        - task: The task to match against previously executed tasks.
        
        Returns:
        - A list of past decision records.
        """
        task_type = task.get('type')
        return self.past_decisions.get(task_type, [])

    def weight_models_based_on_success(self, models: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Weight models based on their historical success rates using CUDA if available.
        
        Args:
        - models: List of models to be weighted.
        
        Returns:
        - List of models with updated weights.
        """
        model_ids = [model['id'] for model in models]
        successes = []
        failures = []

        for model_id in model_ids:
            success_data = self.model_success_history.get(model_id, {'successes': 0, 'failures': 0})
            successes.append(success_data['successes'])
            failures.append(success_data['failures'])

        # Calculate success rates (works with or without torch)
        if _HAS_TORCH:
            successes_tensor = torch.tensor(successes, dtype=torch.float32, device=self.device)
            failures_tensor = torch.tensor(failures, dtype=torch.float32, device=self.device)
            total_attempts = successes_tensor + failures_tensor
            success_rates_tensor = torch.where(
                total_attempts > 0,
                successes_tensor / total_attempts,
                torch.zeros_like(successes_tensor)
            )
            success_rates = success_rates_tensor.tolist()
        else:
            success_rates = [
                (s / (s + f)) if (s + f) > 0 else 0.0
                for s, f in zip(successes, failures)
            ]

        # Update model weights (return new list to avoid mutating input)
        weighted_models = []
        for i, model in enumerate(models):
            new_model = dict(model)
            new_model['weight'] = success_rates[i]
            weighted_models.append(new_model)

        return weighted_models

    def log_model_success(self, model_id: str, success: bool):
        """
        Log the success or failure of a model.
        
        Args:
        - model_id: ID of the model.
        - success: Whether the model's output was successful.
        """
        if success:
            self.model_success_history[model_id]['successes'] += 1
        else:
            self.model_success_history[model_id]['failures'] += 1

        self.logger.info(f"Model {model_id} success: {success}")

    def get_available_models(self) -> List[str]:
        """
        Retrieve a list of available models from Ollama.

        Returns:
        - List of model names available in Ollama
        """
        try:
            result = subprocess.run(
                ["ollama", "list"], 
                capture_output=True, text=True
            )
            if result.returncode == 0:
                # Parse the text table output from 'ollama list'
                lines = result.stdout.strip().split('\n')
                models = []
                for line in lines[1:]:  # Skip header row
                    parts = line.split()
                    if parts:
                        models.append(parts[0])  # First column is the model name
                return models
            else:
                self.logger.error(f"Failed to fetch model list: {result.stderr}")
                return []
        except Exception as e:
            self.logger.error(f"Error fetching model list from Ollama: {e}")
            return []

    def call_ollama_model(self, model_name: str, input_data: str) -> str:
        """
        Call a specific Ollama model and return the output.
        
        Args:
        - model_name: The model to use (e.g., 'gpt-4', 'llama').
        - input_data: The input text for the model.
        
        Returns:
        - The model's output.
        """
        try:
            result = subprocess.run(
                ["ollama", "run", model_name, "--input", input_data], 
                capture_output=True, text=True
            )
            if result.returncode == 0:
                return result.stdout.strip()
            else:
                self.logger.error(f"Ollama model {model_name} failed with error: {result.stderr}")
                return ""
        except Exception as e:
            self.logger.error(f"Error calling Ollama model {model_name}: {e}")
            return ""

    def get_model_output(self, model_name: str, input_data: Any) -> str:
        """
        Get output from a specified model, ensuring consistent output format.
        
        Args:
        - model_name: The model to query (e.g., 'gpt-4', 'deepseek', 'llama').
        - input_data: The input data for the model (may be a string or a task dict).
        
        Returns:
        - The model's output in a standardized format.
        """
        # Fetch the available models
        available_models = self.get_available_models()
        
        if model_name not in available_models:
            self.logger.error(f"Model {model_name} is not available.")
            return ""
        
        # Convert input_data to string if it's a dict (e.g. a task)
        if isinstance(input_data, dict):
            input_text = input_data.get('content', str(input_data))
        else:
            input_text = str(input_data)
        
        # Call the model and get the response
        model_output = self.call_ollama_model(model_name, input_text)
        
        # Return the response in the required format (e.g., string)
        return model_output.strip()  # Ensure no extra spaces or newlines

# Example usage
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # Assume vector_db is passed from the larger system, maybe a mock for testing
    vector_db = None
    
    subconscious = SubconsciousModel(vector_db)
    if not _HAS_TORCH:
        print("WARNING: torch not installed; using CPU-only fallback.")

    # Fetch available models from Ollama
    available_models = subconscious.get_available_models()
    print("Available models:", available_models)

    # Example of calling an Ollama model
    if available_models:
        model_output = subconscious.call_ollama_model(available_models[0], "What is the capital of France?")
        print("Model output:", model_output)

    # Store a decision example
    task_example = {'type': 'text', 'content': 'What is the capital of France?'}
    decision_example = [{'id': 'gpt-4', 'output': 'Paris'}, {'id': 'deepseek', 'output': 'Paris'}]
    result_example = {'success': True}
    
    subconscious.store_decision(task_example, decision_example, result_example)

    # Retrieve past decision example
    past_decisions = subconscious.retrieve_past_decision(task_example)
    print(past_decisions)

    # Weight models based on success rates
    models_example = [
        {'id': 'gpt-4', 'type': 'text', 'weight': 0.5},
        {'id': 'deepseek', 'type': 'text', 'weight': 0.3}
    ]
    
    weighted_models = subconscious.weight_models_based_on_success(models_example)
    print(weighted_models)
