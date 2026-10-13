import re
import unittest
from pathlib import Path

import cli

ROOT = Path(__file__).resolve().parents[1]
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_FENCE = re.compile(r"```.*?```", re.S)


class RepositoryHygieneTests(unittest.TestCase):
    def test_relative_links_in_markdown_resolve(self):
        broken = []
        for document in ROOT.rglob("*.md"):
            if {".git", ".claude", "node_modules"} & set(document.relative_to(ROOT).parts):
                continue
            for target in _LINK.findall(_FENCE.sub("", document.read_text(encoding="utf-8"))):
                path = target.split("#")[0]
                if path and not re.match(r"^[a-z]+:", path) and not (document.parent / path).exists():
                    broken.append(f"{document.relative_to(ROOT)} -> {target}")
        self.assertEqual(broken, [])

    def test_workflow_actions_are_pinned_and_keep_no_token(self):
        for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
            text = workflow.read_text(encoding="utf-8")
            uses = re.findall(r"uses:\s*(\S+)", text)
            with self.subTest(workflow=workflow.name):
                self.assertTrue(uses)
                self.assertEqual([u for u in uses if not re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", u)], [])
                self.assertEqual(text.count("actions/checkout@"), text.count("persist-credentials: false"))

    def test_data_copies_and_env_files_are_never_committed(self):
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        for pattern in (".env", ".env.*", "!.env.example", "data/", "/data-*/", "*.db"):
            self.assertIn(pattern, ignored)
        self.assertIn("data-*", (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())

    def test_the_cli_usage_lists_exactly_the_dispatched_commands(self):
        self.assertIsNotNone(cli.__doc__)  # the docstring must come before the __future__ import
        documented = set(re.findall(r"python cli\.py ([a-z0-9-]+)", cli.__doc__))
        dispatched = set(re.findall(r'cmd == "([a-z0-9-]+)"', (ROOT / "cli.py").read_text(encoding="utf-8")))
        self.assertEqual(documented, dispatched)


if __name__ == "__main__":
    unittest.main()
