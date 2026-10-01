from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".md", ".py", ".toml", ".json", ".yml", ".yaml", ".ps1", ".txt"}
TEXT_NAMES = {".gitignore", "LICENSE", "NOTICE"}
BANNED = ("Pro-Run", "pro-run", "PRO_RUN", "ProRun", "prorun", "PRORUN")


def test_current_tree_uses_pre_active_brand_exclusively() -> None:
    stale: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in TEXT_NAMES:
            continue
        text = path.read_text(encoding="utf-8")
        for token in BANNED:
            if token in text:
                stale.append(f"{path.relative_to(ROOT)}:{token}")

    stale_paths = [
        str(path.relative_to(ROOT))
        for path in ROOT.rglob("*")
        if path.is_file()
        and any(token.lower() in str(path.relative_to(ROOT)).lower() for token in ("prorun", "pro-run"))
    ]

    assert stale == []
    assert stale_paths == []
    assert (ROOT / "src" / "pre_active").is_dir()
    assert (ROOT / "ops" / "windows" / "start-pre-active.ps1").is_file()
