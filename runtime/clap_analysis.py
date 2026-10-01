"""Find the clap rounds in a session's audio and ask whether they locate the source.

The operator's claps did not land in the marked phases: they came at the end, as two rounds of
four claps at each node -- one round in the dark, one with the lights on, in the order A16, S9FE,
A51 (the reverse of the face-test order). Phase markers cannot attribute those, so this works
from the audio itself: every transient the log recorded, in host time, with each device's level,
grouped so that the rounds and the nodes can be read off the pattern instead of assumed.

That repetition is the point. The earlier single-round attempt could not separate "this device is
louder" from "the source was nearer it", because one device was up to 15 dB hotter than the others
throughout. Four claps per node, twice, gives enough samples to look at a device's level relative
to its own median: the fixed sensitivity drops out, and what is left is either structure that
tracks the clap position or nothing at all.

Reads the log dumps written by the session (runtime/clap_ring_<tag>.txt or the tails).

    python runtime/clap_analysis.py --files runtime/clap_ring_*.txt --gap-ms 6000
"""
import argparse
import glob
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fleet_correlation as fc          # noqa: E402  (the log patterns, so parsing cannot drift)

# Measured in situ during the session this data came from; drift over the window was under 1 ms.
OFFSETS_MS = {"S9FE": 992.0, "A51": 1354.0, "A16": 949.0}
# A host epoch that was on the clock when the dumps were taken, for readable labels only.
CLOCK_ANCHOR_MS = 1790806864000.0
CLOCK_ANCHOR_TEXT = "15:21:00"


def parse_dump(path, offset_ms):
    """Transients and presence samples from one device's dump, in host milliseconds."""
    heard, presence = [], []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        head = line.strip().split(None, 1)
        if not head or not head[0].replace(".", "").isdigit():
            continue
        host_ms = float(head[0]) * 1000.0 - offset_ms
        match = fc.HEARD.search(line)
        if match:
            rms = max(float(match.group(3)), 1e-9)
            heard.append({"t": host_ms, "status": match.group(1),
                          "speech": float(match.group(2)),
                          "dbfs": round(20 * math.log10(rms), 1),
                          "raw": line.strip()})
            continue
        match = fc.PRESENCE.search(line)
        if match:
            presence.append({"t": host_ms,
                             "faces": int(match.group(1)),
                             "luma": int(match.group(2)) if match.group(2) else None,
                             "motion": float(match.group(6)) if match.group(6) else None,
                             "dark": int(match.group(8)) if match.group(8) else None,
                             "verdict": match.group(9) or ""})
    return heard, presence


def clock(host_ms):
    """A readable host clock for a sample, anchored to when the dumps were taken."""
    seconds = (host_ms - CLOCK_ANCHOR_MS) / 1000.0
    hh, mm, ss = 15, 21, 0
    total = hh * 3600 + mm * 60 + ss + int(round(seconds))
    return "%02d:%02d:%02d" % ((total // 3600) % 24, (total // 60) % 60, total % 60)


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", default=str(HERE / "clap_ring_*.txt"))
    parser.add_argument("--gap-ms", dest="gap_ms", type=float, default=6000.0,
                        help="transients closer together than this are one node visit")
    parser.add_argument("--loud-db", dest="loud_db", type=float, default=10.0,
                        help="a clap is this many dB above the device's median transient level")
    parser.add_argument("--out", default=str(HERE / "clap_analysis.json"))
    args = parser.parse_args(argv)

    paths = sorted(Path(path) for path in glob.glob(args.files) if "_err" not in Path(path).name)
    devices = {}
    for path in paths:
        tag = next((name for name in OFFSETS_MS if name in path.name), None)
        if tag is None:
            print("  skipping %s: no device name in the file name" % path.name, flush=True)
            continue
        heard, presence = parse_dump(path, OFFSETS_MS[tag])
        # Every `heard:` line is kept, `level_only` included. A clap does not have to match a
        # sound label to be loud, and loudness is the evidence; filtering to labelled detections
        # is what hid the clap rounds from the first pass over this data.
        transients = heard
        devices[tag] = {"heard": transients, "presence": presence, "all_statuses":
                        sorted({event["status"] for event in heard})}
        lumas = [event["luma"] for event in presence if event["luma"] is not None]
        print("  %-5s %-26s transients=%-5d presence=%-5d luma=%s..%s"
              % (tag, path.name, len(transients), len(presence),
                 min(lumas) if lumas else None, max(lumas) if lumas else None), flush=True)
    if not devices:
        print("  no usable dumps", flush=True)
        return 1
    print("  statuses seen: %s"
          % "  ".join("%s=[%s]" % (name, ",".join(devices[name]["all_statuses"]))
                      for name in sorted(devices)), flush=True)

    # A clap has to be found by loudness, not by a detection being present: once the room is
    # noisy the classifier logs "heard: ok" about once a second, so the claps sit inside a
    # continuous stream of detections rather than standing alone in silence.
    for name, sample in devices.items():
        levels = sorted(event["dbfs"] for event in sample["heard"])
        median = levels[len(levels) // 2] if levels else 0.0
        cutoff = median + args.loud_db
        sample["loud"] = [event for event in sample["heard"] if event["dbfs"] >= cutoff]
        print("  %-5s dbfs min=%6.1f median=%6.1f p90=%6.1f max=%6.1f -> loud(>=%6.1f)=%d"
              % (name, levels[0] if levels else 0.0, median,
                 levels[int(len(levels) * 0.9)] if levels else 0.0,
                 levels[-1] if levels else 0.0, cutoff, len(sample["loud"])), flush=True)

    print("\n=== loudest heard lines: a clap should stand out here ===", flush=True)
    for name in sorted(devices):
        events = sorted(devices[name]["heard"], key=lambda event: -event["dbfs"])[:12]
        print("  %s, top %d by rms:" % (name, len(events)), flush=True)
        for event in events:
            print("    %s  %6.1f dBFS  %s" % (clock(event["t"]), event["dbfs"],
                                              event["raw"][:108]), flush=True)

    print("\n=== loud events per minute ===", flush=True)
    loud_minutes = {}
    for name, sample in devices.items():
        for event in sample["loud"]:
            loud_minutes.setdefault(int(event["t"] // 60000), {}).setdefault(
                name, []).append(event["dbfs"])
    for key in sorted(loud_minutes):
        cells = []
        for name in sorted(loud_minutes[key]):
            values = loud_minutes[key][name]
            cells.append("%s x%-3d max=%6.1f" % (name, len(values), max(values)))
        print("  %s   %s" % (clock(key * 60000), "   ".join(cells)), flush=True)

    print("\n=== luma per minute, as the lights changed ===", flush=True)
    minutes = {}
    for name, sample in devices.items():
        for event in sample["presence"]:
            if event["luma"] is None:
                continue
            minutes.setdefault(int(event["t"] // 60000), {}).setdefault(name, []).append(event["luma"])
    for key in sorted(minutes):
        cells = []
        for name in sorted(minutes[key]):
            values = minutes[key][name]
            cells.append("%s=%3d/max%3d" % (name, int(sum(values) / len(values)), max(values)))
        print("  %s   %s" % (clock(key * 60000), "   ".join(cells)), flush=True)

    groups = group_transients(devices, args.gap_ms)
    per_device_levels = {name: [] for name in devices}
    for group in groups:
        for name in devices:
            levels = [event["dbfs"] for event in group["events"] if event["device"] == name]
            if levels:
                per_device_levels[name].append(max(levels))
    medians = {}
    for name, values in per_device_levels.items():
        if values:
            values = sorted(values)
            medians[name] = values[len(values) // 2]

    print("\n=== transient groups, %.0fs gap ===" % (args.gap_ms / 1000.0), flush=True)
    print("  each device's own median level: %s"
          % "  ".join("%s=%.1f" % (name, medians[name]) for name in sorted(medians)), flush=True)
    summary = []
    for index, group in enumerate(groups, 1):
        levels, counts = {}, {}
        for name in devices:
            values = [event["dbfs"] for event in group["events"] if event["device"] == name]
            if values:
                levels[name] = max(values)
                counts[name] = len(values)
        ranking = sorted(levels, key=lambda name: -levels[name])
        relative = {name: round(levels[name] - medians.get(name, 0.0), 1) for name in levels}
        relative_ranking = sorted(relative, key=lambda name: -relative[name])
        print("  #%-2d %s  span=%5.1fs  %s" % (index, clock(group["first"]),
               (group["last"] - group["first"]) / 1000.0,
               "  ".join("%s=%6.1f x%d" % (name, levels[name], counts[name])
                         for name in sorted(levels))), flush=True)
        print("       loudest: %-30s rel-to-own-median: %s"
              % (" > ".join(ranking),
                 "  ".join("%s%+.1f" % (name, relative[name]) for name in relative_ranking)),
              flush=True)
        summary.append({"index": index, "clock": clock(group["first"]),
                        "span_s": round((group["last"] - group["first"]) / 1000.0, 1),
                        "counts": counts, "levels_dbfs": levels, "ranking": ranking,
                        "relative_to_median": relative, "relative_ranking": relative_ranking})

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"files": [str(path) for path in paths], "medians_dbfs": medians,
                   "groups": summary}, handle, indent=2, sort_keys=True)
    print("\n  wrote %s" % args.out, flush=True)
    return 0

def group_transients(devices, gap_ms):
    """Group transients across devices: one group is one node visit, four claps or a burst.

    Devices are pooled because a clap is heard by all of them: grouping per device would produce
    three unreconciled series instead of one event with three levels, which is the whole point.
    """
    events = []
    for name, sample in devices.items():
        for event in sample["loud"]:
            events.append({**event, "device": name})
    events.sort(key=lambda event: event["t"])
    groups = []
    for event in events:
        if groups and event["t"] - groups[-1]["last"] <= gap_ms:
            groups[-1]["events"].append(event)
            groups[-1]["last"] = event["t"]
        else:
            groups.append({"first": event["t"], "last": event["t"], "events": [event]})
    return groups


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
