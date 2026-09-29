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


class TestModeSwitch:
    """One microphone, one owner: the switch that decides, rather than a race.

    Without this, the recogniser holds the microphone for the service's whole life on any
    device with on-device STT, and the sound layer would never run there at all.
    """

    def test_both_providers_have_a_mode_and_it_defaults_to_speech(self):
        audio = _read(RUNTIME / "AudioProvider.kt")
        assert 'var listenMode: String = "speech"' in audio
        assert 'var listenMode: String = "sound"' in _read(RUNTIME / "SoundProvider.kt")

    def test_speech_yields_when_the_mode_says_sound(self):
        audio = _read(RUNTIME / "AudioProvider.kt")
        assert 'if (listenMode != "speech")' in audio          # refuses to start
        assert '&& listenMode == "speech"' in audio            # and will not restart
        assert 'if (isRunning && (!granted || listenMode != "speech")) stop()' in audio

    def test_service_delivers_the_mode_to_both_providers(self):
        src = _read(SERVICE)
        assert "fun setPerceptionMode(mode: String): String" in src
        assert 'audioProvider?.listenMode = if (wanted == "sound") "off" else wanted' in src
        assert 'soundProvider?.listenMode = if (wanted == "sound") "sound" else "off"' in src
        # The release has to happen before the sound provider asks for the mic.
        assert src.index("audioProvider?.sync()", src.index("fun setPerceptionMode")) < \
            src.index("soundProvider?.sync()", src.index("fun setPerceptionMode"))

    def test_python_can_ask_for_the_switch_through_the_bridge(self):
        assert "fun setListenMode(mode: String): String" in _read(SERVICE)

    def test_speech_waits_instead_of_stomping_the_mic(self):
        """A second capture beside the sound layer would read silence -- and say 'quiet'."""
        audio = _read(RUNTIME / "AudioProvider.kt")
        assert 'if (PerceptionState.micOwner == "sound") {' in audio
        assert "scheduleRestartVad(5_000L)" in audio


class TestArbiterHonesty:
    def test_the_mic_is_claimed_only_where_it_is_really_held(self):
        """A claim with no mic behind it starves the sound layer for the service's life."""
        audio = _read(RUNTIME / "AudioProvider.kt")
        start = audio.index("fun start()")
        body = audio[start:audio.index("fun stop()", start)]
        # The unconditional claim must be gone: it now sits inside the recogniser branch
        # and in startVad(), where the AudioRecord actually exists.
        claim = 'PerceptionState.micOwner = "speech"'
        assert claim in body
        assert "if (startPersistentRecognition()) {" in body
        assert audio.count(claim) == 2, "claim belongs to the two paths that take the mic"
        assert claim in audio[audio.index("private fun startVad()"):]

    def test_vad_context_is_dropped_across_a_handover(self):
        src = _read(RUNTIME / "SoundProvider.kt")
        assert "snd.resetVad()" in src


class TestDeviceJsonContract:
    """The JSON the phone writes and the keys Python reads must stay the same set.

    This is the one seam no unit test on either side can see: Kotlin serialises the window,
    Python consumes it, and if a key is renamed on one side the other silently reports "no
    fresh measurement" -- honest, but wrong and invisible. The fixtures in
    tests/test_sound_android_backend.py pin the *shape*; this pins the *names*.
    """

    PUBLISHED_KEYS = ("status", "classified", "frame_ms", "frames", "labels",
                      "speech_prob", "window_ms")
    READ_KEYS = ("status", "frames", "labels", "speech_prob", "frame_ms")
    FRAME_KEYS = ("rms", "speech_prob")
    LABEL_KEYS = ("index", "score")

    def _provider_analyze(self):
        """The whole provider: payload keys are written in analyze(), but the per-frame keys
        are written in captureLoop(), where the frames are actually collected."""
        return _read(RUNTIME / "SoundProvider.kt")

    def _backend_source(self) -> str:
        adapter = _read(ROOT / "sound" / "adapter.py")
        return adapter[adapter.index("class AndroidSoundBackend"):]

    def _reads(self, key: str) -> bool:
        """Any of the access patterns the backend legitimately uses for a key.

        Matching the opening quote only: `payload.get("status", "")` is a correct read with
        a default, and a guard that rejects it would be testing style rather than the seam.
        """
        source = self._backend_source()
        return any('%s.get("%s"' % (holder, key) in source
                   for holder in ("payload", "entry", "raw", "caps"))

    def test_the_provider_publishes_every_key_python_reads(self):
        body = self._provider_analyze()
        for key in self.PUBLISHED_KEYS:
            assert '.put("%s"' % key in body, "SoundProvider no longer publishes %r" % key
        for key in self.FRAME_KEYS:
            assert '.put("%s"' % key in body, "a published frame lost %r" % key
        for key in self.LABEL_KEYS:
            assert '.put("%s"' % key in body, "a published label lost %r" % key

    def test_the_backend_only_reads_keys_the_provider_publishes(self):
        for key in self.READ_KEYS + self.FRAME_KEYS + self.LABEL_KEYS:
            assert self._reads(key), "AndroidSoundBackend no longer reads %r" % key

    def test_a_capability_payload_is_read_where_the_device_writes_it(self):
        """capabilitiesJson() is produced by sound_jni.cpp and consumed by this backend."""
        native = _read(ROOT / "platforms" / "android" / "app" / "src" / "main" / "cpp"
                       / "sound_jni.cpp")
        caps = native[native.index("nativeCapabilitiesJson"):]
        adapter = _read(ROOT / "sound" / "adapter.py")
        for key in ("execution_provider", "vad", "classifier", "classes"):
            assert "\\\"%s\\\"" % key in caps, "native capabilities lost %r" % key
            assert '"%s"' % key in adapter, "the backend stopped reading %r" % key


class TestVersionTruth:
    """The artifact must not disagree with the repo about what it is.

    runtime/deploy_*.py reads versionName off the built APK, so a stale APK does not
    merely look untidy -- it deploys a version the tree says does not exist.
    """

    def test_gradle_version_matches_version_py(self):
        import sys as _sys
        _sys.path.insert(0, str(ROOT))
        import version as _version
        gradle = _read(ROOT / "platforms" / "android" / "app" / "build.gradle")
        name = re.search(r'versionName\s+"([^"]+)"', gradle).group(1)
        assert name == _version.__version__, (
            "build.gradle says %s, version.py says %s" % (name, _version.__version__))

    def test_built_apk_metadata_matches_the_tree(self):
        meta = (ROOT / "platforms" / "android" / "app" / "build" / "outputs" / "apk"
                / "debug" / "output-metadata.json")
        if not meta.exists():
            import pytest
            pytest.skip("no debug APK built on this host")
        import json
        element = json.loads(meta.read_text(encoding="utf-8"))["elements"][0]
        gradle = _read(ROOT / "platforms" / "android" / "app" / "build.gradle")
        name = re.search(r'versionName\s+"([^"]+)"', gradle).group(1)
        code = int(re.search(r"versionCode\s+(\d+)", gradle).group(1))
        assert (element["versionName"], element["versionCode"]) == (name, code), (
            "the APK on disk is stale -- rebuild before deploying")
