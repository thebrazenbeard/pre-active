from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_readme_states_promptless_continuous_runtime_purpose() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Continuous resident runtime for self-initiating LLM agency" in readme
    assert "does **not** require a human prompt to receive every turn" in readme
    assert "USER_PROMPT != MODEL_TURN" in readme
    assert "CONTINUOUS_RUNTIME != CONTINUOUS_INFERENCE" in readme
    assert "AUTONOMOUS_TURN != EFFECT_AUTHORITY" in readme


def test_autonomy_contract_preserves_sources_and_authority_boundaries() -> None:
    contract = (ROOT / "docs" / "AUTONOMOUS_RUNTIME.md").read_text(
        encoding="utf-8"
    )
    for source in ("EXTERNAL", "TEMPORAL", "OPEN_LOOP", "ENDOGENOUS"):
        assert source in contract
    assert "pre_active.request_turn" in contract
    assert "Observer, Initiator, Critic" in contract
    assert "everything the host has deliberately connected and authorized" in contract
    assert "INITIATIVE != PERMISSION" in contract
    assert "CRITIQUE != AUTHORITY" in contract
    assert "TURN_GRANTED" in contract
    assert "CAPABILITY_GRANTED" in contract


def test_effect_contract_says_autonomy_does_not_grant_effect_authority() -> None:
    effect = (ROOT / "docs" / "EFFECT_AND_RECOVERY.md").read_text(
        encoding="utf-8"
    )
    assert "Autonomous cognition is not effect authority" in effect
    assert "AUTONOMOUS_TURN != CAPABILITY_GRANT" in effect
    assert "INITIATIVE != EFFECT_AUTHORITY" in effect
