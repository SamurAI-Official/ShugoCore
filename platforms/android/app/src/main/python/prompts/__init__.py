"""
ShugoCore dynamic prompt builder (v1.29).

Constructs context-aware prompts for the model based on what's happening:
conversation, task execution, or proactive behavior. Combines personality,
conversation history, memory facts, and perception into the final prompt.

Exports:
    build_conversational_prompt()  -> system + context -> full prompt text
"""
from prompts.builder import build_conversational_prompt

__all__ = ["build_conversational_prompt"]
