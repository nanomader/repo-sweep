#!/usr/bin/env python3
"""Review a directory of Git checkouts and print the path to a local HTML report."""
import argparse
import asyncio
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile

from report import read_note, assemble, fallback_groups, render

HERE = Path(__file__).resolve().parent
BLOB_LIMIT = 2 * 1024 * 1024
PARALLEL = 3
TIMEOUT = 1200
GIT_ENV = {k: v for k, v in os.environ.items() if k not in {
    "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES"}}
GIT_ENV.update(GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL="", GIT_TERMINAL_PROMPT="0")


def git(repo, *args, data=None):
    p = subprocess.run(["git", "--no-pager", "--no-replace-objects", "-C", str(repo), *args],
                       input=data, capture_output=True, check=True, env=GIT_ENV)
    return p.stdout


def checkouts(root):
    repos = []
    def fail(err):
        raise err
    for directory, dirs, files in os.walk(root, onerror=fail):
        if ".git" in dirs or ".git" in files:
            repos.append(Path(directory))
        dirs[:] = sorted(d for d in dirs if d not in {".git", "node_modules", ".venv", ".repo-sweep-evidence"}
                         and not (Path(directory) / d).is_symlink())
    return sorted(repos)


def safe_path(name):
    return all(part and part.lower() not in {".", "..", ".git"} for part in name.split("/")) and all(
        ord(c) >= 32 and c not in "\\\x7f" for c in name)


def save_text(path, content):
    if b"\x00" in content:
        return False
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return True


def export(repo, head, dest, only=None):
    """Read regular blobs directly. No checkout, hooks, symlinks or export filters."""
    omitted = 0
    dest.mkdir(parents=True, exist_ok=True)
    listing = git(repo, "ls-tree", "-rzl", head)
    with subprocess.Popen(["git", "--no-replace-objects", "-C", str(repo), "cat-file", "--batch"],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=GIT_ENV) as cat:
        try:
            for entry in listing.split(b"\0"):
                if not entry:
                    continue
                meta, raw = entry.split(b"\t", 1)
                mode, kind, oid, size = meta.split()
                name = os.fsdecode(raw)
                if only is not None and name not in only:
                    continue
                if (mode not in (b"100644", b"100755") or int(size) > BLOB_LIMIT
                        or not safe_path(name) or not raw.isascii() and not valid_utf8(raw)):
                    omitted += 1
                    continue
                cat.stdin.write(oid + b"\n")
                cat.stdin.flush()
                header = cat.stdout.readline().split()
                if header != [oid, b"blob", size]:
                    raise ValueError("Git returned an unexpected blob header")
                content = cat.stdout.read(int(size))
                if len(content) != int(size) or cat.stdout.read(1) != b"\n":
                    raise ValueError("Incomplete Git blob")
                if not save_text(dest / name, content):
                    omitted += 1
                elif mode == b"100755":
                    (dest / name).chmod(0o700)
        finally:
            cat.stdin.close()
        if cat.wait() != 0:
            raise ValueError("Git could not read all source blobs")
    return omitted


def valid_utf8(raw):
    try:
        raw.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def daily_patch(repo, base, head, current, output):
    """Diff only the same safe text blobs that the reviewers can actually read."""
    if base == head:
        output.write_bytes(b"")
        return False
    changed = git(repo, "diff", "--name-only", "--no-renames", "--no-ext-diff", "--no-textconv", "-z", base, head, "--")
    paths = {raw.decode() for raw in changed.split(b"\0") if raw and valid_utf8(raw) and safe_path(raw.decode())}
    with tempfile.TemporaryDirectory(prefix="repo-sweep-diff-") as directory:
        scratch = Path(directory)
        export(repo, base, scratch / "before", only=paths)
        after = scratch / "after"
        after.mkdir()
        for name in paths:
            if (current / name).is_file():
                target = after / name
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(current / name, target)
        with output.open("wb") as out:
            rc = subprocess.run(["git", "diff", "--no-index", "--no-color", "--no-renames", "--no-ext-diff",
                                 "--no-textconv", "--", "before", "after"], cwd=scratch,
                                stdout=out, stderr=subprocess.PIPE, env=GIT_ENV).returncode
        if rc not in (0, 1):
            raise ValueError("Could not compare the committed source snapshots")
    return rc == 1


def baseline(repo, branch, head, previous):
    empty = git(repo, "hash-object", "-t", "tree", "--stdin", data=b"").decode().strip()
    old = previous.get(branch)
    if old:
        rc = subprocess.run(["git", "--no-replace-objects", "-C", str(repo), "merge-base",
                             "--is-ancestor", old, head], capture_output=True, env=GIT_ENV).returncode
        return (old, "last completed daily run") if rc == 0 else (empty, "history changed; full tree")
    old = git(repo, "rev-list", "--first-parent", "-1", "--before=24 hours ago", head).decode().strip()
    return old or empty, "first run; last 24 hours"


def snapshot(root, work, mode, state):
    repos = checkouts(root)
    if not repos:
        raise ValueError("No Git checkouts found in the supplied directory")
    index, problems, tips = {}, [], {}
    (work / "changes").mkdir()
    relative_to = root.parent if root in repos else root
    for num, repo in enumerate(repos, 1):
        key = f"r{num}"
        name = repo.relative_to(relative_to).as_posix()
        try:
            try:
                head = git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
            except subprocess.CalledProcessError:
                # A newly initialized checkout has a valid symbolic HEAD but no refs or source.
                branch = git(repo, "symbolic-ref", "--short", "HEAD").decode().strip()
                if git(repo, "for-each-ref", "--count=1").strip():
                    raise
                (work / key).mkdir()
                index[key] = dict(repository=name, commit="", branch=branch, base=None,
                                  range="no commits on this checkout", changed=False, omitted_entries=0)
                continue
            branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").decode().strip()
            base, method = baseline(repo, branch, head, state.get(name, {})) if mode == "daily" else (None, "whole revision")
            omitted = export(repo, head, work / key)
            changed = True
            if base:
                patch = work / "changes" / (key + ".patch")
                changed = daily_patch(repo, base, head, work / key, patch)
            index[key] = dict(repository=name, commit=head, branch=branch, base=base, range=method,
                              changed=changed, omitted_entries=omitted)
            tips.setdefault(name, {})[branch] = head
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            shutil.rmtree(work / key, ignore_errors=True)
            (work / "changes" / (key + ".patch")).unlink(missing_ok=True)
            problems.append(f"{name}: snapshot unavailable ({type(exc).__name__}).")
    (work / "inventory.json").write_text(json.dumps(index, ensure_ascii=True, indent=2), encoding="utf-8")
    evidence = root / ".repo-sweep-evidence"
    if evidence.is_dir() and not evidence.is_symlink():
        for file in sorted(evidence.iterdir()):
            if (not file.is_symlink() and file.is_file() and safe_path(file.name)
                    and file.suffix.lower() in {".md", ".txt", ".log", ".json", ".sarif"}
                    and file.stat().st_size <= BLOB_LIMIT):
                save_text(work / "evidence" / file.name, file.read_bytes())
    return index, problems, tips


def read_prompts():
    parts = re.split(r"^## ([^\n]+)$", (HERE / "prompts.md").read_text(encoding="utf-8"), flags=re.M)
    ids = [title.partition(" — ")[0].lower().replace(" ", "-") for title in parts[1::2]]
    sections = dict(zip(ids, parts[2::2]))
    if len(sections) != len(ids) or not all(re.fullmatch(r"[a-z]+(?:-[a-z]+)*", key) for key in ids):
        raise ValueError("Invalid or duplicate check name in prompts.md")
    return {k: "\n".join(line for line in text.strip().splitlines() if not line.startswith("Source: "))
            for k, text in sections.items()}


def agent_command(engine="codex", model=None):
    if engine == "claude":
        cmd = ["claude", "-p", "--safe-mode", "--restricted", "--strict-mcp-config", "--no-session-persistence",
               "--tools", "Read,Grep,Glob", "--allowedTools", "Read,Grep,Glob", "--permission-mode", "dontAsk",
               "--output-format", "text"]
    elif engine in {"codex", "ollama"}:
        cmd = ["codex", "--no-daemon", "-a", "never", "exec", "--ignore-user-config", "--ignore-rules",
               "--skip-git-repo-check", "--ephemeral", "--color", "never",
               "-c", 'default_permissions="sweep"', "-c", 'permissions.sweep.filesystem={":minimal"="read",":workspace_roots"="read"}',
               "-c", "permissions.sweep.network.enabled=false", "-c", 'shell_environment_policy.inherit="none"',
               "-c", 'cli_auth_credentials_store="auto"',
               "-c", 'web_search="disabled"', "-c", "project_doc_max_bytes=0",
               "--disable", "apps", "--disable", "plugins", "--disable", "hooks", "--disable", "multi_agent",
               "--enable", "skip_host_skill_discovery"]
        if engine == "ollama":
            cmd += ["--oss", "--local-provider", "ollama"]
            model = model or "gpt-oss:20b"
    else:
        raise ValueError("Choose codex, claude or ollama for each review or consolidation agent")
    if model:
        cmd += ["--model", model]
    if not shutil.which(cmd[0]):
        raise ValueError(f"Install {cmd[0]} or choose an available review or consolidation agent")
    return cmd if engine == "claude" else [*cmd, "-"]


async def ask(cmd, cwd, prompt):
    process = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(prompt.encode()), TIMEOUT)
    except (asyncio.CancelledError, TimeoutError, asyncio.TimeoutError):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        raise
    if process.returncode:
        # Provider diagnostics can contain sensitive source. Surface only known failure categories.
        diagnostic = stderr.lower()
        category = next((label for token, label in [(b"401", "authentication"), (b"unauthorized", "authentication"),
                         (b"rate limit", "rate limit"), (b"429", "rate limit"),
                         (b"unknown option", "unsupported CLI version")] if token in diagnostic), "agent error")
        raise ValueError(f"{cmd[0]} exited {process.returncode} ({category})")
    return stdout.decode().strip()


def atomic_json(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")
    temp.replace(path)


async def sweep(root, mode, store, cmd, merge_cmd=None):
    state_file = store / "checkpoints.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run = store / f"{stamp}-{mode}"
    notes = run / "findings"
    notes.mkdir(parents=True)
    prompts = read_prompts()
    policy, consolidate = prompts.pop("policy"), prompts.pop("consolidate")
    limit = asyncio.Semaphore(PARALLEL)
    with tempfile.TemporaryDirectory(prefix="repo-sweep-") as scratch:
        work = Path(scratch)
        index, problems, tips = snapshot(root, work, mode, state)
        shutil.copyfile(work / "inventory.json", run / "inventory.json")
        scope = ("Review changes in changes/*.patch and follow their callers across every snapshot. Only report "
                 "regressions introduced or exposed by the changes and still present at HEAD. An unchanged "
                 "checkout supplies context. " if mode == "daily" else
                 "Review the current committed snapshots, including unchanged code. Review each applicable "
                 "repository for the assigned check. ")
        instruction = "\n".join([f"Cadence: {mode}. {scope}", "Read inventory.json first. It maps snapshot IDs to repositories and commits."])
        if mode == "daily" and not any(repo["changed"] for repo in index.values()):
            instruction += "\nThere are no changes in any available snapshot. Return SKIPPED with that reason."
        print(f"{len(index)} repositories, {len(prompts)} checks. {run}", file=sys.stderr, flush=True)

        async def worker(name, focus):
            path = notes / f"{name}.md"
            async with limit:
                print(f"Reviewing {name}", file=sys.stderr, flush=True)
                try:
                    raw = await ask(cmd, work, f"{policy}\n\n{instruction}\n\nCheck: {name}\n{focus}")
                    path.write_text(raw + "\n", encoding="utf-8")
                    return read_note(name, raw, index, work)
                except (OSError, ValueError, TimeoutError, asyncio.TimeoutError) as exc:
                    reason = str(exc) or "Agent timed out"
                    if not path.exists():
                        path.write_text(f"Status: PARTIAL\nCoverage: {reason}\n", encoding="utf-8")
                    return name, "PARTIAL", reason, []
        results = await asyncio.gather(*(worker(k, v) for k, v in prompts.items()))
        findings = {f["id"]: f for _, _, _, items in results for f in items}
        # Once all reviewers finish, the final reviewer can check their claims against the same source.
        shutil.copytree(notes, work / "findings")
        payload = [{"id": k, "title": v["title"], "severity": v["severity"], "locations": v["locations"],
                    "evidence": v["Evidence"]} for k, v in findings.items()]
        (work / "candidates.json").write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        try:
            raw = await ask(merge_cmd or cmd, work, consolidate)
            groups, excluded = assemble(raw, findings)
        except (OSError, ValueError, TimeoutError, asyncio.TimeoutError) as exc:
            problems.append(f"Consolidation failed ({str(exc) or 'timeout'}). Showing individual reviewer notes. Duplicates may remain.")
            groups, excluded = fallback_groups(findings), []
        report = run / "report.html"
        report.write_text(render(mode, stamp[:8], index, results, problems, groups, excluded, findings), encoding="utf-8")
        print(report, flush=True)
        complete = not problems and all(status != "PARTIAL" for _, status, _, _ in results)
        if mode == "daily" and complete:
            for name, branches in tips.items():
                state.setdefault(name, {}).update(branches)
            atomic_json(state_file, state)
        return 0 if complete else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repositories", type=Path)
    parser.add_argument("cadence", choices=("daily", "weekly"))
    args = parser.parse_args()
    os.umask(0o077)
    root = args.repositories.expanduser().resolve()
    if not root.is_dir():
        parser.error("repositories must be a directory")
    digest = hashlib.sha256(os.fsencode(root)).hexdigest()[:16]
    store = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "repo-sweep" / digest
    try:
        engine = os.environ.get("SWEEP_AGENT", "codex")
        cmd = agent_command(engine, os.environ.get("SWEEP_MODEL"))
        final_engine = os.environ.get("SWEEP_MERGE_AGENT") or engine
        merge_cmd = cmd if final_engine == engine else agent_command(final_engine)
        store.mkdir(parents=True, exist_ok=True)
        with (store / "run.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return asyncio.run(sweep(root, args.cadence, store, cmd, merge_cmd))
    except BlockingIOError:
        print("Another sweep is running for this repository folder.", file=sys.stderr)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
