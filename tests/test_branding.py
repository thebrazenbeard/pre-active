from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".md", ".py", ".toml", ".json", ".yml", ".yaml", ".ps1", ".txt"}
TEXT_NAMES = {"LICENSE", "NOTICE", ".gitignore"}

FORBIDDEN_TOKENS = (
    "Pro" + "-Run",
    "pro" + "-run",
    "PRO" + "_RUN",
    "Pro" + "Run",
    "pro" + "run",
    "PRO" + "RUN",
)


def _text_files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in {".git", ".pytest_cache", "__pycache__", ".venv"} for part in path.parts):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_NAMES:
            yield path


def test_repository_contains_no_retired_runtime_name():
    offenders = []
    for path in _text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token in FORBIDDEN_TOKENS:
            if token in text:
                offenders.append(f"{path.relative_to(ROOT)}: {token}")

    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT).as_posix()
        for token in FORBIDDEN_TOKENS:
            if token.lower() in rel.lower():
                offenders.append(f"path: {rel}: {token}")

    assert offenders == []


def test_canonical_pre_active_names_are_bound():
    assert (ROOT / "src" / "pre_active").is_dir()
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "pre-active"' in pyproject
    assert 'pre-active = "pre_active.cli:main"' in pyproject
