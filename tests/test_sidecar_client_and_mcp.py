import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest


def _wait_for_sidecar(socket_path, timeout=5.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=str(socket_path)),
                base_url="http://ctxmgr",
                timeout=1.0,
            ) as client:
                return client.get("/v1/healthz").json()
        except Exception as exc:
            last = exc
            time.sleep(0.1)
    raise AssertionError(f"sidecar did not start: {last}")


@pytest.fixture
def running_sidecar(tmp_path):
    socket_path = tmp_path / "ctxmgr.sock"
    db_path = tmp_path / "ctxmgr.db"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "context_manager.sidecar",
            "--socket",
            str(socket_path),
            "--db",
            str(db_path),
            "--log-level",
            "warning",
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    try:
        _wait_for_sidecar(socket_path)
        yield socket_path
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_sidecar_client_roundtrip(running_sidecar):
    from context_manager.sidecar_client import SidecarClient

    client = SidecarClient(str(running_sidecar))
    assert client.healthz()["ok"] is True
    assert client.append("mcp-test", role="user", content="hello")["message_id"] == 1
    outbound = client.build_outbound("mcp-test", fill_ratio=0.0)
    assert outbound["messages"][0]["content"] == "[#1] hello"


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="mcp extra not installed")
@pytest.mark.asyncio
async def test_mcp_server_registers_expected_tools():
    from context_manager.mcp_server import create_server

    server = create_server()
    names = {tool.name for tool in await server.list_tools()}
    assert {
        "ctx_health",
        "ctx_append",
        "ctx_build_outbound",
        "compress",
        "ctx_usage",
        "ctx_set_model",
        "ctx_list_placeholders",
        "ctx_deactivate_placeholder",
        "ctx_parent_summary",
    } <= names
    resources = {str(resource.uri) for resource in await server.list_resources()}
    templates = {str(template.uriTemplate) for template in await server.list_resource_templates()}
    assert "ctxmgr://health" in resources
    assert {
        "ctxmgr://sessions/{session_id}/usage",
        "ctxmgr://sessions/{session_id}/placeholders",
        "ctxmgr://sessions/{session_id}/parent_summary",
    } <= templates


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="mcp extra not installed")
@pytest.mark.asyncio
async def test_mcp_health_tool_calls_sidecar(monkeypatch):
    from context_manager import mcp_server

    class FakeClient:
        def healthz(self):
            return {"ok": True, "version": "test", "db_path": "/tmp/test.db"}

    monkeypatch.setattr(mcp_server, "_client", lambda: FakeClient())
    server = mcp_server.create_server()
    _content, meta = await server.call_tool("ctx_health", {})
    assert meta["result"] == {"ok": True, "version": "test", "db_path": "/tmp/test.db"}
    resource = await server.read_resource("ctxmgr://health")
    assert "\"ok\": true" in resource[0].content
