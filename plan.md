# Implementation plan

Three parts:

- **Part I — Hardening the fan-out** (Phases 0–4): fix and polish the existing
  "launch N independent agents" tool. Phases 0–3 are **done**; Phase 4
  (concurrency) is folded into Part III as the forge worker's admission control.
- **Part II — Parallel pipelines**: staged skill workflows with file handoffs on
  top of the fan-out. Linear v1 has **landed**; verify gate (v2) and dashboard
  grouping remain useful polish.
- **Part III — Forge host** (new priority): close the largest gap vs.
  [SwarmForge](https://github.com/unclebob/swarm-forge) **project-manager** —
  a local host that tracks many work-items / pipelines across registered targets,
  surfaces **Attention** (what needs a human), and bounds concurrent pipelines.
  Still Python stdlib + on-disk state; still one-shot `claude -p` per stage; no
  tmux or Babashka unless a later phase proves we need them.

## Status

| Phase | Scope                                                        | State      |
| ----- | ------------------------------------------------------------ | ---------- |
| 0     | Atomic writes, `blocked` status                              | ✅ done    |
| 1     | Honest job lifecycle (status, timeout, argparse)             | ✅ done    |
| 2     | `cleanup.py`                                                 | ✅ done    |
| 3     | Dashboard: localhost bind, threading, diff tab, richer cards | ✅ done    |
| 4     | Concurrency cap / queue (see III.2 — same mechanism)         | ⏳ pending |
| II.1  | Pipelines v1: shared worktree per work-item, `pipeline.py`   | ✅ done    |
| II.1b | Workspace abstraction: registry + `.swarm/profile.json`      | ✅ done    |
| II.2  | Verify gate (implement↔verify loop), dashboard grouping      | ⏳ pending |
| III.1 | Work-items as first-class forge state                        | ⏳ pending |
| III.2 | Forge worker: queue + max concurrent pipelines               | ⏳ pending |
| III.3 | Dashboard: board, Attention, create / resume work-items      | ⏳ pending |
| III.4 | Lieutenant (planner) — triage inbox → spawn pipelines      | ⏳ pending |

---

## Part I — Hardening the fan-out

A sequenced plan to implement every item in [improvements.md](improvements.md).
Ordered so that (a) small foundational fixes land first, (b) changes to the
same file are batched into one coherent edit, and (c) the one invasive change
(concurrency) comes last.

Each phase lists **files**, the **concrete change**, and a **verify** step.
There is no test harness in this repo, so "verify" means a scripted smoke test
of the kind used during the end-to-end test run (throwaway git repo + a real
`launch_agent.py` job).

Legend: 🐞 bug · 🧹 cleanup/DX · 🔭 deferred-but-in-scope-here · ✅ done.

---

## Phase 0 — Foundations (small, unblocks the rest) ✅

### 0.1 Atomic `meta.json` writes 🐞 (improvements #4)

- **File:** `launch_agent.py` (`write_meta`)
- **Change:** write to `meta.json.tmp` in the same dir, then `os.replace()` it
  onto `meta.json`. Atomic on one filesystem, so `server.py` never reads a
  half-written file.
- **Verify:** launch a job while curling `/jobs` in a tight loop; confirm the
  job never disappears and the server logs no `JSONDecodeError` skips.

### 0.2 Define the status vocabulary

- **Files:** `launch_agent.py`, `public/index.html`
- **Change:** settle the set of statuses before touching lifecycle logic.
  Proposal: keep `starting` / `running` / `done` / `failed`, and add
  **`blocked`** for "ran cleanly but was denied permission / did no work"
  (surfaces the Phase 1 finding without overloading `failed`). Add a
  `blocked` column to the dashboard's `STATUSES` array.
- **Verify:** dashboard renders five columns, empty `blocked` column included.

---

## Phase 1 — Honest job lifecycle (one rewrite of `launch_agent.py`) ✅

Items #1, #2, #3, #8 all rewrite the same launch/main flow, so do them as a
single edit rather than four passes.

### 1.1 Argument parsing 🔭 (improvements #8)

- **Change:** replace the manual `sys.argv` slicing with `argparse`.
  - positionals: `role`, `repo_path`, `task`
  - `--permission-mode` (default `acceptEdits`; per-role default: `reviewer`
    → `plan`/read-only, everything else → `acceptEdits`)
  - `--timeout` (seconds, default `1800`)
- **Verify:** `python3 launch_agent.py --help` shows the flags;
  `reviewer` job defaults to a read-only mode.

### 1.2 Guaranteed terminal status 🐞 (improvements #2)

- **Change:** wrap the worktree+claude body in `try / except / finally`. On any
  unhandled exception (e.g. `claude` not on `PATH`), write the error to the log
  and set `status = "failed"`. A `finally` asserts the job never ends in a
  non-terminal state, killing the zombie-`running` case.
- **Verify:** temporarily rename `claude` off PATH (or pass a bogus repo);
  confirm the job lands in `failed`, not stuck at `running`.

### 1.3 Subprocess timeout 🐞 (improvements #3)

- **Change:** pass `timeout=args.timeout` to `subprocess.run`. Catch
  `subprocess.TimeoutExpired` in the Phase 1.2 handler → `failed` with a
  "timed out after Ns" log line.
- **Verify:** set `--timeout 1` on a real task; confirm `failed` + timeout note.

### 1.4 Status from the real signal 🐞 (improvements #1)

- **Change:** stop deriving status from `proc.returncode` alone. After parsing
  `result`, decide:
  - `failed` if `returncode != 0` or `result.is_error` or
    `result.subtype != "success"`;
  - `blocked` if it otherwise succeeded but `result.permission_denials` is
    non-empty;
  - `done` only when clean.
    Also **hoist** `permission_denials` (and `total_cost_usd`) to the top level
    of `meta.json` so consumers don't have to dig into `result`.
- **Verify:** re-run the original LICENSE task with `--permission-mode default`
  → expect `blocked`; with `acceptEdits` → expect `done` and a non-empty diff.

---

## Phase 2 — Cleanup tooling 🧹 (improvements #5) ✅

> Landed. `cleanup.py` also gained an up-front `worktree_is_dirty` check so
> `--dry-run` reports the "kept, has uncommitted changes" outcome honestly
> instead of only discovering it mid-run.

### 2.1 New `cleanup.py`

- **File:** `cleanup.py` (new)
- **Interface:**
  ```
  python3 cleanup.py <id>       # one job: worktree + branch + jobs/<id>
  python3 cleanup.py --done     # every job whose status is done/failed/blocked
  python3 cleanup.py --all      # all non-running jobs
  python3 cleanup.py --dry-run  # print what would be removed, touch nothing
  python3 cleanup.py --force    # allow removing unmerged branches / dirty trees
  ```
- **Logic per job id:**
  1. Read `jobs/<id>/meta.json` for `repo` and `branch`.
  2. Skip (warn) if status is `running` or `starting`.
  3. `git -C <repo> worktree remove <worktree>` (`--force` only behind
     `--force`, since it discards uncommitted changes).
  4. `git -C <repo> branch -D <branch>` **only** if merged, or `--force` given
     — never silently discard an agent's unmerged work.
  5. `shutil.rmtree(jobs/<id>)`.
- **Verify:** create two throwaway jobs, run `--dry-run` (nothing removed),
  then `--done`; confirm worktrees, branches, and `jobs/` entries are gone and
  the dashboard empties. Confirm a `running` job is left untouched.

### 2.2 Doc the script

- **Files:** `CLAUDE.md`, `README.md`
- **Change:** replace the manual `git worktree remove` instructions with
  `cleanup.py` usage.

---

## Phase 3 — Dashboard & observability ✅

Both items touch `server.py` and/or `public/index.html`; batch with the tiny
server hardening (#10) since we're already in `server.py`.

> Landed, with two refinements over the original plan: the `/jobs/<id>/diff`
> endpoint reads the **live worktree** (so it shows uncommitted agent work too,
> not just committed diffs) and also lists files the agent created but never
> committed; and the `<id>` in every route is validated against real job dirs
> to block path traversal.

### 3.1 Server hardening 🔭 (improvements #10)

- **File:** `server.py`
- **Change:** bind to `127.0.0.1` instead of `""`; swap `TCPServer` →
  `ThreadingTCPServer` so a slow log/diff read doesn't block the poller.
- **Verify:** dashboard still loads; `curl 127.0.0.1:8787/jobs` works;
  a slow request doesn't stall a concurrent one.

### 3.2 Diff endpoint 🔭 (improvements #7)

- **Files:** `server.py`, `public/index.html`
- **Change:** add `GET /jobs/<id>/diff` → run
  `git -C <repo> diff <base>...<branch>` (read `repo`/`branch` from
  `meta.json`) and return it as text. In the UI, add a "Diff" toggle in the log
  panel that fetches it.
- **Verify:** open a completed builder job → the diff shows the file it wrote.

### 3.3 Richer job cards 🧹 (improvements #6)

- **File:** `public/index.html`
- **Change:** on each card, render cost (`total_cost_usd`), duration
  (`finished_at − started_at`), a ⚠️ badge when `permission_denials` is
  non-empty, and a distinct color for `failed`/`blocked`. All data is already
  in the `/jobs` payload (top-level after Phase 1.4).
- **Verify:** a `blocked` job shows the ⚠️ badge; a `done` job shows cost +
  duration.

---

## Phase 4 — Concurrency cap 🔭 (improvements #9) ⏳

**Superseded as a standalone milestone** — implement as **Part III.2** (forge
worker) so admission control and multi-pipeline scheduling ship together. The
cap's unit is unchanged: a pipeline serializes its own stages, so bound
**concurrent pipelines/worktrees**, not raw agent processes (distinct `worktree`
values among jobs in `running`/`starting` is a useful proxy).

### 4.1 On-disk queue + max-in-flight → see III.2

- **Files:** `worker.py` (or `forge_worker.py`), `launch_agent.py`
- **Change:** the forge worker owns the queue; `launch_agent.py` may still run
  stages synchronously when invoked directly, but **scheduled** work-items
  enqueue pipeline runs instead of spawning `claude` immediately.
- **Verify:** same as III.2 — enqueue 6 work-items with `MAX_INFLIGHT=2`;
  never more than 2 pipelines in flight; all reach a terminal state.

---

## Suggested execution order & checkpoints

Part I is complete except Phase 4, which is **not** a separate checkpoint anymore.

**Current priority (Part III — forge):**

1. **III.1** — work-item records on disk (forge state beside `jobs/`).
2. **III.2** — long-lived worker + `queued` + max in-flight pipelines (Phase 4).
3. **III.3** — dashboard as a **board** (projects/work-items, Attention, create
   / resume) instead of only a job column view.
4. **III.4** — optional lieutenant: one-shot planner that reads the inbox and
   starts or prioritizes pipelines (skills in this repo or a small
   `.swarm/lieutenant` skill).

**Part II polish** (grouping strip, implement↔verify v2) can land in parallel with
III.3 but is no longer the main product direction.

## Testing note

Since there's no test harness, each phase is verified with a scripted smoke
test against a throwaway git repo (as done during the end-to-end run). If we
want regression protection, a lightweight `test_smoke.sh` that creates a temp
repo, launches a job, and asserts on the resulting `meta.json` would be a
reasonable stdlib-only addition — but per the README's minimalism, add it only
if these flows start breaking repeatedly.

---

# Part II — Parallel pipelines (target architecture)

**Status: v1 landed.** The data-model change, `pipeline.py`, and per-stage diff
attribution are built and smoke-tested. What remains is the implement↔verify
gate (D below, v2) and pipeline grouping on the dashboard (C below).

## Why

The current tool is a **fan-out**: it launches N _independent_ one-shot agents,
each in its own worktree, with no coordination between them. That is width
without depth. But real work here follows a **staged pipeline with file
handoffs**, run today by hand across separate chat windows:

1. `/spec-author` authors a spec → handoff artifacts: a `.feature` file and a
   `flow.md` file.
2. `/verifier` reproduces the issue/feature → updates `.feature` + `flow.md`.
3. `/implementer` applies the code change.
4. `/verifier` again → and implement/verify may cycle.

Two properties keep pipeline automation cheap even as we add a SwarmForge-style
**forge host** (Part III):

- **The handoff is the files, not a conversation.** `.feature`, `flow.md`, and
  the code live in the repo; each stage reads what the last one wrote. The
  worktree remains the shared blackboard — we are not adopting tmux or
  handoff-mail scripts for stage-to-stage work.
- **The roles are skills that live in the target repo.** A skill's `.md` _is_
  that role's constitution + procedure. The forge adds **coordination for
  humans** (board, Attention, many work-items), not a second prompt layer.

The goal is a **fan-out of pipelines** under a **host**: many work-items in
flight at once (width), each flowing through ordered skill stages (depth), with
bounded concurrency and a single place to see what needs you next.

## The one data-model change

|              | Today (fan-out)           | Part II (pipelines)                                 |
| ------------ | ------------------------- | --------------------------------------------------- |
| Unit of work | a **job** = one agent run | a **pipeline** = one work-item                      |
| Worktree     | one per job               | **one per pipeline**, shared by all its stages      |
| Branch       | `agent/<job-id>`          | `agent/<pipeline-id>`                               |
| A stage      | —                         | one agent run _inside_ the pipeline's worktree      |
| Handoff      | none                      | files in the worktree (`.feature`, `flow.md`, code) |

A pipeline's stages run **sequentially in the same worktree** (so artifacts
accumulate), each as a **fresh `claude -p` invocation** (fresh context — an
independent verifier must not be the agent that implemented). Pipelines run in
**parallel** with each other, exactly as jobs are backgrounded today.

## Proposed pieces

### A. Workflow manifest (in the _target_ repo)

Consistent with skills living in the target repo, the stage order lives there
too — e.g. `.swarm/workflow.json`:

```json
{
  "stages": [
    { "skill": "spec-author", "permission_mode": "acceptEdits" },
    { "skill": "verifier", "permission_mode": "acceptEdits" },
    { "skill": "implementer", "permission_mode": "acceptEdits" },
    { "skill": "verifier", "permission_mode": "acceptEdits" }
  ]
}
```

Each stage names a skill (invoked as `/spec-author` etc.) and reuses the
per-stage `--permission-mode` hook already added in Phase 1.1. The task string
per stage stays tiny (often just the skill invocation) because the skill reads
the handoff files itself; only the first stage needs the work-item description.

### B. `pipeline.py` (new entry point) ✅

> Landed, but **simpler than planned**. Rather than a second runner that
> invokes `claude` itself (which would have duplicated Phase 1's status logic
> and needed a `common.py`), `launch_agent.py` gained one flag and `pipeline.py`
> is a thin sequencer over it. Open decision #2 was resolved the other way —
> **a job dir per stage**, not one `meta.json` with a `stages[]` array — because
> it reuses every existing view unchanged. See "What landed" below.

```
python3 pipeline.py <repo_path> "<work-item description>"
                    [--stages a,b,c] [--workflow PATH] [--item NAME]
                    [--permission-mode MODE] [--timeout SECONDS] [--dry-run]
```

**The whole data-model change is one flag.** `launch_agent.py --item NAME` runs
the job in `worktrees/<NAME>` on branch `agent/<NAME>`, creating the worktree
only if it is missing. Stages of a work-item therefore share one tree, and the
files a stage leaves behind _are_ the handoff. Without `--item`, a job keys the
worktree on its own id exactly as before, so the fan-out is untouched.

`pipeline.py` then just loops: for each stage, shell out to `launch_agent.py …
--item <item>` and stop if it exits non-zero. Phase 1.4's contract (exit `0`
only on `done`) is what gates the pipeline — a `blocked` or `failed` stage
stops it rather than handing a half-finished tree to the next skill. It prints
the exact `launch_agent.py` command to resume from the failed stage.

Each stage is an ordinary job with its own `jobs/<job-id>/` entry, so it shows
on the dashboard as its own card with its own log and diff.

**Per-stage diff attribution.** Jobs now record `worktree`, `base_sha` (the
worktree's HEAD when the stage started) and `end_sha` (where it left it).
`server.py` diffs `base_sha..end_sha` for a stage that later stages have
committed on top of, and `git diff <base_sha>` against the live worktree for
the stage still at the tip (preserving Phase 3.2's uncommitted-work view, and
listing untracked files only for that tip stage). Without this, every earlier
stage's diff would show the _later_ stages' work too. Old jobs with no
`base_sha` fall back to the merge-base path.

**`cleanup.py`** learned that a worktree can be shared: it removes the worktree
and branch only when no job outside the current cleanup set still references
them, and only once however many sibling stages are in the same run. Cleaning
one stage of a live pipeline now drops just that stage's metadata.

#### Verified (stub-skill smoke test)

Per the testing note above, with a throwaway repo and a stub `claude` on `PATH`
that appends to a file and commits:

- a 3-stage pipeline produces **one** worktree, and artifacts accumulate across
  stages (the handoff works);
- each stage's `base_sha` equals the previous stage's `end_sha`, and each
  stage's diff shows exactly its own line;
- a failing stage stops the pipeline (stage 3 never launches) and exits `1`;
- a job without `--item` still gets its own worktree named after its job id;
- cleaning 1 of 3 stages keeps the shared worktree; cleaning the rest removes
  it exactly once.

### C. Dashboard: pipelines as rows of stages

`server.py` already re-reads `jobs/*/meta.json`. Extend the card to render a
pipeline's `stages[]` as a progress strip, e.g.
`spec ✓ · verify ✓ · impl ● · verify ○`, each segment colored by the Phase 1
status vocabulary and clickable to that stage's log/diff. The existing
`/jobs/<id>/diff` view (which already reads the live worktree) shows the
accumulated change across the whole pipeline.

### D. The genuinely new bit: the implement↔verify gate

Everything above is sequencing existing pieces. The one new decision is how a
`verifier` stage reports pass/fail so the runner can **stop or loop back** to
`implementer`:

- **v1 (linear, recommended first):** no auto-loop. Run the manifest straight
  through; the human re-triggers `implementer` if verify failed — exactly
  today's manual behavior, just sequenced. Ships the whole value of file
  handoffs with none of the control-flow risk.
- **v2 (gated loop):** the `verifier` skill writes a machine-readable verdict
  (e.g. a line in `flow.md` or a small sentinel file the runner greps). The
  runner loops `implementer → verifier` until pass or a **max-iterations**
  guard trips (prevents runaway spend). Add only once v1 feels limiting.

## SwarmForge alignment (revised)

We are **no longer** optimizing to stay smaller than
[SwarmForge](https://github.com/unclebob/swarm-forge) at the product level.
The reference product is **project-manager**: dashboard, several projects at
once, Attention, and a host that outlives any single pipeline. Part III is that
host, implemented our way.

| SwarmForge (project-manager) | Our forge (Part III) |
| --- | --- |
| `projects/<name>/` per effort | `work-items/<id>/` (or `forge/items/`) + link to `targets.local.json` target |
| Pack topology (two-pack, six-pack, …) | `.swarm/workflow.json` + [reference manifests](docs/reference/swarm-workflows/README.md) |
| Host lieutenant / planner | III.4 — one-shot `claude -p` triage, not a tmux session |
| Dashboard board + Attention | III.3 — extend `server.py` + `public/` (polling stays) |
| Implicit concurrency across projects | III.2 — explicit queue + `MAX_INFLIGHT` pipelines |

**Still deferred** (mechanics, not product):

- **tmux / long-lived interactive agents** per role — stages stay one-shot
  `claude -p`; revisit only if Attention needs live steering inside a session.
- **Inter-agent chat** — optional later as human↔agent notes on a work-item file,
  not a message bus between stage agents.
- **Babashka / non-stdlib runtime** — Python 3 stdlib for the orchestrator.
- **Multi-backend** (codex, copilot, …) — Claude CLI only until a concrete need.

## Open decisions

1. **Manifest location & format** — ✅ resolved as designed: `.swarm/workflow.json`
   in the target repo, next to the skills it references, with `--workflow` to
   point elsewhere and `--stages a,b,c` to skip it entirely for a one-off.
2. **Stage granularity in `meta.json`** — ✅ resolved **against** the original
   lean: a **job dir per stage**, linked by an `item` field, rather than one
   file with `stages[]`. Every existing view (cards, log, diff, cleanup) then
   works on a stage with no changes. The cost is that the dashboard shows a
   pipeline as N separate cards with no grouping — see C, still open.
3. **v1 vs v2 gate** — ✅ shipped linear (v1). A failed stage stops the run and
   prints the command to resume from it. The verdict loop (v2) is untouched.
4. **Concurrency unit** — ✅ settled: cap **concurrent pipelines** (III.2).
5. **Forge state layout** — ⏳ pick before III.1: e.g. `work-items/<id>/meta.json`
   with `target`, `description`, `workflow`, `item` (worktree key), `status`,
   `attention_reason`, and pointers to the latest stage `job_id`s.
6. **Worker process** — ⏳ long-lived `worker.py` started beside `server.py`
   (recommended) vs. cooperative cap in each launcher — same as old Phase 4.

## What's left in Part II (polish)

- **C. Dashboard grouping** — progress strip per `item` (feeds III.3 board rows).
- **D. Implement↔verify gate (v2)** — machine-readable verdict + loop under
  max-iterations.

## Part III — Forge host (target architecture)

**Status: not started.** Design reference: SwarmForge
[project-manager](https://github.com/unclebob/swarm-forge/blob/project-manager/README.md).

Today you run `pipeline.py` from a terminal per work-item and read job columns.
The forge is the **always-on host**: register targets once, create work-items
from the dashboard (or CLI), let a worker drain the queue within a concurrency
budget, and use **Attention** when a pipeline is `blocked`, `failed`, or waiting
for a human decision (e.g. verify failed, merge conflict, cap hit).

### III.1 Work-items as first-class state

- **Files:** new `work-items/<id>/meta.json` (name TBD), `forge.py` or extend
  `pipeline.py` to write/update work-item meta when `--item` is used.
- **Fields (proposal):** `id`, `target` or `repo`, `description`, `workflow`
  path, `item` (shared worktree key), `pipeline_status` (`queued` | `running` |
  `attention` | `done` | `failed`), `created_at`, `updated_at`, optional
  `attention` `{ reason, job_id }`.
- **Invariant:** one work-item ↔ one pipeline worktree (`--item`); stage jobs
  keep today's `jobs/<job-id>/` + `item` link.
- **Verify:** create two work-items against two targets; each has distinct
  worktree; meta survives server restart.

### III.2 Forge worker (queue + max in-flight)

- **Files:** `worker.py`, `launch_agent.py` / `pipeline.py` hooks for enqueue.
- **Change:** work-items in `queued` start `pipeline.py` (subprocess) when
  `count(running pipelines) < MAX_INFLIGHT`. Stage jobs inside a pipeline stay
  sequential; only whole pipelines compete for slots.
- **Status:** add `queued` to job/work-item vocabulary; dashboard column or
  board section for queued work.
- **Verify:** 6 work-items, `MAX_INFLIGHT=2`; observe slot behavior; all
  terminal.

### III.3 Dashboard: board and Attention

- **Files:** `server.py`, `public/index.html` (+ optional `public/forge.js`).
- **Change:** primary view = **work-items** (rows with target, description,
  pipeline status, cost rollup, progress strip from II.C). **Attention** =
  filter/sidebar for `attention` + `blocked`/`failed` latest stage. Actions:
  **New work-item** (target picker from registry, description, workflow preset),
  **Resume** (re-run from failed stage command), **Dismiss** attention.
- **API (proposal):** `GET/POST /work-items`, `GET /work-items/<id>`, reuse
  `/jobs` for stage detail.
- **Verify:** create work-item from UI; watch it move queued → running → done;
  failed stage lands in Attention with log link.

### III.4 Lieutenant (planner)

- **Files:** orchestrator skill e.g. `.claude/skills/forge-lieutenant/` or
  consumer skill; small `lieutenant.py` entry point.
- **Change:** periodic or on-demand one-shot agent reads open issues / inbox
  file / Attention backlog and enqueues work-items (human approves in v1, auto in
  v2). SwarmForge's host lieutenant as **planner**, not tmux concierge.
- **Verify:** stub inbox → lieutenant proposes two work-items → appear queued.

### Relationship to Part I Phase 4

Phase 4 and III.2 are the **same feature**: bounded parallelism for a real
swarm. Do not ship a headless queue without the board (III.3) unless explicitly
scoping a CLI-only milestone.

## First step — done

The stub-skill smoke test described here (throwaway repo, stub stages that just
write a file and commit, no tokens spent) is how v1 was verified; see B above.
It is worth re-running as the regression check for any Part II change, and is
the obvious basis for the `test_smoke.sh` floated in the testing note.

**Next real step (Part III):** implement **III.1** — `work-items/<id>/meta.json`
and wire `pipeline.py --item` to create/update work-item records. Then **III.2**
worker + **III.3** board/Attention. Optional smoke before coding: run a real
pipeline against a registered consumer (below) to validate skills hand off
files; that does not replace forge state.

#### Verify workspace abstraction (done — keep as regression)

1. **Registry:** Create `targets.local.json` pointing at a temp git repo with a
   minimal `.swarm/workflow.json`. Run
   `python3 pipeline.py --target test --dry-run "x"` — output shows the
   resolved absolute `repo` and stage commands.
2. **Profile:** In that repo, set `.swarm/profile.json` with a custom
   `skills_dir`; dry-run default stage prompts must reference that path.
3. **Backward compat:** `python3 pipeline.py /path/to/repo "x" --dry-run`
   unchanged when `profile.json` is absent.
4. **Regression:** Re-run the stub-skill smoke test (Part II B) — no token spend.
