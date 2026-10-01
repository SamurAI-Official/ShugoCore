"""Produce the phase block from the per-device log tails, which cannot rotate.

`fleet_correlation.py` reads each device's logcat ring once, at the end of a capture. That ring is
capped at 5 MiB on these devices and cannot be raised -- `logcat -G 8M` is refused with "MAX log
buffer size is 5 MiB" -- so a busy device can rotate out the early phases that a staged session
was run to measure. The failure is silent in the worst way: the phase block reports samples=0,
which is indistinguishable from a room that was genuinely empty.

The capture is accompanied by a per-device `logcat` tail to a file, which only grows. This reads
those files and computes the same per-phase numbers from them, so agreement between the two is
evidence about the world and disagreement is evidence about the buffer. It borrows the tool's own
patterns, marker loader and window rule rather than re-implementing them, because a cross-check
that parses differently is testing its own parser instead of the data.

    python runtime/vis_phase_check.py --label vis --out runtime/corr_vis

Writes <out>_phases_check.json and prints the table.
"""
import argparse
import glob
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fleet_correlation as fc          # noqa: E402  (patterns, marker loader, window rule)


def offsets_from_summary(path):
    """{device tag: offset ms}: measured in situ by the capture, so the two agree on the clock."""
    if not Path(path).is_file():
        return {}
    with open(path, encoding="utf-8") as handle:
        summary = json.load(handle)
    return {tag: info.get("before") for tag, info in (summary.get("offsets_ms") or {}).items()}


def tag_for(path, offsets):
    """The summary's key for a tail file, which is named vis_tail_<tag>.txt."""
    stem = path.stem.replace("vis_tail_", "").rstrip("-")
    for key in offsets:
        if key and stem.startswith(key):
            return key
    return stem


def parse_tail(path, offset_ms):
    """Presence samples and audio windows from one tail file, in host milliseconds.

    The same expressions the tool applies to the ring, so a line one reads the other reads.
    """
    presence, audio = [], []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        head = line.strip().split(None, 1)
        if not head or not head[0].replace(".", "").isdigit():
            continue
        host_ms = float(head[0]) * 1000.0 - offset_ms
        match = fc.HEARD.search(line)
        if match:
            audio.append({"t": round(host_ms, 1), "status": match.group(1),
                          "speech": float(match.group(2)), "rms": float(match.group(3))})
            continue
        match = fc.PRESENCE.search(line)
        if match:
            presence.append({
                "t": round(host_ms, 1),
                "faces": int(match.group(1)),
                "luma": int(match.group(2)) if match.group(2) else None,
                "raw": int(match.group(4)) if match.group(4) else None,
                "stretched": int(match.group(5)) if match.group(5) else None,
                "motion": float(match.group(6)) if match.group(6) else None,
                "width": int(match.group(7)) if match.group(7) else None,
                "dark": int(match.group(8)) if match.group(8) else None,
                "verdict": match.group(9) or "",
            })
    return presence, audio


def summarise(devices, windows):
    """Per phase, per device: the tool's fields, plus the ones it does not report.

    luma_min/luma_max and raw_max/stretched_max are the additions that matter here: the dark
    threshold is a luma, and the stretch question is decided by raw vs stretched per device.
    """
    out = {}
    for label, start, end in windows:
        per_device = {}
        for name, sample in devices.items():
            events = [event for event in sample["presence"] if start <= event["t"] <= end]
            audio = [event for event in sample["audio"] if start <= event["t"] <= end]
            lumas = [event["luma"] for event in events if event["luma"] is not None]
            raws = [event["raw"] for event in events if event["raw"] is not None]
            stretched = [event["stretched"] for event in events
                         if event["stretched"] is not None]
            motions = [event["motion"] for event in events if event["motion"] is not None]
            per_device[name] = {
                "samples": len(events),
                "faces_max": max((event["faces"] for event in events), default=0),
                "luma_min": min(lumas) if lumas else None,
                "luma_max": max(lumas) if lumas else None,
                "motion_max": round(max(motions), 1) if motions else None,
                "raw_max": max(raws) if raws else None,
                "stretched_max": max(stretched) if stretched else None,
                "dark": sum(1 for event in events if event.get("dark") == 1),
                "widths": sorted({event["width"] for event in events if event["width"]}),
                "verdicts": sorted({event["verdict"] for event in events if event["verdict"]}),
                "audio_windows": len(audio),
                "onsets": sum(1 for event in audio if event["status"] != "level_only"),
            }
        out[label] = per_device
    return out


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="vis")
    parser.add_argument("--out", default=str(HERE / "corr_vis"))
    parser.add_argument("--tails", default=str(HERE / "vis_tail_*.txt"))
    parser.add_argument("--summary", default=None,
                        help="the capture's summary, for the in-situ clock offsets "
                             "(default: <out>_summary.json)")
    args = parser.parse_args(argv)

    summary_path = args.summary or ("%s_summary.json" % args.out)
    offsets = offsets_from_summary(summary_path)
    if not offsets:
        print("  NOTE: no offsets in %s; using 0, so phases may be attributed ~1s off"
              % summary_path, flush=True)

    phases = fc.load_phases(args.out, args.label)
    if not phases:
        print("  no phase markers for label %s in %s" % (args.label, args.out), flush=True)
        return 1

    tails = sorted(Path(path) for path in glob.glob(args.tails) if "_err" not in Path(path).name)
    if not tails:
        print("  no tail files matching %s" % args.tails, flush=True)
        return 1

    devices = {}
    for path in tails:
        tag = tag_for(path, offsets)
        presence, audio = parse_tail(path, offsets.get(tag) or 0.0)
        devices[tag] = {"presence": presence, "audio": audio, "file": str(path)}
        print("  %-12s %-28s presence=%-5d audio=%-5d offset=%.0fms"
              % (tag, path.name, len(presence), len(audio), offsets.get(tag) or 0.0), flush=True)

    end_t = max((sample["presence"][-1]["t"] for sample in devices.values()
                 if sample["presence"]), default=0.0)
    windows = fc.phase_windows(phases, end_t)
    block = summarise(devices, windows)

    print("\n=== phases (from the tails) ===", flush=True)
    for label, start, end in windows:
        print("  %-26s %6.0fs" % (label, (end - start) / 1000.0), flush=True)
        for name in sorted(block[label]):
            info = block[label][name]
            print("    %-12s samples=%-4d faces_max=%d luma=%s..%s motion_max=%s "
                  "raw_max=%s stretched_max=%s dark=%-3d onsets=%-2d [%s]"
                  % (name, info["samples"], info["faces_max"], info["luma_min"],
                     info["luma_max"], info["motion_max"], info["raw_max"],
                     info["stretched_max"], info["dark"], info["onsets"],
                     ",".join(info["verdicts"])), flush=True)

    out = "%s_phases_check.json" % args.out
    with open(out, "w", encoding="utf-8") as handle:
        json.dump({"label": args.label, "source": [str(path) for path in tails],
                   "offsets_ms": offsets, "phases": block}, handle, indent=2, sort_keys=True)
    print("\n  wrote %s" % out, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
