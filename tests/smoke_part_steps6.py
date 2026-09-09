def step_personality_growth_log(serial: str, logger: List[str]) -> bool:
    """Force growth, then read the growth-driven model paragraph from SQLite,
    and diff it offline against the then-current production paragraph.

    Growth is triggered here by a forced _growth_maybe-equivalent stimulus
    (multiple conversational turns). We read the model JSON both before and
    after, compute compare_models locally, and assert drift > 0.
    """
    import ast
    from personality import PersonalityModel

    def read_prod_model(serial: str) -> Optional[PersonalityModel]:
        rc, out, _ = run_as_shell(
            serial,
            "cat /data/data/com.samurai.shugocore/files/"
            "shugo_core_prod_personality_model.json 2>/dev/null",
        )
        if rc != 0 or not out.strip():
            return None
        try:
            return PersonalityModel.from_dict(ast.literal_eval(out))
        except Exception:
            return None

    before = read_prod_model(serial)
    if before is None:
        _log_red(logger, "  cannot read production model before growth")
        return False
    _log(logger, "  pre-growth generation:", before.generation)

    turns = [
        "what is my favorite color",
        "remember that I adopted a rescue greyhound named June",
        "you are being very kind today",
        "remember that my favorite dessert is mango sticky rice",
        "thank you for remembering things about me",
    ]
    for t in turns:
        inject_transcript(serial, t, wait_s=2.0)

    after = read_prod_model(serial)
    if after is None:
        _log_red(logger, "  cannot read production model after growth")
        return False
    _log(logger, "  post-growth generation:", after.generation)

    try:
        comparison = PersonalityModel.compare_models(before, after)
    except Exception as exc:
        _log_red(logger, "  compare_models raised:", exc)
        return False

    drift = comparison.get("drift")
    _log(logger, "  comparison drift:", repr(drift))
    _log(
        logger,
        "  comparison traits:",
        json.dumps(comparison.get("traits"), sort_keys=True, indent=2),
    )
    ok = (drift is not None) and (drift > 0.0)
    _log(logger, "  growth drift > 0:", "OK" if ok else "NONE/STALE")
    return ok
