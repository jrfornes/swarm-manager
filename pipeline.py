#!/usr/bin/env python3
"""
Run one work-item through an ordered list of skill stages.

Usage:
    python3 pipeline.py <repo_path> "<work-item description>"
    python3 pipeline.py --target NAME "<work-item description>"
                        [--stages a,b,c] [--workflow PATH] [--item NAME]
                        [--permission-mode MODE] [--timeout SECONDS] [--dry-run]

Background several to run pipelines in parallel, exactly as with single jobs:

    python3 pipeline.py ~/code/app "Fix the signup 500"      &
    python3 pipeline.py ~/code/app "Add rate limiting"       &

Each pipeline gets ONE worktree (worktrees/<item> on branch agent/<item>) and
runs its stages sequentially inside it, each stage a fresh `claude -p` via
launch_agent.py. There is no message bus and no handoff protocol: the files a
stage leaves in the worktree (a .feature, a flow.md, the code) *are* the
handoff, and the next stage simply reads them.

Every stage is a normal job, so it gets its own jobs/<job-id>/ entry and shows
up as its own card on the dashboard.

Stages come from --stages, else the workflow manifest (--workflow, default from
the repo's `.swarm/profile.json` or `.swarm/workflow.json`), which looks like:

    {"stages": [{"skill": "spec-author"},
                {"skill": "verifier"},
                {"skill": "implementer", "permission_mode": "acceptEdits"},
                {"skill": "verifier"}]}

A stage may override "permission_mode", "timeout", and "task"; by default a
stage's task asks Claude to follow `<skills_dir>/<skill>/SKILL.md` (from the
repo profile) for the work-item description. If "task" is set, any
"{description}" (or "{DESC}") placeholder is replaced with the work-item
description so AFK/express prompts can still carry the issue key.

Exits 0 only if every stage reached `done`, so pipelines chain with && too.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid

from target_config import (
    load_local_registry,
    load_workspace_profile,
    resolve_target,
    resolve_workflow_path,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.join(BASE_DIR, "launch_agent.py")


def slug(text: str, limit: int = 24) -> str:
    """A readable work-item name from the description, for worktrees/<item>."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()[:limit].strip("-")
    return s or "item"


def default_stage_task(skill: str, description: str, skills_dir: str) -> str:
    skill_path = f"{skills_dir.rstrip('/')}/{skill}/SKILL.md"
    return (
        f"You are the '{skill}' station. Read and follow "
        f"'{skill_path}' in this repository.\n\n"
        f"Work-item:\n{description}"
    )


def load_stages(
    repo: str, workflow_path: str | None, stages_flag: str | None
) -> list[dict]:
    if stages_flag:
        return [{"skill": s.strip()} for s in stages_flag.split(",") if s.strip()]

    path = workflow_path
    if not path:
        raise SystemExit("internal error: workflow path required when --stages is not set")
    try:
        with open(path) as f:
            manifest = json.load(f)
    except FileNotFoundError:
        raise SystemExit(
            f"no stages given and no manifest at {path}.\n"
            f"Pass --stages spec-author,verifier,implementer,verifier "
            f"or create the manifest."
        )
    except json.JSONDecodeError as e:
        raise SystemExit(f"{path} is not valid JSON: {e}")

    stages = manifest.get("stages")
    if not isinstance(stages, list) or not stages:
        raise SystemExit(f"{path} has no non-empty 'stages' array.")
    for s in stages:
        if not isinstance(s, dict) or not s.get("skill"):
            raise SystemExit(f"{path}: every stage needs a 'skill' name (got {s!r}).")
    return stages


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run a work-item through ordered skill stages in one worktree.")
    p.add_argument(
        "repo_path",
        nargs="?",
        default=None,
        help="path to the git repo the agents work in (or use --target)",
    )
    p.add_argument("description", help="what this work-item is")
    p.add_argument(
        "--target",
        default=None,
        metavar="NAME",
        help="consumer repo from targets.local.json (mutually exclusive with repo_path)",
    )
    p.add_argument("--stages", default=None,
                   help="comma-separated skill names, overriding the manifest")
    p.add_argument("--workflow", default=None,
                   help="manifest path under repo, or absolute (default: profile workflow)")
    p.add_argument("--item", default=None,
                   help="work-item name (default: a slug of the description + suffix)")
    p.add_argument("--permission-mode", default=None,
                   help="permission mode for stages that don't set their own")
    p.add_argument("--timeout", type=int, default=None,
                   help="per-stage timeout in seconds")
    p.add_argument("--dry-run", action="store_true",
                   help="print the stage commands without running them")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if bool(args.repo_path) == bool(args.target):
        raise SystemExit("pass exactly one of repo_path or --target NAME.")

    target_workflow = None
    target_skills_dir = None
    if args.target:
        resolved = resolve_target(load_local_registry(BASE_DIR), args.target)
        repo = resolved["repo"]
        target_workflow = resolved.get("workflow")
        target_skills_dir = resolved.get("skills_dir")
        print(f"target {resolved['target_name']}: repo {repo}")
    else:
        repo = os.path.abspath(args.repo_path)

    profile = load_workspace_profile(repo)
    skills_dir = target_skills_dir or profile["skills_dir"]
    workflow_path = resolve_workflow_path(
        repo, args.workflow, profile, target_workflow
    )
    stages = load_stages(repo, workflow_path, args.stages)
    item = args.item or f"{slug(args.description)}-{uuid.uuid4().hex[:4]}"

    print(f"pipeline {item}: {len(stages)} stage(s) in worktrees/{item} "
          f"on branch agent/{item}")

    for i, stage in enumerate(stages, 1):
        skill = stage["skill"]
        if stage.get("task"):
            task = (
                stage["task"]
                .replace("{description}", args.description)
                .replace("{DESC}", args.description)
            )
        else:
            task = default_stage_task(skill, args.description, skills_dir)
        cmd = [
            sys.executable,
            LAUNCHER,
            skill,
            repo,
            task,
            "--item",
            item,
            "--skills-dir",
            skills_dir,
        ]

        mode = stage.get("permission_mode") or args.permission_mode
        if mode:
            cmd += ["--permission-mode", mode]
        timeout = stage.get("timeout") or args.timeout
        if timeout:
            cmd += ["--timeout", str(timeout)]

        print(f"\n--- stage {i}/{len(stages)}: {skill} ---")
        if args.dry_run:
            print("  " + " ".join(cmd))
            continue

        # launch_agent.py exits 0 only when the stage landed in `done`, so its
        # exit code is the gate: a blocked/failed stage stops the pipeline
        # rather than handing a half-finished tree to the next skill.
        if subprocess.run(cmd).returncode != 0:
            print(f"\npipeline {item}: stopped at stage {i} ({skill}) — it did not "
                  f"finish cleanly. The worktree is left in place; inspect it on "
                  f"the dashboard, then re-run this stage with:\n"
                  f"  python3 launch_agent.py {skill} {repo} {task!r} --item {item}")
            raise SystemExit(1)

    if args.dry_run:
        print(f"\npipeline {item}: dry run — nothing was launched.")
        return
    print(f"\npipeline {item}: all {len(stages)} stage(s) done. "
          f"Review with: git -C {repo} diff HEAD...agent/{item}")


if __name__ == "__main__":
    main()
