import importlib.util

import pytest
import pytest_asyncio

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("fastapi") is None,
    reason="sidecar optional dependencies are not installed",
)


@pytest_asyncio.fixture
async def sidecar_client(tmp_path):
    import httpx

    from context_manager.sidecar import create_app

    app = create_app(db_path=tmp_path / "ctx.db", offload_root=tmp_path / "offload")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ctxmgr") as client:
        yield client


@pytest.mark.asyncio
async def test_sidecar_healthz(sidecar_client):
    resp = await sidecar_client.get("/v1/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["db_path"].endswith("ctx.db")


@pytest.mark.asyncio
async def test_sidecar_append_build_compress_and_deactivate(sidecar_client):
    sid = "opencode:repo:main"

    r1 = await sidecar_client.post(
        f"/v1/sessions/{sid}/append",
        json={"role": "user", "content": "research phase"},
    )
    r2 = await sidecar_client.post(
        f"/v1/sessions/{sid}/append",
        json={"role": "assistant", "content": "findings"},
    )
    assert r1.json()["message_id"] == 1
    assert r2.json()["message_id"] == 2

    built = await sidecar_client.post(
        f"/v1/sessions/{sid}/build_outbound",
        json={"recent_n": 20, "fill_ratio": 0.0},
    )
    messages = built.json()["messages"]
    assert messages[0]["content"] == "[#1] research phase"
    assert messages[1]["content"] == "[#2] findings"

    compressed = await sidecar_client.post(
        f"/v1/sessions/{sid}/compress",
        json={
            "mode": "range",
            "start_message_id": "1",
            "end_message_id": "2",
            "summary": "Goal: research. Progress: findings captured.",
        },
    )
    assert compressed.status_code == 200
    ph_id = compressed.json()["placeholder_id"]
    assert ph_id == 1

    built_after = await sidecar_client.post(
        f"/v1/sessions/{sid}/build_outbound",
        json={"recent_n": 20, "fill_ratio": 0.0},
    )
    assert built_after.json()["placeholder_count"] == 1
    assert "DCP placeholder" in built_after.json()["messages"][0]["content"]

    listed = await sidecar_client.get(f"/v1/sessions/{sid}/placeholders")
    assert listed.json()[0]["active"] is True
    assert "Goal: research" in listed.json()[0]["summary_preview"]

    deactivated = await sidecar_client.post(
        f"/v1/sessions/{sid}/placeholders/{ph_id}/deactivate"
    )
    assert deactivated.json() == {"ok": True}

    rebuilt = await sidecar_client.post(
        f"/v1/sessions/{sid}/build_outbound",
        json={"recent_n": 20, "fill_ratio": 0.0},
    )
    assert rebuilt.json()["placeholder_count"] == 0
    assert rebuilt.json()["messages"][0]["content"] == "[#1] research phase"


@pytest.mark.asyncio
async def test_sidecar_usage_model_and_parent_summary(sidecar_client):
    parent = "opencode:repo:main"
    child = "opencode:repo:main:task:1"

    await sidecar_client.post(
        f"/v1/sessions/{parent}/set_model",
        json={"model": "opus-4.7"},
    )
    usage = await sidecar_client.get(f"/v1/sessions/{parent}/usage")
    assert usage.json()["window_size"] > 0

    store = sidecar_client._transport.app.state.context_store
    store.set_summary(parent, "parent summary")

    resp = await sidecar_client.get(f"/v1/sessions/{child}/parent_summary")
    assert resp.json() == {"summary": "parent summary"}


def test_prepare_socket_path_refuses_non_socket(tmp_path):
    from context_manager.sidecar import _prepare_socket_path

    path = tmp_path / "ctxmgr.sock"
    path.write_text("not a socket", encoding="utf-8")
    with pytest.raises(RuntimeError):
        _prepare_socket_path(path)
