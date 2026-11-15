import asyncio
import base64
import contextlib
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import report
import sweep


def note(level="high", title="Refunds include the fee", location="r1/app.py:2"):
    return f"""Status: COMPLETE
Coverage: Checked the order handler and its caller.
### {level} | {title}
Locations: {location}
Trigger: Refund 1000 cents with a 100 cent fee.
Expected: Return 900 cents under the documented net-fee contract.
Actual: The handler returns 1100 cents.
Impact: Customers receive more than the refundable amount.
Evidence: The handler adds the fee and passes the sum to the payment adapter.
Fix: Subtract the fee in refund_cents.
Test: Verify that refund_cents(1000, 100) returns 900.
"""


def git(repo, *args):
    return (
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Example",
                "-c",
                "user.email=example@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "repositories"
        self.repo = self.root / "apps" / "order service"
        self.repo.mkdir(parents=True)
        git(self.repo, "init", "-b", "main")
        (self.repo / "app.py").write_text("# Net refund in cents\nrefund = paid + fee\n")
        self.commit()
        self.store = self.base / "runs"
        self.store.mkdir()

    def commit(self):
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "Example revision")
        return git(self.repo, "rev-parse", "HEAD")

    def run_sweep(self, cadence, answer, merge_command=None):
        with patch.object(sweep, "run_agent", answer), contextlib.redirect_stdout(io.StringIO()) as out:
            result = asyncio.run(sweep.sweep(self.root, cadence, self.store, ["mock"], merge_command))
        return result, Path(out.getvalue().strip())

    def copy_prompt_files(self):
        project = self.base / "project"
        shutil.copytree(sweep.PROJECT_DIR / "prompts", project / "prompts")
        shutil.copyfile(sweep.PROJECT_DIR / "prompts.md", project / "prompts.md")
        return project

    def assert_schedule_runs(self, cadence, check_name, expected_scope):
        arguments = ["sweep.py", str(self.root), cadence, "--list"]
        preview = io.StringIO()
        unexpected_agent = patch.object(
            sweep, "agent_command", side_effect=AssertionError("preview started an agent")
        )
        with patch.object(sys, "argv", arguments), contextlib.redirect_stdout(preview), unexpected_agent:
            self.assertEqual(sweep.main(), 0)
        output = preview.getvalue()
        self.assertIn(f"1. {check_name.title()} —", output)
        self.assertNotIn("2. ", output)
        self.assertIn(expected_scope, output)
        self.assertIn("Consolidate —", output)

        calls = []

        async def agent(command, workspace, instructions):
            if (workspace / "candidates.json").is_file():
                calls.append("consolidate")
                return "<main></main>"
            self.assertIn(expected_scope, instructions)
            self.assertIn("Focus on real, meaningful findings", instructions)
            calls.append(instructions.split("Check: ")[1].splitlines()[0])
            return "Status: COMPLETE\nCoverage: Checked the fixture repository."

        exit_code, report_path = self.run_sweep(cadence, agent)
        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [check_name, "consolidate"])
        notes = (report_path.parent / "findings").glob("*.md")
        self.assertEqual([path.stem for path in notes], [check_name])

    def test_preview_and_execution_read_the_selected_schedule(self):
        project = self.copy_prompt_files()
        # Different lists make it possible to catch a runner that always launches every check.
        for cadence, title in (("daily", "Security"), ("weekly", "Performance")):
            path = project / "prompts" / f"{cadence}.md"
            lines = path.read_text().splitlines()
            keep = [line for line in lines if not line.startswith("- [") or line.startswith(f"- [{title} —")]
            path.write_text("\n".join(keep) + "\n")

        with patch.object(sweep, "PROJECT_DIR", project):
            self.assert_schedule_runs("daily", "security", "Review committed changes")
            self.assert_schedule_runs("weekly", "performance", "Review the current commit")

    def test_schedule_rejects_broken_links_and_duplicates(self):
        project = self.copy_prompt_files()
        daily = project / "prompts" / "daily.md"
        original = daily.read_text()
        entry = next(line for line in original.splitlines() if line.startswith("- [Security"))
        cases = [
            (entry + "\n" + entry, "Repeated check"),
            (entry.replace("#security", "#typo"), "mismatched link"),
        ]
        with patch.object(sweep, "PROJECT_DIR", project):
            for replacement, error in cases:
                daily.write_text(original.replace(entry, replacement))
                with self.assertRaisesRegex(ValueError, error):
                    sweep.load_schedule("daily")

    def test_headings_in_prompt_examples_stay_in_the_prompt(self):
        path = self.base / "catalog.md"
        example = "````markdown\n## Output\n```\n````\nKeep reviewing."
        path.write_text("# Catalog\n\n## Review\n" + example + "\n\n## Next\nContinue.")
        self.assertEqual(sweep.markdown_sections(path), {"Review": example, "Next": "Continue."})

    def test_concurrent_reviews_then_one_merge(self):
        active = 0
        peak = 0
        calls = []
        checks = {check.name for check in sweep.load_schedule("weekly").checks}

        async def agent(command, workspace, prompt):
            nonlocal active, peak
            if (workspace / "candidates.json").exists():
                self.assertEqual(command, ["editor"])
                self.assertEqual(set(calls), checks)
                self.assertEqual(len(calls), len(checks))
                self.assertEqual(active, 0)
                self.assertEqual(len(list((workspace / "findings").glob("*.md"))), len(checks))
                self.assertTrue((workspace / "r1" / "app.py").is_file())
                return (
                    '<main><article data-refs="test-health-1"><h2>Gate accepts an invalid refund</h2>'
                    "<p>The assertion reads a constant.</p></article>"
                    '<article data-refs="correctness-1 user-journeys-1"><h2>Refunds include the fee</h2>'
                    "<p>A refund of 1000 cents with a 100 cent fee returns 1100 cents.</p></article></main>"
                )
            name = prompt.split("Check: ")[1].splitlines()[0]
            self.assertEqual(command, ["mock"])
            calls.append(name)
            active += 1
            peak = max(active, peak)
            await asyncio.sleep(0.015)
            active -= 1
            if name == "test-health":
                return note("medium", "Gate accepts an invalid refund")
            if name in {"correctness", "user-journeys"}:
                return note()
            return "Status: SKIPPED\nCoverage: No matching component."

        exit_code, path = self.run_sweep("weekly", agent, ["editor"])
        self.assertEqual(exit_code, 0)
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, sweep.MAX_PARALLEL_CHECKS)
        html = path.read_text()
        self.assertEqual(html.count('<article class="finding">'), 2)
        self.assertLess(html.index("<h2>Refunds include"), html.index("<h2>Gate accepts"))
        self.assertIn(git(self.repo, "rev-parse", "HEAD"), html)
        self.assertIn("apps/order service", html)
        self.assertIn("Verify that refund_cents", html)
        self.assertFalse((self.store / "checkpoints.json").exists())

    def test_daily_checkpoint_and_failures(self):
        patches = []

        async def clean(command, workspace, prompt):
            if (workspace / "candidates.json").exists():
                return "<main></main>"
            patches.append((workspace / "changes" / "r1.patch").read_text())
            return "Status: COMPLETE\nCoverage: Checked all changed files."

        self.assertEqual(self.run_sweep("daily", clean)[0], 0)
        self.assertIn("refund = paid + fee", patches[0])
        patches.clear()
        self.assertEqual(self.run_sweep("daily", clean)[0], 0)
        self.assertEqual(set(patches), {""})
        previous = (self.store / "checkpoints.json").read_bytes()
        (self.repo / "app.py").write_text("net = paid - fee\n")
        self.commit()

        async def broken(command, workspace, prompt):
            if "Check: correctness\n" in prompt:
                return "malformed"
            return await clean(command, workspace, prompt)

        exit_code, path = self.run_sweep("daily", broken)
        self.assertEqual(exit_code, 2)
        self.assertIn("Incomplete review", path.read_text())
        self.assertEqual(previous, (self.store / "checkpoints.json").read_bytes())
        self.assertEqual(self.run_sweep("daily", clean)[0], 0)
        self.assertNotEqual(previous, (self.store / "checkpoints.json").read_bytes())
        self.assertIn("-refund = paid + fee", patches[-1])

    def test_failed_first_daily_run_retries_the_original_changes(self):
        first_base = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "app.py").write_text("refund = paid - fee\n")
        current_commit = self.commit()
        cutoff_commit = first_base
        actual_git_output = sweep.git_output
        seen_patches = []

        def git_at_cutoff(repository, *arguments, **options):
            if "--before=24 hours ago" in arguments:
                return (cutoff_commit + "\n").encode()
            return actual_git_output(repository, *arguments, **options)

        async def reviewer(command, workspace, instructions):
            if (workspace / "candidates.json").exists():
                return "<main></main>"
            seen_patches.append((workspace / "changes" / "r1.patch").read_text())
            if first_attempt and "Check: correctness\n" in instructions:
                return "malformed note"
            return "Status: COMPLETE\nCoverage: Reviewed the committed changes."

        with patch.object(sweep, "git_output", git_at_cutoff):
            first_attempt = True
            self.assertEqual(self.run_sweep("daily", reviewer)[0], 2)
            initial_patch = seen_patches[0]
            self.assertIn("+refund = paid - fee", initial_patch)

            # By the retry, Git's moving 24-hour cutoff would select HEAD and yield an empty diff.
            first_attempt = False
            cutoff_commit = current_commit
            seen_patches.clear()
            self.assertEqual(self.run_sweep("daily", reviewer)[0], 0)
            self.assertEqual(set(seen_patches), {initial_patch})

    def test_snapshot_ignores_uncommitted_symlink_and_large_content(self):
        large = self.repo / "large.txt"
        large.write_text("EXCLUDED_MARK" * 100)
        (self.repo / "broken-link").symlink_to(self.base / "private")
        self.commit()
        (self.repo / "app.py").write_text("DIRTY")
        (self.repo / "local.txt").write_text("UNTRACKED")
        linked = self.root / "linked worktree"
        git(self.repo, "worktree", "add", "--detach", str(linked), "HEAD")
        with tempfile.TemporaryDirectory() as directory, patch.object(sweep, "MAX_FILE_BYTES", 64):
            workspace = Path(directory)
            index, problems = sweep.snapshot(self.root, workspace, "daily", {})
            self.assertFalse(problems)
            self.assertEqual(len(index), 2)
            self.assertEqual(index["r1"]["omitted_entries"], 2)
            for key in index:
                patch_text = (workspace / "changes" / (key + ".patch")).read_text()
                self.assertNotIn("EXCLUDED_MARK", patch_text)
                self.assertFalse((workspace / key / "broken-link").exists())
                self.assertFalse((workspace / key / "local.txt").exists())
                self.assertNotIn("DIRTY", (workspace / key / "app.py").read_text())

    def test_notes_reject_bad_lines_preserve_good_findings_and_escape_html(self):
        workspace = self.base / "source"
        (workspace / "r1").mkdir(parents=True)
        (workspace / "r1" / "app.py").write_text("one\ntwo\n")
        index = {
            "r1": {"repository": "a<b", "commit": "f" * 40, "range": "whole revision", "omitted_entries": 0}
        }
        bad_block = note(location="r1/../r1/app.py:999").split("###", 1)[1]
        raw = note(title="<img onerror=alert(1)>") + "###" + bad_block
        result = report.read_note("correctness", raw, index, workspace)
        self.assertEqual(result.status, "PARTIAL")
        self.assertEqual(len(result.findings), 1)
        finding = result.findings[0]
        findings = {finding["id"]: finding}
        html = report.render(
            "weekly", "20260101", index, [result], [], report.fallback_groups(findings), [], findings
        )
        self.assertNotIn("<img onerror", html)
        self.assertIn("&lt;img", html)
        for fragment in (
            "<main/>",
            "<main><script/></main>",
            '<main><article data-refs="correctness-1"><h2 onclick="x()">t</h2><p>s</p></article></main>',
            '<main><aside data-refs="correctness-1 correctness-1">x</aside></main>',
        ):
            with self.assertRaises(ValueError):
                report.assemble(fragment, findings)
        groups, exclusions = report.assemble(
            '<main><aside data-refs="correctness-1">Input rejected by guard.</aside></main>', findings
        )
        self.assertFalse(groups)
        self.assertEqual(len(exclusions), 1)

    def test_empty_checkout_and_executable_change(self):
        empty = self.root / "empty"
        empty.mkdir()
        git(empty, "init", "-b", "main")
        previous = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-index", "--chmod=+x", "app.py")
        git(self.repo, "commit", "-qm", "Make the entry point executable")
        state = {"apps/order service": {"main": previous}}
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            index, problems = sweep.snapshot(self.root, workspace, "daily", state)
            self.assertFalse(problems)
            self.assertFalse(index["r2"]["changed"])
            self.assertEqual(index["r2"]["commit"], "")
            self.assertIn("new mode 100755", (workspace / "changes" / "r1.patch").read_text())

    def test_browser_policy_allows_only_the_bundled_script(self):
        (body,) = re.findall(r"<script>(.*?)</script>", report.TEMPLATE, re.DOTALL)
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        self.assertIn(f"script-src 'sha256-{digest}'", report.TEMPLATE)
        self.assertNotIn("<script src=", report.TEMPLATE)

    def test_invalid_consolidation_keeps_findings_and_checkpoint(self):
        async def bad_merge(command, workspace, prompt):
            if (workspace / "candidates.json").exists():
                return "<main><script>alert(1)</script></main>"
            if "Check: correctness\n" in prompt:
                return note()
            return "Status: SKIPPED\nCoverage: No corresponding subsystem."

        exit_code, path = self.run_sweep("daily", bad_merge)
        self.assertEqual(exit_code, 2)
        self.assertIn("Consolidation failed", path.read_text())
        self.assertIn("Refunds include the fee", path.read_text())
        inventory = json.loads((path.parent / "inventory.json").read_text())["r1"]
        checkpoints = json.loads((self.store / "checkpoints.json").read_text())
        self.assertEqual(checkpoints[inventory["repository"]]["main"], inventory["base"])
        self.assertNotEqual(inventory["base"], inventory["commit"])


if __name__ == "__main__":
    unittest.main()
