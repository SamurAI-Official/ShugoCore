"""The device side of the sound contract: who holds the microphone, and who says so.

The provider is Kotlin, so these are source-level assertions. They are here because each
one is an integration seam that has broken or nearly broken in this codebase before:
a registration name that must match on both sides, frame geometry that must not drift
from the verified native constants, and an arbiter that has to be maintained by BOTH
providers -- a second capture beside the recogniser reads silence, and publishing that
silence as "quiet" is the lie the Python contract already refuses to tell.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JAVA = ROOT / "platforms" / "android" / "app" / "src" / "main" / "java" / "com" / "samurai" / "shugocore"
RUNTIME = JAVA / "runtime"
SERVICE = JAVA / "ShugoCoreService.kt"
BRIDGE = JAVA / "inference" / "SoundBridge.kt"
AGENT = ROOT / "shugocore_agent.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestArbiter:
    def test_perception_state_has_the_arbiter_and_the_signals(self):
        src = _read(RUNTIME / "PerceptionState.kt")
        assert 'var micOwner: String = "none"' in src
        assert "var speechProbability:" in src
        assert "var soundEvent:" in src

    def test_speech_pipeline_claims_and_releases_the_mic(self):
        src = _read(RUNTIME / "AudioProvider.kt")
        assert 'PerceptionState.micOwner = "speech"' in src
        assert 'PerceptionState.micOwner = "none"' in src

    def test_sound_provider_refuses_a_mic_the_speech_pipeline_owns(self):
        src = _read(RUNTIME / "SoundProvider.kt")
        # Refuses while owned, claims while listening, and hands it back on stop.
        assert 'PerceptionState.micOwner == "speech"' in src
        assert 'PerceptionState.micOwner = "sound"' in src
        assert 'if (PerceptionState.micOwner == "sound") PerceptionState.micOwner = "none"' in src

    def test_sync_is_what_grants_the_turn_at_the_microphone(self):
        """Mode, permission, runtime and arbiter are all re-checked every tick."""
        src = _read(RUNTIME / "SoundProvider.kt")
        assert "fun sync()" in src
        assert 'listenMode == "sound" && hasPermission()' in src
        assert 'bridge?.isReady == true && PerceptionState.micOwner != "speech"' in src
        assert "soundProvider?.sync()" in _read(SERVICE)


class TestNoDrift:
    def test_provider_does_not_redeclare_the_frame_geometry(self):
        """One source of truth: the geometry is the bridge's, and the .so test pins it."""
        src = _read(RUNTIME / "SoundProvider.kt")
        for name in ("SAMPLE_RATE", "CHUNK_SAMPLES", "WINDOW_SAMPLES"):
            assert "= SoundBridge.%s" % name in src
        assert "16000" not in src and "15600" not in src

    def test_bridge_still_declares_what_the_so_test_pins(self):
        src = _read(BRIDGE)
        assert "const val SAMPLE_RATE = 16000" in src
        assert "const val CHUNK_SAMPLES = 512" in src
        assert "const val WINDOW_SAMPLES = 15600" in src

    def test_only_one_place_opens_a_microphone(self):
        """SoundProvider is the only new AudioRecord; the recogniser's is a service."""
        for path in sorted(RUNTIME.glob("*.kt")):
            src = _read(path)
            if path.name == "SoundProvider.kt":
                assert "AudioRecord(" in src
            elif "AudioRecord(" in src:
                # Pre-existing providers may wrap the recogniser but must not open a second
                # raw capture for perception; that is SoundProvider's job now.
                assert path.name == "AudioProvider.kt", path.name


class TestRegistrationNames:
    def test_kotlin_calls_a_method_the_agent_actually_has(self):
        assert re.search(r'callAttr\("register_sound_analyzer"', _read(SERVICE))
        assert "def register_sound_analyzer(" in _read(AGENT)

    def test_python_only_calls_bridge_methods_that_exist(self):
        service = _read(SERVICE)
        start = service.index("class SoundAnalyzerBridge(")
        methods = set(re.findall(r"fun (\w+)\(", service[start:]))
        assert {"isAvailable", "micOwner", "listenMode", "isListening",
                "capabilitiesJson", "lastSoundEventJson"} <= methods
        called = set(re.findall(r"\ba\.(\w+)\(\)", _read(AGENT)))
        assert called, "the status probe must probe something"
        assert called <= methods, "Python calls a bridge method that does not exist: %s" % (
            sorted(called - methods),)

    def test_the_python_side_is_absent_safe(self):
        src = _read(AGENT)
        assert 'getattr(self, "_sound_analyzer", None)' in src
        assert "self._sound_analyzer: Optional[Any] = None" in src

    def test_sound_runtime_is_released_at_shutdown(self):
        src = _read(SERVICE)
        assert "soundProvider?.stop()" in src
        assert "soundProvider = null" in src
        assert "soundBridge?.close()" in src
        assert "soundBridge = null" in src
