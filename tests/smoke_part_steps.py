# ---------------------------------------------------------------------------
# Phase step functions — each is a (serial, logger) -> result callable.
# ---------------------------------------------------------------------------


def _log(logger: List[str], *parts: Any) -> None:
    line = " ".join(str(p) for p in parts)
    print(line, flush=True)
    logger.append(line)


def _log_blue(logger: List[str], *parts: Any) -> None:
    _log(logger, "\033[36m" + " ".join(str(p) for p in parts) + "\033[0m")


def _log_green(logger: List[str], *parts: Any) -> None:
    _log(logger, "\033[32m" + " ".join(str(p) for p in parts) + "\033[0m")


def _log_red(logger: List[str], *parts: Any) -> None:
    _log(logger, "\033[31m" + " ".join(str(p) for p in parts) + "\033[0m")

def step_timer_set(serial: str, logger: List[str]) -> bool:
    """Seed a timer, inject the transcript, and look for the ack."""
    inject_scanout(
        serial, "set a timer for two seconds and tell me when it goes off"
    )
    ok = expect_in_logs(serial, "timer set for 2 seconds", within_s=12.0)
    _log(logger, "  timer ack log line:", "FOUND" if ok else "MISSING")
    return ok


def _sep(logger: List[str]) -> None:
    _log(logger, "=" * 78)


def step_service_alive(serial: str, logger: List[str]) -> bool:
    """Low-level probe: adb shell, package resolvable, Python runtime alive."""
    rc, out, err = run(
        "-s", serial, "shell", "ps", "|", "grep", "-i", "shugocore"
    )
    alive = rc == 0 and "shugocore" in out.lower()
    rc2, ver, _ = run_as_shell(
        serial, "python3 -c 'import sys; print(sys.version.split()[0])'"
    )
    py_ok = rc2 == 0 and ver.strip()
    for ln in [
        f"  ps grep shugocore: {'yes' if alive else 'no'}",
        f"  python3 -c version: {'OK ' + ver.strip() if py_ok else 'MISSING/ERR'}",
    ]:
        _log(logger, ln)
    return alive and py_ok
