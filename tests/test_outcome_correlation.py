"""Hostile tests for read-only MESO -> Volition -> outcome *claim* correlation."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path

from pre_active.outcome_correlation import OutcomeCorrelation, correlate_outcome_candidate
from pre_active.outcomes import (
    ClaimedOutcome,
    OutcomeCandidate,
    OutcomeLedger,
    OutcomeState,
)
from pre_active.store import Store


class OutcomeCorrelationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state.db"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.ledger = OutcomeLedger(self.store)
        self.source_id = "meso-source-1"
        self.signal_id = self.store.enqueue_event(
            kind="volition.signal",
            payload={
                "target": "battery",
                "kind": "open_loop",
                "provenance": "model_generated",
                "source": "meso-crct:intent:approach:approach:meso:test",
                "meso_source_event_id": self.source_id,
                "effect_authority": False,
            },
            now=1,
        )
        self.intent = {
            "target_id": "battery",
            "kind": "approach",
            "strength": 0.8,
            "source_tendency": "approach",
            "source": "meso:test",
            "effect_authorized": False,
            "can_execute": False,
        }
        self.store.connection.execute("BEGIN IMMEDIATE")
        self.store.record_meso_intent_ingress(
            source_event_id=self.source_id,
            intent=self.intent,
            signal_event_id=self.signal_id,
            now=1,
        )
        self.store.connection.execute("COMMIT")
        self.store.record_volition_signal_receipt(
            source_event_id=self.signal_id,
            state_revision=1,
            cognition_event_id=None,
            goal_id="goal-1",
            target="battery",
            urgency=0.8,
            provenance="model_generated",
            signal_source="meso-crct:intent:approach:approach:meso:test",
            choice_id="choice-1",
            choice_class="POLICY_DERIVED",
            choice_source="volition:policy",
            now=2,
        )
        self.candidate = OutcomeCandidate(
            event_id="claimed-observation-1",
            need_id="claimant-need-1",
            goal_id="goal-1",
            action_request_id="unverified-action-1",
            claimed_outcome=ClaimedOutcome.SUCCESS,
            claimed_need_change=0.5,
            source_kind="model_generated",
            source_actor_id="model:untrusted",
            source_revision="model@v1",
            evidence_ref="evidence://asserted",
            evidence_sha256="a" * 64,
            observed_at=12,
            expires_at=90,
        )
        self.ledger.record(self.candidate, now=20)

    def correlate(self, **changes: object):
        params = {
            "store": self.store,
            "candidate_event_id": self.candidate.event_id,
            "meso_source_event_id": self.source_id,
            "now": 21,
        }
        params.update(changes)
        return correlate_outcome_candidate(**params)

    def test_exact_link_is_inert_and_need_and_effect_remain_unverified(self) -> None:
        before = self.store.connection.total_changes
        before_events = self.store.list_events()
        before_volition = self.store.get_volition_state()
        first = self.correlate()
        self.assertEqual(first.meso_source_event_id, self.source_id)
        self.assertEqual(first.signal_event_id, self.signal_id)
        self.assertEqual(first.goal_id, "goal-1")
        self.assertEqual(first.choice_id, "choice-1")
        self.assertEqual(first.candidate_event_id, self.candidate.event_id)
        self.assertEqual(first.need_id_claimed, "claimant-need-1")
        self.assertEqual(first.action_request_id_claimed, "unverified-action-1")
        self.assertEqual(first.claimed_outcome, ClaimedOutcome.SUCCESS)
        self.assertEqual(first.evidence_sha256_claimed, "a" * 64)
        self.assertEqual(first.candidate_status, OutcomeState.CANDIDATE)
        self.assertFalse(first.evidence_admitted)
        self.assertFalse(first.candidate_choice_link_verified)
        self.assertFalse(first.need_lineage_verified)
        self.assertFalse(first.tool_effect_verified)
        self.assertFalse(first.effect_authority)
        self.assertTrue(first.historical_only)
        self.assertEqual(self.correlate(), first)
        self.assertEqual(self.store.connection.total_changes, before)
        self.assertEqual(self.store.list_events(), before_events)
        self.assertEqual(self.store.get_volition_state(), before_volition)

    def test_authority_flags_cannot_be_forged_by_constructor_or_replace(self) -> None:
        joined = self.correlate()
        constructor_fields = {item.name: getattr(joined, item.name) for item in fields(joined)}
        for flag in (
            "evidence_admitted", "candidate_choice_link_verified",
            "need_lineage_verified", "tool_effect_verified", "effect_authority",
        ):
            with self.subTest(flag=flag):
                self.assertFalse(getattr(joined, flag))
                with self.assertRaises(TypeError):
                    OutcomeCorrelation(**constructor_fields, **{flag: True})
                with self.assertRaises(TypeError):
                    replace(joined, **{flag: True})
                with self.assertRaises((AttributeError, TypeError)):
                    setattr(joined, flag, True)

    def test_replay_after_restart_preserves_claim_only_correlation(self) -> None:
        previous = self.correlate()
        self.store.close()
        self.store = Store(self.path)
        self.ledger = OutcomeLedger(self.store)
        self.assertEqual(self.correlate(), previous)

    def test_rejects_forged_and_mismatched_lineage(self) -> None:
        with self.assertRaises(ValueError):
            self.correlate(meso_source_event_id="unknown-meso")
        with self.assertRaises(ValueError):
            self.correlate(candidate_event_id="nonexistent")

        self.ledger.record(
            replace(self.candidate, event_id="wrong-goal", goal_id="goal-spoof"),
            now=20,
        )
        with self.assertRaises(ValueError):
            self.correlate(candidate_event_id="wrong-goal")

        self.store.connection.execute(
            "UPDATE volition_signal_receipts SET target=? WHERE source_event_id=?",
            ("unrelated", self.signal_id),
        )
        with self.assertRaises(ValueError):
            self.correlate()

    def test_rejects_tampered_signal_and_missing_choice(self) -> None:
        self.store.connection.execute(
            "UPDATE volition_signal_receipts SET choice_id=NULL WHERE source_event_id=?",
            (self.signal_id,),
        )
        with self.assertRaises(ValueError):
            self.correlate()
        self.store.connection.execute(
            "UPDATE volition_signal_receipts SET choice_id='choice-1' WHERE source_event_id=?",
            (self.signal_id,),
        )
        self.store.connection.execute(
            "UPDATE events SET kind='autonomous.turn' WHERE id=?",
            (self.signal_id,),
        )
        with self.assertRaises(ValueError):
            self.correlate()

    def test_rejects_unbound_signal_payload_and_authority_spoof(self) -> None:
        self.store.connection.execute(
            "UPDATE events SET payload_json=? WHERE id=?",
            ('{"effect_authority":true,"meso_source_event_id":"meso-source-1",'
             '"provenance":"model_generated","source":'
             '"meso-crct:intent:approach:approach:meso:test","kind":'
             '"open_loop","target":"battery"}',
             self.signal_id),
        )
        with self.assertRaises(ValueError):
            self.correlate()

    def test_rejects_forged_intent_kind_with_same_goal_and_target(self) -> None:
        self.store.connection.execute(
            "UPDATE meso_intent_ingress SET intent_json=? WHERE source_event_id=?",
            ('{"target_id":"battery","kind":"inspect",'
             '"source":"meso:test","source_tendency":"approach",'
             '"effect_authorized":false,"can_execute":false}',
             self.source_id),
        )
        with self.assertRaises(ValueError):
            self.correlate()

    def test_matching_goal_is_not_evidence_of_choice_or_need_causation(self) -> None:
        # An observer can claim any need and action ID under a real goal ID.
        # Correlation must NEVER interpret these strings as authenticated.
        unrelated = replace(
            self.candidate,
            event_id="unrelated-claim",
            need_id="other-need",
            action_request_id="never-executed-request",
            source_kind="observer",
            source_actor_id="untrusted:claims-to-be-human",
        )
        self.ledger.record(unrelated, now=20)
        joined = self.correlate(candidate_event_id="unrelated-claim")
        self.assertEqual(joined.need_id_claimed, "other-need")
        self.assertEqual(joined.action_request_id_claimed, "never-executed-request")
        self.assertFalse(joined.candidate_choice_link_verified)
        self.assertFalse(joined.need_lineage_verified)
        self.assertFalse(joined.tool_effect_verified)

    def test_same_goal_two_historical_choices_cannot_prove_unique_causation(self) -> None:
        """The V1 claim has no choice ID: two sources may link to one claim.

        Correlation intentionally remains historical-only; neither join may
        count as evidence that its choice caused this outcome.
        """
        second_source = "meso-source-2"
        signal_payload = {
            "target": "battery", "kind": "open_loop", "provenance": "model_generated",
            "source": "meso-crct:intent:approach:approach:meso:test",
            "meso_source_event_id": second_source, "effect_authority": False,
        }
        second_signal = self.store.enqueue_event(
            kind="volition.signal", payload=signal_payload, now=3,
        )
        self.store.connection.execute("BEGIN IMMEDIATE")
        self.store.record_meso_intent_ingress(
            source_event_id=second_source,
            intent=self.intent,
            signal_event_id=second_signal,
            now=3,
        )
        self.store.connection.execute("COMMIT")
        self.store.record_volition_signal_receipt(
            source_event_id=second_signal, state_revision=2,
            cognition_event_id=None, goal_id="goal-1", target="battery",
            urgency=0.8, provenance="model_generated",
            signal_source="meso-crct:intent:approach:approach:meso:test",
            choice_id="choice-2", choice_class="POLICY_DERIVED",
            choice_source="volition:policy", now=4,
        )

        changes_before = self.store.connection.total_changes
        first = self.correlate()
        second = self.correlate(meso_source_event_id=second_source)
        self.assertEqual(first.goal_id, second.goal_id)
        self.assertEqual(first.candidate_event_id, second.candidate_event_id)
        self.assertNotEqual(first.choice_id, second.choice_id)
        for result in (first, second):
            self.assertTrue(result.historical_only)
            self.assertFalse(result.candidate_choice_link_verified)
            self.assertFalse(result.need_lineage_verified)
            self.assertFalse(result.effect_authority)
        self.assertEqual(self.store.connection.total_changes, changes_before)

    def test_rejects_unadmitted_or_expired_candidate_and_time_inversion(self) -> None:
        self.assertIsNone(self.store.get_volition_state())
        with self.assertRaises(ValueError):
            self.correlate(now=90)
        with self.assertRaises(ValueError):
            self.correlate(now=float("nan"))
        with self.assertRaises(ValueError):
            self.correlate(now=True)
        self.ledger.reject(self.candidate.event_id, reason="UNVERIFIED_SOURCE", now=22)
        with self.assertRaises(ValueError):
            self.correlate(now=23)

    def test_rejects_candidate_predating_signal_and_hold_ingress(self) -> None:
        self.ledger.record(
            replace(self.candidate, event_id="old-claim", observed_at=1.5),
            now=20,
        )
        with self.assertRaises(ValueError):
            self.correlate(candidate_event_id="old-claim")

        self.store.connection.execute("BEGIN IMMEDIATE")
        self.store.record_meso_intent_ingress(
            source_event_id="held-meso",
            intent={**self.intent, "kind": "hold"},
            signal_event_id=None,
            now=1,
        )
        self.store.connection.execute("COMMIT")
        with self.assertRaises(ValueError):
            self.correlate(meso_source_event_id="held-meso")


if __name__ == "__main__":
    unittest.main()
