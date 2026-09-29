"""Audio-perception contract: descriptors in, labels out, never audio.

Mirrors the NRR contract layer at the *descriptor* level. The mesh carries what was
heard, never the sound: a window descriptor goes out, a SoundEventResult comes back,
and raw samples stay on the device that captured them. That is the same rule as NRR's
"descriptors, never pixels", and here it is also the privacy rule.

Three layers, deliberately separable:

* Tier 0 -- ``sound.descriptors``: acoustic description from frame statistics (level,
  trend, activity, onsets, silence). No model, no licence, no weights.
* Tier 1 -- speech vs not, from Silero VAD (MIT) driven by the native layer.
* Tier 2 -- what the sound *was*, from YAMNet (521 AudioSet classes).

This package is the contract and the pure logic. It holds no weights and imports no
inference runtime: the device runs the models through the existing native ORT/TFLite
path, and ``sound.models`` only *locates* the files (see MODELS.md for licences).

Validation is fail-closed, like the rest of the fleet: a malformed descriptor raises
before anything is dispatched, and a worker with no backend answers ``not_supported``
rather than guessing.
"""
