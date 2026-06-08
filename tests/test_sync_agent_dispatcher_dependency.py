from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sync_agent_dispatcher_dependency.py"
SPEC = importlib.util.spec_from_file_location("sync_agent_dispatcher_dependency", SCRIPT_PATH)
assert SPEC is not None
sync_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sync_module
assert SPEC.loader is not None
SPEC.loader.exec_module(sync_module)


def write_context_manager_pyproject(path: Path, version: str) -> None:
    path.write_text(
        f"""
[project]
name = "context-manager"
version = "{version}"
""".lstrip(),
        encoding="utf-8",
    )


def write_agent_dispatcher_pyproject(path: Path, version: str) -> None:
    path.write_text(
        f"""
[project]
name = "agent-dispatcher"
dependencies = [
    "context-manager>={version}",
]
""".lstrip(),
        encoding="utf-8",
    )


def test_sync_dependency_updates_agent_dispatcher_requirement(tmp_path: Path) -> None:
    context_manager_pyproject = tmp_path / "context-manager.toml"
    agent_dispatcher_pyproject = tmp_path / "agent-dispatcher.toml"
    write_context_manager_pyproject(context_manager_pyproject, "0.3.0")
    write_agent_dispatcher_pyproject(agent_dispatcher_pyproject, "0.2.0")

    result = sync_module.sync_dependency(context_manager_pyproject, agent_dispatcher_pyproject)

    assert result.changed is True
    assert result.old_dependency == '"context-manager>=0.2.0"'
    assert result.new_dependency == '"context-manager>=0.3.0"'
    assert '"context-manager>=0.3.0"' in agent_dispatcher_pyproject.read_text(encoding="utf-8")


def test_sync_dependency_check_mode_reports_drift_without_writing(tmp_path: Path) -> None:
    context_manager_pyproject = tmp_path / "context-manager.toml"
    agent_dispatcher_pyproject = tmp_path / "agent-dispatcher.toml"
    write_context_manager_pyproject(context_manager_pyproject, "0.3.0")
    write_agent_dispatcher_pyproject(agent_dispatcher_pyproject, "0.2.0")

    result = sync_module.sync_dependency(context_manager_pyproject, agent_dispatcher_pyproject, check=True)

    assert result.changed is True
    assert '"context-manager>=0.2.0"' in agent_dispatcher_pyproject.read_text(encoding="utf-8")


def test_sync_dependency_noops_when_versions_match(tmp_path: Path) -> None:
    context_manager_pyproject = tmp_path / "context-manager.toml"
    agent_dispatcher_pyproject = tmp_path / "agent-dispatcher.toml"
    write_context_manager_pyproject(context_manager_pyproject, "0.2.0")
    write_agent_dispatcher_pyproject(agent_dispatcher_pyproject, "0.2.0")

    result = sync_module.sync_dependency(context_manager_pyproject, agent_dispatcher_pyproject)

    assert result.changed is False
    assert result.old_dependency == result.new_dependency
