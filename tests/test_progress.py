from __future__ import annotations

import json
from pathlib import Path

import pytest

from pre_active.cli import main
from pre_active.progress import ProgressLedger
from pre_active.store import Store


def test_candidate_progress_is_not_trusted_until_explicit_verification(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    run_id = store.create_run(
        task="Build the thing.",
        capabilities=set(),
        now=1.0,
    )
    ledger = ProgressLedger(store)

    checkpoint_id = ledger.add_candidate(
        run_id=run_id,
        step=0,
        summary="The implementation appears complete.",
        evidence={"tests": "not yet independently checked"},
        producer="executor-model",
        now=2.0,
    )

    candidate = ledger.get(checkpoint_id)
    assert candidate["state"] == "CANDIDATE"
    assert ledger.trusted(run_id=run_id) == []

    ledger.decide(
        checkpoint_id,
        decision="VERIFIED",
        verifier="pytest",
        reason="Independent acceptance suite passed.",
        now=3.0,
    )

    trusted = ledger.trusted(run_id=run_id)
    assert [item["id"] for item in trusted] == [checkpoint_id]
    assert trusted[0]["state"] == "VERIFIED"
    assert trusted[0]["verifier"] == "pytest"
    assert trusted[0]["decision_reason"] == "Independent acceptance suite passed."

    journal = store.list_journal(subject_id=run_id)
    assert any(
        item["event_type"] == "PROGRESS_CANDIDATE_RECORDED"
        for item in journal
    )
    assert any(item["event_type"] == "PROGRESS_VERIFIED" for item in journal)


def test_rejected_progress_remains_auditable_and_cannot_be_rewritten(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    run_id = store.create_run(
        task="Do not launder failure into progress.",
        capabilities=set(),
        now=1.0,
    )
    ledger = ProgressLedger(store)
    checkpoint_id = ledger.add_candidate(
        run_id=run_id,
        step=0,
        summary="Claimed fix.",
        evidence={"claim": "looks good"},
        producer="worker",
        now=2.0,
    )

    ledger.decide(
        checkpoint_id,
        decision="REJECTED",
        verifier="auditor",
        reason="The observed file still contains the defect.",
        now=3.0,
    )

    rejected = ledger.get(checkpoint_id)
    assert rejected["state"] == "REJECTED"
    assert rejected["evidence"] == {"claim": "looks good"}
    assert ledger.trusted(run_id=run_id) == []

    with pytest.raises(RuntimeError, match="immutable"):
        ledger.decide(
            checkpoint_id,
            decision="VERIFIED",
            verifier="same-auditor",
            reason="Changed my mind.",
            now=4.0,
        )


def test_checkpoint_cannot_claim_a_future_run_step(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    run_id = store.create_run(
        task="Current generation only.",
        capabilities=set(),
        now=1.0,
    )
    ledger = ProgressLedger(store)

    with pytest.raises(ValueError, match="exceeds current run step"):
        ledger.add_candidate(
            run_id=run_id,
            step=1,
            summary="Future work",
            evidence={},
            producer="worker",
            now=2.0,
        )


def test_checkpoint_evidence_must_be_structured_object(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    run_id = store.create_run(
        task="Structured evidence.",
        capabilities=set(),
        now=1.0,
    )
    ledger = ProgressLedger(store)

    with pytest.raises(ValueError, match="JSON object"):
        ledger.add_candidate(
            run_id=run_id,
            step=0,
            summary="Bad evidence shape",
            evidence=["not", "an", "object"],  # type: ignore[arg-type]
            producer="worker",
            now=2.0,
        )


def test_checkpoint_cli_round_trip(tmp_path: Path, capsys) -> None:
    state = tmp_path / "state.db"

    assert main(["--state", str(state), "submit", "Verify my progress."]) == 0
    run_id = json.loads(capsys.readouterr().out)["run_id"]

    assert main([
        "--state",
        str(state),
        "checkpoint",
        "add",
        run_id,
        "Acceptance evidence collected.",
        "--producer",
        "executor",
        "--evidence-json",
        '{"artifact":"build/output.txt","sha256":"abc123"}',
    ]) == 0
    candidate = json.loads(capsys.readouterr().out)
    checkpoint_id = candidate["id"]
    assert candidate["state"] == "CANDIDATE"

    assert main([
        "--state",
        str(state),
        "checkpoint",
        "list",
        run_id,
        "--trusted",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["checkpoints"] == []

    assert main([
        "--state",
        str(state),
        "checkpoint",
        "verify",
        checkpoint_id,
        "--verifier",
        "acceptance-harness",
        "--reason",
        "Observed artifact and checks match acceptance criteria.",
    ]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["state"] == "VERIFIED"

    assert main([
        "--state",
        str(state),
        "checkpoint",
        "list",
        run_id,
        "--trusted",
    ]) == 0
    trusted = json.loads(capsys.readouterr().out)["checkpoints"]
    assert [item["id"] for item in trusted] == [checkpoint_id]


def test_checkpoint_cli_rejects_non_object_evidence(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state.db"
    store = Store(state)
    run_id = store.create_run(
        task="Bad CLI evidence",
        capabilities=set(),
        now=1.0,
    )
    store.close()

    with pytest.raises(SystemExit, match="JSON object"):
        main([
            "--state",
            str(state),
            "checkpoint",
            "add",
            run_id,
            "Bad shape",
            "--evidence-json",
            '["list"]',
        ])


def test_progress_ledger_constructor_does_not_commit_active_transaction(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    store.connection.execute("BEGIN IMMEDIATE")
    try:
        ProgressLedger(store)
        assert store.connection.in_transaction is True
    finally:
        store.connection.execute("ROLLBACK")


def test_exact_repeated_verifier_decision_is_idempotent(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    run_id = store.create_run(
        task="Idempotent verifier retry.",
        capabilities=set(),
        now=1.0,
    )
    ledger = ProgressLedger(store)
    checkpoint_id = ledger.add_candidate(
        run_id=run_id,
        step=0,
        summary="Candidate",
        evidence={"check": "passed"},
        producer="worker",
        now=2.0,
    )

    for _ in range(2):
        ledger.decide(
            checkpoint_id,
            decision="VERIFIED",
            verifier="acceptance-harness",
            reason="Exact same verification retry.",
            now=3.0,
        )

    verified_events = [
        item
        for item in store.list_journal(subject_id=run_id)
        if item["event_type"] == "PROGRESS_VERIFIED"
    ]
    assert len(verified_events) == 1
    assert ledger.get(checkpoint_id)["state"] == "VERIFIED"
