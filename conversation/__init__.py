"""
ShugoCore conversation manager (v1.29).

Tracks dialogue state so Shugo can handle multi-turn conversation, barge-in,
and turn-taking without hardcoding any responses.

Exports:
    ConversationState   — enum of dialogue states
    ConversationManager  — state machine + turn tracking
"""
from conversation.manager import ConversationState, ConversationManager

__all__ = ["ConversationState", "ConversationManager"]
