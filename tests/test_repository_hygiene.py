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

    def test_windows_scripts_are_ascii(self):
        # Windows PowerShell 5.1 reads a script without a BOM in the local code page.
        for script in (ROOT / "deploy" / "windows").glob("*.ps1"):
            with self.subTest(script=script.name):
                self.assertTrue(script.read_bytes().isascii())

    def test_the_documented_update_line_runs_the_update_script(self):
        line = r"cd ~\Server\infohub; git pull; .\update.cmd"
        script = (ROOT / "deploy" / "windows" / "update.ps1").read_text(encoding="ascii")
        self.assertIn(line, script)
        self.assertIn(r'-File "%~dp0deploy\windows\update.ps1"', (ROOT / "update.cmd").read_text(encoding="ascii"))
        for guide in ("OWNER_GUIDE.md", "WINDOWS_CUTOVER.md"):
            with self.subTest(guide=guide):
                self.assertIn(line, (ROOT / "docs" / guide).read_text(encoding="utf-8"))
        # The build must never start from a pull that did not reach GitHub main.
        self.assertLess(script.index("origin/main"), script.index("docker compose up"))
        # Nor on a C: too full for the new image and the copy before a schema upgrade (10-31).
        self.assertLess(script.index("(Free-GB) -lt $needGB"), script.index("docker compose up"))

    def test_the_documented_stay_awake_line_runs_the_script(self):
        line = r"cd ~\Server\infohub; git pull; .\stay-awake.cmd"
        script = (ROOT / "deploy" / "windows" / "stay-awake.ps1").read_text(encoding="ascii")
        self.assertIn(r'-File "%~dp0deploy\windows\stay-awake.ps1"', (ROOT / "stay-awake.cmd").read_text(encoding="ascii"))
        self.assertIn(line, script)
        self.assertIn(line, (ROOT / "docs" / "OWNER_GUIDE.md").read_text(encoding="utf-8"))
        for setting in ("standby-timeout-ac 0", "hibernate-timeout-ac 0", "LIDACTION 0"):
            self.assertIn(setting, script)  # and it checks the values afterwards
        self.assertLess(script.index("LIDACTION 0"), script.index('Show "after"'))

    def test_the_update_only_removes_unused_builds_after_the_new_version_runs(self):
        script = (ROOT / "deploy" / "windows" / "update.ps1").read_text(encoding="ascii")
        body = script[script.index("function Remove-OldBuilds"):script.index("Write-Host \"== 1/3 git pull\"")]
        self.assertIn("docker image prune -f", body)
        self.assertIn('docker builder prune -f --filter "until=168h"', body)
        for dangerous in ("--volumes", "system prune", "volume rm", "volume prune", "-a"):
            self.assertNotIn(dangerous, body)  # the data lives in a volume: never touch it
        # Only once the site and the worker report the new version.
        self.assertLess(script.index("$ready.worker.version -eq $head"), script.index("    Remove-OldBuilds"))

    def test_the_disk_report_only_reads(self):
        script = (ROOT / "deploy" / "windows" / "disk-report.ps1").read_text(encoding="ascii")
        self.assertIn(r'-File "%~dp0deploy\windows\disk-report.ps1"', (ROOT / "disk-report.cmd").read_text(encoding="ascii"))
        self.assertIn(r"cd ~\Server\infohub; git pull; .\disk-report.cmd", script)
        for change in ("Remove-Item", "prune", "Clear-", "Optimize-VHD", "compact vdisk", "powercfg", " rm ", "wsl --"):
            self.assertNotIn(change, script)

    def test_the_disk_compaction_never_touches_the_data_and_always_restarts(self):
        line = r"cd ~\Server\infohub; git pull; .\compact-docker-disk.cmd"
        script = (ROOT / "deploy" / "windows" / "compact-docker-disk.ps1").read_text(encoding="ascii")
        self.assertIn(r'-File "%~dp0deploy\windows\compact-docker-disk.ps1"', (ROOT / "compact-docker-disk.cmd").read_text(encoding="ascii"))
        self.assertIn(line, script)
        self.assertIn(line, (ROOT / "docs" / "OWNER_GUIDE.md").read_text(encoding="utf-8"))
        for dangerous in ("volume rm", "volume prune", "system prune", "image prune", "--volumes", "rmi ", "Remove-Item"):
            self.assertNotIn(dangerous, script)
        self.assertIn("attach vdisk readonly", script)
        self.assertEqual(script.count('"detach vdisk"'), 2)  # also after a failed compaction
        # InfoHub stops before Docker shuts down, and diskpart only touches a file nothing holds open.
        self.assertLess(script.index("docker stop -t 180"), script.index("wsl --shutdown"))
        self.assertLess(script.index('[IO.File]::Open($disk.FullName, "Open", "Read", "None")'), script.index("diskpart /s"))
        # Every way out after the stop starts InfoHub again.
        body = script[script.index("function Stop-Compact"):script.index("Write-Host \"== 1/5")]
        self.assertIn("Start-InfoHub", body)

    def test_the_documented_backfill_line_runs_every_step_and_restarts_the_worker(self):
        line = r"cd ~\Server\infohub; git pull; .\backfill.cmd"
        script = (ROOT / "deploy" / "windows" / "backfill.ps1").read_text(encoding="ascii")
        self.assertIn(r'-File "%~dp0deploy\windows\backfill.ps1"', (ROOT / "backfill.cmd").read_text(encoding="ascii"))
        # It runs this folder's code, so it first checks that the site already runs it.
        self.assertLess(script.index("/api/live"), script.index("docker compose stop worker"))
        self.assertIn(line, script)
        self.assertIn(line, (ROOT / "docs" / "WINDOWS_CUTOVER.md").read_text(encoding="utf-8"))
        guide = (ROOT / "docs" / "WINDOWS_CUTOVER.md").read_text(encoding="utf-8")
        step7 = guide[guide.index("## 7."):guide.index("## 7b.")]
        step8 = guide[guide.index("## 8."):guide.index("## 8b.")]
        audits = set(re.findall(r'"([a-z-]+)"', step8[:step8.index("```", step8.index("```") + 3)]))
        for command in sorted(set(re.findall(r"python cli\.py ([a-z-]+)", step7)) | audits):
            with self.subTest(command=command):
                self.assertIn(f'"{command}"', script)
        deferred = guide[guide.index("## 7b."):guide.index("## 8.")]  # D32: not run by the script
        for command in re.findall(r"python cli\.py ([a-z-]+)", deferred):
            with self.subTest(deferred=command):
                self.assertNotIn(f'"{command}"', script)
        self.assertIn("finally", script)  # the worker is started again after a failure too
        self.assertLess(script.index("finally"), script.index("docker compose start worker"))

    def test_the_signals_launcher_runs_its_script(self):
        self.assertIn(r'-File "%~dp0deploy\windows\signals.ps1"', (ROOT / "signals.cmd").read_text(encoding="ascii"))
        script = (ROOT / "deploy" / "windows" / "signals.ps1").read_text(encoding="ascii")
        self.assertIn("signals-key ai-research", script)
        self.assertNotIn("Write-Host $token", script)  # the key is never printed

    def test_windows_launchers_keep_crlf_line_endings(self):
        self.assertIn("*.cmd text eol=crlf", (ROOT / ".gitattributes").read_text(encoding="ascii"))
        for launcher in ROOT.glob("*.cmd"):
            with self.subTest(launcher=launcher.name):
                data = launcher.read_bytes()
                self.assertTrue(data.isascii())
                self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))

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
