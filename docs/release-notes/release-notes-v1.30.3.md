# ShugoCore v1.30.3 — desktop activity API, CSFA soak, bounded model scoring

The loop made *visible* in v1.30.2 is now **verifiable over time** — and the
soak immediately paid for itself with two real bug finds.

## Highlights

- **Desktop activity API** — `/api/v1/activity` (per-endpoint counters,
  outcome buckets, latency ring, requests/minute, verbatim agent loop block
  when hosted), `/api/v1/uptime`, additive activity keys on `/api/v1/status`.
- **CSFA soak tools** — wall-clock host endurance (`tests/csfa_soak.py`)
  and adb device endurance (`tests/android_device_soak.py`) with invariant
  checks and honest JSON verdicts.
- **Fixed** — `VERIFY_ATTENTION` stage liveness (real work was invisible);
  unbounded model-performance growth (×1.1/success, no cap — would overflow
  at ~7300 cycles; observed 1.3e12 after 293). Now capped at 100.0.
- **Soak-verified** — host 5 min STABLE (294 ticks); Tab S9 FE 15 min
  STABLE (82 decisions); A51 15 min STABLE (22 decisions); 0 crashes on all.

## Compatibility

- Same 1.x public API; no breaking changes. The performance-score cap
  changes stored scores only (bounded at 100.0).
- One APK self-determines its CPU kernels at startup (portable vs
  dotprod/fp16 via runtime feature detection).
- Full suite: 1020 tests passing; mirrors byte-identical; ruff clean.

## Upgrade

```bash
pip install --upgrade shugocore==1.30.3
```

APK: `shugocore-1.30.3.apk` (attached, release-signed). Install over any
1.30.x build; mesh peers and memory persist.
