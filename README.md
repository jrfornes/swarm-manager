# Agent swarm orchestrator (file-based forge)

No database, no framework, no websockets. `jobs/*/meta.json` is the job state
store today; [plan.md](plan.md) **Part III** adds **work-items** and a forge
worker so several pipelines can run under a concurrency cap with a board and
**Attention** view (SwarmForge [project-manager](https://github.com/unclebob/swarm-forge)
shape, Python stdlib implementation). A stdlib HTTP server reads state from disk
and a static HTML page polls it.

## Requirements

- Python 3
- Claude Code installed and on your `PATH` (`claude` command works from any
  directory)
- A local git repo you want agents to work in (or register it in `targets.local.json`)

## Target registry (optional)

To avoid typing absolute paths, copy `targets.example.json` to
`targets.local.json` (gitignored) and set each target's `repo` path. See
[docs/workspace-contract.md](docs/workspace-contract.md) for the consumer-repo
`.swarm/profile.json` contract.

```bash
python3 pipeline.py --target my-nx-app "Fix the signup 500" --dry-run
```

Ad-hoc jobs still use `repo_path` directly (see below).

## 1. Start the dashboard

```
python3 server.py
```

Open http://localhost:8787 - it'll be empty until you launch a job.

## 2. Launch agents

In another terminal, from the `orchestrator/` directory:

```
python3 launch_agent.py builder  ~/code/myapp "Add input validation to the signup form"  &
python3 launch_agent.py tester   ~/code/myapp "Write tests for the signup form"          &
python3 launch_agent.py reviewer ~/code/myapp "Review the diff on branch agent/<id>"     &
```

The trailing `&` backgrounds each launch so you can fire off several at
once. Each one:

1. Creates an isolated git worktree at `worktrees/<job-id>/` on a new branch
   `agent/<job-id>` - agents never touch the same files.
2. Runs `claude -p "<task>" --output-format json` inside that worktree.
3. Writes `jobs/<job-id>/meta.json` (status, timestamps, and on completion
   the full JSON result Claude Code returns - including token/cost usage)
   and `jobs/<job-id>/output.log` (raw output, for the log panel).

Refresh the dashboard and the job appears under "starting" → "running" →
"done"/"failed". Click any card to tail its log.

## Where this stays minimal (for now)

- **No database** — JSON files under `jobs/` (and soon `work-items/`) are read
  fresh on each request.
- **No websockets** — the dashboard polls every 2s.
- **Stages are one-shot** — each pipeline stage is `claude -p`; no tmux sessions
  per role (see plan Part III for the forge host).
- **Roles are labels + skills** — workflow stages invoke skills from the consumer
  repo; `builder`/`tester` on ad-hoc jobs are just dashboard labels.

**On the roadmap (Part III):** work-item board, Attention, queue, and max
concurrent pipelines. Until the worker lands, jobs still start as soon as you
launch them — background carefully or run one pipeline at a time.

## Cleaning up

Each job leaves a worktree, an `agent/<id>` branch, and a `jobs/<id>/` dir
behind on disk - none are auto-removed. `cleanup.py` removes all three, and
will not throw away uncommitted or unmerged agent work unless you pass
`--force`:

```
python3 cleanup.py <job-id>          # one job
python3 cleanup.py --done            # all done/blocked/failed jobs
python3 cleanup.py --all             # all jobs that aren't running
python3 cleanup.py --dry-run --all   # preview without changing anything
```

## Pipelines

Run a work-item through ordered skill stages (manifest defaults come from the
consumer repo's `.swarm/profile.json` and `.swarm/workflow.json`):

```
python3 pipeline.py ~/code/myapp "Add rate limiting" --dry-run
python3 pipeline.py --target my-nx-app "Add rate limiting" --dry-run
```

A worktree with uncommitted changes is left in place; a branch with unmerged
commits is kept (its worktree and jobs dir are still removed) so you can merge
it later. Add `--force` to override both.
# swarm-manager
