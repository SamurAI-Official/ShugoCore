def step_timer_fires_while_away(serial: str, logger: List[str]) -> bool:
    """Force-stop, then verify the expired timer is re-announced."""
    ensure_service_started(serial)
    inject_scanout(
        serial, "set a timer for two seconds and tell me when it fires"
    )
    if not expect_in_logs(serial, "timer set for 2 seconds", within_s=12.0):
        _log_red(logger, "  timer ack missing before teardown")
        return False
    run("-s", serial, "shell", "am", "force-stop", SHUGOCORE_PACKAGE)
    _log(logger, "  force-stop issued; waiting for timer expiry+restart")
    time.sleep(8.0)
    ensure_service_started(serial)
    ok = expect_in_logs(serial, "while you were away", within_s=12.0)
    _log(logger, "  'while you were away' log line:", "FOUND" if ok else "MISSING")
    return ok
