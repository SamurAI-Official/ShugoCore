"""The terminal's ear: hearing through the node's own recogniser, and saying so.

Hearing is the capability whose failure is indistinguishable from quiet, so these lock
the three things that keep it honest: a node with no recogniser (or no microphone)
reports a *reason* rather than reporting silence, the microphone's single owner is decided
by policy rather than by the terminal, and words arrive as *heard* speech down the fleet's
own pipeline -- never as something this terminal invented, and never marked typed.
"""
import importlib.util
import io
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESKTOP = os.path.join(ROOT, "clients", "desktop", "shugocore_desktop.py")


def _desktop_module():
    """Load the control plane without starting Tk, the GUI or the agent."""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    spec = importlib.util.spec_from_file_location("terminal_ear_under_test", DESKTOP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DESK = _desktop_module()


class _Result:
    """The shape of subprocess.run's answer, without running anything."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _runner(returncode=0, stdout="ok|MS-1033-80-DESK", stderr=""):
    calls = []

    def run(script, env):
        calls.append((script, dict(env)))
        return _Result(returncode, stdout, stderr)

    run.calls = calls
    return run


class _FakeProc:
    """A child that says what it was told to say, and can be watched stopping."""

    def __init__(self, lines, stderr_text=""):
        self.stdout = iter([f"{line}\n" for line in lines])
        self.stderr = io.StringIO(stderr_text)
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def _popen(lines, stderr_text=""):
    calls = []

    def launch(command, env):
        calls.append((list(command), dict(env)))
        return _FakeProc(lines, stderr_text)

    launch.calls = calls
    return launch


class _Agent:
    """Just enough agent: a policy answer and the observation sink."""

    def __init__(self, mode="speech", raises=None):
        self.mode = mode
        self.raises = raises
        self.published = []
        self.asked = 0

    def apply_sound_policy(self, force=False):
        self.asked += 1
        if self.raises:
            raise self.raises
        return self.mode

    def publish_human_observation(self, data):
        self.published.append(data)
        return {"accepted": True, "type": data.get("type")}


class AnEarThatIsNotThereTestCase(unittest.TestCase):
    """Honest degradation: no ear is a reason, never a silent nothing."""

    def test_a_missing_engine_is_reported_as_a_reason(self):
        def missing(script, env):
            raise FileNotFoundError("powershell.exe")

        ear = DESK.WindowsEar(runner=missing)
        self.assertFalse(ear.probe(), "a host with no engine claims an ear")
        self.assertIn("FileNotFoundError", ear.reason)
        self.assertEqual(ear.transcribe_wave("nowhere.wav"), "")
        self.assertEqual(ear.phrases, 0)

    def test_a_host_with_no_microphone_says_so(self):
        ear = DESK.WindowsEar(runner=_runner(stdout="error|No audio input device"))
        self.assertFalse(ear.probe())
        self.assertIn("No audio input device", ear.reason,
                      "a node that cannot open a capture must say why: its silence is "
                      "otherwise indistinguishable from a quiet room")

    def test_the_probe_is_asked_once(self):
        run = _runner()
        ear = DESK.WindowsEar(runner=run)
        ear.probe()
        ear.probe()
        self.assertEqual(len(run.calls), 1)

    def test_a_broken_child_is_a_reason_not_a_crash(self):
        launch = _popen([], stderr_text="No default audio device\n")
        ear = DESK.WindowsEar(runner=_runner(), popen=launch)
        self.assertTrue(ear.start(lambda phrase: None))
        ear._thread.join(timeout=5)
        self.assertIn("No default audio device", ear.reason)


class ThePolicyOwnsTheMicrophoneTestCase(unittest.TestCase):
    """A terminal does not get to decide for itself that it may listen."""

    def test_speech_mode_allows_the_recogniser(self):
        allowed, why = DESK.ear_permission(_Agent(mode="speech"))
        self.assertTrue(allowed)
        self.assertIn("listen_mode=speech", why)

    def test_the_acoustic_layer_keeps_its_microphone(self):
        allowed, why = DESK.ear_permission(_Agent(mode="sound"))
        self.assertFalse(allowed, "the ear opened a capture the sound layer owns")
        self.assertIn("acoustic layer", why)

    def test_off_is_off(self):
        allowed, why = DESK.ear_permission(_Agent(mode="off"))
        self.assertFalse(allowed)
        self.assertIn("off", why)

    def test_an_unreadable_policy_is_not_permission(self):
        allowed, why = DESK.ear_permission(_Agent(raises=RuntimeError("no registry")))
        self.assertFalse(allowed, "an unreadable policy was read as permission")
        self.assertIn("RuntimeError", why)

    def test_the_terminal_asks_policy_before_it_opens_a_microphone(self):
        with open(DESKTOP, encoding="utf-8") as handle:
            terminal = handle.read().split("def run_terminal(")[1]
        self.assertIn("ear_permission(agent)", terminal)
        self.assertLess(terminal.index("ear_permission(agent)"),
                        terminal.index("ear.start("),
                        "the ear opened the microphone before asking whose it is")

    def test_the_ear_is_off_unless_asked_for(self):
        parser = DESK.build_parser()
        plain = parser.parse_args(["--terminal"])
        self.assertFalse(plain.ear, "the node started listening without being asked")
        asked = parser.parse_args(["--terminal", "--ear", "--ear-window", "3"])
        self.assertTrue(asked.ear)
        self.assertEqual(asked.ear_window, 3.0)


class EchoDeadTimeTestCase(unittest.TestCase):
    """A node must not answer its own voice."""

    def test_a_speaking_voice_is_dead_time(self):
        class Voice:
            speaking = True

        self.assertTrue(DESK.echo_dead_time(Voice()),
                        "a phrase heard while the node was speaking would be fed back "
                        "in, and the terminal would argue with itself")

    def test_a_silent_voice_is_not(self):
        class Voice:
            speaking = False

        self.assertFalse(DESK.echo_dead_time(Voice()))

    def test_a_node_with_no_voice_has_no_dead_time(self):
        self.assertFalse(DESK.echo_dead_time(None))

    def test_the_terminal_checks_it_before_publishing(self):
        with open(DESKTOP, encoding="utf-8") as handle:
            terminal = handle.read().split("def run_terminal(")[1]
        self.assertIn("if echo_dead_time(voice):", terminal)
        self.assertLess(terminal.index("if echo_dead_time(voice):"),
                        terminal.index("publish_heard(agent, phrase"),
                        "the heard phrase is published before the echo check")


class AnEarAndAVoiceAgreeTestCase(unittest.TestCase):
    """The acceptance check: say something, then hear it back.

    The machine running this cannot talk into its own microphone, but it can write down
    what it would have said and listen to that: same voice, same ear, and the sentence has
    to survive the round trip for either half to be worth anything.
    """

    @unittest.skipUnless(os.name == "nt", "System.Speech is a Windows engine")
    def test_a_synthesized_sentence_comes_back_through_the_ear(self):
        voice = DESK.WindowsVoice()
        ear = DESK.WindowsEar()
        if not voice.probe():
            self.skipTest(f"no voice on this host: {voice.reason}")
        if not ear.probe():
            self.skipTest(f"no ear on this host: {ear.reason}")
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "roundtrip.wav")
            self.assertTrue(voice.synthesize("check the battery level", path),
                            f"synthesis failed: {voice.reason}")
            heard = ear.transcribe_wave(path)
        if not heard:
            self.skipTest("the recogniser returned nothing for a synthetic sentence")
        self.assertIn("batter", heard.lower(),
                      f"the ear heard {heard!r} for 'check the battery level'")


class ARealEarTestCase(unittest.TestCase):
    """What the ear does when there *is* one."""

    def test_the_wave_path_never_rides_the_command_line(self):
        run = _runner(stdout="the battery is at fifty percent")
        ear = DESK.WindowsEar(runner=run)
        ear.transcribe_wave("C:/tmp/awkward name; rm -rf .wav")
        script, env = run.calls[-1]
        self.assertNotIn("awkward", script,
                         "the wave path was interpolated into the script")
        self.assertEqual(env["SHUGO_EAR_WAV"], "C:/tmp/awkward name; rm -rf .wav")
        self.assertIn("SHUGO_EAR_WINDOW", env)

    def test_a_transcription_counts_as_a_phrase(self):
        ear = DESK.WindowsEar(runner=_runner(stdout="the battery is at fifty percent"))
        self.assertEqual(ear.transcribe_wave("line.wav"),
                         "the battery is at fifty percent")
        self.assertEqual(ear.phrases, 1)
        self.assertEqual(ear.last, "the battery is at fifty percent")

    def test_silence_is_not_a_phrase(self):
        ear = DESK.WindowsEar(runner=_runner(stdout=""))
        self.assertEqual(ear.transcribe_wave("line.wav"), "")
        self.assertEqual(ear.phrases, 0,
                         "an empty recognition counted as something heard")

    def test_a_live_phrase_arrives_as_heard_speech(self):
        launch = _popen(["the battery is at fifty percent"])
        ear = DESK.WindowsEar(runner=_runner(), popen=launch)
        agent = _Agent()
        self.assertTrue(ear.start(
            lambda phrase: DESK.publish_heard(agent, phrase, ear.source)))
        ear._thread.join(timeout=5)
        self.assertEqual(ear.phrases, 1)
        self.assertEqual(len(agent.published), 1)
        observation = agent.published[0]
        self.assertEqual(observation["type"], "speech")
        self.assertEqual(observation["source"], "on_device_stt")
        self.assertEqual(observation["payload"]["transcript"],
                         "the battery is at fifty percent")
        self.assertNotIn("typed", observation,
                         "a heard phrase was marked typed, which is the one "
                         "distinction this terminal exists to keep")
        self.assertNotIn("typed", observation.get("payload", {}))

    def test_the_child_is_released_when_listening_stops(self):
        launch = _popen([])
        ear = DESK.WindowsEar(runner=_runner(), popen=launch)
        ear.start(lambda phrase: None)
        proc = ear._proc
        ear.stop()
        self.assertTrue(proc.terminated,
                        "the child was left running, so it would hold the microphone "
                        "after the terminal that asked for it had gone")
        self.assertIsNone(ear._proc)

    def test_listening_asks_for_the_window_it_was_given(self):
        launch = _popen([])
        ear = DESK.WindowsEar(runner=_runner(), popen=launch, window=3)
        ear.start(lambda phrase: None)
        command, env = launch.calls[-1]
        self.assertEqual(env["SHUGO_EAR_WINDOW"], "3")
        self.assertIn("SpeechRecognitionEngine", " ".join(command))


if __name__ == "__main__":
    unittest.main()
