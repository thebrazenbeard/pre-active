from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".md", ".py", ".toml", ".json", ".yml", ".yaml", ".ps1", ".txt"}
TEXT_NAMES = {".gitignore", "LICENSE", "NOTICE"}
SKIP_PARTS = {".git", ".pytest_cache", "__pycache__", ".venv", "venv", "build", "dist"}
BANNED = (
    "Pro" + "-Run",
    "pro" + "-run",
    "PRO" + "_RUN",
    "Pro" + "Run",
    "pro" + "run",
    "PRO" + "RUN",
)


def _repository_text_files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_PARTS or part.endswith(".egg-info") for part in path.parts):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_NAMES:
            yield path


def test_current_tree_uses_pre_active_brand_exclusively() -> None:
    stale: list[str] = []
    for path in _repository_text_files():
        text = path.read_text(encoding="utf-8")
        for token in BANNED:
            if token in text:
                stale.append(f"{path.relative_to(ROOT)}:{token}")

    path_tokens = ("pro" + "run", "pro" + "-run")
    stale_paths = [
        str(path.relative_to(ROOT))
        for path in ROOT.rglob("*")
        if path.is_file()
        and not any(part.endswith(".egg-info") for part in path.parts)
        and any(token in str(path.relative_to(ROOT)).lower() for token in path_tokens)
    ]

    assert stale == []
    assert stale_paths == []
    assert (ROOT / "src" / "pre_active").is_dir()
    assert (ROOT / "ops" / "windows" / "start-pre-active.ps1").is_file()
