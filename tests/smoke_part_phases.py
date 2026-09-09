# ---------------------------------------------------------------------------
# PHASES — module-level list of dicts. Each entry drives one smoke probe.
# ---------------------------------------------------------------------------

PHASES: List[Dict[str, Any]] = [
    {
        "name": "service_alive",
        "desc": "app is running and agent loop is active",
        "tags": ("preflight",),
    },
    {
        "name": "timer_set",
        "desc": "set a timer and see the ack log line",
        "tags": ("timer",),
    },
    {
        "name": "timer_fires_while_away",
        "desc": "expired timer is re-announced after force-stop+restart",
        "tags": ("timer", "restart"),
    },
    {
        "name": "fact_stores",
        "desc": "assert fact (name, value) landed in SQLite",
        "tags": ("memory", "fact"),
    },
    {
        "name": "memory_question",
        "desc": "ask a memory question and see a memory-based answer",
        "tags": ("memory", "question"),
    },
    {
        "name": "fact_survives_restart",
        "desc": "OS-level fact survives force-stop + restart",
        "tags": ("memory", "fact", "restart"),
    },
    {
        "name": "full_teardown_announced",
        "desc": "full teardown round-trip is announced in logs",
        "tags": ("lifecycle",),
    },
    {
        "name": "personality_model_genesis",
        "desc": "first production model paragraph exists in SQLite",
        "tags": ("personality", "genesis"),
    },
    {
        "name": "personality_growth_log",
        "desc": "growth report can be read from SQLite and diffed offline",
        "tags": ("personality", "growth", "offline"),
    },
]
