"""Sample the sound layer's cost while it runs, so the claim comes from a file.

Per interval: windows published by SoundProvider, how many of those were *classified* (the
expensive path) versus level-only, the app's share of CPU, the hottest thermal zone, and the
battery's level/temperature. The delta between samples is the duty cycle; level and thermal
deltas are the cost.

    python runtime/sound_duty_sampler.py --serial <s> --minutes 15 --phase listen

One JSON object per sample in --out (JSONL). Counting reads `logcat -d` totals with the buffer
cleared at the start: the provider publishes ~1 line/second, so a 30-minute window fits easily.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ADB = r"G:\Android\Sdk\platform-tools\adb.exe"
PACKAGE = "com.samurai.shugocore"


def adb(serial, *args, timeout=60):
    out = subprocess.run([ADB, "-s", serial, *args], capture_output=True, text=True,
                         timeout=timeout)
    return out.stdout or ""


def battery(serial):
    text = adb(serial, "shell", "dumpsys", "battery")
    values = {}
    for line in text.splitlines():
        line = line.strip()
        for key, name in (("level:", "level"), ("temperature:", "battery_temp_c"),
                          ("voltage:", "voltage_mv"), ("status:", "status")):
            if line.startswith(key):
                raw = line.split(":", 1)[1].strip()
                try:
                    number = float(raw)
                except ValueError:
                    values[name] = raw
                    break
                if name == "battery_temp_c":
                    number = number / 10.0        # dumpsys reports tenths of a degree
                values[name] = number
                break
    return values


def hottest_zone(serial):
    """The warmest temperature the device reports, in Celsius, or None if it will not say.

    dumpsys thermalservice is preferred over /sys/class/thermal, whose zone files vary by
    vendor and came back empty on the A51. mValue is sometimes tenths of a degree and sometimes
    already Celsius, so both readings are considered and only plausible phone temperatures
    survive.
    """
    text = adb(serial, "shell", "dumpsys", "thermalservice")
    readings = []
    for token in text.replace(",", " ").split():
        if not token.startswith("mValue="):
            continue
        try:
            value = float(token.split("=", 1)[1])
        except ValueError:
            continue
        for candidate in (value, value / 10.0):
            if 5.0 <= candidate <= 120.0:
                readings.append(candidate)
    return max(readings) if readings else None


def cpu_seconds(serial):
    """CPU time this app has consumed, in seconds, from its own /proc accounting.

    dumpsys cpuinfo reports a since-boot figure, so every sample inside a 15-minute window
    reads the same number and says nothing about what the microphone adds. CPU-time deltas are
    exact, and the app's own ticks are the only honest attribution available without root.
    """
    pids = adb(serial, "shell", "pidof " + PACKAGE).strip()
    if not pids:
        return None
    pid = pids.split()[0]
    stat = adb(serial, "shell", "cat /proc/%s/stat" % pid)
    try:
        # Field 2 is the command, which may contain spaces and parentheses: cut at the last ")".
        fields = stat.rstrip().rsplit(") ", 1)[1].split()
        return round((int(fields[11]) + int(fields[12])) / 100.0, 3)
    except (IndexError, ValueError):
        return None


def provider_counts(serial):
    """(published, classified, level_only) totals since the buffer was cleared."""
    text = adb(serial, "shell", "logcat", "-d", "-s", "SoundProvider:*", timeout=120)
    published = classified = level_only = 0
    for line in text.splitlines():
        if "heard:" not in line:
            continue
        published += 1
        if "heard: ok," in line:
            classified += 1
        elif "heard: level_only" in line:
            level_only += 1
    return published, classified, level_only


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", required=True)
    parser.add_argument("--minutes", type=float, default=15.0)
    parser.add_argument("--phase", default="listen")
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--out", default=str(REPO / "runtime" / "sound_duty.jsonl"))
    args = parser.parse_args(argv)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    adb(args.serial, "logcat", "-c")

    started = time.time()
    deadline = started + args.minutes * 60.0
    previous = None
    print("phase=%s serial=%s minutes=%.1f" % (args.phase, args.serial, args.minutes), flush=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        while time.time() < deadline:
            sample = {"t": round(time.time() - started, 1), "phase": args.phase}
            sample.update(battery(args.serial))
            published, classified, level_only = provider_counts(args.serial)
            cpu_now = cpu_seconds(args.serial)
            sample.update({"windows": published, "classified": classified,
                           "level_only": level_only, "cpu_s": cpu_now,
                           "hottest_c": hottest_zone(args.serial)})
            if previous:
                elapsed = sample["t"] - previous["t"]
                sample["d_windows"] = published - previous["windows"]
                sample["d_classified"] = classified - previous["classified"]
                if elapsed > 0:
                    sample["windows_per_min"] = round(60.0 * sample["d_windows"] / elapsed, 1)
                    sample["classified_per_min"] = round(
                        60.0 * sample["d_classified"] / elapsed, 2)
                    if cpu_now is not None and previous.get("cpu_s") is not None:
                        # percent of one core: CPU seconds spent / wall-clock seconds * 100
                        sample["cpu_pct"] = round(
                            100.0 * (cpu_now - previous["cpu_s"]) / elapsed, 1)
            handle.write(json.dumps(sample, sort_keys=True) + "\n")
            handle.flush()
            print(json.dumps({k: sample[k] for k in
                              ("t", "level", "battery_temp_c", "hottest_c", "cpu_pct",
                               "windows_per_min", "classified_per_min")
                              if k in sample}, sort_keys=True), flush=True)
            previous = sample
            time.sleep(args.interval)

    first, last = None, None
    with open(out_path, encoding="utf-8") as handle:
        for line in handle:
            entry = json.loads(line)
            first = first or entry
            last = entry
    if first and last:
        span = max(1.0, last["t"] - first["t"]) / 60.0
        print("\nsummary phase=%s over %.1f min: windows=%d classified=%d level_only=%d "
              "(%.0f/min, %.2f classified/min), battery %s->%s, hottest %s->%s C"
              % (args.phase, span, last["windows"], last["classified"], last["level_only"],
                 last["windows"] / span, last["classified"] / span,
                 first.get("level"), last.get("level"),
                 first.get("hottest_c"), last.get("hottest_c")), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
