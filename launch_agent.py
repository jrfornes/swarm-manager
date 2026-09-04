#!/usr/bin/env python3
"""
Launch one Claude Code agent job in an isolated git worktree.

Usage:
    python3 launch_agent.py <role> <repo_path> <task description>
                            [--permission-mode MODE] [--timeout SECONDS]
                            [--item NAME]

Pass --item to run several jobs as *stages* of one work-item: they share a
single worktree (worktrees/<item> on branch agent/<item>), so each stage reads
the files the last one left behind. That is the whole handoff mechanism — see
pipeline.py, which sequences stages this way.

Run it in the background to fire off multiple agents at once:
    python3 launch_agent.py builder ~/code/myapp "Add input validation to the signup form" &
    python3 launch_agent.py tester  ~/code/myapp "Write tests for the signup form" &
    python3 launch_agent.py reviewer ~/code/myapp "Review the diff on branch agent/xyz" &

Each job gets its own directory under jobs/<id>/ with:
    meta.json     - status, role, branch, timestamps, final result (incl. token/cost usage)
    output.log    - raw stdout/stderr from git + claude

No database. The jobs/ directory *is* the database. The dashboard server
just reads these files.

Job status reflects the real outcome, not just the process exit code:
    starting -> running -> done      (agent finished and did its work)
                        -> blocked    (agent ran cleanly but was denied permission)
                        -> failed     (git/worktree error, agent error, or timeout)
"""
from __future__ import annotations  # allow `dict | None` hints on Python 3.9

import argparse
import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(BASE_DIR, "jobs")
WORKTREES_DIR = os.path.join(BASE_DIR, "worktrees")

DEFAULT_TIMEOUT = 1800  # 30 minutes
PERMISSION_MODES = ["default", "acceptEdits", "bypassPermissions", "plan"]
DEBUG_LOG_PATH = os.path.join(BASE_DIR, "jobs", "debug-3b000d.log")
DEBUG_SESSION_ID = "3b000d"

# A work-item name becomes both a directory under worktrees/ and a git branch
# name, so keep it to characters that are safe in both.
ITEM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*$")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_out(repo: str, *args: str) -> str:
    """Run a git command and return its stdout stripped, or '' on failure."""
    r = subprocess.run(
        ["git", "-C", repo, *args],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def write_meta(job_dir: str, meta: dict) -> None:
    """Write meta.json atomically so a concurrent reader (server.py) never
    sees a half-written file: write to a temp file, then os.replace() it into
    place (atomic on the same filesystem)."""
    path = os.path.join(job_dir, "meta.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f, indent=2)
    os.replace(tmp, path)


def debug_log(run_id: str, hypothesis_id: str, location: str, message: str, data: dict) -> None:
    payload = {
        "sessionId": DEBUG_SESSION_ID,
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
    }
    try:
        os.makedirs(os.path.dirname(DEBUG_LOG_PATH), exist_ok=True)
        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except Exception:
        # Debug logging must never change runtime behavior.
        pass


def default_permission_mode(role: str) -> str:
    """A reviewer only needs to read; give it a read-only mode so it can't edit
    files. Every other role defaults to acceptEdits so it can actually work."""
    return "plan" if role == "reviewer" else "acceptEdits"


def classify(returncode: int, result: dict | None) -> str:
    """Derive the true job outcome. `claude -p` exits 0 even when it did no
    work (e.g. a permission prompt was denied), so the exit code alone is not
    trustworthy -- inspect the result payload."""
    if returncode != 0:
        return "failed"
    if result is None:
        # exited 0 but produced no parseable JSON result -- treat as failure.
        return "failed"
    result_text = str(result.get("result") or "")
    if result_text.startswith("Unknown command:"):
        # The agent did not run a real turn; this is a no-op failure.
        return "failed"
    if "Failed to authenticate" in result_text or result.get("terminal_reason") == "api_error":
        return "failed"
    if "Failed to authenticate" in result_text or result.get("terminal_reason") == "api_error":
        return "failed"
    if result.get("is_error") or result.get("subtype") != "success":
        return "failed"
    if result.get("permission_denials"):
        return "blocked"
    return "done"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Launch one Claude Code agent job.")
    p.add_argument("role", help="label shown on the dashboard (builder/tester/reviewer/...)")
    p.add_argument("repo_path", help="path to the git repo the agent works in")
    p.add_argument("task", nargs="+", help="task description for the agent")
    p.add_argument(
        "--permission-mode",
        choices=PERMISSION_MODES,
        default=None,
        help="claude permission mode (default: 'plan' for reviewer, 'acceptEdits' otherwise)",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"max seconds to let the agent run before failing (default: {DEFAULT_TIMEOUT})",
    )
    p.add_argument(
        "--item",
        default=None,
        help="work-item name. Runs the job in worktrees/<item> on branch "
             "agent/<item>, creating it only if missing, so several staged "
             "jobs share one tree and hand off through the files in it. "
             "Without this each job gets its own worktree (the default fan-out).",
    )
    p.add_argument(
        "--skills-dir",
        default=".claude/skills",
        help="relative path in the worktree to skill folders (for prompts/debug metadata)",
    )
    args = p.parse_args()
    if args.item is not None and not ITEM_RE.match(args.item):
        p.error("--item must start with a letter or digit and contain only "
                "letters, digits, '.', '_' or '-'")
    return args


def main() -> None:
    args = parse_args()
    role = args.role
    repo_path = os.path.abspath(args.repo_path)
    task = " ".join(args.task)
    permission_mode = args.permission_mode or default_permission_mode(role)

    job_id = uuid.uuid4().hex[:8]
    job_dir = os.path.join(JOBS_DIR, job_id)
    # Every stage of a work-item shares one worktree/branch (keyed on the item
    # name) so each stage reads what the last one wrote; a plain job keys them
    # on its own id and so gets a tree to itself.
    work_id = args.item or job_id
    worktree_dir = os.path.join(WORKTREES_DIR, work_id)
    branch = f"agent/{work_id}"
    log_path = os.path.join(job_dir, "output.log")

    os.makedirs(job_dir, exist_ok=True)
    os.makedirs(WORKTREES_DIR, exist_ok=True)

    meta = {
        "id": job_id,
        "role": role,
        "item": args.item,
        "repo": repo_path,
        "skills_dir": args.skills_dir,
        "branch": branch,
        "worktree": worktree_dir,
        "base_sha": None,
        "end_sha": None,
        "task": task,
        "permission_mode": permission_mode,
        "timeout": args.timeout,
        "status": "starting",
        "started_at": now(),
        "finished_at": None,
        "total_cost_usd": None,
        "permission_denials": [],
        "error": None,
        "result": None,
    }
    write_meta(job_dir, meta)

    # Everything below runs under try/finally so the job ALWAYS lands in a
    # terminal status. Without this, any exception after status="running"
    # (claude missing from PATH, a kill, etc.) would strand the job as a
    # zombie "running" that never resolves.
    try:
        with open(log_path, "a") as log:
            if os.path.isdir(worktree_dir):
                # A previous stage of this work-item already made the tree; run
                # in it as-is so its files (the handoff) are still there.
                log.write(f"--- reusing worktree {worktree_dir} on branch {branch} ---\n")
            else:
                log.write(f"--- creating worktree {worktree_dir} on branch {branch} ---\n")
                wt = subprocess.run(
                    ["git", "-C", repo_path, "worktree", "add", "-b", branch, worktree_dir],
                    stdout=log,
                    stderr=log,
                )
                if wt.returncode != 0:
                    raise RuntimeError("git worktree add failed (see log)")

            # Where this job started, so its diff shows only what *it* changed
            # rather than everything the earlier stages accumulated.
            meta["base_sha"] = git_out(worktree_dir, "rev-parse", "HEAD") or None
            meta["status"] = "running"
            write_meta(job_dir, meta)
            # region agent log
            debug_log(
                run_id=job_id,
                hypothesis_id="H2",
                location="launch_agent.py:worktree_ready",
                message="Stage run context prepared",
                data={
                    "role": role,
                    "task": task,
                    "worktree": worktree_dir,
                    "skills_dir": args.skills_dir,
                    "skills_dir_exists": os.path.isdir(
                        os.path.join(worktree_dir, args.skills_dir)
                    ),
                    "permission_mode": permission_mode,
                },
            )
            # endregion

            log.write(f"--- launching claude code ({permission_mode}): {task!r} ---\n")
            # --permission-mode lets the agent act without a human to approve
            # prompts in this non-interactive run. Each agent is confined to its
            # own worktree, so edits stay isolated.
            proc = subprocess.run(
                ["claude", "-p", task, "--output-format", "json",
                 "--permission-mode", permission_mode],
                cwd=worktree_dir,
                stdout=subprocess.PIPE,
                stderr=log,
                text=True,
                timeout=args.timeout,
            )
            log.write(proc.stdout or "")

        try:
            result = json.loads(proc.stdout)
        except (json.JSONDecodeError, TypeError):
            result = None
        # region agent log
        debug_log(
            run_id=job_id,
            hypothesis_id="H1",
            location="launch_agent.py:post_claude",
            message="Claude run completed",
            data={
                "returncode": proc.returncode,
                "stdout_prefix": (proc.stdout or "")[:200],
                "parsed_result": result is not None,
                "subtype": (result or {}).get("subtype"),
                "is_error": (result or {}).get("is_error"),
                "num_turns": (result or {}).get("num_turns"),
                "permission_denials": (result or {}).get("permission_denials"),
            },
        )
        # endregion

        meta["result"] = result
        if result:
            meta["total_cost_usd"] = result.get("total_cost_usd")
            meta["permission_denials"] = result.get("permission_denials") or []
        meta["status"] = classify(proc.returncode, result)
        # region agent log
        debug_log(
            run_id=job_id,
            hypothesis_id="H3",
            location="launch_agent.py:status_classified",
            message="Stage status classified",
            data={
                "status": meta["status"],
                "subtype": (result or {}).get("subtype"),
                "is_error": (result or {}).get("is_error"),
                "num_turns": (result or {}).get("num_turns"),
                "result_prefix": ((result or {}).get("result") or "")[:120],
            },
        )
        # endregion

    except subprocess.TimeoutExpired:
        meta["status"] = "failed"
        meta["error"] = f"timed out after {args.timeout}s"
        with open(log_path, "a") as log:
            log.write(f"--- {meta['error']} ---\n")
    except Exception as e:  # noqa: BLE001 - any failure must still resolve the job
        meta["status"] = "failed"
        meta["error"] = str(e)
        with open(log_path, "a") as log:
            log.write(f"--- error: {e} ---\n")
    finally:
        # Where this job left the tree. Together with base_sha it bounds the
        # stage's own commits, so a later stage's work is not attributed to it.
        meta["end_sha"] = git_out(worktree_dir, "rev-parse", "HEAD") or None
        meta["finished_at"] = now()
        write_meta(job_dir, meta)

    # Exit 0 only on a clean, working run so `launch_agent.py ... && next` and
    # the future queue worker can trust the exit code.
    raise SystemExit(0 if meta["status"] == "done" else 1)


if __name__ == "__main__":
    main()
