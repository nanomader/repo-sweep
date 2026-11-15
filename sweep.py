#!/usr/bin/env python3
"""Run the scheduled checks across Git checkouts, save their notes and print the local HTML report path."""

import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import report

PROJECT_DIR = Path(__file__).resolve().parent
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_PARALLEL_CHECKS = 3
AGENT_TIMEOUT_SECONDS = 20 * 60

# Read each checkout's own objects. A partial clone must not fetch missing objects during a scan.
GIT_ENV = os.environ.copy()
for variable in (
    "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
):
    GIT_ENV.pop(variable, None)
GIT_ENV.update(GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL="", GIT_TERMINAL_PROMPT="0")


@dataclass
class Prompt:
    name: str
    title: str
    text: str


@dataclass
class Schedule:
    cadence: str
    scope: str
    policy: Prompt
    checks: list[Prompt]
    consolidation: Prompt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repositories", type=Path, help="a Git checkout or a folder containing checkouts")
    parser.add_argument("cadence", choices=("daily", "weekly"))
    parser.add_argument("--list", action="store_true", help="print the selected prompts and exit before starting agents")
    args = parser.parse_args()

    try:
        if args.list:
            print(describe_schedule(load_schedule(args.cadence)))
            return 0

        root = args.repositories.expanduser().resolve()
        if not root.is_dir():
            parser.error("repositories must be a directory")
        os.umask(0o077)
        root_id = hashlib.sha256(os.fsencode(root)).hexdigest()[:16]
        state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        store = state_home / "repo-sweep" / root_id
        engine = os.environ.get("SWEEP_AGENT", "codex")
        review_command = agent_command(engine, os.environ.get("SWEEP_MODEL"))
        merge_engine = os.environ.get("SWEEP_MERGE_AGENT") or engine
        merge_command = review_command if merge_engine == engine else agent_command(merge_engine)

        store.mkdir(parents=True, exist_ok=True)
        with (store / "run.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("Another sweep is running for this repository folder.", file=sys.stderr)
                return 1
            return asyncio.run(sweep(root, args.cadence, store, review_command, merge_command))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 1


async def sweep(root, cadence, store, review_command, merge_command=None):
    schedule = load_schedule(cadence)
    checkpoint_file = store / "checkpoints.json"
    checkpoints = {}
    if checkpoint_file.exists():
        checkpoints = json.loads(checkpoint_file.read_text(encoding="utf-8"))

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = store / f"{timestamp}-{cadence}"
    notes_dir = run_dir / "findings"
    notes_dir.mkdir(parents=True)
    print(describe_schedule(schedule), file=sys.stderr, flush=True)

    with tempfile.TemporaryDirectory(prefix="repo-sweep-") as directory:
        workspace = Path(directory)
        inventory, problems = snapshot(root, workspace, cadence, checkpoints)
        shutil.copyfile(workspace / "inventory.json", run_dir / "inventory.json")
        if cadence == "daily":
            save_checkpoints(checkpoint_file, checkpoints, inventory, review_complete=False)
        print(f"Git checkouts in scope: {len(inventory)}. Output: {run_dir}", file=sys.stderr, flush=True)

        reviews = await run_checks(schedule, review_command, workspace, notes_dir, inventory)
        findings = {}
        for review in reviews:
            for finding in review.findings:
                findings[finding["id"]] = finding

        try:
            groups, excluded = await consolidate_notes(
                merge_command or review_command, schedule.consolidation.text, workspace, notes_dir, findings,
            )
        except (OSError, ValueError, asyncio.TimeoutError) as error:
            reason = str(error) or "timeout"
            problems.append(f"Consolidation failed ({reason}). Showing individual reviewer notes. Duplicates may remain.")
            groups, excluded = report.fallback_groups(findings), []

    html = report.render(cadence, timestamp[:8], inventory, reviews, problems, groups, excluded, findings)
    report_path = run_dir / "report.html"
    report_path.write_text(html, encoding="utf-8")
    print(report_path, flush=True)

    complete = not problems and all(review.status != "PARTIAL" for review in reviews)
    if cadence == "daily" and complete:
        save_checkpoints(checkpoint_file, checkpoints, inventory, review_complete=True)
    return 0 if complete else 2


def load_schedule(cadence):
    catalog = {}
    check_links = {}
    for title, body in markdown_sections(PROJECT_DIR / "prompts.md").items():
        name = title.partition(" — ")[0].lower().replace(" ", "-")
        if not re.fullmatch(r"[a-z]+(?:-[a-z]+)*", name) or name in catalog:
            raise ValueError(f"Invalid or duplicate prompt name '{name}' in prompts.md")
        instructions = [line for line in body.splitlines() if not line.startswith("Source: ")]
        prompt = Prompt(name, title, "\n".join(instructions).strip())
        catalog[name] = prompt
        if name not in {"policy", "consolidate"}:
            # Match the whole Markdown link so the visible title always names the check we run.
            anchor = re.sub(r"[^\w -]", "", title.lower()).replace(" ", "-")
            check_links[f"- [{title}](../prompts.md#{anchor})"] = prompt

    schedule_path = PROJECT_DIR / "prompts" / f"{cadence}.md"
    sections = markdown_sections(schedule_path)
    if not sections.get("Scope"):
        raise ValueError(f"Describe the review scope under Scope in {schedule_path.name}")
    if not sections.get("Checks"):
        raise ValueError(f"List at least one prompt under Checks in {schedule_path.name}")
    if "policy" not in catalog or "consolidate" not in catalog:
        raise ValueError("prompts.md needs a policy and a consolidation prompt")

    selected = {}
    for entry in sections["Checks"].splitlines():
        if not entry.strip():
            continue
        prompt = check_links.get(entry.strip())
        if prompt is None:
            raise ValueError(f"Unknown check or mismatched link in {schedule_path.name}: {entry}")
        if prompt.name in selected:
            raise ValueError(f"Repeated check in {schedule_path.name}: {prompt.name}")
        selected[prompt.name] = prompt

    return Schedule(
        cadence=cadence, scope=sections["Scope"], policy=catalog["policy"],
        checks=list(selected.values()), consolidation=catalog["consolidate"],
    )


def markdown_sections(path):
    """Read level-two headings, preserving headings quoted inside fenced prompt examples."""
    sections = {}
    body = None
    open_fence = None
    for line in path.read_text(encoding="utf-8").splitlines():
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            marker, rest = fence.groups()
            if open_fence is None:
                open_fence = marker
            elif marker.startswith(open_fence) and not rest.strip():
                open_fence = None
        elif open_fence is None and line.startswith("## "):
            title = line.removeprefix("## ").strip()
            if title in sections:
                raise ValueError(f"Duplicate heading '{title}' in {path.name}")
            body = []
            sections[title] = body
            continue
        if body is not None:
            body.append(line)
    return {title: "\n".join(lines).strip() for title, lines in sections.items()}


def describe_schedule(schedule):
    check_label = "check" if len(schedule.checks) == 1 else "checks"
    lines = [
        f"{schedule.cadence.title()} review (prompts/{schedule.cadence}.md)",
        schedule.scope,
        "Shared instructions for every check",
        f"  {schedule.policy.title}",
        f"{len(schedule.checks)} {check_label} (up to {MAX_PARALLEL_CHECKS} at a time)",
    ]
    for position, prompt in enumerate(schedule.checks, start=1):
        lines.append(f"  {position}. {prompt.title}")
    lines.extend(["Final prompt (once, after all checks)", f"  {schedule.consolidation.title}"])
    return "\n".join(lines)


async def run_checks(schedule, command, workspace, notes_dir, inventory):
    common_instructions = [
        schedule.policy.text,
        f"Cadence: {schedule.cadence}. {schedule.scope}",
        "Read inventory.json first. It maps snapshot IDs to repositories and commits.",
    ]
    if schedule.cadence == "daily" and not any(repo["changed"] for repo in inventory.values()):
        common_instructions.append("There are no changes in any available snapshot. Return SKIPPED with that reason.")

    available_slots = asyncio.Semaphore(MAX_PARALLEL_CHECKS)

    async def review(check):
        note_path = notes_dir / f"{check.name}.md"
        instructions = "\n\n".join([*common_instructions, f"Check: {check.name}\n{check.text}"])
        async with available_slots:
            print(f"Reviewing {check.name}", file=sys.stderr, flush=True)
            try:
                note = await run_agent(command, workspace, instructions)
                note_path.write_text(note + "\n", encoding="utf-8")
                return report.read_note(check.name, note, inventory, workspace)
            except (OSError, ValueError, asyncio.TimeoutError) as error:
                reason = str(error) or "Agent timed out"
                if not note_path.exists():
                    note_path.write_text(f"Status: PARTIAL\nCoverage: {reason}\n", encoding="utf-8")
                return report.ReviewResult(check.name, "PARTIAL", reason, [])

    return await asyncio.gather(*(review(check) for check in schedule.checks))


async def consolidate_notes(command, instructions, workspace, notes_dir, findings):
    shutil.copytree(notes_dir, workspace / "findings")
    candidates = []
    for finding in findings.values():
        candidates.append({
            "id": finding["id"],
            "title": finding["title"],
            "severity": finding["severity"],
            "locations": finding["locations"],
            "evidence": finding["Evidence"],
        })
    (workspace / "candidates.json").write_text(json.dumps(candidates, indent=2), encoding="utf-8")
    response = await run_agent(command, workspace, instructions)
    return report.assemble(response, findings)


def save_checkpoints(path, checkpoints, inventory, *, review_complete):
    """Freeze a new branch's initial cutoff before reviewers start. Advance to HEAD only after success."""
    for repository in inventory.values():
        if not repository["commit"]:
            continue
        branches = checkpoints.setdefault(repository["repository"], {})
        branch = repository["branch"]
        if review_complete:
            branches[branch] = repository["commit"]
        else:
            branches.setdefault(branch, repository["base"])
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(checkpoints, indent=2), encoding="utf-8")
    temporary.replace(path)


def snapshot(root, workspace, cadence, checkpoints):
    repositories = find_repositories(root)
    if not repositories:
        raise ValueError("No Git checkouts found in the supplied directory")
    inventory = {}
    problems = []
    changes = workspace / "changes"
    changes.mkdir()
    relative_to = root.parent if root in repositories else root

    for position, repository in enumerate(repositories, start=1):
        snapshot_id = f"r{position}"
        name = repository.relative_to(relative_to).as_posix()
        destination = workspace / snapshot_id
        patch_file = changes / f"{snapshot_id}.patch"
        try:
            inventory[snapshot_id] = snapshot_repository(
                repository, name, destination, patch_file, cadence, checkpoints.get(name, {}),
            )
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            shutil.rmtree(destination, ignore_errors=True)
            patch_file.unlink(missing_ok=True)
            problems.append(f"{name}: snapshot unavailable ({type(error).__name__}).")

    (workspace / "inventory.json").write_text(json.dumps(inventory, indent=2), encoding="utf-8")
    copy_local_evidence(root, workspace)
    return inventory, problems


def find_repositories(root):
    repositories = []
    ignored = {".git", "node_modules", ".venv", ".repo-sweep-evidence"}

    def raise_walk_error(error):
        raise error

    for directory, subdirectories, files in os.walk(root, onerror=raise_walk_error):
        path = Path(directory)
        if ".git" in subdirectories or ".git" in files:
            repositories.append(path)
        subdirectories[:] = [
            name for name in subdirectories if name not in ignored and not (path / name).is_symlink()
        ]
    return sorted(repositories)


def snapshot_repository(repository, name, destination, patch_file, cadence, checkpoints):
    try:
        head = git_output(repository, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    except subprocess.CalledProcessError:
        # An empty repository has a symbolic HEAD but no refs or committed files.
        branch = git_output(repository, "symbolic-ref", "--short", "HEAD").decode().strip()
        if git_output(repository, "for-each-ref", "--count=1").strip():
            raise
        destination.mkdir()
        return {
            "repository": name, "commit": "", "branch": branch, "base": None,
            "range": "no commits on this checkout", "changed": False, "omitted_entries": 0,
        }

    branch = git_output(repository, "rev-parse", "--abbrev-ref", "HEAD").decode().strip()
    omitted = export_snapshot(repository, head, destination)
    if cadence == "daily":
        base, scope = daily_baseline(repository, branch, head, checkpoints)
        changed = write_daily_patch(repository, base, head, destination, patch_file)
    else:
        base = None
        scope = "whole revision"
        changed = True
    return {
        "repository": name, "commit": head, "branch": branch, "base": base,
        "range": scope, "changed": changed, "omitted_entries": omitted,
    }


def daily_baseline(repository, branch, head, checkpoints):
    empty_tree = git_output(repository, "hash-object", "-t", "tree", "--stdin", data=b"").decode().strip()
    previous = checkpoints.get(branch)
    if previous == empty_tree:
        return empty_tree, "saved daily baseline; full tree"
    if previous:
        command = ["git", "--no-replace-objects", "-C", str(repository), "merge-base", "--is-ancestor", previous, head]
        ancestry = subprocess.run(command, capture_output=True, env=GIT_ENV, check=False)
        if ancestry.returncode == 0:
            return previous, "since saved daily baseline"
        reason = "history changed; full tree"
    else:
        recent_base = git_output(repository, "rev-list", "--first-parent", "-1", "--before=24 hours ago", head)
        reason = "first run; last 24 hours"
        if recent_base.strip():
            return recent_base.decode().strip(), reason

    return empty_tree, reason


def write_daily_patch(repository, base, head, snapshot_dir, patch_file):
    """Compare exported text on both sides so excluded files also stay out of the patch."""
    if base == head:
        patch_file.write_bytes(b"")
        return False

    changed = git_output(
        repository, "diff", "--name-only", "--no-renames", "--no-ext-diff", "--no-textconv", "-z", base, head, "--",
    )
    paths = set()
    for raw_path in changed.split(b"\0"):
        if is_utf8(raw_path):
            name = raw_path.decode("utf-8")
            if safe_relative_path(name):
                paths.add(name)

    with tempfile.TemporaryDirectory(prefix="repo-sweep-diff-") as directory:
        working_dir = Path(directory)
        export_snapshot(repository, base, working_dir / "before", selected_paths=paths)
        after = working_dir / "after"
        after.mkdir()
        for name in paths:
            current = snapshot_dir / name
            if current.is_file():
                target = after / name
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(current, target)

        command = [
            "git", "diff", "--no-index", "--no-color", "--no-renames", "--no-ext-diff",
            "--no-textconv", "--", "before", "after",
        ]
        with patch_file.open("wb") as output:
            result = subprocess.run(
                command, cwd=working_dir, stdout=output, stderr=subprocess.PIPE, env=GIT_ENV, check=False,
            )
        # git diff --no-index returns 1 when it finds changes, even on a successful run.
        if result.returncode not in (0, 1):
            raise ValueError("Could not compare the committed source snapshots")
        return result.returncode == 1


def export_snapshot(repository, revision, destination, selected_paths=None):
    """Export regular text blobs directly so checkout hooks and filters cannot run."""
    destination.mkdir(parents=True, exist_ok=True)
    entries = git_output(repository, "ls-tree", "-rzl", revision)
    command = ["git", "--no-replace-objects", "-C", str(repository), "cat-file", "--batch"]
    omitted = 0
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=GIT_ENV) as batch:
        with batch.stdin:
            for entry in entries.split(b"\0"):
                if not entry:
                    continue
                metadata, raw_path = entry.split(b"\t", 1)
                mode, _, object_id, size = metadata.split()
                name = os.fsdecode(raw_path)
                if selected_paths is not None and name not in selected_paths:
                    continue
                if mode not in (b"100644", b"100755") or int(size) > MAX_FILE_BYTES:
                    omitted += 1
                    continue
                if not is_utf8(raw_path) or not safe_relative_path(name):
                    omitted += 1
                    continue
                path = destination / name
                content = read_git_blob(batch, object_id, size)
                if not write_text_blob(path, content):
                    omitted += 1
                elif mode == b"100755":
                    path.chmod(0o700)
        if batch.wait() != 0:
            raise ValueError("Git could not read all source blobs")
    return omitted


def read_git_blob(batch, object_id, size):
    # Git replies with a header, the exact bytes of the blob, and a trailing newline.
    batch.stdin.write(object_id + b"\n")
    batch.stdin.flush()
    if batch.stdout.readline().split() != [object_id, b"blob", size]:
        raise ValueError("Git returned an unexpected blob header")
    content = batch.stdout.read(int(size))
    if len(content) != int(size) or batch.stdout.read(1) != b"\n":
        raise ValueError("Incomplete Git blob")
    return content


def copy_local_evidence(root, workspace):
    folder = root / ".repo-sweep-evidence"
    if not folder.is_dir() or folder.is_symlink():
        return
    for path in sorted(folder.iterdir()):
        if path.is_symlink() or not path.is_file() or not safe_relative_path(path.name):
            continue
        if path.suffix.lower() in {".md", ".txt", ".log", ".json", ".sarif"} and path.stat().st_size <= MAX_FILE_BYTES:
            write_text_blob(workspace / "evidence" / path.name, path.read_bytes())


def write_text_blob(path, data):
    if b"\0" in data or not is_utf8(data):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True


def safe_relative_path(name):
    if any(part.lower() in {"", ".", "..", ".git"} for part in name.split("/")):
        return False
    return all(ord(character) >= 32 and character not in "\\\x7f" for character in name)


def is_utf8(data):
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def git_output(repository, *arguments, data=None):
    result = subprocess.run(
        ["git", "--no-pager", "--no-replace-objects", "-C", str(repository), *arguments],
        input=data, capture_output=True, check=True, env=GIT_ENV,
    )
    return result.stdout


def agent_command(engine="codex", model=None):
    if engine == "claude":
        command = [
            "claude", "-p", "--safe-mode", "--restricted", "--strict-mcp-config", "--no-session-persistence",
            "--tools", "Read,Grep,Glob", "--allowedTools", "Read,Grep,Glob",
            "--permission-mode", "dontAsk", "--output-format", "text",
        ]
    elif engine in {"codex", "ollama"}:
        command = [
            "codex", "--no-daemon", "-a", "never", "exec", "--ignore-user-config", "--ignore-rules",
            "--skip-git-repo-check", "--ephemeral", "--color", "never",
        ]
        for setting in (
            'default_permissions="sweep"',
            'permissions.sweep.filesystem={":minimal"="read",":workspace_roots"="read"}',
            "permissions.sweep.network.enabled=false",
            'shell_environment_policy.inherit="none"',
            'cli_auth_credentials_store="auto"',
            'web_search="disabled"',
            "project_doc_max_bytes=0",
        ):
            command.extend(["-c", setting])
        for feature in ("apps", "plugins", "hooks", "multi_agent"):
            command.extend(["--disable", feature])
        command.extend(["--enable", "skip_host_skill_discovery"])
        if engine == "ollama":
            command.extend(["--oss", "--local-provider", "ollama"])
            model = model or "gpt-oss:20b"
    else:
        raise ValueError("Choose codex, claude or ollama for each review or consolidation agent")

    if not shutil.which(command[0]):
        raise ValueError(f"Install {command[0]} or choose an available review or consolidation agent")
    if model:
        command.extend(["--model", model])
    if engine != "claude":
        command.append("-")
    return command


async def run_agent(command, workspace, instructions):
    process = await asyncio.create_subprocess_exec(
        *command, cwd=workspace, start_new_session=True,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        response = process.communicate(instructions.encode("utf-8"))
        stdout, stderr = await asyncio.wait_for(response, AGENT_TIMEOUT_SECONDS)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        raise
    if process.returncode:
        category = agent_error_category(stderr)
        raise ValueError(f"{command[0]} exited {process.returncode} ({category})")
    return stdout.decode("utf-8").strip()


def agent_error_category(stderr):
    # A provider may echo private source in its diagnostics. Only the category goes into the report.
    message = stderr.lower()
    if b"401" in message or b"unauthorized" in message:
        return "authentication"
    if b"429" in message or b"rate limit" in message:
        return "rate limit"
    if b"unknown option" in message:
        return "unsupported CLI version"
    return "agent error"


if __name__ == "__main__":
    sys.exit(main())
