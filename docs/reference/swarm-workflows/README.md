# Swarm workflow manifests (reference)

Copy into a consumer repo as `.swarm/*.json`, or pass `--workflow` with a path
relative to the consumer repo root (or an absolute path).

- **AcmeCo** stages need folders under the consumer `skills_dir` (default
  `.claude/skills/`).
- **Smoke** stages (`spec-author`, `verifier`, `implementer`, `publisher`) need
  those skills installed from `bucket-of-skills/` into the same `skills_dir`.

See [workspace-contract.md](../../workspace-contract.md) for `profile.json` and
the target registry.

## Quick picker

| Intent                                                   | File                                    |
| -------------------------------------------------------- | --------------------------------------- |
| Smallest AcmeCo (issue ready)                            | `workflow-1-pack-implement.json`        |
| Implement + independent review                           | `workflow-2-pack-implement-review.json` |
| Spec + implement (express; may create one issue)         | `workflow-2-pack-express-acmeco.json`   |
| Spec + implement + review                                | `workflow-3-pack-review-acmeco.json`    |
| Jira bug → issue slice → implement → review (AFK intake) | `workflow-3-pack-bug-acmeco.json`       |
| Full AcmeCo plan → one slice                             | `workflow-4-pack-acmeco.json`           |
| Plan → slice → review → PR prep                          | `workflow-5-pack-acmeco-ship.json`      |
| Existing `flow.md`; patch + Cypress                      | `workflow-2-pack-quick-ui-smoke.json`   |
| New Jira UI bug (red → fix → green)                      | `workflow-4-pack-smoke.json`            |
| Smoke loop + review + gated PR                           | `workflow-6-pack-ship-smoke.json`       |

## Example

```bash
# Smoke (Cypress) consumer with bucket-of-skills installed
python3 pipeline.py --target my-app "ACME-12345: fix save button" \
  --workflow docs/reference/swarm-workflows/workflow-4-pack-smoke.json \
  --dry-run

# AcmeCo: Jira key in description; issues-only intake then implement
python3 pipeline.py --target my-app "ACME-12345: save button does nothing" \
  --workflow docs/reference/swarm-workflows/workflow-3-pack-bug-acmeco.json \
  --dry-run
```

After copying manifests into the consumer, use paths like
`.swarm/workflow-4-pack-smoke.json` instead.

## All manifests

| File                                    | Stages                                                                             |
| --------------------------------------- | ---------------------------------------------------------------------------------- |
| `workflow-1-pack-implement.json`        | 1                                                                                  |
| `workflow-2-pack-implement-review.json` | 2                                                                                  |
| `workflow-2-pack-express-acmeco.json`   | 2                                                                                  |
| `workflow-3-pack-review-acmeco.json`    | 3                                                                                  |
| `workflow-3-pack-bug-acmeco.json`       | 3 (`to-issues` → `implement` → `code-review`; pipeline `task` overrides on intake) |
| `workflow-4-pack-acmeco.json`           | 4                                                                                  |
| `workflow-5-pack-acmeco-ship.json`      | 5                                                                                  |
| `workflow-2-pack-quick-ui-smoke.json`   | 2                                                                                  |
| `workflow-4-pack-smoke.json`            | 4                                                                                  |
| `workflow-6-pack-ship-smoke.json`       | 6                                                                                  |
| `workflow-default-acmeco.json`          | 4 (same as 4-pack AcmeCo; use as `.swarm/workflow.json` template)                  |
