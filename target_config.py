"""Target registry and per-repo workspace profile (stdlib only)."""
from __future__ import annotations

import json
import os

DEFAULT_PROFILE = {
    "skills_dir": ".claude/skills",
    "workflow": ".swarm/workflow.json",
}

LOCAL_REGISTRY_NAME = "targets.local.json"
EXAMPLE_REGISTRY_NAME = "targets.example.json"


def load_registry(path: str) -> dict:
    """Parse a targets.json-shaped document; validate shape."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        raise SystemExit(f"registry not found: {path}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"{path} is not valid JSON: {e}")

    targets = doc.get("targets")
    if not isinstance(targets, dict) or not targets:
        raise SystemExit(f"{path}: 'targets' must be a non-empty object.")

    default = doc.get("default")
    if default is not None and default not in targets:
        raise SystemExit(f"{path}: 'default' key {default!r} is not in 'targets'.")

    for name, entry in targets.items():
        if not isinstance(entry, dict) or not entry.get("repo"):
            raise SystemExit(
                f"{path}: target {name!r} must be an object with a 'repo' path."
            )
    return doc


def resolve_target(registry: dict, name: str | None) -> dict:
    """Return {repo, workflow?, skills_dir?} with repo absolutized."""
    targets = registry["targets"]
    key = name or registry.get("default")
    if not key:
        raise SystemExit(
            "no target name given and registry has no 'default' key."
        )
    if key not in targets:
        known = ", ".join(sorted(targets))
        raise SystemExit(f"unknown target {key!r}; known targets: {known}")

    entry = dict(targets[key])
    repo = os.path.abspath(os.path.expanduser(entry.pop("repo")))
    out: dict = {"repo": repo, "target_name": key}
    for opt in ("workflow", "skills_dir"):
        if opt in entry:
            out[opt] = entry.pop(opt)
    if entry:
        raise SystemExit(
            f"target {key!r}: unknown keys {sorted(entry)} "
            f"(allowed besides repo: workflow, skills_dir)."
        )
    return out


def load_workspace_profile(repo: str) -> dict:
    """Read <repo>/.swarm/profile.json if present; else DEFAULT_PROFILE."""
    path = os.path.join(repo, ".swarm", "profile.json")
    profile = dict(DEFAULT_PROFILE)
    if not os.path.isfile(path):
        return profile
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        raise SystemExit(f"{path} is not valid JSON: {e}")
    if not isinstance(raw, dict):
        raise SystemExit(f"{path} must be a JSON object.")
    for key in ("skills_dir", "workflow"):
        if key in raw:
            profile[key] = raw[key]
    return profile


def load_local_registry(base_dir: str) -> dict:
    path = os.path.join(base_dir, LOCAL_REGISTRY_NAME)
    if not os.path.isfile(path):
        example = os.path.join(base_dir, EXAMPLE_REGISTRY_NAME)
        raise SystemExit(
            f"no {LOCAL_REGISTRY_NAME} in {base_dir}.\n"
            f"Copy {EXAMPLE_REGISTRY_NAME} to {LOCAL_REGISTRY_NAME} "
            f"and set absolute paths to your consumer repos."
        )
    return load_registry(path)


def resolve_workflow_path(
    repo: str, workflow_arg: str | None, profile: dict, target_workflow: str | None
) -> str | None:
    """Manifest path for load_stages when --stages is not set."""
    if workflow_arg:
        if os.path.isabs(workflow_arg):
            return workflow_arg
        return os.path.join(repo, workflow_arg)
    rel = target_workflow or profile["workflow"]
    return os.path.join(repo, rel)
