"""
ShugoCore subsystems (v1.30).

Modular capabilities that the agent composes at runtime:
    IntentParser    — classify user intent (question/command/chitchat)
    CommandExecutor — route commands to the right tool/action
    MemoryManager   — durable user facts + topic tracking (Phase 3.1)
    ToolRegistry    — real tool execution & timers (Phase 3.2)
    RetryPolicy / FallbackChain — graceful error handling (Phase 3.3)
    DialogueState   — clarification & coreference state (Phase 3.4)

Each subsystem is independent — improve or replace without touching the
others. The agent shell composes them; they never import the shell.
"""
from subsystems.intent import IntentParser, UserIntent, IntentType
from subsystems.command_router import CommandExecutor, CommandResult
from subsystems.memory import FactMemory, TopicTracker, MemoryManager
from subsystems.tools import (ToolRegistry, Tool, ToolResult, Timer,
                              TimerManager, default_tools)
from subsystems.fallback import RetryPolicy, FallbackChain, retry_call
from subsystems.dialogue import DialogueState, extract_named_entity

__all__ = [
    "IntentParser",
    "UserIntent",
    "IntentType",
    "CommandExecutor",
    "CommandResult",
    # Phase 3.1 memory
    "FactMemory",
    "TopicTracker",
    "MemoryManager",
    # Phase 3.2 tools
    "ToolRegistry",
    "Tool",
    "ToolResult",
    "Timer",
    "TimerManager",
    "default_tools",
    # Phase 3.3 fallback
    "RetryPolicy",
    "FallbackChain",
    "retry_call",
    # Phase 3.4 dialogue
    "DialogueState",
    "extract_named_entity",
]
