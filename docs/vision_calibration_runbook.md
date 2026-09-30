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
- Record the NRR baseline before changing anything, from a fresh launch:
  - `NRR self-test ok: 768 bytes, 17 distinct, 3.6-9.5ms`
  - `NRR camera frame render ok: 320x426 -> 408960 bytes, 88 distinct, 11.0ms`
  - `NRRBridge: power status: scale=... battery=... charging=...` (or the stub
    line), and `VisionProvider: analysis width=320 (NRR advised ...)`.

## Setting the vision policy (rough edge, read this)

Three preferences drive Stage B, and **nothing in the UI writes them yet**:

| pref | meaning | default |
|---|---|---|
| `vision_dark_luma_max` | mean luma at or below which the camera is blind | 12 |
| `vision_calibration` | log one line per analysed frame instead of per change | false |
| `vision_exposure_steps` | Camera2 AE compensation steps (0 = auto) | 0 |

On a debug build they can be placed directly, e.g.
`adb shell run-as com.samurai.shugocore` and edit
`shared_prefs/shugocore_prefs.xml`, then relaunch. The alternative is to set the
constant and redeploy. Either way the app logs what it applied
(`VisionProvider: dark threshold=...`, `low-light exposure: ...`), so the log is
the record of what the session actually ran with.

Start the session with `vision_calibration` **on**: the luma transition band is
the thing being measured, and change-driven logging hides it.

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

Two rules for the phases:

- **One person, one device at a time**, and say where they stand. Two people in
  frame make every count ambiguous.
- **Hold still for the "faces" phases and move for the "motion" ones.** Motion
  without faces is the dark's only presence evidence, so it needs a phase where
  movement is the deliberate variable.

## Reading the result

`runtime/corr_vis_summary.json` carries a `phases` block: per phase, per device,
`samples`, `faces_max`, `motion_max`, `dark`, and the `verdicts` seen. Then:

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
