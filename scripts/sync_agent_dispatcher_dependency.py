#!/usr/bin/env python3
"""Sync agent-dispatcher's context-manager dependency with this package version."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_HEADER = "[project]"
DEPENDENCY_RE = re.compile(r'("context-manager>=)([^"\s]+)(")')
VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"\s*$')


@dataclass(frozen=True)
class SyncResult:
    version: str
    old_dependency: str
    new_dependency: str
    changed: bool


def read_project_version(pyproject_path: Path) -> str:
    in_project = False
    for line in pyproject_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_project = stripped == PROJECT_HEADER
            continue
        if in_project:
            match = VERSION_RE.match(stripped)
            if match:
                return match.group(1)
    raise ValueError(f"No [project] version found in {pyproject_path}")


def sync_dependency(context_manager_pyproject: Path, agent_dispatcher_pyproject: Path, check: bool = False) -> SyncResult:
    version = read_project_version(context_manager_pyproject)
    contents = agent_dispatcher_pyproject.read_text(encoding="utf-8")
    match = DEPENDENCY_RE.search(contents)
    if not match:
        raise ValueError(f"No context-manager dependency found in {agent_dispatcher_pyproject}")

    old_dependency = "".join(match.groups())
    new_dependency = f'"context-manager>={version}"'
    changed = old_dependency != new_dependency

    if changed and not check:
        updated = DEPENDENCY_RE.sub(rf'\g<1>{version}\g<3>', contents, count=1)
        agent_dispatcher_pyproject.write_text(updated, encoding="utf-8")

    return SyncResult(version, old_dependency, new_dependency, changed)


def default_context_manager_pyproject() -> Path:
    return Path(__file__).resolve().parents[1] / "pyproject.toml"


def default_agent_dispatcher_pyproject() -> Path:
    return Path(__file__).resolve().parents[2] / "agent-dispatcher" / "pyproject.toml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--context-manager-pyproject",
        type=Path,
        default=default_context_manager_pyproject(),
        help="Path to context-manager pyproject.toml",
    )
    parser.add_argument(
        "--agent-dispatcher-pyproject",
        type=Path,
        default=default_agent_dispatcher_pyproject(),
        help="Path to agent-dispatcher pyproject.toml",
    )
    parser.add_argument("--check", action="store_true", help="Fail if agent-dispatcher is not already synced")
    args = parser.parse_args(argv)

    try:
        result = sync_dependency(
            args.context_manager_pyproject,
            args.agent_dispatcher_pyproject,
            check=args.check,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if result.changed:
        if args.check:
            print(
                f"agent-dispatcher is out of sync: {result.old_dependency} should be {result.new_dependency}",
                file=sys.stderr,
            )
            return 1
        print(f"updated agent-dispatcher dependency: {result.old_dependency} -> {result.new_dependency}")
    else:
        print(f"agent-dispatcher dependency already synced at context-manager {result.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
