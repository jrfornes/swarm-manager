# Improvements

Ideas for making the swarm orchestrator better, grouped by theme and roughly
ordered by value within each. Written after an end-to-end test run that
exposed several of these.

This project stays **stdlib-first and file-based** (see the README). The goal is
**not** to bolt on a heavy framework — but we **are** building toward a
SwarmForge-style **forge host** (multi-project board, Attention, bounded
parallelism). See [plan.md](plan.md) Part III. Items are tagged:

- 🐞 **bug** — a correctness problem worth fixing now.
- 🧹 **cleanup/DX** — quality-of-life, low risk.
- 🔭 **when needed** — good ideas that the README's ethos says to defer until
  something actually hurts.

## Status (updated)

Items #1–#8 and #10 are **done** — see [plan.md](plan.md) Part I. **#9**
(concurrency) and **#12** (forge host) are the active direction — Part III in
`plan.md` (queue + board + work-items). Pipelines v1 (**#11**) is **done**.

| # | Item | State |
|---|---|---|
| 1 | Honest job status | ✅ done |
| 2 | No zombie `running` jobs | ✅ done |
| 3 | Subprocess timeout | ✅ done |
| 4 | Atomic `meta.json` writes | ✅ done |
| 5 | `cleanup.py` | ✅ done |
| 6 | Richer job cards | ✅ done |
| 7 | Diff view | ✅ done |
| 8 | Per-role permission mode | ✅ done |
| 9 | Concurrency cap / queue | ⏳ pending → Part III.2 |
| 10 | Server hardening (localhost + threading) | ✅ done |
| 11 | Parallel pipelines (`pipeline.py`) | ✅ done |
| 12 | Forge host (board, Attention, work-items) | ⏳ pending → Part III |

Workspace abstraction (`target_config.py`, `targets.local.json`, consumer
`.swarm/profile.json`) is **done** — see `docs/workspace-contract.md`.
🔭 `{ticket}` / work-item JSON placeholders in stage tasks remain deferred.

The original write-ups are kept below for context.

---

## 1. Job status is derived from the wrong signal 🐞 ✅

`launch_agent.py` sets `status = "done" if proc.returncode == 0 else "failed"`.
But `claude -p` exits `0` even when it accomplished nothing. In our first test
run the agent's `Write` was denied, it did no work, and the job still showed as
**done** — a false success.

The result JSON already carries the true outcome. Prefer these over the exit
code:

- `result.is_error` (bool)
- `result.subtype` (`"success"` vs. error subtypes)
- `result.permission_denials` (non-empty ⇒ the agent was blocked)
- `result.terminal_reason` (`"completed"` vs. other)

**Suggestion:** mark a job `done` only when `returncode == 0` *and*
`is_error` is false *and* `permission_denials` is empty; otherwise `failed`
(or a new `blocked` status). Surface `permission_denials` in `meta.json` at the
top level so it's obvious why.

## 2. Zombie "running" jobs on unexpected errors 🐞

Once status is written as `running`, any exception before the final
`write_meta` (e.g. `claude` not on `PATH` → `FileNotFoundError`, or the process
being killed) leaves `meta.json` stuck at `running` forever. Nothing ever
reconciles it, so the dashboard shows a job that will never finish.

**Suggestion:** wrap the launch body in `try/except/finally` and, on any
unhandled exception, write `status = "failed"` with the error text into the
log and `meta.json`. A `finally` that guarantees a terminal status is the
cheapest fix.

## 3. No timeout on the agent subprocess 🐞

`subprocess.run([...])` has no `timeout`. A hung or runaway agent stays
`running` indefinitely and holds its worktree.

**Suggestion:** add a `--timeout` arg (default e.g. 30 min), pass it to
`subprocess.run(timeout=...)`, and on `TimeoutExpired` mark the job `failed`
(→ ties into #2's `finally`).

## 4. Non-atomic meta.json writes cause flicker 🐞

`server.py` reads `meta.json` while `launch_agent.py` may be mid-write.
`json.dump` isn't atomic, so a partial read raises `JSONDecodeError` — the
server catches it and *skips that job*, making it briefly vanish from the
board. Rare, but real.

**Suggestion:** write to `meta.json.tmp` then `os.replace()` it into place.
`os.replace` is atomic on the same filesystem, so readers always see a
complete file.

---

## 5. A cleanup script 🧹

Today, every job leaves three things behind that are **never** removed:

1. the worktree under `worktrees/<id>/`,
2. the `agent/<id>` branch in the target repo,
3. the `jobs/<id>/` metadata dir.

The README documents `git worktree remove` by hand, one at a time. A small
`cleanup.py` would make this a one-liner. Sketch of a useful interface:

```bash
python3 cleanup.py <id>            # remove one job's worktree + branch + jobs dir
python3 cleanup.py --done          # clean every job whose status is done/failed
python3 cleanup.py --all           # nuke all jobs and worktrees
python3 cleanup.py --dry-run ...   # print what would be removed
```

Implementation notes:
- Read `meta.json` to find the `repo` and `branch` for each id.
- `git -C <repo> worktree remove <path>` (add `--force` only behind a flag —
  it discards uncommitted changes).
- Delete the branch with `git -C <repo> branch -D agent/<id>` **only** after
  confirming it's merged or the user passed a force flag, so an agent's work
  isn't silently thrown away.
- Then `shutil.rmtree(jobs/<id>)`.
- Skip (and warn about) any job still `running`.

This is the highest-value cleanup item and the natural next thing to build.

## 6. Surface the captured result data on the dashboard 🧹

`meta.json` already stores cost, token usage, and `permission_denials`, but
`index.html` shows only role / task / branch / time. Cheap, high-value adds to
each card:

- **cost** (`result.total_cost_usd`) and **duration**
  (`finished_at − started_at`),
- a **⚠️ badge** when `permission_denials` is non-empty (would have made our
  first-run failure obvious at a glance),
- distinct color for `failed` vs. `done`.

## 7. Show the diff an agent produced 🔭

The whole point is the code an agent writes, but there's no way to see it from
the dashboard — you have to `cd` into the worktree. A `GET /jobs/<id>/diff`
endpoint running `git -C <repo> diff main...agent/<id>` (or `git -C <worktree>
diff HEAD`) rendered in the log panel would close the loop from "launch" to
"review."

## 8. Per-job / per-role permission mode 🔭

We hardcoded `--permission-mode acceptEdits`. That's right for `builder`, but a
`reviewer` should stay read-only, and some users may want
`--dangerously-skip-permissions` for throwaway worktrees. Make it a
`launch_agent.py` flag (e.g. `--permission-mode`) with a sensible per-role
default, rather than one value baked in.

---

## 9. Concurrency cap / queue 🔭

Called out in the README as the *first* thing to add when needed. Jobs
currently spawn a `claude` process the instant they're launched; fire off 20
and you'll launch 20 at once. A simple on-disk queue + a max-in-flight count
(no external dependency needed) is the natural shape when that starts to bite.

## 10. Server hardening 🔭

Minor, only if this ever runs anywhere but your own laptop:

- `TCPServer(("", PORT), ...)` binds to **all** interfaces. Bind to
  `127.0.0.1` so the dashboard isn't exposed on the local network.
- `SimpleHTTPRequestHandler` is single-threaded; one slow request blocks
  others. `socketserver.ThreadingTCPServer` is a one-line swap if it matters.

---

---

## 11. Direction: from fan-out to parallel pipelines 🧭 ✅ v1 landed

> **Built.** `launch_agent.py --item NAME` shares one worktree across a
> work-item's stages, and `pipeline.py` sequences them. The implement↔verify
> gate and dashboard grouping are still open — see [plan.md](plan.md) Part II.

The items above harden what this tool already is: a **fan-out** — N independent
one-shot agents, no coordination. That's width. The way work actually gets done
here is a **staged pipeline with file handoffs**, run today by hand across
separate chat windows: `/spec-author` → (`.feature` + `flow.md`) → `/verifier`
→ `/implementer` → `/verifier`, cycling implement/verify. That's depth.

The target is a **fan-out of pipelines**: many work-items in flight, each
flowing through the ordered skill stages. Full design and what shipped (data
model, `pipeline.py`, workflow manifest, the implement↔verify gate) is in
[plan.md](plan.md) **Part II**. The essentials:

- **One worktree per work-item**, shared by its stages (so `.feature`/`flow.md`/
  code accumulate); each stage is a fresh `claude -p "/skill"` run. Pipelines
  run in parallel with each other.
- **The handoff is the files**, so no inter-agent chat, message bus, or tmux is
  needed — the worktree is the shared blackboard.
- **A role is a skill** in the target repo, so no bespoke "constitution" prompt
  system is needed — skills already are that.

### Reference point: SwarmForge (Uncle Bob)

[unclebob/swarm-forge](https://github.com/unclebob/swarm-forge) **project-manager**
is the product reference for Part III: several pipelines/projects at once, a
dashboard with **Attention**, and a host lieutenant. We keep file handoffs and
consumer-repo skills; we adopt the **forge** shape, not tmux/Babashka.

| SwarmForge concept | Our direction |
|---|---|
| Role packs / topologies | Workflow manifests + [reference packs](docs/reference/swarm-workflows/README.md) |
| Layered constitution prompts | Skills in the target repo (native) |
| Handoff protocol / message notes | Files in the worktree (`.feature`, `flow.md`) |
| Handoff re-verification gate | Independent `/verifier` stage; v2 loop in Part II.D |
| project-manager forge / board | **Part III** — work-items, Attention, worker |
| Host lieutenant | **Part III.4** — one-shot planner, not tmux |
| tmux sessions, agent chat | Deferred — stages stay `claude -p`; chat optional later on work-item notes |
| Babashka | Not used — Python stdlib orchestrator |

**Handoff verification** (honest status per stage, independent verifier) remains
non-negotiable — items #1–#3 and Part II.D.

---

## 12. Forge host (SwarmForge project-manager direction) 🧭 ⏳

> **Planned.** Full breakdown in [plan.md](plan.md) **Part III**.

Largest gap vs. SwarmForge today: no **host** — you CLI-launch pipelines one at
a time and read raw job columns. Target:

- **Work-items** on disk (description, target, workflow, pipeline status).
- **Forge worker** — queue + `MAX_INFLIGHT` concurrent pipelines (item #9).
- **Dashboard** — board + **Attention** (blocked/failed/needs human), create and
  resume work-items from the UI.
- **Lieutenant** (later) — planner agent that triages an inbox into queued
  work-items.

Mechanics we are *not* copying wholesale: tmux per role, Babashka, inter-agent
chat bus. Stage execution stays one-shot `claude -p` in shared worktrees.

---

## Already done ✅

Fan-out hardening (details in [plan.md](plan.md) Part I):

- **Static-file bug** — `index.html` moved to `public/` (where `PUBLIC_DIR`
  points), so the dashboard loads.
- **Agents can modify files** — `--permission-mode` on the `claude` invocation
  (per-role default), so agents actually do work.
- **Honest lifecycle** — status derived from the result payload, guaranteed
  terminal status, subprocess timeout, atomic `meta.json` writes, `blocked`
  status.
- **`cleanup.py`** — worktree + branch + jobs dir, with `--dry-run`/`--force`
  and refusal to discard uncommitted/unmerged work.
- **Dashboard** — localhost bind, threaded server, path-traversal-guarded
  routes, a live-worktree diff tab, and cards showing cost/duration/denials.
