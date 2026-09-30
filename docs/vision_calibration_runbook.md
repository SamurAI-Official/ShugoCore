# Vision calibration runbook

What the staged session is for, and how to run it. Four questions get answers
here that no amount of desk work can supply:

1. **The in-situ acoustic offsets.** A grouped calibration cannot be carried into
   the room (measured: the A51 sits ~10 dB down in-room while the group was ~1 dB
   apart), so the offsets have to be taken where the devices actually stand.
2. **The per-device dark threshold.** `DARK_LUMA_MAX = 12` is a starting point, not
   a measurement. The transition band is what matters: the luma at which faces
   stop appearing while a person is still visibly there.
3. **Whether the stretch helps or invents.** `raw=` vs `stretched=` per device, in
   light and dark, with the person's position known.
4. **That NRR is unchanged.** The self-test and camera-probe lines against the
   recorded baseline, with exposure at its default.

## Preconditions

- Build and install: `runtime/deploy_1_30_24.py` (the current version's script).
- The camera binds **only while the UI lifecycle is alive**, so the app has to be
  in the foreground for frames to arrive at all. A device sitting at a black
  screen looks exactly like a dead camera.
- Raise the screen timeout on every device before anything else. The A51 shipped with a 60 s
  timeout: it would have slept a minute into the run, unbinding the camera while the capture
  carried on. `adb -s <serial> shell settings put system screen_off_timeout 1800000`, and put the
  originals back afterwards.
- Record the NRR baseline before changing anything, from a fresh launch, somewhere that outlives
  the log buffer (`runtime/vis_baseline.txt` in the first session):
  - `NRR self-test ok: 768 bytes, 17 distinct, 3.6-9.5ms`
  - `NRR camera frame render ok: 320x426 -> 408960 bytes, 88 distinct, 11.0ms`
  - `NRRBridge: power status: scale=... battery=... charging=...` (or the stub
    line), and `VisionProvider: analysis width=320 (NRR advised ...)`.

## Setting the vision policy (SENSORS tab, or prefs as a fallback)

Three preferences drive this stage:

| pref | meaning | default |
|---|---|---|
| `vision_dark_luma_max` | mean luma at or below which the camera is blind | 12 |
| `vision_calibration` | log one line per analysed frame instead of per change | false |
| `vision_exposure_steps` | Camera2 AE compensation steps (0 = auto) | 0 |

**The SENSORS tab drives all three** — the `NIGHT VISION` section, just below the
camera preview, which is the camera they act on:

- *Dark threshold* and *Exposure* are steppers (`-10 / -1 / +1 / +10` and
  `reset`). Each writes the preference and asks the service to apply it at once,
  so the provider's log line follows within about a second.
- *Calibration log* toggles per-frame logging.
- The line under the controls is the device's own readout, and it is **state, not
  an echo of the request**: `22 (pref 22)` means the provider is running with 22
  and 22 is stored; `12 (pref -1)` means running with the default because nothing
  is stored yet. When the two numbers disagree, the apply has not landed — that is
  worth looking at rather than assuming.
- The readout also carries what the last analysed frame said, e.g.
  `luma=105 · motion=4.5 · verdict=motion · faces/stretched=0/0 · width=320`.
  That is the same number the log will be analysed from, read on the phone that
  produced it, with no host in the loop.

Fallback if the pane is unusable: on a debug build set the preference directly
(`adb shell run-as com.samurai.shugocore`, then edit
`shared_prefs/shugocore_prefs.xml`) and relaunch. Either way the app logs what it
applied (`VisionProvider: dark threshold=...`, `low-light exposure: ...`), so the
log stays the record of what the session actually ran with.

Start the session with `vision_calibration` **on**: the luma transition band is
the thing being measured, and change-driven logging hides it.

### Two things learned on hardware

- **Keep the app in the foreground.** The camera binds only while the UI lifecycle
  is alive, so the pane readout and the presence log both go quiet the moment the
  app is backgrounded — a device at a black screen looks exactly like a dead
  camera.
- **Check for an ANR dialog before believing a control is broken.** On the S9FE a
  tap on this pane produced `ANR ... Reason: Input dispatching timed out` twice.
  The first happened *before any of these controls was touched*, so it is the
  pane's own per-second refresh, not the controls. Two mitigations are in: the
  preview JPEG is decoded only when a new frame arrives, and the immediate apply
  runs on the service's executor rather than the UI thread. Neither is proof the
  stall cannot recur, and a tap that lands on the dialog does nothing at all.

## The session

Two captures, or one long one with phases marked. The phases are recorded from a
second shell, in host time, while the capture keeps running:

```
python runtime/fleet_correlation.py --serials <SERIALS> --minutes 12 --label vis
# in another shell, as each step happens:
python runtime/fleet_correlation.py --label vis --mark-phase "lights on"
python runtime/fleet_correlation.py --label vis --mark-phase "person 1m a51"
python runtime/fleet_correlation.py --label vis --mark-phase "person 1m s9fe"
python runtime/fleet_correlation.py --label vis --mark-phase "person 1m a16"
python runtime/fleet_correlation.py --label vis --mark-phase "claps a51 position"
python runtime/fleet_correlation.py --label vis --mark-phase "claps s9fe position"
python runtime/fleet_correlation.py --label vis --mark-phase "lights off"
python runtime/fleet_correlation.py --label vis --mark-phase "dark empty"
python runtime/fleet_correlation.py --label vis --mark-phase "dark person a51"
...
```

### Run a logcat tail per device as well

`fleet_correlation.py` reads each device's ring once, at the end. The ring is 5 MiB on these
devices and cannot be enlarged -- `logcat -G 8M` is refused with "MAX log buffer size is 5 MiB" --
and a phase that rotates out reads as `samples=0`, which is exactly how an empty room reads. That
is why the tool now warns when every phase comes back empty, which is also the symptom a units
mistake produces.

The tails are cheap insurance, and in the first session they *confirmed* rather than corrected the
ring: all fifteen session phases came out with the same sample count from both sources. What did
differ was the two reads' windows -- the tails' totals include the old ring content they dump at
start, and the ring's `end` phase kept collecting past the tails' last line -- so compare phases,
not totals.

```
adb -s <serial> logcat -v epoch -s SoundProvider:* VisionProvider:* > runtime/vis_tail_<tag>.txt
```

One per device, running for the whole capture. Then read the phase block from those files, with
the offsets the capture measured in situ:

```
python runtime/vis_phase_check.py --label vis --out runtime/corr_vis
```

Two rules for the phases:

- **One person, one device at a time**, and say where they stand. Two people in
  frame make every count ambiguous.
- **Hold still for the "faces" phases and move for the "motion" ones.** Motion
  without faces is the dark's only presence evidence, so it needs a phase where
  movement is the deliberate variable.

## Reading the result

`runtime/corr_vis_summary.json` carries a `phases` block, and `runtime/vis_phase_check.py` writes
the same block from the tails; in the first session the two agreed phase for phase. Read whichever
you have, but know that the summary's copy comes from the ring, and that until the units bug was
fixed this code compared markers (seconds) against samples (milliseconds) and so reported every
phase empty except the last, which collected the whole session -- so a phases block that is empty
everywhere except `end` is the signature of that bug, not of an empty room. From either source the
numbers are per phase, per device: `samples`, `faces_max`, `motion_max`, `dark`, and the
`verdicts` seen. Then:

- **Dark threshold**: the phase with the person present but `faces_max=0` gives the
  luma to set `vision_dark_luma_max` from -- the highest luma at which the camera
  could not see them.
- **Stretch**: `stretched > raw` with the person there is the gain it earned;
  `stretched > 0` with the phase marked empty is it inventing a face.
- **Motion floor**: `motion_max` in the dark-empty phase is the noise floor;
  `motion_max` in the dark-person phase is the signal. `MOTION_MIN` belongs
  between them.
- **Exposure A/B**: run the dark-person phase at `vision_exposure_steps=0` and
  again at a raised value, and compare the two phase blocks *and* the NRR camera
  probe's distinct-byte count. Raising exposure moves NRR's pixels too, so the
  probe figure changing is expected -- what would not be acceptable is an
  unrecorded change.
- **Offsets**: the per-device clock offsets the tool prints are what the acoustic
  vote compares against the visual vote. The distance claim stays withdrawn until
  these are in-situ rather than grouped.

### What the first staged session established

Written down because the next session should not have to re-learn it.

- **The in-situ offsets are measured** -- question 1 is answered: 992 ms / 1354 ms / 949 ms
  (S9FE / A51 / A16), sampled as bursts with residuals of ±33 / ±16 / ±26 ms. That was the part
  of the distance claim that was missing.
- **The distance claim still does not hold, for a better reason.** 26 of 31 shared clap events
  voted the A16 nearest regardless of where the claps were made, with its levels 7-15 dB above the
  others' throughout: that is a per-device sensitivity difference, not geometry. Arrival time
  cannot arbitrate either -- the differences cluster at 0.37-0.60 s, which is the 1 Hz publishing
  quantum rather than sound propagation. Ranking by level needs per-device level calibration;
  ranking by time needs a faster rate than the logger publishes at.
- **The room never got dark.** All sixteen phases reported `dark=0`, with luma between 89 and
  134 -- the "lights off" phases included. Faces were still detected at luma 43, so the dark
  threshold is not measured; what is measured is that detection survives to luma 43, which is why
  the default of 12 has not been contradicted. Note that with `vision_exposure_steps=0` (AE auto)
  a dark room is brightened straight back up, so "dark" may not be observable at all until
  exposure is pinned. Run the exposure A/B first, then another dark attempt.
- **"Dark empty" has to mean nobody in frame.** The first session's wording was "nobody moving",
  the operator reasonably stayed in view, and the phase recorded `faces_max=1` -- so it could not
  serve as the empty reference the stretch-invention test needs.
- **The A51 samples thinly**: 0.15 presence lines/s against the A16's 0.57 and the S9FE's 0.19,
  and its own lit phase caught only 3 samples. Hold still longer in front of it, or accept that
  its phase evidence is weaker than the others'.
