from __future__ import annotations

from pathlib import Path
import re


_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")


def base_model_revision(model_path: str | Path) -> str | None:
    parts = Path(model_path).parts
    for index, part in enumerate(parts[:-1]):
        if part.lower() != "snapshots":
            continue
        candidate = parts[index + 1]
        if _REVISION_RE.fullmatch(candidate):
            return candidate
    return None


def model_provenance(model_path: str | Path) -> dict[str, object] | None:
    revision = base_model_revision(model_path)
    if revision is None:
        return None
    return {
        "base_model_revision": revision,
        "adapter_active": False,
        "adapter_model_sha256": None,
    }
