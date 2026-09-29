"""Versioned constants for the audio-perception contract.

Kept as plain module constants (no classes) so both the device side and the host side
can agree on numbers rather than conventions. Every value that a model dictates was
verified against the real model file, not assumed:

* Silero VAD v5 (ONNX, 2.2 MB): a 512-sample chunk at 16 kHz, prepended with a
  64-sample context from the previous chunk (576 samples in), an RNN ``state`` of
  ``[2, 1, 128]`` carried across chunks, and a scalar sample rate. Getting the context
  wrong makes it read junk -- verified: 0.005 on silence, 0.813 mean on real speech.
* YAMNet (MediaPipe float32 bundle, 3.9 MB): one raw waveform window of 15600 samples
  = 0.975 s at 16 kHz in, 521 AudioSet class scores out. It computes its own log-mel
  features internally, so there is no frontend to write. Verified on real speech:
  ``Speech`` 0.984.
"""
SCHEMA_VERSION = 1

# The rate the device mic path already runs at (AudioProvider: 16 kHz mono, 20 ms frames).
SAMPLE_RATE = 16000
FRAME_SAMPLES = 320              # 20 ms at 16 kHz -- what the VAD fallback already frames

# Silero VAD v5 chunk contract.
VAD_CHUNK = 512
VAD_CONTEXT = 64
VAD_INPUT = VAD_CHUNK + VAD_CONTEXT
VAD_STATE_SHAPE = (2, 1, 128)
VAD_THRESHOLD = 0.5              # the model's own documented decision boundary

# YAMNet window contract.
WINDOW_SAMPLES = 15600           # 0.975 s at 16 kHz
WINDOW_SECONDS = WINDOW_SAMPLES / float(SAMPLE_RATE)
YAMNET_CLASSES = 521

# Acoustic description vocabulary (Tier 0). Deliberately short, like capabilities.py.
KNOWN_LEVELS = ("quiet", "conversational", "loud")
KNOWN_TRENDS = ("steady", "rising", "falling")

# Publishing rules. AudioSet labels are weak (YAMNet's balanced mAP is 0.306), so a
# label is never a fact: it needs a floor, and only the top few ever travel.
MAX_LABELS = 5
MIN_LABEL_CONFIDENCE = 0.10
MAX_LABEL_CHARS = 48

# dBFS bands for the level vocabulary. Chosen so "conversational" covers normal speech
# from a phone across a room, not a calibrated SPL.
#
# Re-tuned against real device captures (A51, 319 windows, one room):
#   quiet tail min -45.5, p25 -31.3, p50 -26.7, p75 -25.3, max -13.5 dBFS
# The old -45 QUIET_MAX put a steady fan-and-desk room at -31 dBFS into "conversational",
# which is not how a person in that room would describe it: steady ambient noise is quiet,
# and the old threshold only fired for a room that was silent in the acoustically-treated
# sense. -33 keeps room tone quiet while ordinary speech (-26..-24 here) stays conversational,
# and -22 reserves "loud" for something that actually stands out. SILENCE_DBFS is unchanged:
# it means "nearly nothing at all", and the quietest window this device produced was -45.5,
# so nothing observed is being relabelled as silence.
#
# One room, one device. These are anchors, not constants of nature: a second room should be
# measured before the vocabulary travels to the hive as a claim about a place.
QUIET_MAX_DBFS = -33.0
LOUD_MIN_DBFS = -22.0
SILENCE_DBFS = -55.0

# A jump this large between consecutive frames counts as an onset (a discrete event,
# a door, a glass) rather than a level drift.
ONSET_RISE_DB = 9.0

# Who holds the microphone, and what this node is listening for.
#
# This matters more than it looks. Android gives audio to one capture at a time: the
# device's AudioProvider keeps a persistent on-device SpeechRecognizer holding the mic
# continuously (v1.18 -- "the recognizer IS the listener"), so a sound classifier that
# opened a second stream would receive silence, not an error. Reporting that silence as
# "quiet" would be a confident lie of exactly the kind this contract refuses, so the state
# is explicit: a busy microphone produces `not_supported` naming the owner, and the
# capability is withheld while another consumer has the mic.
#
# `sound` and `speech` are modes an operator chooses between rather than concurrent
# consumers; `off` means this node offers no acoustic perception at all.
KNOWN_MIC_STATES = ("available", "busy_speech", "denied", "absent")
KNOWN_LISTEN_MODES = ("sound", "speech", "off")
