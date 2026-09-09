def step_fact_stores(serial: str, logger: List[str]) -> bool:
    """Assert an OS-level fact landed in SQLite."""
    inject_scanout(serial, "remember that my favorite color is midnight blue")
    ok = expect_in_logs(serial, "remembered", within_s=12.0)
    _log(logger, "  'remembered' log line:", "FOUND" if ok else "MISSING")
    return ok


def step_memory_question(serial: str, logger: List[str]) -> bool:
    """Ask a memory question and confirm the answer uses memory content."""
    clear_logcat(serial)
    inject_transcript(serial, "what is my favorite color")
    time.sleep(3.0)
    before = set(log_lines_containing(serial, "midnight blue"))
    inject_transcript(serial, "remind me what my favorite color is")
    time.sleep(3.0)
    after = log_lines_containing(serial, "midnight blue")
    ok = len(after) > len(before) and any(
        "midnight blue" in ln.lower() for ln in after
    )
    _log(logger, "  memory question returns stored fact:", "FOUND" if ok else "MISSING")
    _log(logger, "    lines mentioning midnight blue after question:", len(after))
    return ok
