"""The Quest 3 path: the headset's own transcript, judged by the real parser.

The producer needs a headset attached, so what is pinned here is the part that decides the
claim -- and, more importantly, the transcripts that must *not* pass: one where the headset
never reached the agent, one where an unauthenticated request was not refused, and one where
the "headset" was not a headset at all.
"""
import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import claim_matrix  # noqa: E402


def _module(name, relative):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relative))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


QUEST = _module("quest_probe_under_test", os.path.join("runtime", "tools", "quest_probe.py"))


def _facts(**overrides):
    facts = {
        "serial": "2G97C5ZH5P01GZ", "model": "Quest 3", "device_ip": "192.168.1.151",
        "desktop": "192.168.1.152", "port": 64343, "token": True, "android": "14",
        "abi": "arm64-v8a", "status": 200, "task": 200, "notoken": 401,
        "status_body": '{"version": "1.30.24", "model": "shugocore-local"}',
        "task_body": '{"status": "not_implemented", "action_type": "speak"}',
        "notoken_body": '{"error": "unauthorized"}',
        "decision": {"action_type": "speak",
                     "params": {"text": "I'm here - what did you want to talk about?"}},
        "server_tail": [], "error": "",
    }
    facts.update(overrides)
    return facts


class TheTranscriptTestCase(unittest.TestCase):
    def _text(self, **overrides):
        return "\n".join(QUEST.transcript_lines(_facts(**overrides)))

    def test_a_reached_headset_is_proven_by_the_real_parser(self):
        ok, detail = claim_matrix.quest3_reach(self._text())
        self.assertTrue(ok, detail)
        self.assertIn("Quest 3", detail)
        self.assertIn("192.168.1.151", detail)

    def test_the_agent_s_own_answer_is_quoted_from_the_server_s_log(self):
        text = self._text()
        self.assertIn("from the server's own log", text)
        self.assertIn("what the agent said:", text)
        self.assertIn("what did you want to talk about", text)

    def test_the_device_identity_is_the_headset_s_own(self):
        text = self._text()
        self.assertIn("device=Quest 3", text)
        self.assertIn("serial=2G97C5ZH5P01GZ", text)
        self.assertIn("ip=192.168.1.151", text)

    def test_a_headset_that_never_reached_the_agent_is_not_proven(self):
        ok, detail = claim_matrix.quest3_reach(self._text(status=0, status_body=""))
        self.assertFalse(ok)
        self.assertIn("never got a 200", detail)

    def test_a_task_that_did_not_run_is_not_proven(self):
        ok, detail = claim_matrix.quest3_reach(self._text(task=0, task_body=""))
        self.assertFalse(ok)
        self.assertIn("not executed", detail)

    def test_an_agent_that_lets_anyone_in_is_not_proven(self):
        # The gate is half the claim: reaching *an* agent is not reaching *its* agent.
        ok, detail = claim_matrix.quest3_reach(self._text(notoken=200))
        self.assertFalse(ok)
        self.assertIn("no token", detail)

    def test_a_phone_is_not_a_headset(self):
        ok, detail = claim_matrix.quest3_reach(self._text(model="SM_S515DL"))
        self.assertFalse(ok)
        self.assertIn("not a Quest", detail)

    def test_no_headset_attached_says_so_rather_than_inventing_one(self):
        text = "\n".join(QUEST.transcript_lines(
            {"serial": "", "model": "", "error": "no Quest headset is attached over adb, so "
                                                 "nothing was probed (unproven, not failed)"}))
        self.assertIn("no Quest headset is attached", text)
        # Not evaluable, rather than contradicted: a headset that went to sleep is not a
        # claim that failed.
        self.assertIsNone(claim_matrix.quest3_reach(text)[0])


class TheReaderTestCase(unittest.TestCase):
    def test_the_decision_is_read_from_a_python_dict_repr(self):
        # The logger prints a dict, not JSON: single quotes and all.
        log = ("2026-10-02 00:53:07,654 - INFO - Decision: {'action_type': 'speak', "
               "'params': {'text': \"I'm here - what did you want to talk about?\"}, "
               "'confidence': 0.3}\n")
        decision = QUEST.read_decision(log)
        self.assertEqual(decision.get("action_type"), "speak")
        self.assertIn("what did you want to talk about", decision["params"]["text"])

    def test_json_is_still_understood(self):
        log = 'INFO - Decision: {"action_type": "speak", "params": {"text": "hi"}}'
        self.assertEqual(QUEST.read_decision(log).get("action_type"), "speak")

    def test_a_log_with_no_decision_reads_as_nothing(self):
        self.assertEqual(QUEST.read_decision("INFO - nothing happened"), {})

    def test_the_response_status_and_body_are_split_apart(self):
        raw = ("HTTP/1.1 200 OK\r\nServer: ShugoCoreServer/1.8\r\n"
               "Content-Type: application/json\r\nContent-Length: 30\r\n\r\n"
               '{"version": "1.30.24"}\r\n')
        self.assertEqual(QUEST.http_status(raw), 200)
        self.assertEqual(QUEST.http_body(raw), '{"version": "1.30.24"}')

    def test_a_body_with_newlines_is_collapsed_to_one_line(self):
        # Collapsed so a line-based reader sees the whole body as the status line's tail; the
        # spaces it leaves around the braces are cosmetic, one line is the point.
        raw = "HTTP/1.0 200 OK\r\n\r\n{\n  \"a\": 1,\n  \"b\": 2\n}"
        self.assertEqual(QUEST.http_body(raw), '{ "a": 1, "b": 2 }')
        self.assertNotIn("\n", QUEST.http_body(raw))

    def test_a_response_with_no_status_reads_as_zero(self):
        self.assertEqual(QUEST.http_status("connection refused"), 0)

    def test_adb_find_order_is_stated_not_guessed(self):
        self.assertEqual(QUEST.find_adb("G:/definitely/not/here"), "")

    def test_this_desktop_is_found_in_the_headsets_subnet(self):
        # Derived from the headset's address: an address on another interface must not win.
        address = QUEST.desktop_ip_for("192.168.1.151")
        self.assertTrue(address == "" or address.startswith("192.168.1."))


if __name__ == "__main__":
    unittest.main()