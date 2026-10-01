"""The agency parsers: what counts as proof that the hive is doing something.

`claim_matrix.py` calls these over a captured transcript, so each one is a pure function of
text -- and can therefore be re-checked here, without a node, from lines a real session
actually recorded. The interesting cases are the negative ones: a reminder the operator had
to ask for twice, a memory that only looked durable because one process answered it, a
health surface that paints everything green. Each of those must *fail* the claim, because a
parser that says "proven" too easily is worse than no parser at all.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import claim_matrix  # noqa: E402  (path set above)

# Lines shaped like the ones the sessions actually wrote.
TIMED = """[SESSION] scenario=timed node=shugo-desktop role=primary-capable
[TURN   ] typed: remind me in two seconds to check the battery
[SPEAK  ] Got it - timer set for 2 seconds.
[AGENT  ] [AGENT] intent: UserIntent(command, conf=0.85)
[AGENT  ] [AGENT] command: timer_set
[SPEAK  ] Timer is done!
[AGENT  ] [AGENT] say: Timer is done!
[AGENT  ] [TIMER] timer fired: Timer (d02e6a90)
[SESSION] speaks=2 early_stop=True
"""

MEMORY = """[SESSION] scenario=memory node=shugo-desktop role=primary-capable
[TURN   ] typed: remember that my sister's name is Ana
[SPEAK  ] I'll remember that.
[SESSION] phase=first: the process ends here
[SESSION] scenario=memory node=shugo-desktop role=primary-capable
[SESSION] phase=second: a new process, the same data dir
[TURN   ] typed: what is my sister's name
[SPEAK  ] My sister's name is Ana.
[AGENT  ] [AGENT] memory recall (question path)
"""

PERSONALITY = """[SESSION] scenario=personality node=shugo-desktop
[AGENT  ] [PERSONALITY] resumed at gen 1
[AGENT  ] [PERSONALITY] governor live (gen 1)
[AGENT  ] [PERSONALITY] grew gen 1 -> 2 (questions 40%; long session (25 turns))
[SESSION] turns=27 (GROWTH_EVERY=25)
"""


class TimedAutonomyTestCase(unittest.TestCase):
    """Did the node act later, on its own, on something it accepted now?"""

    def test_a_goal_acted_on_unprompted_is_proof(self):
        ok, detail = claim_matrix.timer_fired(TIMED)
        self.assertTrue(ok, detail)
        self.assertIn("unprompted", detail)

    def test_a_reminder_the_operator_had_to_repeat_is_not(self):
        text = TIMED.replace("[SPEAK  ] Timer is done!",
                             "[TURN   ] typed: is my timer done?\n[SPEAK  ] Timer is done!")
        ok, detail = claim_matrix.timer_fired(text)
        self.assertFalse(ok, "a repeated reminder passed as autonomy")
        self.assertIn("had to type", detail)

    def test_a_timer_that_fired_silently_is_not(self):
        ok, detail = claim_matrix.timer_fired(TIMED.replace("[SPEAK  ] Timer is done!\n", ""))
        self.assertFalse(ok)
        self.assertIn("said nothing", detail)

    def test_a_timer_that_never_fired_is_not(self):
        text = TIMED.replace("[AGENT  ] [TIMER] timer fired: Timer (d02e6a90)\n", "")
        ok, detail = claim_matrix.timer_fired(text)
        self.assertFalse(ok)
        self.assertIn("no timer ever fired", detail)

    def test_an_empty_session_is_not(self):
        ok, detail = claim_matrix.timer_fired("[SESSION] scenario=timed")
        self.assertFalse(ok)
        self.assertIn("never had a goal accepted", detail)


class MemoryDurabilityTestCase(unittest.TestCase):
    """Did the fact come back in a *new* process, or only in the one that heard it?"""

    def test_a_fact_recalled_after_a_restart_is_proof(self):
        ok, detail = claim_matrix.memory_recalled(MEMORY)
        self.assertTrue(ok, detail)
        self.assertIn("Ana", detail)

    def test_one_process_answering_its_own_memory_is_not(self):
        text = MEMORY.replace(
            "[SESSION] phase=second: a new process, the same data dir\n", "")
        ok, detail = claim_matrix.memory_recalled(text)
        self.assertFalse(ok, "durability passed without a restart")
        self.assertIn("no second process", detail)

    def test_a_forgotten_fact_is_not(self):
        text = MEMORY.replace("My sister's name is Ana.", "I don't know your sister.")
        ok, detail = claim_matrix.memory_recalled(text)
        self.assertFalse(ok)
        self.assertIn("not recalled", detail)

    def test_a_session_that_taught_nothing_is_not(self):
        ok, detail = claim_matrix.memory_recalled(
            MEMORY.replace("[TURN   ] typed: remember that my sister's name is Ana\n", ""))
        self.assertFalse(ok)
        self.assertIn("never taught", detail)


PERCEPTION = """[SESSION] scenario=perception node=shugo-desktop role=primary-capable
[HEARD  ] what is the battery level
[HEARD  ] accepted=True type=speech detail=USER_PRESENT
[AGENT  ] [AGENT] intent: UserIntent(question, conf=0.80)
[SPEAK  ] Battery data isn't available right now.
[AGENT  ] [AGENT] say: Battery data isn't available right now.
"""

STATUS = ('[SESSION] scenario=status node=shugo-desktop\n'
          '[STATUS ] pipeline={"stages": {"sensors": "unknown", "vision": "unknown", '
          '"hearing": "unknown", "speech": "ok", "model": "ok", "memory": "ok"}, '
          '"overall": "unknown", "round_trips": 0}\n')


class HeardCausedActionTestCase(unittest.TestCase):
    """Did words it *heard* do anything, without the operator typing as well?"""

    def test_a_heard_phrase_answered_is_proof(self):
        ok, detail = claim_matrix.heard_caused_action(PERCEPTION)
        self.assertTrue(ok, detail)
        self.assertIn("unprompted", detail)

    def test_a_typed_prompt_beside_the_hearing_is_not(self):
        text = PERCEPTION.replace(
            "[AGENT  ] [AGENT] intent:",
            "[TURN   ] typed: what is the battery level\n[AGENT  ] [AGENT] intent:")
        ok, detail = claim_matrix.heard_caused_action(text)
        self.assertFalse(ok, "a reply to something typed passed as a reply to hearing")
        self.assertIn("typed before the node answered", detail)

    def test_hearing_without_a_reply_is_not(self):
        text = PERCEPTION.replace("[SPEAK  ] Battery data isn't available right now.\n", "")
        ok, detail = claim_matrix.heard_caused_action(text)
        self.assertFalse(ok)
        self.assertIn("said nothing", detail)

    def test_speech_the_node_refused_is_not(self):
        ok, detail = claim_matrix.heard_caused_action(
            PERCEPTION.replace("accepted=True", "accepted=False"))
        self.assertFalse(ok)
        self.assertIn("did not accept", detail)

    def test_a_session_with_nothing_heard_is_not(self):
        ok, detail = claim_matrix.heard_caused_action("[SESSION] scenario=timed")
        self.assertFalse(ok)
        self.assertIn("never gave the node something to hear", detail)


class PipelineHealthTestCase(unittest.TestCase):
    """A health surface that cannot say "unknown" cannot be trusted when it says "ok"."""

    def test_a_snapshot_in_the_house_vocabulary_is_proof(self):
        ok, detail = claim_matrix.pipeline_reported(STATUS)
        self.assertTrue(ok, detail)
        self.assertIn("vision=unknown", detail)
        self.assertIn("overall=unknown", detail)

    def test_an_invented_stage_word_is_not(self):
        ok, detail = claim_matrix.pipeline_reported(
            STATUS.replace('"vision": "unknown"', '"vision": "fine"'))
        self.assertFalse(ok, "a stage outside the vocabulary passed")
        self.assertIn("vocabulary", detail)

    def test_a_missing_organ_is_not(self):
        ok, detail = claim_matrix.pipeline_reported(
            STATUS.replace('"vision": "unknown", ', ""))
        self.assertFalse(ok)
        self.assertIn("organs missing", detail)

    def test_a_snapshot_that_is_not_json_is_not(self):
        ok, detail = claim_matrix.pipeline_reported("[STATUS ] pipeline={broken")
        self.assertFalse(ok)
        self.assertIn("not JSON", detail)

    def test_no_snapshot_at_all_is_not(self):
        ok, detail = claim_matrix.pipeline_reported("[SESSION] scenario=status")
        self.assertFalse(ok)
        self.assertIn("never reported its pipeline", detail)


class NoThirdPartyEgressTestCase(unittest.TestCase):
    """The privacy claim, read off a transcript rather than asserted in a README."""

    def test_localhost_only_is_proof(self):
        ok, detail = claim_matrix.no_third_party_egress(
            STATUS + "[AGENT  ] [AGENT] agent ready (api=http://127.0.0.1:11434)\n")
        self.assertTrue(ok, detail)
        self.assertIn("no external host", detail)

    def test_an_analytics_host_fails_the_claim(self):
        text = STATUS + ("INFO:backoff:Backing off send_request(...) "
                         "(HTTPSConnectionPool(host='us.i.posthog.com', port=443))\n")
        ok, detail = claim_matrix.no_third_party_egress(text)
        self.assertFalse(ok, "analytics egress passed the privacy claim")
        self.assertIn("posthog", detail)

    def test_an_ordinary_external_host_is_reported_but_not_a_failure(self):
        ok, detail = claim_matrix.no_third_party_egress(
            STATUS + "[AGENT  ] [AGENT] bridge -> https://fleet.example.org/api\n")
        self.assertTrue(ok, detail)
        self.assertIn("fleet.example.org", detail)


class RegistryTestCase(unittest.TestCase):
    """The matrix's own wiring: a live check nobody registered reads as `unproven`."""

    def test_every_claim_live_check_is_registered(self):
        for claim in claim_matrix.CLAIMS:
            for check in claim.get("checks", []):
                if check.get("kind") == "live":
                    self.assertIn(check["name"], claim_matrix.LIVE_CHECKS,
                                  "%s names a live check that does not exist"
                                  % claim["id"])

    def test_every_live_check_is_used_by_a_claim(self):
        used = {check["name"] for claim in claim_matrix.CLAIMS
                for check in claim.get("checks", []) if check.get("kind") == "live"}
        unused = sorted(set(claim_matrix.LIVE_CHECKS) - used)
        self.assertEqual(unused, [], "live checks no claim uses: %s" % unused)

    def test_the_agency_rows_are_in_the_matrix(self):
        ids = {claim["id"] for claim in claim_matrix.CLAIMS}
        for expected in ("agency.timed_autonomy", "agency.memory_durability",
                         "agency.personality_growth", "agency.perception_to_action",
                         "node.pipeline_health", "privacy.no_third_party_egress",
                         "world.desktop"):
            self.assertIn(expected, ids, "%s is not in the matrix" % expected)


if __name__ == "__main__":
    unittest.main()


