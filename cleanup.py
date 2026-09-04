#!/usr/bin/env python3
"""
Remove the artifacts a finished job leaves behind: its git worktree, its
`agent/<id>` branch, and its `jobs/<id>/` metadata dir. Nothing here is ever
removed automatically, so run this when you're done with a job.

Usage:
    python3 cleanup.py <id> [<id> ...]   # clean specific jobs
    python3 cleanup.py --done            # clean every done/blocked/failed job
    python3 cleanup.py --all             # clean every job that isn't running
    python3 cleanup.py --dry-run ...     # print what would happen, change nothing
    python3 cleanup.py --force ...       # discard uncommitted / unmerged work too

Safety: without --force this never throws away an agent's work.
  - A worktree with uncommitted or untracked changes is left in place (the
    whole job is skipped) rather than force-removed.
  - A branch whose commits are not yet merged into the repo's HEAD is kept
    (the worktree and jobs dir are still removed) so you can merge it later.
--force overrides both.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(BASE_DIR, "jobs")
WORKTREES_DIR = os.path.join(BASE_DIR, "worktrees")

TERMINAL_STATUSES = {"done", "blocked", "failed"}
ACTIVE_STATUSES = {"starting", "running"}


def load_meta(job_id: str) -> dict | None:
    path = os.path.join(JOBS_DIR, job_id, "meta.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def all_job_ids() -> list[str]:
    if not os.path.isdir(JOBS_DIR):
        return []
    return sorted(
        d for d in os.listdir(JOBS_DIR)
        if os.path.isdir(os.path.join(JOBS_DIR, d))
    )


def job_worktree(job_id: str, meta: dict | None) -> str:
    """Where this job ran. Stages of a work-item share one worktree, so it is
    not derivable from the job id any more; fall back for pre-`--item` jobs."""
    return (meta or {}).get("worktree") or os.path.join(WORKTREES_DIR, job_id)


def worktree_owners() -> dict[str, set[str]]:
    """Map each worktree to every job that ran in it. A worktree shared by
    several stages must survive until the last of them is cleaned."""
    owners: dict[str, set[str]] = {}
    for jid in all_job_ids():
        owners.setdefault(job_worktree(jid, load_meta(jid)), set()).add(jid)
    return owners


def git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo, *args],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def worktree_is_dirty(worktree: str) -> bool:
    """True if the worktree has uncommitted or untracked changes, i.e. a plain
    `git worktree remove` would refuse it and force would discard real work."""
    r = subprocess.run(
        ["git", "-C", worktree, "status", "--porcelain"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    return bool(r.stdout.strip())


def branch_is_merged(repo: str, branch: str) -> bool:
    """True if `branch`'s tip is already reachable from the repo's HEAD, i.e.
    deleting the branch discards no unmerged commits."""
    # `git merge-base --is-ancestor <branch> HEAD` exits 0 when branch is an
    # ancestor of HEAD (merged / no new commits), non-zero otherwise.
    r = subprocess.run(
        ["git", "-C", repo, "merge-base", "--is-ancestor", branch, "HEAD"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


def clean_job(job_id: str, *, force: bool, dry_run: bool,
              owners: dict[str, set[str]], cleaning: set[str],
              handled: set[str]) -> bool:
    """Clean one job. Returns True if it was fully removed."""
    meta = load_meta(job_id)
    if meta is None:
        print(f"  {job_id}: no readable meta.json — removing jobs dir only")
        if not dry_run:
            shutil.rmtree(os.path.join(JOBS_DIR, job_id), ignore_errors=True)
        return True

    status = meta.get("status")
    repo = meta.get("repo")
    branch = meta.get("branch") or f"agent/{job_id}"
    worktree = job_worktree(job_id, meta)

    if status in ACTIVE_STATUSES and not force:
        print(f"  {job_id}: status={status} — still active, skipping (use --force to override)")
        return False

    prefix = "[dry-run] " if dry_run else ""

    # A worktree shared with stages that are NOT being cleaned holds work those
    # stages still need, so drop only this job's metadata and leave the tree.
    survivors = owners.get(worktree, {job_id}) - cleaning
    if survivors:
        print(f"  {job_id}: worktree shared with {len(survivors)} other job(s) "
              f"({', '.join(sorted(survivors))}) — keeping it; removing jobs dir only")
        print(f"  {prefix}{job_id}: rm -rf jobs/{job_id}")
        if not dry_run:
            shutil.rmtree(os.path.join(JOBS_DIR, job_id), ignore_errors=True)
        return True

    # Otherwise the last stage cleans up for the whole work-item — but only
    # once, however many of its stages are in this run.
    if worktree in handled:
        print(f"  {prefix}{job_id}: worktree/branch already removed by an earlier "
              f"stage — removing jobs dir only")
        print(f"  {prefix}{job_id}: rm -rf jobs/{job_id}")
        if not dry_run:
            shutil.rmtree(os.path.join(JOBS_DIR, job_id), ignore_errors=True)
        return True

    # 1. Remove the worktree. A dirty worktree (uncommitted/untracked changes)
    #    holds real agent work, so without --force we keep the whole job rather
    #    than discard it. Checking dirtiness up front keeps --dry-run honest.
    if os.path.isdir(worktree) and repo:
        if worktree_is_dirty(worktree) and not force:
            print(f"  {job_id}: worktree has uncommitted changes — kept "
                  f"(merge/commit it, or re-run with --force to discard)")
            return False
        wt_args = ["worktree", "remove", worktree] + (["--force"] if force else [])
        print(f"  {prefix}{job_id}: git worktree remove {worktree}"
              + (" --force" if force else ""))
        if not dry_run:
            r = git(repo, *wt_args)
            if r.returncode != 0:
                print(f"      kept: {r.stdout.strip()}")
                return False
    elif os.path.isdir(worktree):
        # No repo recorded but a worktree dir exists — remove the dir directly.
        print(f"  {prefix}{job_id}: rm worktree dir {worktree}")
        if not dry_run:
            shutil.rmtree(worktree, ignore_errors=True)

    # 2. Delete the branch, but never silently discard unmerged commits.
    if repo:
        if force or branch_is_merged(repo, branch):
            print(f"  {prefix}{job_id}: git branch -D {branch}")
            if not dry_run:
                git(repo, "branch", "-D", branch)
        else:
            print(f"  {job_id}: branch {branch} kept — has unmerged commits "
                  f"(merge it, or re-run with --force)")

    # The worktree and branch are gone now, so any sibling stage still to be
    # processed in this run must not try to remove them again.
    handled.add(worktree)

    # 3. Remove the jobs metadata dir.
    print(f"  {prefix}{job_id}: rm -rf jobs/{job_id}")
    if not dry_run:
        shutil.rmtree(os.path.join(JOBS_DIR, job_id), ignore_errors=True)

    return True


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Clean up finished agent jobs.")
    p.add_argument("ids", nargs="*", help="specific job ids to clean")
    p.add_argument("--done", action="store_true",
                   help="clean every job in a terminal status (done/blocked/failed)")
    p.add_argument("--all", action="store_true",
                   help="clean every job that isn't currently running")
    p.add_argument("--force", action="store_true",
                   help="discard uncommitted worktree changes and unmerged branches")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be removed without changing anything")
    return p.parse_args()


def select_ids(args: argparse.Namespace) -> list[str]:
    if args.ids:
        return args.ids
    ids = all_job_ids()
    if args.done:
        return [i for i in ids if (load_meta(i) or {}).get("status") in TERMINAL_STATUSES]
    if args.all:
        return [i for i in ids if (load_meta(i) or {}).get("status") not in ACTIVE_STATUSES
                or args.force]
    return []


def main() -> None:
    args = parse_args()
    if not (args.ids or args.done or args.all):
        print("nothing selected. pass job ids, or --done / --all. See --help.")
        raise SystemExit(2)

    ids = select_ids(args)
    if not ids:
        print("no matching jobs.")
        return

    print(f"{'Would clean' if args.dry_run else 'Cleaning'} {len(ids)} job(s):")
    owners = worktree_owners()
    cleaning = set(ids)
    handled: set[str] = set()
    removed = sum(
        clean_job(i, force=args.force, dry_run=args.dry_run,
                  owners=owners, cleaning=cleaning, handled=handled)
        for i in ids
    )
    verb = "would be removed" if args.dry_run else "removed"
    print(f"done — {removed}/{len(ids)} {verb}.")


if __name__ == "__main__":
    main()
