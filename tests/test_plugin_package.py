import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin" / "pre-active"


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    end = text.index("\n---\n", 4)
    result: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip()
    return result


def test_private_plugin_package_has_stable_identity_and_ui_contract() -> None:
    manifest = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "pre-active"
    assert manifest["version"] == "0.1.0"
    interface = manifest["extensions"]["com.openai"]["interface"]
    assert interface["displayName"] == "Pre-Active"
    assert len(interface["shortDescription"]) <= 30
    assert interface["capabilities"] == ["Interactive"]


def test_plugin_skill_preserves_runtime_and_effect_boundaries() -> None:
    skill = PLUGIN / "skills" / "pre-active" / "SKILL.md"
    meta = _frontmatter(skill)
    text = skill.read_text(encoding="utf-8")
    assert meta["name"] == "pre-active"
    assert "REQUEST != AUTHORITY != ATTEMPT != EFFECT != VERIFIED EFFECT" in text
    assert "Loading this skill does not prove a daemon is running." in text
    assert "requires explicit authority for that exact effect" in text
    assert "legacy predecessor binding" in text


def test_plugin_runtime_reference_is_packaged() -> None:
    reference = PLUGIN / "skills" / "pre-active" / "references" / "RUNTIME_CONTRACT.md"
    text = reference.read_text(encoding="utf-8")
    assert reference.is_file()
    assert r"C:\ProgramData\PreActive" in text
    assert r"C:\ProgramData\PreActive\state\state.db" in text
    assert "status response" in text


def test_plugin_preserves_autonomous_turn_semantics() -> None:
    skill = PLUGIN / "skills" / "pre-active" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    assert "USER_PROMPT != MODEL_TURN" in text
    assert "CONTINUOUS_RUNTIME != CONTINUOUS_INFERENCE" in text
    assert "AUTONOMOUS_TURN != CAPABILITY_GRANT != EFFECT_AUTHORITY" in text
    assert "pre_active.request_turn" in text
    assert "WAITING" in text
    assert "does not by itself install or authorize an observer" in text
