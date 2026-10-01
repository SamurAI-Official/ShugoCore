"""The terminal's voice: sound is added, delivery is not redefined.

A node that can print has already reached the person sitting at it, so ``can_speak``
must not change meaning when a loudspeaker appears -- and a host with no engine must
lose nothing. These lock what the voice adds (audio, and a real gate in front of
anything the node says out loud), what it must not break (printing, routing), and
the one thing a subprocess-based voice must never do: let a reply become shell
syntax.
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
    spec = importlib.util.spec_from_file_location("terminal_voice_under_test",
                                                  DESKTOP)
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


def _runner(returncode=0, stdout="ok|Microsoft David Desktop", stderr=""):
    calls = []

    def run(script, env):
        calls.append((script, dict(env)))
        return _Result(returncode, stdout, stderr)

    run.calls = calls
    return run


class AVoiceThatIsNotThereTestCase(unittest.TestCase):
    """Honest degradation: no engine is a reason, not a traceback."""

    def test_a_missing_engine_is_reported_as_a_reason(self):
        def missing(script, env):
            raise FileNotFoundError("powershell.exe")

        voice = DESK.WindowsVoice(runner=missing)
        self.assertFalse(voice.probe(), "a host with no engine claims a voice")
        self.assertIn("FileNotFoundError", voice.reason)
        self.assertFalse(voice.say("hello"),
                         "a sentence was accepted with no engine to speak it")
        self.assertFalse(voice.synthesize("hello", "nowhere.wav"))

    def test_a_failing_engine_names_what_went_wrong(self):
        voice = DESK.WindowsVoice(runner=_runner(returncode=1,
                                                 stderr="no such voice: Zarvox"))
        self.assertFalse(voice.probe())
        self.assertIn("no such voice: Zarvox", voice.reason)

    def test_an_unhelpful_engine_still_yields_a_reason(self):
        voice = DESK.WindowsVoice(runner=_runner(returncode=0, stdout=""))
        self.assertFalse(voice.probe())
        self.assertTrue(voice.reason.strip())
        self.assertNotEqual(voice.reason, "not probed yet",
                            "a failed probe left the operator without a reason")

    def test_the_probe_is_asked_once(self):
        run = _runner()
        voice = DESK.WindowsVoice(runner=run)
        voice.probe()
        voice.probe()
        self.assertEqual(len(run.calls), 1,
                         "the probe runs on every reply, so every reply pays for "
                         "a PowerShell start")


class TheTerminalWiresItUpTestCase(unittest.TestCase):
    """Source-level: the voice is attached by the terminal, and only if asked."""

    def _terminal_source(self):
        with open(DESKTOP, encoding="utf-8") as handle:
            return handle.read().split("def run_terminal(")[1]

    def test_the_terminal_attaches_the_voice_to_its_speaker(self):
        terminal = self._terminal_source()
        self.assertIn("speaker.attach_voice(voice)", terminal)
        self.assertIn('getattr(agent, "attention", None)', terminal,
                      "the voice never told the arbiter when the node was talking, "
                      "so this node would not be interruptible while it spoke")

    def test_a_host_without_a_voice_says_why_and_keeps_running(self):
        terminal = self._terminal_source()
        self.assertIn("voice.probe()", terminal)
        self.assertIn("replies still print", terminal,
                      "a node with no voice left the operator without a reason")

    def test_the_voice_is_off_unless_asked_for(self):
        parser = DESK.build_parser()
        plain = parser.parse_args(["--terminal"])
        self.assertFalse(plain.voice,
                         "the node started speaking aloud without being asked")
        asked = parser.parse_args(["--terminal", "--voice", "--voice-rate=-3",
                                   "--voice-engine", "Microsoft Zira Desktop"])
        self.assertTrue(asked.voice)
        self.assertEqual(asked.voice_rate, -3)
        self.assertEqual(asked.voice_engine, "Microsoft Zira Desktop")


class ARealWindowsVoiceTestCase(unittest.TestCase):
    """The acceptance check: on a host that has a voice, measure it."""

    @unittest.skipUnless(os.name == "nt", "System.Speech is a Windows engine")
    def test_a_line_of_speech_weighs_more_than_nothing(self):
        voice = DESK.WindowsVoice()
        if not voice.probe():
            self.skipTest(f"no System.Speech on this host: {voice.reason}")
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "probe.wav")
            self.assertTrue(voice.synthesize("Shugo can speak.", path),
                            f"synthesis failed: {voice.reason}")
            with open(path, "rb") as handle:
                head = handle.read(4)
            self.assertEqual(head, b"RIFF",
                             "the engine wrote something that is not a wave")
            self.assertGreater(os.path.getsize(path), 1000,
                               "a whole sentence came out as a near-empty file")


class ARealVoiceTestCase(unittest.TestCase):

    def test_the_probe_reports_which_voice_it_found(self):
        voice = DESK.WindowsVoice(runner=_runner(stdout="ok|Microsoft Zira Desktop"),
                                  rate=-2)
        self.assertTrue(voice.probe())
        described = voice.describe()
        self.assertIn("Microsoft Zira Desktop", described)
        self.assertIn("rate -2", described)

    def test_one_sentence_is_queued_and_then_spoken(self):
        run = _runner()
        voice = DESK.WindowsVoice(runner=run)
        self.assertTrue(voice.say("the node is listening"))
        self.assertIsNotNone(voice._worker)
        voice._worker.join(timeout=5)
        spoken = [env for script, env in run.calls
                  if env.get("SHUGO_SPEAK_TEXT")]
        self.assertEqual(len(spoken), 1)
        self.assertEqual(spoken[0]["SHUGO_SPEAK_TEXT"], "the node is listening")
        self.assertEqual(voice.spoken, 1)

    def test_the_queue_counts_what_it_drops(self):
        voice = DESK.WindowsVoice(runner=_runner())
        voice._ensure_worker = lambda: None          # hold the queue still
        while not voice._queue.full():
            voice._queue.put_nowait("filler")
        self.assertFalse(voice.enqueue("one sentence too many"))
        self.assertEqual(voice.dropped, 1,
                         "a full queue swallowed a reply without saying so")

    def test_speaking_is_stamped_so_the_node_can_be_interrupted(self):
        seen = []

        class Arbiter:
            def stamp_tts(self, speaking):
                seen.append(speaking)

        voice = DESK.WindowsVoice(runner=_runner())
        voice.attach(Arbiter())
        voice.say("hello")
        voice._worker.join(timeout=5)
        self.assertEqual(seen, [True, False],
                         "the arbiter was never told the node was speaking, so a "
                         "voice here would not be interruptible")
        self.assertFalse(voice.speaking)

    def test_a_wav_can_be_written_instead_of_spoken(self):
        def writing(script, env):
            if not env:                     # the probe is asked with no payload
                return _Result(0, "ok|Microsoft David Desktop")
            with open(env["SHUGO_SPEAK_WAV"], "wb") as handle:
                handle.write(b"RIFF----WAVEfmt ")
            return _Result(0, "written")

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "line.wav")
            voice = DESK.WindowsVoice(runner=writing)
            self.assertTrue(voice.synthesize("say this", path))
            self.assertGreater(os.path.getsize(path), 0)


class TheScreenIsStillTheDeliveryTestCase(unittest.TestCase):
    """A loudspeaker is an addition, never a precondition."""

    class _FailingVoice:
        def __init__(self):
            self.tried = []

        def say(self, text):
            self.tried.append(text)
            return False

    def test_a_reply_prints_even_when_the_voice_cannot_speak(self):
        stream = io.StringIO()
        voice = self._FailingVoice()
        speaker = DESK.TerminalSpeaker(stream=stream)
        speaker.attach_voice(voice)
        self.assertTrue(speaker.speak("the battery is at 49 percent"),
                        "a failing loudspeaker made the terminal report that the "
                        "operator had not been reached")
        self.assertIn("the battery is at 49 percent", stream.getvalue())
        self.assertEqual(voice.tried, ["the battery is at 49 percent"])
        self.assertEqual(speaker.delivered, 1)

    def test_a_node_without_a_voice_still_delivers(self):
        stream = io.StringIO()
        speaker = DESK.TerminalSpeaker(stream=stream)
        self.assertTrue(speaker.speak("hello"))
        self.assertIn("hello", stream.getvalue())

    def test_an_empty_reply_is_not_delivered(self):
        speaker = DESK.TerminalSpeaker(stream=io.StringIO())
        self.assertFalse(speaker.speak("   "))
        self.assertEqual(speaker.delivered, 0)


class SlashSayUsesTheAgentsOwnGateTestCase(unittest.TestCase):
    """/say must not become a second way for this node to decide to talk."""

    class _Agent:
        def __init__(self, result=None, raises=None):
            self.result = {"status": "ok"} if result is None else result
            self.raises = raises
            self.asked = []

        def speak_test(self, text=None):
            self.asked.append(text)
            if self.raises:
                raise self.raises
            return self.result

        def get_status(self):
            return {"node_id": "shugo-desktop", "mesh_role": "primary"}

    def _capture(self, text, agent):
        out = io.StringIO()
        saved, sys.stdout = sys.stdout, out
        try:
            handled = DESK.handle_terminal_command(agent, text)
        finally:
            sys.stdout = saved
        return handled, out.getvalue()

    def test_say_is_handed_to_the_agent(self):
        agent = self._Agent(result={"status": "delivered", "speaker": "terminal"})
        handled, shown = self._capture("/say the light is green", agent)
        self.assertTrue(handled)
        self.assertEqual(agent.asked, ["the light is green"],
                         "the terminal did not drive the agent's gated speech path")
        self.assertIn("status=delivered", shown)

    def test_a_bare_say_asks_the_agent_for_its_default(self):
        agent = self._Agent()
        self._capture("/say", agent)
        self.assertEqual(agent.asked, [None])

    def test_status_is_the_agents_status(self):
        handled, shown = self._capture("/status", self._Agent())
        self.assertTrue(handled)
        self.assertIn("shugo-desktop", shown)

    def test_a_word_that_merely_starts_with_slash_say_is_a_turn(self):
        agent = self._Agent()
        handled, _ = self._capture("/sayonara", agent)
        self.assertFalse(handled, "'/sayonara' was eaten as a command")
        self.assertEqual(agent.asked, [])

    def test_a_refusal_is_reported_and_does_not_end_the_session(self):
        agent = self._Agent(result={"status": "refused", "reason": "mesh_follower"})
        handled, shown = self._capture("/say hello", agent)
        self.assertTrue(handled)
        self.assertIn("refused", shown)

    def test_a_failing_gate_is_reported_and_does_not_raise(self):
        agent = self._Agent(raises=RuntimeError("engine unavailable"))
        handled, shown = self._capture("/say hello", agent)
        self.assertTrue(handled)
        self.assertIn("RuntimeError", shown)

