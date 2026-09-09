# ---------------------------------------------------------------------------
# CLI + main
# ---------------------------------------------------------------------------

STEP_BY_NAME: Dict[str, Callable[[str, List[str]], bool]] = {
    "service_alive": step_service_alive,
    "timer_set": step_timer_set,
    "timer_fires_while_away": step_timer_fires_while_away,
    "fact_stores": step_fact_stores,
    "memory_question": step_memory_question,

    "fact_survives_restart": step_fact_survives_restart,
    "full_teardown_announced": step_full_teardown_announced,
    "personality_model_genesis": step_personality_model_genesis,
    "personality_growth_log": step_personality_growth_log,
}


def _select_phases(
    names: Optional[List[str]],
    tags_filter: Optional[Tuple[str, ...]],
) -> List[Dict[str, Any]]:
    if names:
        by_name = {p["name"]: p for p in PHASES}
        missing = [n for n in names if n not in by_name]
        if missing:
            raise SystemExit(f"unknown phase(s): {', '.join(missing)}")
        return [by_name[n] for n in names]
    if tags_filter:
        return [p for p in PHASES if tags_filter and not set(tags_filter).isdisjoint(p["tags"])]
    return PHASES


def _run_phase(serial: str, phase: Dict[str, Any], logger: List[str]) -> bool:
    fn = STEP_BY_NAME[phase["name"]]
    _sep(logger)
    _log_blue(logger, f"PHASE: {phase['name']}")
    _log(logger, f"  {phase['desc']}")
    try:
        ok = fn(serial, logger)
    except Exception as exc:
        _log_red(logger, f"  EXCEPTION: {type(exc).__name__}: {exc}")
        ok = False
    status = "PASS" if ok else "FAIL"
    _log(logger, f"  => {status}")
    return ok


def _banner(title: str, logger: List[str]) -> None:
    _sep(logger)
    _log(logger, title)
    _sep(logger)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="ShugoCore Android device smoke probe (dependency-injected, read-only)."
    )
    ap.add_argument(
        "--device", required=False, help="adb serial (e.g. adb-R52WC05JPMW-4kMS88...._adb-tls-connect._tcp)"
    )
    ap.add_argument(
        "--repeat", type=int, default=1, help="repeat full phase set N times"
    )
    ap.add_argument(
        "--phases", nargs="+", help="phase names to run (default: all)"
    )
    ap.add_argument(
        "--tag", action="append", dest="tags", help="include phases with this tag"
    )
    ap.add_argument("--list", action="store_true", help="list phases and exit")
    ap.add_argument("--json", action="store_true", help="emit JSON result on stdout")
    args = ap.parse_args(argv)

    if args.list:
        for p in PHASES:
            print(f"{p['name']:32s} {p['desc']}")
        return 0

    tags = tuple(args.tags) if args.tags else None
    phases = _select_phases(args.phases, tags)
    if not phases:
        print("no phases selected", file=sys.stderr)
        return 2

    serial = args.device
    ensure_connected(serial)
    ensure_service_started(serial)

    results: List[Dict[str, Any]] = []
    run_log: List[str] = []

    if args.repeat > 1:
        _banner(f"REPEAT {args.repeat}x — device {serial}", run_log)

    for ridx in range(args.repeat):
        if args.repeat > 1:
            _banner(f"RUN {ridx + 1}/{args.repeat}", run_log)
        for phase in phases:
            ok = _run_phase(serial, phase, run_log)
            results.append({
                "run": ridx + 1,
                "phase": phase["name"],
                "desc": phase["desc"],
                "ok": ok,
            })

    _sep(run_log)
    passed = sum(1 for r in results if r["ok"])
    total = len(results)
    _log(run_log, f"RESULTS: {passed}/{total} passed")
    if passed != total:
        _log_red(run_log, "SOME PHASES FAILED")

    if args.json:
        payload = {
            "device": serial,
            "repeat": args.repeat,
            "phases_requested": args.phases,
            "tags_requested": list(tags) if tags else None,
            "total_phases": total,
            "passed": passed,
            "results": results,
        }
        print(json.dumps(payload, indent=2))

    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
