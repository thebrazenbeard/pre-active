"""Source-only advisory audit tests, no network, model calls, or execution."""

import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from pre_active.opportunity_advisory import (
    GoalObservationLink, OpportunityAdvisoryLedger, build_route_advisory,
)
from pre_active.store import Store


PLANNER_HEAD = "c19ef17a9f93514910a01be9df07c74210252496"
LINK = GoalObservationLink("goal_1", 3, "observation_7", "a" * 64, PLANNER_HEAD)


def plan(**overrides):
    basic = {
        "status": "AVAILABLE", "action_ids": ["inspect"], "total_cost": 0,
        "missing_approvals": [], "blockers": [], "explored_states": 4,
        "executed": False, "verified": False,
        "evidence_class": "CALLER_SUPPLIED_UNVERIFIED",
        "provenance": {"inspect": "caller_supplied_unverified"},
    }
    basic.update(overrides)
    return basic


class AdvisoryContractTests(unittest.TestCase):
    def test_available_never_becomes_permission(self):
        r = build_route_advisory(LINK, plan())
        self.assertEqual(r["route"]["status"], "AVAILABLE")
        self.assertFalse(r["permission_authenticated"])
        self.assertFalse(r["may_execute"])
        self.assertFalse(r["verified_result"])
        self.assertEqual(r["effect_authority"], "NONE")
        self.assertEqual(r["observation_claim"]["admission"], "UNVERIFIED")

    def test_hash_is_deterministic_and_binds_revision(self):
        a = build_route_advisory(LINK, plan())
        self.assertEqual(a, build_route_advisory(LINK, plan()))
        self.assertNotEqual(a["envelope_sha256"], build_route_advisory(replace(LINK, goal_revision=4), plan())["envelope_sha256"])

    def test_pending_and_search_unknown_are_preserved(self):
        p = build_route_advisory(LINK, plan(status="NEEDS_APPROVAL", missing_approvals=["owner"]))
        self.assertEqual(p["route"]["missing_approvals"], ["owner"])
        self.assertFalse(p["may_execute"])
        unknown = build_route_advisory(LINK, plan(status="INCOMPLETE_UNKNOWN", blockers=["BOUNDED_SEARCH_LIMIT"]))
        self.assertEqual(unknown["route"]["status"], "INCOMPLETE_UNKNOWN")

    def test_rejects_effect_and_evidence_laundering(self):
        cases = [
            plan(executed=True), plan(verified=True), plan(evidence_class="HOST_VERIFIED"),
            plan(effect_authority="EXECUTE"), plan(granted_approvals=["owner"]),
            plan(dispatch_task="do stuff"),
        ]
        for item in cases:
            with self.subTest(item=item), self.assertRaises(ValueError):
                build_route_advisory(LINK, item)

    def test_rejects_nonzero_budget_and_bad_status(self):
        for bad in [plan(total_cost=1), plan(total_cost=-1), plan(total_cost=True),
                    plan(status="AVAILABLE", missing_approvals=["owner"]),
                    plan(status="NEEDS_APPROVAL"), plan(status="NO_ROUTE")]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                build_route_advisory(LINK, bad)

    def test_rejects_unbounded_names_and_provenance(self):
        for bad in [
            plan(action_ids=["run tool && exit"], provenance={"run tool && exit": "x"}),
            plan(action_ids=["inspect", "inspect"], provenance={"inspect": "x"}),
            plan(provenance={"other": "claim"}),
            plan(action_ids=[f"a{i}" for i in range(33)],
                 provenance={f"a{i}": "x" for i in range(33)}),
        ]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                build_route_advisory(LINK, bad)

    def test_rejects_ambiguous_link_values(self):
        for change in [{"goal_id": "person with spaces"}, {"goal_revision": True},
                       {"observation_sha256": "bad"}, {"planner_head": "0" * 39}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(LINK, **change)


class AdvisoryLedgerTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.ledger = OpportunityAdvisoryLedger(self.store)

    def tearDown(self):
        self.store.close()

    def test_one_record_journal_no_queue_or_run(self):
        before_events = self.store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        before_runs = self.store.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        r = self.ledger.record(proposal_id="proposal_1", link=LINK, route_plan=plan(), now=5.0)
        self.assertTrue(r["newly_recorded"])
        self.assertEqual(r["effect_authority"], "NONE")
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], before_events)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], before_runs)
        journal = self.store.list_journal(subject_id="proposal_1")
        self.assertEqual(len(journal), 1)
        self.assertEqual(journal[0]["event_type"], "OPPORTUNITY_ADVISORY_RECORDED")
        self.assertFalse(journal[0]["payload"]["may_execute"])

    def test_exact_replay_idempotent_even_with_new_timestamp(self):
        first = self.ledger.record(proposal_id="proposal_1", link=LINK, route_plan=plan(), now=5.0)
        second = self.ledger.record(proposal_id="proposal_1", link=LINK, route_plan=copy.deepcopy(plan()), now=6.0)
        self.assertFalse(second["newly_recorded"])
        self.assertEqual(first["journal_seq"], second["journal_seq"])
        self.assertEqual(len(self.store.list_journal(subject_id="proposal_1")), 1)

    def test_divergent_replay_conflicts_without_another_journal(self):
        self.ledger.record(proposal_id="proposal_1", link=LINK, route_plan=plan(), now=5.0)
        with self.assertRaises(ValueError):
            self.ledger.record(proposal_id="proposal_1", link=replace(LINK, goal_revision=4), route_plan=plan(), now=6.0)
        self.assertEqual(len(self.store.list_journal(subject_id="proposal_1")), 1)

    def test_invalid_plan_fails_before_transaction_and_write(self):
        with self.assertRaises(ValueError):
            self.ledger.record(proposal_id="proposal_1", link=LINK, route_plan=plan(verified=True), now=5.0)
        self.assertFalse(self.store.connection.in_transaction)
        self.assertEqual(len(self.store.list_journal(subject_id="proposal_1")), 0)

    def test_invalid_clock_and_proposal_id(self):
        for proposal_id, now in [("bad id", 0.0), ("good", float("nan")),
                                 ("good", True), ("good", 10**1000)]:
            with self.subTest(proposal_id=proposal_id, now=now), self.assertRaises(ValueError):
                self.ledger.record(proposal_id=proposal_id, link=LINK, route_plan=plan(), now=now)

    def test_journal_payload_cannot_be_promoted_by_caller(self):
        raw = json.loads(json.dumps(plan()))
        raw["status"] = "AVAILABLE"
        r = self.ledger.record(proposal_id="proposal_2", link=LINK, route_plan=raw, now=10.0)
        self.assertEqual(r["receipt"]["source"], "UNTRUSTED_CALLER_PLAN")
        self.assertFalse(r["receipt"]["permission_authenticated"])
        self.assertEqual(r["receipt"]["effect_authority"], "NONE")

    def test_reopen_reuses_exact_receipt_without_reenqueuing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "advisory.sqlite3"
            first_store = Store(path)
            first = OpportunityAdvisoryLedger(first_store).record(
                proposal_id="restart_p1", link=LINK, route_plan=plan(), now=5.0)
            first_store.close()
            second_store = Store(path)
            second = OpportunityAdvisoryLedger(second_store).record(
                proposal_id="restart_p1", link=LINK, route_plan=plan(), now=500.0)
            self.assertEqual(first["journal_seq"], second["journal_seq"])
            self.assertFalse(second["newly_recorded"])
            self.assertEqual(len(second_store.list_journal(subject_id="restart_p1")), 1)
            self.assertEqual(second_store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
            second_store.close()


if __name__ == "__main__":
    unittest.main()
