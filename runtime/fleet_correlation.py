"""Correlate what several devices heard and saw, on one clock.

Cross-device acoustic agreement is the point: two or three microphones hearing the same event
should agree on *when*, and the difference in arrival time plus the difference in level should
agree about *which was closer*. If those two disagree, the match is suspect and this says so
rather than reporting a tidy number.

Device clocks are not the host's clock, and the offsets measured on these phones are 0.7-1.3 s
-- the same order as the 1 Hz publishing rate -- so every device timestamp is converted to host
time using an offset sampled at the start and end of the capture, and the residuals are printed.
No skew correction would mean "the devices agree within a second" could be an artefact.

    python runtime/fleet_correlation.py --serials <a> <b> [<c>] --minutes 10 --out runtime/corr

Writes <out>_<serial>.jsonl per device and <out>_summary.json, and prints the summary.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ADB = r"G:\Android\Sdk\platform-tools\adb.exe"
MATCH_WINDOW_MS = 3000.0        # how far apart two devices' onsets may be and still be "the same"
HEARD = re.compile(r"heard: (\w+), .*speech=([0-9.]+), rms=([0-9.]+)")
PRESENCE = re.compile(
    # The legacy tokens stay first and contiguous; the night-vision change
    # appends raw/stretched/motion/width/dark/verdict after them, so a series
    # recorded before or after it parses with the same expression. `dark` is the
    # device's own verdict against its calibrated threshold, which is why it is
    # preferred below over the hard-coded luma the older lines imply.
    r"presence faces=(\d+)(?: luma=(-?\d+))?(?: unavailable=(\w+))?"
    r"(?: raw=(\d+))?(?: stretched=(\d+))?(?: motion=([0-9.]+))?"
    r"(?: width=(\d+))?(?: dark=(\d+))?(?: verdict=(\w+))?")


def adb(serial, *args, timeout=90):
    return subprocess.run([ADB, "-s", serial, *args], capture_output=True, text=True,
                          timeout=timeout).stdout or ""


def device_ms(serial):
    """The device's own clock, in epoch ms."""
    text = adb(serial, "shell", "date +%s%3N").strip()
    try:
        return int(text)
    except ValueError:
        return None


def offset_model(serial, samples=5, gap=2.0):
    """Offset (device minus host) sampled as a burst, with the sampling jitter reported.

    Two samples 1 s apart showed offsets 240 ms apart between devices -- the same order as the
    1 Hz event rate -- so timing agreement could not be claimed: a 204 ms match was inside the
    clock residual. A burst measures the jitter itself (adb round-trips are not instant), and a
    before/after pair turns real drift into a linear correction. Whatever is left is what this
    tool prints as residual, instead of being quietly attributed to the devices.
    """
    def burst():
        values = []
        for index in range(samples):
            host = int(time.time() * 1000)
            device = device_ms(serial)
            if device is not None:
                values.append(device - host)
            if index < samples - 1:
                time.sleep(gap)
        if not values:
            return None, 0.0
        values.sort()
        mean = sum(values) / len(values)
        return mean, (values[-1] - values[0]) / 2.0

    before, before_spread = burst()
    return {"before": before, "before_spread": before_spread,
            "t_before_ms": time.time() * 1000, "after": None, "after_spread": 0.0}


def close_offset_model(serial, model, samples=5, gap=2.0):
    """Add the post-capture burst, so the offset can be interpolated over the window."""
    after, spread = offset_model(serial, samples=samples, gap=gap)["before"], None
    model["after"] = after
    model["t_after_ms"] = time.time() * 1000
    # Spread of the closing burst, measured the same way as the opening one.
    values = []
    for _ in range(max(2, samples // 2)):
        host = int(time.time() * 1000)
        device = device_ms(serial)
        if device is not None:
            values.append(device - host)
        time.sleep(0.5)
    if values:
        values.sort()
        model["after_spread"] = (values[-1] - values[0]) / 2.0
    return model


def offset_at(model, device_value_ms):
    """The offset to subtract at a moment: linear between the two bursts."""
    before = model.get("before") or 0.0
    after = model.get("after")
    if after is None:
        return before
    span = model.get("t_after_ms", 0.0) - model.get("t_before_ms", 0.0)
    if span <= 0:
        return (before + after) / 2.0
    where = min(1.0, max(0.0, (device_value_ms - model["t_before_ms"]) / span))
    return before + (after - before) * where


def listening_state(serial):
    """Whether this device is really listening, read before anything is cleared.

    Activity, not archaeology. The first version looked for the startup line "listening for
    sound", which rotates out of the buffer within minutes -- so it reported three audibly
    working devices as NOT LISTENING. Caught by running the tool on itself. A window published in
    the last few seconds is proof; the startup line is only a fallback for a device that has
    been quiet since it started.
    """
    text = adb(serial, "logcat", "-d", "-v", "epoch", "-s", "SoundProvider:*")
    latest_audio, last_listen = 0.0, ""
    for line in text.splitlines():
        if "heard:" in line:
            head = line.strip().split(None, 1)
            if head and head[0].replace(".", "").isdigit():
                latest_audio = max(latest_audio, float(head[0]))
        elif "listening" in line:
            last_listen = line.strip()
    age = (time.time() - latest_audio) if latest_audio else None
    return {"listening": bool(age is not None and age <= 15.0),
            "audio_age_s": None if age is None else round(age, 1),
            "last_line": (last_listen or "")[-90:]}


def parse_events(serial, offset_model_for_device):
    """Per-device series, in host time. Audio windows and presence transitions."""
    text = adb(serial, "logcat", "-d", "-v", "epoch", "-s", "SoundProvider:*",
               "VisionProvider:*", timeout=180)
    audio, presence = [], []
    for line in text.splitlines():
        head = line.strip().split(None, 1)
        if not head or not head[0].replace(".", "").isdigit():
            continue
        device_ts = float(head[0]) * 1000.0            # -v epoch prints seconds with decimals
        host_ts = device_ts - offset_at(offset_model_for_device, device_ts)
        match = HEARD.search(line)
        if match:
            audio.append({"t": round(host_ts, 1), "status": match.group(1),
                          "speech": float(match.group(2)),
                          "dbfs": round(20 * __import__("math").log10(max(float(match.group(3)),
                                                                         1e-9)), 1)})
            continue
        match = PRESENCE.search(line)
        if match:
            presence.append({
                "t": round(host_ts, 1), "faces": int(match.group(1)),
                "luma": int(match.group(2)) if match.group(2) else None,
                "unavailable": match.group(3) or "",
                # Absent on lines recorded before the night-vision change, where
                # None means "not measured" rather than "measured as zero".
                "raw": int(match.group(4)) if match.group(4) else None,
                "stretched": int(match.group(5)) if match.group(5) else None,
                "motion": float(match.group(6)) if match.group(6) else None,
                "width": int(match.group(7)) if match.group(7) else None,
                "dark": int(match.group(8)) if match.group(8) else None,
                "verdict": match.group(9) or "",
            })
    return {"audio": audio, "presence": presence}


ONSET_RISE_DB = 9.0            # the contract's own onset rule: a rise, not a level
CLUSTER_WINDOW_MS = 1500.0     # 1 Hz publishing means one event lands up to a second apart


def onsets(sample):
    """Event onsets: a level jump between consecutive windows.

    The contract defines an onset as a *rise* (ONSET_RISE_DB), never a VAD event -- and the first
    version of this tool counted VAD speech, so a room full of hand-claps produced almost no
    onsets at all and the operator's events looked like silence. A clap is level, not speech.

    Note what this implies for timing: the devices publish once a second and sound crosses 3 m in
    9 ms, so delta-T between devices in one room is dominated by *window quantisation*, not by
    propagation. Within a room the level is the usable cross-device signal; delta-T only helps to
    exclude a match, never to measure distance.
    """
    found = []
    for index in range(1, len(sample["audio"])):
        previous, current = sample["audio"][index - 1], sample["audio"][index]
        if current["dbfs"] - previous["dbfs"] >= ONSET_RISE_DB:
            found.append(current)
    return found


def match_across(devices):
    """Cluster co-occurring onsets across devices, and rank who heard each event best.

    The old version compared two devices at a time and called a match suspect when "earlier" and
    "louder" disagreed. Within one room that verdict is meaningless -- sound crosses 3 m in 9 ms
    while the devices publish once a second, so delta-T is quantisation, not propagation -- and it
    printed 17 mixed verdicts over data whose level ordering was unambiguous. The useful question
    is not whether the times agree but *which device was nearest, and whether that changes when
    the source moves*.
    """
    events = []
    for name in sorted(devices):
        for onset in onsets(devices[name]):
            events.append({"device": name, **onset})
    events.sort(key=lambda event: event["t"])

    clusters, current = [], []
    for event in events:
        if current and event["t"] - current[-1]["t"] > CLUSTER_WINDOW_MS:
            clusters.append(current)
            current = []
        current.append(event)
    if current:
        clusters.append(current)

    shared, votes = [], {}
    for cluster in clusters:
        by_device = {}
        for event in cluster:
            # A device can fire twice inside one cluster; keep its loudest window.
            if (event["device"] not in by_device
                    or event["dbfs"] > by_device[event["device"]]):
                by_device[event["device"]] = event["dbfs"]
        if len(by_device) < 2:
            continue
        nearest = max(by_device, key=by_device.get)
        votes[nearest] = votes.get(nearest, 0) + 1
        shared.append({"t": round(cluster[0]["t"], 1),
                       "devices": sorted(by_device),
                       "levels_dbfs": {name: round(value, 1)
                                       for name, value in sorted(by_device.items())},
                       "nearest": nearest,
                       "spread_db": round(max(by_device.values())
                                          - min(by_device.values()), 1),
                       "dt_ms": round(cluster[-1]["t"] - cluster[0]["t"], 1)})

    if len(votes) == 1:
        note = ("one device won every shared event -- which is also what a constant mic-gain "
                "difference looks like. Before concluding distance, repeat with the source "
                "beside each device in turn and check the ranking moves.")
    elif votes:
        note = ("the nearest device changed between events, which is what a ranking that tracks "
                "the source's position looks like")
    else:
        note = "no shared events: nothing to correlate"
    return {"shared_events": shared, "nearest_votes": votes,
            "unanimous": bool(votes) and len(votes) == 1, "note": note}


def presence_agreement(devices, bucket_ms=30000):
    """Whether two cameras saw someone at the same time -- and whether they could see at all.

    Zero overlap was once reported as "not the same space", which was wrong: the devices were in
    one room with the lights off, and a face detector that cannot work in the dark produced
    faces=0 everywhere. Absence of detection was reported as absence of people. A dark bucket now
    says so, and the verdict distinguishes "cannot see" from "sees nothing".
    """
    buckets, dark = {}, {}
    for name, sample in devices.items():
        seen, dark_buckets = set(), set()
        start = sample["presence"][0]["t"] if sample["presence"] else 0.0
        for event in sample["presence"]:
            where = int((event["t"] - start) // bucket_ms)
            if event["faces"] > 0:
                seen.add(where)
            luma = event.get("luma")
            # The device's own judgement first: `dark` is its calibrated threshold
            # applied to its own sensor and exposure. The luma rule stays for series
            # recorded before the threshold became per-device.
            if (event.get("unavailable") == "too_dark" or event.get("dark") == 1
                    or (event.get("dark") is None and luma is not None
                        and 0 <= luma <= 12)):
                dark_buckets.add(where)
        buckets[name] = seen
        dark[name] = dark_buckets

    names = sorted(devices)
    shared, total = 0, 0
    for index, first in enumerate(names):
        for second in names[index + 1:]:
            span = max(buckets[first] | buckets[second] | dark[first] | {0})
            total += span + 1
            shared += len(buckets[first] & buckets[second])

    dark_observations = sum(len(dark[name]) for name in names)
    if total > 0 and dark_observations >= total * 0.5:
        verdict = ("cameras were too dark to be evidence: presence says nothing about who was "
                   "there, so the visual axis is unavailable rather than contradictory")
    elif shared > 0:
        verdict = "%d shared presence buckets: the views overlap" % shared
    else:
        verdict = ("no shared presence and the cameras were not dark: they are not looking at "
                   "the same place")
    return {"shared_presence_buckets": shared, "buckets_in_span": max(1, total),
            "dark_buckets": {name: len(dark[name]) for name in names}, "verdict": verdict}


PHASE_KIND = "phase"


def mark_phase(out_dir, label, text):
    """Append a phase marker for this run, in host time.

    Phases are marked by a separate invocation rather than by the capture loop:
    the operator marks them as they happen ("lights on", "person 1m in front of
    the A51") from a second shell while the capture keeps running. Host time is
    the right clock for that -- these are physical events, not device events, and
    the presence series is already read in host time.
    """
    entry = {"kind": PHASE_KIND, "label": text, "t": round(time.time(), 1)}
    with open("%s_%s_phases.jsonl" % (out_dir, label), "a",
              encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def load_phases(out_dir, label):
    """The phase markers recorded for this run, ordered by host time, in milliseconds.

    `mark_phase` writes host time in seconds, because that is what its confirmation line shows a
    human, but every series these markers are compared against is in milliseconds: `parse_events`
    converts each device timestamp to epoch ms, the offset models are measured in ms, and
    `phase_windows` compares against those numbers directly. Comparing the two without converting
    made every window miss every sample -- the phases block came back empty for every phase except
    the last, which quietly collected the entire session under the label "end". Converted here,
    once, at the only place the two meet.
    """
    path = "%s_%s_phases.jsonl" % (out_dir, label)
    phases = []
    if Path(path).is_file():
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry.get("kind") == PHASE_KIND:
                    entry["t"] = round(float(entry["t"]) * 1000.0, 1)
                    phases.append(entry)
    return sorted(phases, key=lambda entry: entry["t"])


def phase_windows(phases, end_t):
    """[(label, start, end)]: each phase runs to the next marker, or to end_t."""
    windows = []
    for index, entry in enumerate(phases):
        end = phases[index + 1]["t"] if index + 1 < len(phases) else end_t
        windows.append((entry["label"], entry["t"], end))
    return windows


def phase_presence_summary(devices, phases):
    """Per phase, per device: what was seen, and whether the camera was blind.

    This is what a staged session is for. A lit phase with no faces and no motion
    is an empty room; the same phase with dark samples is the camera refusing to
    answer, which is a different fact about the world and has to read as one.
    """
    end_t = max((sample["presence"][-1]["t"] for sample in devices.values()
                 if sample["presence"]), default=0.0)
    summary = {}
    for label, start, end in phase_windows(phases, end_t):
        per_device = {}
        for name, sample in devices.items():
            events = [event for event in sample["presence"]
                      if start <= event["t"] <= end]
            per_device[name] = {
                "samples": len(events),
                "faces_max": max((event["faces"] for event in events), default=0),
                "motion_max": max((event.get("motion") or 0.0 for event in events),
                                  default=0.0),
                "dark": sum(1 for event in events if event.get("dark") == 1),
                "verdicts": sorted({event["verdict"] for event in events
                                    if event.get("verdict")}),
            }
        summary[label] = per_device
    return summary


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--serials", nargs="+",
                        help="device serials to capture (not needed to mark a phase)")
    parser.add_argument("--minutes", type=float, default=10.0)
    parser.add_argument("--out", default=str(REPO / "runtime" / "corr"))
    parser.add_argument("--label", default="run")
    parser.add_argument("--mark-phase", dest="mark_phase", default=None,
                        metavar="TEXT",
                        help="append a phase marker for --label and exit, so a "
                             "staged session can be marked from a second shell "
                             "while the capture keeps running")
    args = parser.parse_args(argv)

    if args.mark_phase:
        entry = mark_phase(args.out, args.label, args.mark_phase)
        print("phase marked at t=%.1f: %s" % (entry["t"], args.mark_phase),
              flush=True)
        return 0
    if not args.serials:
        parser.error("--serials is required for a capture run")

    models, listening = {}, {}
    print("=== pre-flight ===", flush=True)
    for serial in args.serials:
        listening[serial[4:14]] = listening_state(serial)
        model = offset_model(serial)
        models[serial] = model
        print("  %s listening=%s  offset=%.0fms  burst_spread=±%.0fms"
              % (serial[4:14], listening[serial[4:14]]["listening"],
                 model["before"] or 0.0, model["before_spread"]), flush=True)
        if not listening[serial[4:14]]["listening"]:
            print("    NOT LISTENING: %s" % listening[serial[4:14]]["last_line"], flush=True)
    for serial in args.serials:
        adb(serial, "logcat", "-c")

    print("\n=== capturing for %.1f minutes ===" % args.minutes, flush=True)
    time.sleep(max(5.0, args.minutes * 60.0))

    print("=== closing offsets ===", flush=True)
    for serial in args.serials:
        close_offset_model(serial, models[serial])
        model = models[serial]
        print("  %s offset %.0f -> %.0fms  (drift %+.0fms over the window, residual ±%.0fms)"
              % (serial[4:14], model["before"] or 0.0, model["after"] or 0.0,
                 (model["after"] or 0.0) - (model["before"] or 0.0),
                 max(model["before_spread"], model["after_spread"])), flush=True)

    devices = {serial[4:14]: parse_events(serial, models[serial]) for serial in args.serials}
    phases = load_phases(args.out, args.label)
    phase_block = phase_presence_summary(devices, phases) if phases else {}
    if phases and all(info["samples"] == 0
                      for per_device in phase_block.values()
                      for info in per_device.values()):
        # The symptom this guards against was silent: empty phases read as an empty room.
        print("  WARNING: %d phase markers matched zero of the %d presence events that were "
              "read; check that the markers and the series are in the same unit"
              % (len(phases), sum(len(sample["presence"]) for sample in devices.values())),
              flush=True)
    names = sorted(devices)
    for name in names:
        sample = devices[name]
        with open("%s_%s_%s.jsonl" % (args.out, args.label, name), "w",
                  encoding="utf-8") as handle:
            for entry in sample["audio"]:
                handle.write(json.dumps({"kind": "audio", **entry}, sort_keys=True) + "\n")
            for entry in sample["presence"]:
                handle.write(json.dumps({"kind": "presence", **entry}, sort_keys=True) + "\n")

    correlation = match_across(devices)
    presence = presence_agreement(devices)
    summary = {
        "label": args.label,
        "minutes": args.minutes,
        "offsets_ms": {serial[4:14]: {"before": model["before"], "after": model["after"],
                                      "residual": max(model["before_spread"],
                                                      model["after_spread"])}
                       for serial, model in models.items()},
        "listening": listening,
        "per_device": {name: {"audio_windows": len(devices[name]["audio"]),
                              "onsets": len(onsets(devices[name])),
                              "presence_changes": len(devices[name]["presence"]),
                              "faces_seen": sum(1 for event in devices[name]["presence"]
                                                if event["faces"] > 0)}
                       for name in names},
        "correlation": correlation,
        "presence_agreement": presence,
        "phases": phase_block,
    }
    out = "%s_summary.json" % args.out
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)

    print("\n=== summary ===", flush=True)
    for name in names:
        info = summary["per_device"][name]
        print("  %s  windows=%d onsets=%d presence_changes=%d faces_seen=%d"
              % (name, info["audio_windows"], info["onsets"],
                 info["presence_changes"], info["faces_seen"]), flush=True)
    print("  shared events: %d   nearest votes: %s"
          % (len(correlation["shared_events"]), correlation["nearest_votes"]), flush=True)
    for event in correlation["shared_events"][:20]:
        print("    dt=%6.1fms spread=%5.1fdb NEAREST=%-8s  %s"
              % (event["dt_ms"], event["spread_db"], event["nearest"][:8],
                 "  ".join("%s=%.1f" % (name[:6], value)
                           for name, value in event["levels_dbfs"].items())), flush=True)
    print("  %s" % correlation["note"], flush=True)
    print("  presence: %d of %d buckets shared  dark=%s"
          % (presence["shared_presence_buckets"], presence["buckets_in_span"],
             presence.get("dark_buckets", {})), flush=True)
    print("  %s" % presence["verdict"], flush=True)
    if phase_block:
        print("\n=== phases ===", flush=True)
        for label, per_device in phase_block.items():
            print("  %s" % label, flush=True)
            for name in sorted(per_device):
                info = per_device[name]
                print("    %-6s samples=%d faces_max=%d motion_max=%.1f dark=%d %s"
                      % (name, info["samples"], info["faces_max"],
                         info["motion_max"], info["dark"],
                         ",".join(info["verdicts"])), flush=True)
    print("  wrote %s" % out, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
