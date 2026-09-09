def step_fact_survives_restart(serial: str, logger: List[str]) -> bool:
    """Seed a fact, force-stop, restart, ask, and confirm the fact survives."""
    ensure_service_started(serial)
    inject_scanout(serial, "remember that my favorite color is midnight blue")
    if not expect_in_logs(serial, "remembered", within_s=12.0):
        _log_red(logger, "  fact not stored before teardown")
        return False
    run("-s", serial, "shell", "am", "force-stop", SHUGOCORE_PACKAGE)
    _log(logger, "  force-stop issued")
    time.sleep(2.0)
    ensure_service_started(serial)
    clear_logcat(serial)
    inject_transcript(serial, "remind me what my favorite color is")
    time.sleep(4.0)
    ok = expect_in_logs(serial, "midnight blue", within_s=12.0)
    _log(logger, "  fact survives restart:", "FOUND" if ok else "MISSING")
    return ok


def step_full_teardown_announced(serial: str, logger: List[str]) -> bool:
    """Full teardown round-trip: seed timers/facts, force-stop, restart,
    and confirm the agent announces what was restored."""
    ensure_service_started(serial)
    inject_scanout(
        serial,
        "set a timer for two seconds and remember that my middle name is June",
    )
    if not expect_in_logs(serial, "timer set for 2 seconds", within_s=12.0):
        _log_red(logger, "  timer ack missing before teardown")
        return False
    run("-s", serial, "shell", "am", "force-stop", SHUGOCORE_PACKAGE)
    _log(logger, "  force-stop issued; waiting for timer expiry + restart")
    time.sleep(8.0)
    ensure_service_started(serial)
    ok1 = expect_in_logs(serial, "while you were away", within_s=12.0)
    _log(logger, "  'while you were away' after restart:", "FOUND" if ok1 else "MISSING")
    clear_logcat(serial)
    inject_transcript(serial, "what is my middle name")
    time.sleep(4.0)
    ok2 = expect_in_logs(serial, "june", within_s=12.0)
    _log(logger, "  restored fact (june) after restart:", "FOUND" if ok2 else "MISSING")
    return ok1 and ok2
