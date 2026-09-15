# ShugoCore v1.30.2 — CSFA instrumentation, endurance verification, ACTIVITY tab

The Continuous Synthetic Functional Agency loop is now **verifiable, not
assumed**: instrumented end to end, endurance-tested, and visible in the app.

## Highlights

- **Loop instrumentation** — bounded counters (cycles, by_outcome, by_source,
  by_action, conversational ticks), cycles-per-minute, a 100-entry activity
  ring, per-stage liveness for all 8 CSFA stages, uptime, and mesh activity.
  Purely observational; no change to decisions or gates.
- **Fixed** — `decision_source` status key was a constant `"none"`; it now
  carries the real proposal source and action type.
- **Endurance verification** — a 30-cycle scripted model-driven suite
  (full stage trails, zero rule fallbacks, audit-chain integrity) plus the
  null-dialect 3-strike fallback case; then verified on hardware: A51
  (0.5B, 33 cycles, 28 model-sourced) and Tab S9 FE (1.5B, 33 cycles,
  23 model-sourced), 0 crashes.
- **Android UI** — new **ACTIVITY tab**: recent-cycle ring
  (`action [outcome] via source · duration`), outcome ledger, loop
  rate/uptime, and memory-mesh activity by provenance peer. AGENT tab gains
  a Loop section and Loop-stages liveness. Missing evidence renders as
  `—` — never fabricated success.

## Compatibility

- Same 1.x public API; no breaking changes.
- One APK self-determines its CPU kernels at startup (portable vs
  dotprod/fp16 via runtime feature detection).
- Full suite: 1007 tests passing; mirrors byte-identical; ruff clean.

## Upgrade

```bash
pip install --upgrade shugocore==1.30.2
```

APK: `shugocore-1.30.2.apk` (attached, release-signed). Install over any
1.30.x build; mesh peers and memory persist.
