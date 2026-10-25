"""Adversarial contract checks for inert, untrusted outcome candidates."""

from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pre_active.outcomes import (
    OUTCOME_CLAIM_SCHEMA_VERSION,
    ClaimedOutcome,
    OutcomeCandidate,
    OutcomeConflict,
    OutcomeLedger,
    OutcomeState,
)
from pre_active.store import Store


class OutcomeLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.db"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.ledger = OutcomeLedger(self.store)

    def candidate(self, **changes: object) -> OutcomeCandidate:
        baseline = OutcomeCandidate(
            event_id="observation-1",
            need_id="battery",
            goal_id="goal-1",
            action_request_id="tool-request-1",
            claimed_outcome=ClaimedOutcome.SUCCESS,
            claimed_need_change=0.5,
            source_kind="observer",
            source_actor_id="host-monitor-1",
            source_revision="monitor@v1",
            evidence_ref="evidence://monitor/observation-1",
            evidence_sha256="a" * 64,
            observed_at=12.0,
            expires_at=90.0,
        )
        return replace(baseline, **changes)

    def test_candidate_is_only_a_claim_and_cannot_change_motives(self) -> None:
        self.store.save_volition_state(
            {"schema": "VOLITION_STATE_V2", "satiation": {"battery": 0.0}},
            expected_revision=0,
            now=1.0,
        )
        prior = self.store.get_volition_state()
        receipt = self.ledger.record(self.candidate(), now=20.0)

        self.assertEqual(receipt.status, OutcomeState.CANDIDATE)
        self.assertEqual(receipt.claimed_outcome, ClaimedOutcome.SUCCESS)
        self.assertEqual(self.store.get_volition_state(), prior)
        self.assertEqual(self.ledger.count(), 1)

    def test_model_self_reported_success_cannot_be_promoted_or_applied(self) -> None:
        receipt = self.ledger.record(
            self.candidate(
                source_kind="model_generated",
                source_actor_id="model:untrusted",
                claimed_need_change=1.0,
            ),
            now=20.0,
        )
        self.assertEqual(receipt.status, OutcomeState.CANDIDATE)
        self.assertNotIn("VERIFIED", {state.value for state in OutcomeState})
        self.assertFalse(hasattr(self.ledger, "verify"))
        self.assertFalse(hasattr(self.ledger, "apply_satisfaction"))
        self.assertIsNone(self.store.get_volition_state())

    def test_exact_replay_is_idempotent_but_changed_payload_conflicts(self) -> None:
        first = self.ledger.record(self.candidate(), now=20.0)
        repeat = self.ledger.record(self.candidate(), now=21.0)
        self.assertEqual(first, repeat)
        self.assertEqual(self.ledger.count(), 1)
        with self.assertRaises(OutcomeConflict):
            self.ledger.record(self.candidate(claimed_need_change=0.9), now=22.0)
        self.assertEqual(self.ledger.get("observation-1"), first)

    def test_versioned_canonical_receipt_survives_restart(self) -> None:
        first = self.ledger.record(self.candidate(), now=20.0)
        self.assertEqual(first.claim.schema_version, OUTCOME_CLAIM_SCHEMA_VERSION)
        payload = self.store.connection.execute(
            "SELECT claim_json FROM outcome_candidates WHERE event_id=?",
            ("observation-1",),
        ).fetchone()[0]
        self.assertIn('"schema_version":1', payload)
        self.store.close()
        self.store = Store(self.path)
        self.ledger = OutcomeLedger(self.store)
        self.assertEqual(self.ledger.record(self.candidate(), now=21.0), first)
        self.assertEqual(self.ledger.count(), 1)

    def test_expired_and_future_claims_are_rejected_with_receipts(self) -> None:
        expired = self.ledger.record(self.candidate(), now=90.0)
        self.assertEqual(expired.status, OutcomeState.REJECTED)
        self.assertEqual(expired.reject_reason, "EXPIRED_ON_ARRIVAL")
        self.assertEqual(self.ledger.record(self.candidate(), now=91.0), expired)

        future = self.ledger.record(
            self.candidate(
                event_id="observation-2", observed_at=100.0, expires_at=190.0
            ),
            now=20.0,
        )
        self.assertEqual(future.status, OutcomeState.REJECTED)
        self.assertEqual(future.reject_reason, "FUTURE_OBSERVATION")

    def test_expiry_and_rejection_are_durable_and_cannot_be_reversed(self) -> None:
        candidate = self.ledger.record(self.candidate(), now=20.0)
        self.assertEqual(self.ledger.reject_expired(now=90.0), ("observation-1",))
        self.assertEqual(self.ledger.reject_expired(now=100.0), ())
        rejected = self.ledger.get(candidate.event_id)
        self.assertEqual(rejected.status, OutcomeState.REJECTED)
        self.assertEqual(rejected.reject_reason, "EXPIRED")

        self.store.close()
        self.store = Store(self.path)
        self.ledger = OutcomeLedger(self.store)
        self.assertEqual(self.ledger.record(self.candidate(), now=101.0), rejected)
        self.assertEqual(self.ledger.get(candidate.event_id), rejected)
        with self.assertRaises(OutcomeConflict):
            self.ledger.reject(
                candidate.event_id, reason="CONTRADICTORY_EVIDENCE", now=102.0
            )

    def test_explicit_rejection_is_immutable_and_idempotent(self) -> None:
        self.ledger.record(self.candidate(), now=20.0)
        first = self.ledger.reject(
            "observation-1", reason="UNVERIFIED_SOURCE", now=21.0
        )
        self.assertEqual(first.status, OutcomeState.REJECTED)
        self.assertEqual(first.reject_reason, "UNVERIFIED_SOURCE")
        self.assertEqual(
            self.ledger.reject("observation-1", reason="UNVERIFIED_SOURCE", now=25.0),
            first,
        )
        with self.assertRaises(KeyError):
            self.ledger.reject("missing", reason="UNVERIFIED_SOURCE", now=22.0)
        with self.assertRaises(ValueError):
            self.ledger.reject("observation-1", reason="ACCEPTED", now=22.0)

    def test_manual_expired_rejection_cannot_preempt_expiry(self) -> None:
        self.ledger.record(self.candidate(), now=20.0)
        with self.assertRaises(ValueError):
            self.ledger.reject("observation-1", reason="EXPIRED", now=89.0)
        self.assertEqual(
            self.ledger.get("observation-1").status, OutcomeState.CANDIDATE
        )
        self.assertEqual(
            self.ledger.reject("observation-1", reason="EXPIRED", now=90.0).status,
            OutcomeState.REJECTED,
        )

    def test_nested_record_failure_rolls_back_despite_parent_commit(self) -> None:
        self.store.connection.execute("BEGIN IMMEDIATE")
        with (
            patch.object(
                self.store, "append_journal", side_effect=RuntimeError("journal fault")
            ),
            self.assertRaisesRegex(RuntimeError, "journal fault"),
        ):
            self.ledger.record(self.candidate(), now=20.0)
        self.assertTrue(self.store.connection.in_transaction)
        self.store.connection.execute("COMMIT")
        self.assertEqual(self.ledger.count(), 0)

    def test_nested_reject_failure_preserves_candidate_after_parent_commit(
        self,
    ) -> None:
        self.ledger.record(self.candidate(), now=20.0)
        self.store.connection.execute("BEGIN IMMEDIATE")
        with (
            patch.object(
                self.store, "append_journal", side_effect=RuntimeError("journal fault")
            ),
            self.assertRaisesRegex(RuntimeError, "journal fault"),
        ):
            self.ledger.reject("observation-1", reason="UNVERIFIED_SOURCE", now=21.0)
        self.store.connection.execute("COMMIT")
        self.assertEqual(
            self.ledger.get("observation-1").status, OutcomeState.CANDIDATE
        )

    def test_nested_expiry_batch_failure_rolls_back_every_rejection(self) -> None:
        self.ledger.record(self.candidate(), now=20.0)
        self.ledger.record(self.candidate(event_id="observation-2"), now=20.0)
        original_append = self.store.append_journal
        calls = 0

        def fail_second_journal(**kwargs: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("second journal fault")
            original_append(**kwargs)

        self.store.connection.execute("BEGIN IMMEDIATE")
        with (
            patch.object(self.store, "append_journal", side_effect=fail_second_journal),
            self.assertRaisesRegex(RuntimeError, "second journal fault"),
        ):
            self.ledger.reject_expired(now=90.0)
        self.store.connection.execute("COMMIT")
        self.assertEqual(
            self.ledger.get("observation-1").status, OutcomeState.CANDIDATE
        )
        self.assertEqual(
            self.ledger.get("observation-2").status, OutcomeState.CANDIDATE
        )

    def test_parent_transaction_rollback_removes_claim_and_journal(self) -> None:
        self.store.connection.execute("BEGIN IMMEDIATE")
        self.ledger.record(self.candidate(), now=20.0)
        self.assertEqual(self.ledger.count(), 1)
        self.store.connection.execute("ROLLBACK")
        self.assertEqual(self.ledger.count(), 0)
        self.assertEqual(self.store.list_journal(subject_id="observation-1"), [])

    def test_separate_connections_cannot_create_duplicate_event(self) -> None:
        claim = self.candidate()

        def insert() -> object:
            client = Store(self.path)
            try:
                return OutcomeLedger(client).record(claim, now=20.0)
            finally:
                client.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = tuple(pool.map(lambda _: insert(), range(2)))
        self.assertEqual(first, second)
        self.assertEqual(self.ledger.count(), 1)

    def test_database_record_tampering_is_detected_on_read(self) -> None:
        self.ledger.record(self.candidate(), now=20.0)
        self.store.connection.execute(
            "UPDATE outcome_candidates SET evidence_sha256=? WHERE event_id=?",
            ("b" * 64, "observation-1"),
        )
        with self.assertRaises(RuntimeError):
            self.ledger.get("observation-1")

    def test_invalid_custody_claims_and_nonfinite_amounts_fail_before_writes(
        self,
    ) -> None:
        invalid = (
            {"event_id": ""},
            {"source_actor_id": " "},
            {"evidence_ref": " "},
            {"evidence_sha256": "z" * 64},
            {"claimed_need_change": float("nan")},
            {"claimed_need_change": 2.0},
            {"observed_at": float("inf")},
            {"expires_at": 11.0},
            {"claimed_outcome": ClaimedOutcome.NO_EFFECT, "claimed_need_change": 0.5},
            {"claimed_outcome": ClaimedOutcome.FAILURE, "claimed_need_change": 0.3},
            {"claimed_outcome": ClaimedOutcome.UNKNOWN, "claimed_need_change": -0.1},
            {"source_kind": "authenticated"},
            {"source_kind": ["observer"]},
            {"schema_version": 2},
            {"schema_version": True},
        )
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.candidate(**fields)
        self.assertEqual(self.ledger.count(), 0)


if __name__ == "__main__":
    unittest.main()
