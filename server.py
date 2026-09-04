#!/usr/bin/env python3
"""
Minimal dashboard server. No database, no framework - reads jobs/*/meta.json
straight off disk and serves a static HTML page that polls it.

Usage:
    python3 server.py
    open http://localhost:8787
"""
from __future__ import annotations  # allow `dict | None` hints on Python 3.9

import http.server
import json
import os
import socketserver
import subprocess
import urllib.parse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(BASE_DIR, "jobs")
WORKTREES_DIR = os.path.join(BASE_DIR, "worktrees")
PUBLIC_DIR = os.path.join(BASE_DIR, "public")
HOST = "127.0.0.1"  # localhost only - don't expose the dashboard on the network
PORT = 8787


def valid_job_id(job_id: str) -> bool:
    """Only accept ids that are real job dirs. Guards the path-building routes
    (log/diff) against traversal like `..%2f..` in the URL."""
    if not job_id or "/" in job_id or "\\" in job_id or job_id in (".", ".."):
        return False
    return os.path.isdir(os.path.join(JOBS_DIR, job_id))


def read_meta(job_id: str) -> dict | None:
    meta_path = os.path.join(JOBS_DIR, job_id, "meta.json")
    try:
        with open(meta_path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def list_jobs() -> list:
    jobs = []
    if not os.path.isdir(JOBS_DIR):
        return jobs
    for job_id in sorted(os.listdir(JOBS_DIR)):
        meta = read_meta(job_id)
        if meta is not None:
            jobs.append(meta)
    jobs.sort(key=lambda j: j.get("started_at") or "", reverse=True)
    return jobs


def job_diff(job_id: str) -> str:
    """Show what the agent changed: tracked changes (committed or not) since the
    job started, plus any files it created but never committed. Runs against
    the job's worktree when it still exists so uncommitted work is included."""
    meta = read_meta(job_id)
    if not meta:
        return "(no metadata for this job)"
    repo = meta.get("repo")
    branch = meta.get("branch") or f"agent/{job_id}"
    # Stages of a work-item share a worktree, so it is no longer derivable from
    # the job id — read what the job recorded, falling back for older jobs.
    worktree = meta.get("worktree") or os.path.join(WORKTREES_DIR, job_id)
    if not repo or not os.path.isdir(repo):
        return "(repo path is gone — cannot compute diff)"

    # Where this job started. For a plain job that is the fork point; for one
    # stage of a work-item it is the previous stage's tip, so the diff shows
    # that stage's own contribution rather than the whole pipeline's.
    base = meta.get("base_sha") or subprocess.run(
        ["git", "-C", repo, "merge-base", "HEAD", branch],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    ).stdout.strip()

    gitdir = worktree if os.path.isdir(worktree) else repo

    # Is this job still the tip of its branch? If a later stage has committed on
    # top, diff a bounded range so this stage isn't credited with that work.
    # Only the tip is diffed against the live tree, where uncommitted work lives.
    end = meta.get("end_sha")
    head = subprocess.run(
        ["git", "-C", gitdir, "rev-parse", "HEAD"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    ).stdout.strip()
    at_tip = gitdir == worktree and (not end or end == head)

    if base and at_tip:
        # `git diff <base>` against a live worktree includes uncommitted edits
        # to tracked files as well as everything committed on the branch.
        diff_args = ["diff", base]
    elif base and end:
        diff_args = ["diff", base, end]
    elif base:
        diff_args = ["diff", base, branch]
    else:
        diff_args = ["diff"]
    diff = subprocess.run(
        ["git", "-C", gitdir, *diff_args],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    ).stdout

    parts = []
    if diff.strip():
        parts.append(diff.rstrip())

    # Untracked files never show up in `git diff`; list them so an agent that
    # created a file but didn't commit it isn't invisible. Only for the tip
    # stage — anything untracked now was left by whoever ran last.
    if at_tip and os.path.isdir(worktree):
        untracked = subprocess.run(
            ["git", "-C", worktree, "ls-files", "--others", "--exclude-standard"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        ).stdout.strip()
        if untracked:
            parts.append("Untracked (uncommitted) files:\n" +
                         "\n".join("  + " + f for f in untracked.splitlines()))

    return "\n\n".join(parts) if parts else "(no changes)"


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PUBLIC_DIR, **kwargs)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]

        if parts == ["jobs"]:
            self._send_json(list_jobs())
            return

        if len(parts) == 3 and parts[0] == "jobs" and parts[2] in ("log", "diff"):
            job_id = parts[1]
            if not valid_job_id(job_id):
                self.send_response(404)
                self.end_headers()
                return
            if parts[2] == "log":
                log_path = os.path.join(JOBS_DIR, job_id, "output.log")
                if not os.path.isfile(log_path):
                    self._send_text("(no log yet)")
                    return
                with open(log_path, errors="replace") as f:
                    lines = f.readlines()[-200:]
                self._send_text("".join(lines))
            else:
                self._send_text(job_diff(job_id))
            return

        super().do_GET()

    def _send_json(self, data) -> None:
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str) -> None:
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Server(socketserver.ThreadingTCPServer):
    # Threaded so one slow request (a git diff, a big log read) doesn't block
    # the 2s poller. Reuse the address so restarts don't hit "address in use".
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    os.makedirs(JOBS_DIR, exist_ok=True)
    os.makedirs(PUBLIC_DIR, exist_ok=True)
    with Server((HOST, PORT), Handler) as httpd:
        print(f"Dashboard running at http://localhost:{PORT}")
        httpd.serve_forever()
