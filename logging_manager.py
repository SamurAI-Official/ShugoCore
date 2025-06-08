import logging
from datetime import datetime

class LoggingManager:
    def __init__(self, log_file: str = "decision_engine.log"):
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.INFO)
        
        # Log to file
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)
        
        # Add file handler to logger
        self.logger.addHandler(file_handler)
        
        # Also log to console
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)
    
    def log_decision(self, task: dict, decision: dict, result: dict):
        """
        Log the decision-making process, including the task, the decision made, and the result.
        """
        self.logger.info(f"Decision made for task: {task['type']} - {task.get('content', 'N/A')}")
        self.logger.info(f"Decision: {decision}")
        self.logger.info(f"Execution result: {result}")
    
    def log_task_execution(self, task: dict, success: bool, message: str = ""):
        """
        Log task execution results.
        """
        status = "SUCCESS" if success else "FAILURE"
        self.logger.info(f"Task execution {status}: {task['type']} - {task.get('content', 'N/A')} - {message}")
    
    def log_model_selection(self, task: dict, selected_models: list):
        """
        Log the models selected for a task.
        """
        model_ids = [model['id'] for model in selected_models]
        self.logger.info(f"Models selected for task {task['type']} ({task.get('content', 'N/A')}): {model_ids}")
    
    def log_model_performance(self, model_id: str, performance: float):
        """
        Log model performance after task execution.
        """
        self.logger.info(f"Model {model_id} performance updated. New performance: {performance}")
    
    def log_error(self, message: str, exception: Exception = None):
        """
        Log errors with exception details if provided.
        """
        if exception:
            self.logger.error(f"{message} - Exception: {exception}")
        else:
            self.logger.error(message)
    
    def log_reinforcement_learning_update(self, task: dict, decision: dict, reward: float):
        """
        Log updates from the reinforcement learning process.
        """
        self.logger.info(f"Reinforcement learning update: Task: {task['type']} - Reward: {reward} - Decision: {decision}")

    def log_model_addition(self, model: dict):
        """
        Log the addition of new models.
        """
        self.logger.info(f"New model added: {model['id']} - {model['type']}")
