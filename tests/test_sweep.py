import asyncio
import base64
import contextlib
import hashlib
import io
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import report
import sweep


def note(level="high", title="Refunds include the fee", loc="r1/app.py:2"):
    return f"""Status: COMPLETE
Coverage: Checked the order handler and its caller.
### {level} | {title}
Locations: {loc}
Trigger: Refund 1000 cents with a 100 cent fee.
Expected: Return 900 cents under the documented net-fee contract.
Actual: The handler returns 1100 cents.
Impact: Customers receive more than the refundable amount.
Evidence: The handler adds the fee and passes the sum to the payment adapter.
Fix: Subtract the fee in refund_cents.
Test: Verify that refund_cents(1000, 100) returns 900.
"""


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Example",
                           "-c", "user.email=example@example.invalid", "-c", "commit.gpgsign=false",
                           *args], check=True, capture_output=True).stdout.decode().strip()


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

    def run_sweep(self, cadence, answer, merge_cmd=None):
        with patch.object(sweep, "ask", answer), contextlib.redirect_stdout(io.StringIO()) as out:
            result = asyncio.run(sweep.sweep(self.root, cadence, self.store, ["mock"], merge_cmd))
        return result, Path(out.getvalue().strip())

    def test_concurrent_reviews_then_one_merge(self):
        active, peak, calls = 0, 0, []
        checks = set(sweep.read_prompts()) - {"policy", "consolidate"}
        async def agent(cmd, work, prompt):
            nonlocal active, peak
            if (work / "candidates.json").exists():
                self.assertEqual(cmd, ["editor"])
                self.assertEqual(set(calls), checks)
                self.assertEqual(len(calls), len(checks))
                self.assertEqual(active, 0)
                self.assertEqual(len(list((work / "findings").glob("*.md"))), len(checks))
                self.assertTrue((work / "r1" / "app.py").is_file())
                return ('<main><article data-refs="test-health-1"><h2>Gate accepts an invalid refund</h2>'
                        '<p>The assertion reads a constant.</p></article>'
                        '<article data-refs="correctness-1 user-journeys-1"><h2>Refunds include the fee</h2>'
                        '<p>A refund of 1000 cents with a 100 cent fee returns 1100 cents.</p></article></main>')
            name = prompt.split("Check: ")[1].splitlines()[0]
            self.assertEqual(cmd, ["mock"])
            calls.append(name)
            active += 1
            peak = max(active, peak)
            await asyncio.sleep(.015)
            active -= 1
            if name == "test-health":
                return note("medium", "Gate accepts an invalid refund")
            if name in {"correctness", "user-journeys"}:
                return note()
            return "Status: SKIPPED\nCoverage: No matching component."
        rc, path = self.run_sweep("weekly", agent, ["editor"])
        self.assertEqual(rc, 0)
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, sweep.PARALLEL)
        html = path.read_text()
        self.assertEqual(html.count('<article class="finding">'), 2)
        self.assertLess(html.index('<h2>Refunds include'), html.index('<h2>Gate accepts'))
        self.assertIn(git(self.repo, "rev-parse", "HEAD"), html)
        self.assertIn("apps/order service", html)
        self.assertIn("Verify that refund_cents", html)
        self.assertFalse((self.store / "checkpoints.json").exists())

    def test_daily_checkpoint_and_failures(self):
        patches = []
        async def clean(cmd, work, prompt):
            if (work / "candidates.json").exists():
                return "<main></main>"
            patches.append((work / "changes" / "r1.patch").read_text())
            return "Status: COMPLETE\nCoverage: Checked all changed files."
        self.assertEqual(self.run_sweep("daily", clean)[0], 0)
        self.assertIn("refund = paid + fee", patches[0])
        patches.clear()
        self.assertEqual(self.run_sweep("daily", clean)[0], 0)
        self.assertEqual(set(patches), {""})
        old = (self.store / "checkpoints.json").read_bytes()
        (self.repo / "app.py").write_text("net = paid - fee\n")
        self.commit()
        async def broken(cmd, work, prompt):
            return "malformed" if "Check: correctness\n" in prompt else await clean(cmd, work, prompt)
        rc, path = self.run_sweep("daily", broken)
        self.assertEqual(rc, 2)
        self.assertIn("Incomplete review", path.read_text())
        self.assertEqual(old, (self.store / "checkpoints.json").read_bytes())
        self.assertEqual(self.run_sweep("daily", clean)[0], 0)
        self.assertNotEqual(old, (self.store / "checkpoints.json").read_bytes())
        self.assertIn("-refund = paid + fee", patches[-1])

    def test_snapshot_ignores_uncommitted_symlink_and_large_content(self):
        large = self.repo / "large.txt"
        large.write_text("EXCLUDED_MARK" * 100)
        (self.repo / "broken-link").symlink_to(self.base / "private")
        self.commit()
        (self.repo / "app.py").write_text("DIRTY")
        (self.repo / "local.txt").write_text("UNTRACKED")
        linked = self.root / "linked worktree"
        git(self.repo, "worktree", "add", "--detach", str(linked), "HEAD")
        with tempfile.TemporaryDirectory() as directory, patch.object(sweep, "BLOB_LIMIT", 64):
            work = Path(directory)
            index, problems, _ = sweep.snapshot(self.root, work, "daily", {})
            self.assertFalse(problems)
            self.assertEqual(len(index), 2)
            self.assertEqual(index["r1"]["omitted_entries"], 2)
            for key in index:
                patch_text = (work / "changes" / (key + ".patch")).read_text()
                self.assertNotIn("EXCLUDED_MARK", patch_text)
                self.assertFalse((work / key / "broken-link").exists())
                self.assertFalse((work / key / "local.txt").exists())
                self.assertNotIn("DIRTY", (work / key / "app.py").read_text())

    def test_notes_reject_bad_lines_preserve_good_findings_and_escape_html(self):
        work = self.base / "source"
        (work / "r1").mkdir(parents=True)
        (work / "r1" / "app.py").write_text("one\ntwo\n")
        index = {"r1": {"repository": "a<b", "commit": "f"*40, "range": "whole revision", "omitted_entries": 0}}
        bad_block = note(loc="r1/../r1/app.py:999").split("###", 1)[1]
        raw = note(title="<img onerror=alert(1)>") + "###" + bad_block
        result = report.read_note("correctness", raw, index, work)
        self.assertEqual(result[1], "PARTIAL")
        self.assertEqual(len(result[3]), 1)
        f = result[3][0]
        findings = {f["id"]: f}
        html = report.render("weekly", "20260101", index, [result], [], report.fallback_groups(findings), [], findings)
        self.assertNotIn("<img onerror", html)
        self.assertIn("&lt;img", html)
        for fragment in ('<main/>', '<main><script/></main>',
                         '<main><article data-refs="correctness-1"><h2 onclick="x()">t</h2><p>s</p></article></main>',
                         '<main><aside data-refs="correctness-1 correctness-1">x</aside></main>'):
            with self.assertRaises(ValueError):
                report.assemble(fragment, findings)
        groups, exclusions = report.assemble('<main><aside data-refs="correctness-1">Input rejected by guard.</aside></main>', findings)
        self.assertFalse(groups)
        self.assertEqual(len(exclusions), 1)

    def test_empty_checkout_and_executable_change(self):
        empty = self.root / "empty"
        empty.mkdir()
        git(empty, "init", "-b", "main")
        old = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-index", "--chmod=+x", "app.py")
        git(self.repo, "commit", "-qm", "Make the entry point executable")
        state = {"apps/order service": {"main": old}}
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            index, problems, _ = sweep.snapshot(self.root, work, "daily", state)
            self.assertFalse(problems)
            self.assertFalse(index["r2"]["changed"])
            self.assertEqual(index["r2"]["commit"], "")
            self.assertIn("new mode 100755", (work / "changes" / "r1.patch").read_text())

    def test_browser_policy_allows_only_the_bundled_script(self):
        body, = re.findall(r"<script>(.*?)</script>", report.TEMPLATE, re.S)
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        self.assertIn(f"script-src 'sha256-{digest}'", report.TEMPLATE)
        self.assertNotIn("<script src=", report.TEMPLATE)

    def test_invalid_consolidation_keeps_findings_and_checkpoint(self):
        async def bad_merge(cmd, work, prompt):
            if (work / "candidates.json").exists():
                return '<main><script>alert(1)</script></main>'
            if "Check: correctness\n" in prompt:
                return note()
            return "Status: SKIPPED\nCoverage: No corresponding subsystem."
        rc, path = self.run_sweep("daily", bad_merge)
        self.assertEqual(rc, 2)
        self.assertIn("Consolidation failed", path.read_text())
        self.assertIn("Refunds include the fee", path.read_text())
        self.assertFalse((self.store / "checkpoints.json").exists())


if __name__ == "__main__":
    unittest.main()
