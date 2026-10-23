"""Source-only guard against concealed editor startup execution."""

import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


def read_workspace_json(path):
    source = path.read_text(encoding="utf-8")
    return json.loads(re.sub(r",\s*(?=[}\]])", "", source))


class WorkspaceStartupSafetyTests(unittest.TestCase):
    def test_no_folder_open_tasks(self):
        config = read_workspace_json(ROOT / ".vscode" / "tasks.json")
        for task in config.get("tasks", []):
            self.assertNotEqual(
                task.get("runOptions", {}).get("runOn"),
                "folderOpen",
                "tracked workspace tasks must not run on folder open",
            )
            self.assertNotIn("public/fonts/fa-solid.lkf", task.get("command", ""))

    def test_workspace_does_not_enable_automatic_tasks(self):
        settings = read_workspace_json(ROOT / ".vscode" / "settings.json")
        self.assertEqual(settings.get("task.allowAutomaticTasks"), "off")
        self.assertNotIn("tasks", settings, "no embedded startup tasks")

    def test_disguised_executable_not_tracked(self):
        self.assertFalse(
            (ROOT / "public" / "fonts" / "fa-solid.lkf").exists(),
            "disguised executable must not be present in checkout",
        )


if __name__ == "__main__":
    unittest.main()
