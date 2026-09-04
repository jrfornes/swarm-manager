# Consumer workspace contract

The orchestrator repo does not embed a product monorepo. You point it at any git
consumer with local config and a small layout inside that repo.

## Orchestrator: target registry

1. Copy `targets.example.json` to `targets.local.json` (gitignored).
2. Set each target's `repo` to an absolute path to your consumer git repo.
3. Run pipelines with `--target <name>` instead of passing `repo_path`.

Optional per-target overrides in the registry (applied before the repo profile):

| Key          | Purpose                                        |
| ------------ | ---------------------------------------------- |
| `repo`       | Required. Path to the consumer git repository. |
| `workflow`   | Relative path under `repo` to the manifest.    |
| `skills_dir` | Relative path under `repo` to skill folders.   |

## Consumer repo: `.swarm/profile.json` (optional)

If missing, defaults match today's layout:

```json
{
  "skills_dir": ".claude/skills",
  "workflow": ".swarm/workflow.json"
}
```

- **`skills_dir`** — directory containing one folder per stage `skill` name.
  Default stage prompts tell the agent to read
  `<skills_dir>/<skill>/SKILL.md`.
- **`workflow`** — path (relative to repo root) to the stage manifest.

## Consumer repo: `.swarm/workflow.json`

Existing convention. Each stage's `"skill"` name matches a folder under
`skills_dir`. Stages may set `permission_mode`, `timeout`, and `task`; `{description}`
and `{DESC}` in `task` are replaced with the work-item text.

Reference manifests (1-pack through 6-pack, AcmeCo and smoke presets) live in
this orchestrator repo at
[reference/swarm-workflows/](reference/swarm-workflows/README.md). Copy into the
consumer `.swarm/` or pass `--workflow` pointing at a copied file.

## Skills and docs in the consumer

Workflow skills live in the consumer repo under `skills_dir` (often populated
from your team's skill setup or promotion process). The orchestrator only reads
paths from `profile.json` and the registry — it does not vendor or install
skills.

## Example

`targets.local.json`:

```json
{
  "default": "my-app",
  "targets": {
    "my-app": {
      "repo": "/Users/you/code/my-nx-app"
    }
  }
}
```

```bash
python3 pipeline.py --target my-app "Fix the signup 500" --dry-run
```

Ad-hoc jobs still pass `repo_path` directly:

```bash
python3 launch_agent.py builder /Users/you/code/my-nx-app "Some task"
```

## Forge host (planned)

Part III in [plan.md](../plan.md) adds a SwarmForge-style **local host**:
work-items linked to registry targets, a worker with queue + max concurrent
pipelines, and a dashboard **board** with **Attention**. Until that ships, use
`pipeline.py` from the CLI and the existing job-column dashboard.
