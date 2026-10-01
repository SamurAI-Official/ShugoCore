"""Did vision survive being backgrounded and switched off -- and did audio?

Leaning on vision for presence rests on an untested assumption: that a device can report presence
whenever it is on. The camera here is bound to a lifecycle while Android gives audio to a service
that outlives the UI, so the two channels may have different uptime. This measures both in one
window, with each action stamped in the *device's* clock -- the clock the logcat timestamps use --
so interpreting the result needs no offset model.

The two untouched devices are the control: if their cadence moves across the same window, what
moved was the world rather than the actions taken on the A16.

    python runtime/vis_survival_check.py
"""
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent

# logcat -v epoch: "<epoch> <pid> <tid> <level> <tag>: <message>"
LINE = re.compile(r"^\s*(\d+\.\d+)\s+(\d+)\s+(\d+)\s+([IWEDV])\s+([\w.$]+):\s*(.*)$")
ACTION = re.compile(r"^(.+?)\s+device_ms=(\d+)\s+host=")


def kind_of(tag, message):
    if tag == "VisionProvider":
        if "presence faces=" in message:
            return "presence"
        if "first camera frame published" in message:
            return "rebind"
        if ("analysis width=" in message or "dark threshold=" in message
                or "calibration logging" in message):
            return "policy"
    if tag == "SoundProvider" and "heard:" in message:
        return "heard"
    if tag == "ShugoCoreService" and "NRR self-test" in message:
        return "nrrtest"
    return None


def parse_tail(path):
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = LINE.match(line)
        if not match:
            continue
        kind = kind_of(match.group(5).strip(), match.group(6))
        if kind:
            events.append({"t": float(match.group(1)) * 1000.0, "kind": kind,
                           "msg": match.group(6).strip()})
    events.sort(key=lambda event: event["t"])
    return events


def parse_actions(path):
    actions = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = ACTION.match(line)
        if match:
            actions.append((match.group(1).strip(), int(match.group(2))))
    return actions


def stats(events, start, end, kind):
    picked = [event["t"] for event in events
              if kind == event["kind"] and start <= event["t"] <= end]
    if len(picked) > 1:
        gaps = sorted(picked[i] - picked[i - 1] for i in range(1, len(picked)))
        median, worst = round(gaps[len(gaps) // 2] / 1000.0, 1), round(gaps[-1] / 1000.0, 1)
    else:
        median, worst = None, None
    return len(picked), median, worst


def clock(ms):
    seconds = int(ms // 1000) % 86400
    return "%02d:%02d:%02d" % (seconds // 3600, (seconds // 60) % 60, seconds % 60)


def main():
    actions = parse_actions(HERE / "vis_actions.txt")
    if len(actions) < 5:
        print("action log too short: %d entries" % len(actions))
        return 1
    at = dict(actions)
    tails = sorted(path for path in HERE.glob("vis_tail2_*.txt") if "_err" not in path.name)
    if not tails:
        print("no tail files")
        return 1
    begins = [parse_tail(path)[0]["t"] for path in tails if parse_tail(path)]
    windows = [
        ("baseline (foreground)", min(begins), at["after_HOME_keyevent3"]),
        ("HOME pressed", at["after_HOME_keyevent3"], at["before_power_off"]),
        ("screen off", at["after_power_keyevent26"], at["before_wake"]),
        ("relaunched", at["after_am_start"], at["end_relaunched_65s"]),
    ]

    lines = ["=== vision/audio survival, actions in device clock ==="]
    for name, ms in actions:
        lines.append("  %-26s %s" % (name, clock(ms)))
    lines.append("")

    for path in tails:
        tag = path.name.replace("vis_tail2_", "").replace(".txt", "")
        events = parse_tail(path)
        lines.append("=== %s: %d parsed events ===" % (tag, len(events)))
        for label, start, end in windows:
            pcount, pmed, pmax = stats(events, start, end, "presence")
            acount, amed, _ = stats(events, start, end, "heard")
            rebinds = [event["t"] for event in events
                       if event["kind"] == "rebind" and start <= event["t"] <= end]
            lines.append("  %-23s %6.1fs  presence=%-4d gap med=%-5s max=%-5s "
                         "audio=%-4d gap med=%-5s rebinds=%d"
                         % (label, (end - start) / 1000.0, pcount, pmed, pmax, acount, amed,
                            len(rebinds)))
            for moment in rebinds:
                lines.append("        rebind at %s" % clock(moment))
        lines.append("")

    events = parse_tail(HERE / "vis_tail2_A16.txt")
    for label, moment in (("HOME", at["after_HOME_keyevent3"]),
                          ("power off", at["after_power_keyevent26"]),
                          ("relaunch", at["after_am_start"])):
        lines.append("=== A16 around %s (offset seconds from the action) ===" % label)
        near = [event for event in events if abs(event["t"] - moment) <= 25_000]
        for event in near:
            lines.append("  %+7.1fs  %-8s %s"
                         % ((event["t"] - moment) / 1000.0, event["kind"], event["msg"][:74]))
        if not near:
            lines.append("  (nothing within 25s)")
        lines.append("")

    # How blind can vision be while it is running? The median gap says nothing about the tail:
    # a design that reads "no lines for N seconds" as absence depends entirely on the maximum.
    lines.append("=== presence gap percentiles while foregrounded (before HOME) ===")
    for path in tails:
        tag = path.name.replace("vis_tail2_", "").replace(".txt", "")
        times = [event["t"] for event in parse_tail(path)
                 if event["kind"] == "presence" and event["t"] < at["after_HOME_keyevent3"]]
        if len(times) < 3:
            lines.append("  %-5s too few lines" % tag)
            continue
        gaps = sorted(times[i] - times[i - 1] for i in range(1, len(times)))
        pick = lambda q: round(gaps[min(len(gaps) - 1, int(len(gaps) * q))] / 1000.0, 1)
        lines.append("  %-5s n=%-5d p50=%-5s p90=%-5s p99=%-5s max=%-6s  over %.0f min"
                     % (tag, len(times), pick(0.50), pick(0.90), pick(0.99), pick(1.0),
                        (times[-1] - times[0]) / 60000.0))
    lines.append("")

    # A heartbeat is not evidence. `rms=0.0000` exactly is a capture delivering nothing, which is
    # the class of lie the sound contract exists to refuse: silence reported as a quiet room.
    lines.append("=== audio content through the actions (rms per device) ===")
    for path in tails:
        tag = path.name.replace("vis_tail2_", "").replace(".txt", "")
        events = parse_tail(path)
        for label, start, end in windows[1:]:
            values = []
            zeros = 0
            for event in events:
                if event["kind"] != "heard" or not (start <= event["t"] <= end):
                    continue
                match = re.search(r"rms=([0-9.]+)", event["msg"])
                if match:
                    value = float(match.group(1))
                    values.append(value)
                    if value == 0.0:
                        zeros += 1
            if not values:
                lines.append("  %-5s %-14s no audio lines" % (tag, label))
                continue
            values.sort()
            lines.append("  %-5s %-14s n=%-4d exactly_zero=%-4d median=%.4f max=%.4f"
                         % (tag, label, len(values), zeros, values[len(values) // 2],
                            values[-1]))
        lines.append("")

    report = "\n".join(lines)
    (HERE / "vis_survival_report.txt").write_text(report, encoding="utf-8")
    print("wrote %s (%d lines)" % (HERE / "vis_survival_report.txt", len(lines)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
