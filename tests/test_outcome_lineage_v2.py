"""Source-only V2 lineage claims must never become evidence or effect grants."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path

from pre_active.outcome_correlation import correlate_outcome_candidate
from pre_active.outcome_lineage_v2 import (
    ClaimedPhysicalEffectV2,
    ClaimedSourceLineageV2,
    OutcomeLineageV2,
    correlate_outcome_lineage_v2,
)
from pre_active.outcomes import ClaimedOutcome, OutcomeCandidate, OutcomeLedger
from pre_active.store import Store


class OutcomeLineageV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / "state.db")
        self.addCleanup(self.store.close)
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
        self.store.connection.execute("BEGIN IMMEDIATE")
        self.store.record_meso_intent_ingress(
            source_event_id=self.source_id,
            intent={
                "target_id": "battery",
                "kind": "approach",
                "strength": 0.8,
                "source_tendency": "approach",
                "source": "meso:test",
                "effect_authorized": False,
                "can_execute": False,
            },
            signal_event_id=self.signal_id,
            now=1,
        )
        self.store.connection.execute("COMMIT")
        self.store.record_volition_signal_receipt(
            source_event_id=self.signal_id,
            state_revision=2,
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
            event_id="claimed-outcome-1",
            need_id="need-1",
            goal_id="goal-1",
            action_request_id="action-1",
            claimed_outcome=ClaimedOutcome.SUCCESS,
            claimed_need_change=0.3,
            source_kind="model_generated",
            source_actor_id="model:untrusted",
            source_revision="model@v1",
            evidence_ref="evidence://claimed",
            evidence_sha256="a" * 64,
            observed_at=12,
            expires_at=90,
        )
        OutcomeLedger(self.store).record(self.candidate, now=20)
        self.correlation = correlate_outcome_candidate(
            store=self.store, candidate_event_id=self.candidate.event_id,
            meso_source_event_id=self.source_id, now=21,
        )
        self.source = ClaimedSourceLineageV2(
            candidate_event_id=self.candidate.event_id,
            meso_source_event_id=self.source_id,
            signal_event_id=self.signal_id,
            goal_id="goal-1",
            choice_id="choice-1",
            goal_revision=2,
            need_id="need-1",
            need_revision=5,
            need_observation_id="observation-1",
            action_request_id="action-1",
            attempt_id="attempt-1",
        )
        self.effect = ClaimedPhysicalEffectV2(
            attempt_id="attempt-1",
            physical_effect_id="effect-1",
            evidence_sha256="a" * 64,
        )

    def link(self, **changes: object) -> OutcomeLineageV2:
        parameters = {
            "correlations": (self.correlation,),
            "source": self.source,
            "effect": self.effect,
            "expected_goal_revision": 2,
            "expected_need_revision": 5,
            "expected_attempt_id": "attempt-1",
            "expected_physical_effect_id": "effect-1",
            "prior": (),
        }
        parameters.update(changes)
        return correlate_outcome_lineage_v2(**parameters)

    def test_exact_claim_remains_inert_and_does_not_write(self) -> None:
        before = self.store.connection.total_changes
        envelope = self.link()
        self.assertIs(envelope.source, self.source)
        self.assertIs(envelope.effect, self.effect)
        self.assertEqual(envelope.candidate_sha256, self.correlation.candidate_sha256)
        self.assertTrue(envelope.historical_only)
        self.assertFalse(envelope.evidence_admitted)
        self.assertFalse(envelope.need_lineage_verified)
        self.assertFalse(envelope.choice_link_verified)
        self.assertFalse(envelope.physical_effect_verified)
        self.assertFalse(envelope.effect_authority)
        self.assertEqual(self.store.connection.total_changes, before)
        with self.assertRaises((AttributeError, TypeError)):
            envelope.source = self.source
        with self.assertRaises(TypeError):
            OutcomeLineageV2(source=self.source, effect=self.effect,
                             candidate_sha256="a" * 64)
        for flag in ("evidence_admitted", "need_lineage_verified",
                     "choice_link_verified", "physical_effect_verified",
                     "effect_authority"):
            with self.subTest(flag=flag):
                self.assertNotIn(flag, {item.name for item in fields(envelope)})
                with self.assertRaises(TypeError):
                    replace(envelope, **{flag: True})

    def test_same_goal_different_choice_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "choice"):
            self.link(source=replace(self.source, choice_id="choice-other"))

    def test_missing_and_ambiguous_correlations_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.link(correlations=())
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.link(correlations=(self.correlation, self.correlation))

    def test_goal_and_need_revision_claim_mismatches_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "goal revision"):
            self.link(source=replace(self.source, goal_revision=1))
        with self.assertRaisesRegex(ValueError, "need revision"):
            self.link(source=replace(self.source, need_revision=4))
        with self.assertRaises(ValueError):
            self.link(expected_goal_revision=True)

    def test_forged_or_absent_effect_claim_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "effect"):
            self.link(effect=None)
        with self.assertRaisesRegex(ValueError, "physical effect"):
            self.link(effect=replace(self.effect, physical_effect_id="fake-effect"))
        with self.assertRaisesRegex(ValueError, "digest"):
            self.link(effect=replace(self.effect, evidence_sha256="b" * 64))
        with self.assertRaises(ValueError):
            ClaimedPhysicalEffectV2(
                attempt_id="attempt-1", physical_effect_id="",
                evidence_sha256="a" * 64,
            )

    def test_wrong_attempt_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "attempt"):
            self.link(effect=replace(self.effect, attempt_id="unrelated-attempt"))
        with self.assertRaisesRegex(ValueError, "attempt"):
            self.link(expected_attempt_id="unrelated-attempt")

    def test_candidate_replay_is_rejected_in_supplied_prior(self) -> None:
        first = self.link()
        with self.assertRaisesRegex(ValueError, "replay"):
            self.link(
                source=replace(self.source, attempt_id="new-attempt"),
                effect=replace(self.effect, attempt_id="new-attempt", physical_effect_id="new-effect"),
                expected_attempt_id="new-attempt",
                expected_physical_effect_id="new-effect",
                prior=(first,),
            )

    def test_attempt_replay_is_rejected_with_distinct_candidate_and_effect(self) -> None:
        first = self.link()
        # This synthetic correlation is a caller-provided claim, not a
        # second authenticated database event.
        second_id = "second-candidate"
        with self.assertRaisesRegex(ValueError, "replay"):
            self.link(
                correlations=(replace(self.correlation, candidate_event_id=second_id),),
                source=replace(self.source, candidate_event_id=second_id),
                effect=replace(self.effect, physical_effect_id="second-effect"),
                expected_physical_effect_id="second-effect",
                prior=(first,),
            )

    def test_effect_replay_is_rejected_with_distinct_candidate_and_attempt(self) -> None:
        first = self.link()
        second_id = "second-candidate"
        with self.assertRaisesRegex(ValueError, "replay"):
            self.link(
                correlations=(replace(self.correlation, candidate_event_id=second_id),),
                source=replace(self.source, candidate_event_id=second_id, attempt_id="second-attempt"),
                effect=replace(self.effect, attempt_id="second-attempt"),
                expected_attempt_id="second-attempt",
                prior=(first,),
            )

    def test_missing_action_or_need_binding_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "action"):
            self.link(source=replace(self.source, action_request_id="other-action"))
        with self.assertRaisesRegex(ValueError, "need"):
            self.link(source=replace(self.source, need_id="other-need"))
        with self.assertRaises(ValueError):
            ClaimedSourceLineageV2(
                **{**{item.name: getattr(self.source, item.name)
                     for item in fields(self.source)},
                   "need_observation_id": ""}
            )
