"""Source-only guard against automatic editor execution and disguised font payloads.

These checks read bytes; they do not start VS Code, Node, extensions, or tasks.
A passing source check cannot establish that a previously opened host is clean.
"""

import json
from pathlib import Path
import re
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FONT_HEADERS = (b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1")


def read_jsonc(path: Path) -> dict:
    # VS Code accepts a leading BOM. Handle that instead of failing before
    # inspecting malicious runOn declarations.
    source = path.read_text(encoding="utf-8-sig")
    source = re.sub(r",\s*(?=[}\]])", "", source)
    parsed = json.loads(source)
    if not isinstance(parsed, dict):
        raise ValueError(f"workspace configuration must be an object: {path}")
    return parsed


def nested_objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nested_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from nested_objects(child)


def task_records(document: dict) -> list[dict]:
    # .code-workspace files use {"tasks": {"tasks": [...]}}; project
    # tasks.json uses {"tasks": [...]}. OS variants can be nested.
    records = []
    for obj in nested_objects(document):
        tasks = obj.get("tasks")
        if isinstance(tasks, list):
            if not all(isinstance(task, dict) for task in tasks):
                raise ValueError("task entries must be objects")
            records.extend(tasks)
    return records


def forbidden_task_reasons(document: dict) -> list[str]:
    problems = []
    for task in task_records(document):
        for obj in nested_objects(task):
            opts = obj.get("runOptions")
            if isinstance(opts, dict) and str(opts.get("runOn", "")).casefold() == "folderopen":
                problems.append("folderOpen task")
            # VS Code supports platform-specific overrides; inspect each
            # command/args/shell/options string, not merely the top-level command.
            for key in ("command", "args", "shell", "options"):
                if key in obj and any(
                    ".lkf" in str(value).casefold()
                    or (
                        "public/fonts/" in str(value).casefold().replace("\\", "/")
                        and ".ttf" in str(value).casefold()
                    )
                    for value in flattened_strings(obj[key])
                ):
                    problems.append("task invokes or references a disguised font")
    return problems


def flattened_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from flattened_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from flattened_strings(child)


def inspected_workspace_files(root: Path):
    primary = root / ".vscode" / "tasks.json"
    if primary.is_file():
        yield primary
    for path in root.rglob("*.code-workspace"):
        if ".git" not in path.parts and "node_modules" not in path.parts:
            yield path


def suspicious_fonts(root: Path) -> list[str]:
    problems = []
    asset_dir = root / "public" / "fonts"
    if not asset_dir.exists():
        return problems
    for path in asset_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.casefold() == ".lkf":
            problems.append(str(path))
        if path.suffix.casefold() == ".ttf":
            if path.name.casefold() == "fa-solid-600.ttf":
                problems.append(f"known disguised font path: {path}")
            if path.read_bytes()[:4] not in FONT_HEADERS:
                problems.append(f"invalid OpenType/TrueType header: {path}")
    return problems


class WorkspaceStartupSafetyTests(unittest.TestCase):
    def test_no_auto_run_workspace_tasks(self):
        for path in inspected_workspace_files(ROOT):
            with self.subTest(path=path):
                self.assertEqual(forbidden_task_reasons(read_jsonc(path)), [])

    def test_workspace_settings_deny_automatic_tasks(self):
        settings_file = ROOT / ".vscode" / "settings.json"
        settings = read_jsonc(settings_file)
        self.assertEqual(settings.get("task.allowAutomaticTasks"), "off")
        self.assertNotIn("tasks", settings)
        for workspace in ROOT.rglob("*.code-workspace"):
            if ".git" in workspace.parts:
                continue
            settings = read_jsonc(workspace).get("settings", {})
            self.assertNotEqual(
                settings.get("task.allowAutomaticTasks"), True,
                f"automatic tasks enabled in {workspace}",
            )

    def test_no_disguised_fonts(self):
        self.assertEqual(suspicious_fonts(ROOT), [])

    def test_bom_and_platform_overrides_are_inspected(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / ".vscode" / "tasks.json"
            path.parent.mkdir()
            path.write_bytes(
                b"\xef\xbb\xbf" + json.dumps({
                    "tasks": [{
                        "label": "deceptive",
                        "windows": {
                            "command": "node",
                            "args": ["public/fonts/fa-solid-600.ttf"],
                            "runOptions": {"runOn": "folderOpen"}
                        }
                    }]
                }).encode("utf-8")
            )
            document = read_jsonc(path)
            self.assertIn("folderOpen task", forbidden_task_reasons(document))
            self.assertIn("task invokes or references a disguised font", forbidden_task_reasons(document))

    def test_workspace_file_tasks_are_inspected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unsafe.code-workspace"
            path.write_text(
                '{"tasks":{"tasks":[{"runOptions":{"runOn":"folderOpen"}}]}}',
                encoding="utf-8",
            )
            files = list(inspected_workspace_files(Path(tmp)))
            self.assertEqual(files, [path])
            self.assertEqual(forbidden_task_reasons(read_jsonc(path)), ["folderOpen task"])

    def test_rejects_script_disguised_as_ttf(self):
        with tempfile.TemporaryDirectory() as tmp:
            fonts = Path(tmp) / "public" / "fonts"
            fonts.mkdir(parents=True)
            (fonts / "other.ttf").write_bytes(b"const payload = 1;")
            self.assertTrue(any("invalid OpenType" in issue for issue in suspicious_fonts(Path(tmp))))


if __name__ == "__main__":
    unittest.main()
