def step_personality_model_genesis(serial: str, logger: List[str]) -> bool:
    """Verify the first production model paragraph exists in SQLite.

    We deliberately probe the production-model file, not the dev one.
    The harness must not assert on dev-model artifacts.
    """
    rc, out, err = run_as_shell(
        serial,
        "cat /data/data/com.samurai.shugocore/files/"
        "shugo_core_prod_personality_model.json 2>/dev/null | head -c 200",
    )
    body = (out or "") + (err or "")
    ok = rc == 0 and len(body.strip()) > 120 and "ShugoCore" in body
    _log(logger, "  production model JSON readable:", "OK" if ok else "MISSING/EMPTY")
    if not ok:
        _log(logger, "    first 200 chars:", body.strip()[:200])
    return ok
