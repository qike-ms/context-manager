"""HTTP sidecar for cross-language access to context-manager DCP.

The sidecar is intentionally thin: one ContextStore, one DCPMiddleware, and
JSON endpoints over a local Unix domain socket. FastAPI/uvicorn are optional
dependencies; importing :mod:`context_manager` does not import this module.
"""

import argparse
import os
import stat
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .dcp import DCPConfig, DCPMiddleware, tag_ctx_ids
from .offload import OffloadPolicy
from .store import ContextStore


def _require_fastapi():
    try:
        from fastapi import FastAPI, HTTPException
        from pydantic import BaseModel, Field
    except ImportError as exc:  # pragma: no cover - exercised by base installs
        raise RuntimeError(
            "context-manager sidecar dependencies are not installed; "
            "install with `pip install context-manager[sidecar]`"
        ) from exc
    return FastAPI, HTTPException, BaseModel, Field


def default_db_path() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
    return base / "ctxmgr" / "ctxmgr.db"


def default_socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        base = Path(runtime)
    else:
        base = Path("~/.local/run").expanduser()
    return Path(os.environ.get("CTXMGR_SOCK", base / "ctxmgr" / "ctxmgr.sock")).expanduser()


def create_app(
    *,
    db_path: Optional[Path] = None,
    offload_root: Optional[Path] = None,
    dcp_config: Optional[DCPConfig] = None,
):
    """Create the FastAPI ASGI app for tests or uvicorn.

    The app owns one ContextStore. Store methods already serialize SQLite
    access with an internal re-entrant lock; DCP placeholder writes use the
    same connection and are called from request handlers without opening a
    second writer process.
    """

    FastAPI, HTTPException, BaseModel, Field = _require_fastapi()

    class AppendRequest(BaseModel):
        role: str
        content: Optional[Any] = None
        tool_name: Optional[str] = None
        tool_calls: Optional[Any] = None
        tool_call_id: Optional[str] = None
        metadata: Optional[Dict[str, Any]] = None

    class BuildOutboundRequest(BaseModel):
        recent_n: int = Field(default=200, ge=1)
        fill_ratio: Optional[float] = None

    class CompressRequest(BaseModel):
        mode: str = "range"
        start_message_id: Optional[str] = None
        end_message_id: Optional[str] = None
        message_ids: Optional[List[str]] = None
        summary: str

    class SetModelRequest(BaseModel):
        model: Optional[str] = None

    store = ContextStore(db_path or default_db_path())
    if offload_root is not None:
        store.set_offload_policy(OffloadPolicy(enabled=True, root_dir=offload_root))
    dcp = DCPMiddleware(store.connection(), dcp_config or DCPConfig.from_env())
    lock = threading.RLock()

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            store.close()

    app = FastAPI(title="context-manager sidecar", version=__version__, lifespan=lifespan)

    @app.get("/v1/healthz")
    def healthz() -> Dict[str, Any]:
        # Exercise the DB connection, not just the router.
        with lock:
            store.connection().execute("SELECT 1").fetchone()
        return {"ok": True, "version": __version__, "db_path": str(store.db_path)}

    @app.post("/v1/sessions/{session_id}/append")
    def append(session_id: str, req: AppendRequest) -> Dict[str, int]:
        content = req.content
        if content is not None and not isinstance(content, str):
            import json

            content = json.dumps(content, ensure_ascii=False)
        with lock:
            mid = store.append(
                session_id,
                req.role,
                content,
                tool_name=req.tool_name,
                tool_calls=req.tool_calls,
                tool_call_id=req.tool_call_id,
                metadata=req.metadata,
            )
            if req.role == "user":
                dcp.note_user_turn(session_id)
        return {"message_id": mid}

    @app.post("/v1/sessions/{session_id}/build_outbound")
    def build_outbound(session_id: str, req: BuildOutboundRequest) -> Dict[str, Any]:
        with lock:
            messages = tag_ctx_ids(store.get_recent(session_id, limit=req.recent_n))
            fill_ratio = req.fill_ratio
            if fill_ratio is None:
                fill_ratio = store.token_usage(session_id).window_pct or 0.0
            out = dcp.build_outbound(session_id, messages, fill_ratio=fill_ratio)
            placeholder_count = dcp.active_placeholder_count(session_id)
        return {
            "messages": out,
            "placeholder_count": placeholder_count,
            "nudge_emitted": any(m.get("role") == "system" and "context-manager DCP" in str(m.get("content", "")) for m in out),
        }

    @app.post("/v1/sessions/{session_id}/compress")
    def compress(session_id: str, req: CompressRequest) -> Dict[str, Any]:
        if hasattr(req, "model_dump"):
            args = req.model_dump(exclude_none=True)
        else:  # pragma: no cover - pydantic v1 compatibility
            args = req.dict(exclude_none=True)
        with lock:
            messages = tag_ctx_ids(store.get_recent(session_id, limit=10000))
            result = dcp.handle_compress(session_id, messages, args)
        if result.error:
            raise HTTPException(status_code=400, detail={"error": result.error, "tool_result_text": result.tool_result_text})
        return {
            "placeholder_id": result.placeholder.id if result.placeholder else None,
            "tool_result_text": result.tool_result_text,
            "tokens_removed": result.tokens_removed,
            "tokens_summary": result.tokens_summary,
            "items_messages": result.items_messages,
            "items_tools": result.items_tools,
        }

    @app.get("/v1/sessions/{session_id}/usage")
    def usage(session_id: str) -> Dict[str, Any]:
        with lock:
            u = store.token_usage(session_id)
        return {
            "active_tokens": u.active_tokens,
            "total_seen": u.total_seen,
            "window_size": u.window_size,
            "window_pct": u.window_pct,
            "calibrated": u.calibrated,
            "missing_estimates": u.missing_estimates,
        }

    @app.post("/v1/sessions/{session_id}/set_model")
    def set_model(session_id: str, req: SetModelRequest) -> Dict[str, bool]:
        with lock:
            store.set_model(session_id, req.model)
        return {"ok": True}

    @app.get("/v1/sessions/{session_id}/placeholders")
    def placeholders(session_id: str) -> List[Dict[str, Any]]:
        out = []
        with lock:
            history = dcp.placeholder_history(session_id, limit=100)
        for ph in history:
            out.append(
                {
                    "id": ph.id,
                    "kind": ph.kind,
                    "span_start": ph.span_start,
                    "span_end": ph.span_end,
                    "msg_ids": ph.msg_ids,
                    "summary_preview": ph.summary[:500],
                    "active": ph.active,
                    "created_at": ph.created_at,
                    "deactivated_at": ph.deactivated_at,
                    "nested_in_id": ph.nested_in_id,
                }
            )
        return out

    @app.post("/v1/sessions/{session_id}/placeholders/{placeholder_id}/deactivate")
    def deactivate_placeholder(session_id: str, placeholder_id: int) -> Dict[str, bool]:
        # session_id is part of the route for client symmetry; placeholder ids
        # are globally unique in the table.
        with lock:
            dcp.deactivate_placeholder(session_id, placeholder_id)
        return {"ok": True}

    @app.get("/v1/sessions/{session_id}/parent_summary")
    def parent_summary(session_id: str) -> Dict[str, Optional[str]]:
        parent_id = _parent_session_id(session_id)
        if not parent_id:
            return {"summary": None}
        with lock:
            summary = store.get_summary(parent_id)
        return {"summary": summary}

    app.state.context_store = store
    app.state.dcp = dcp
    return app


def _parent_session_id(session_id: str) -> Optional[str]:
    marker = ":task:"
    if marker in session_id:
        return session_id.rsplit(marker, 1)[0]
    return None


def _prepare_socket_path(socket_path: Path) -> Path:
    socket_path = socket_path.expanduser()
    parent_exists = socket_path.parent.exists()
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if not parent_exists or socket_path.parent.owner() == Path.home().owner():
        try:
            socket_path.parent.chmod(0o700)
        except PermissionError:
            # /tmp or another caller-supplied parent may be intentionally
            # shared. The default XDG path is still created as 0700.
            pass
    if socket_path.exists():
        mode = socket_path.stat().st_mode
        if stat.S_ISSOCK(mode):
            socket_path.unlink()
        else:
            raise RuntimeError(f"refusing to remove non-socket path: {socket_path}")
    return socket_path


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the context-manager sidecar")
    parser.add_argument("--db", type=Path, default=default_db_path())
    parser.add_argument("--socket", type=Path, default=default_socket_path())
    parser.add_argument("--offload-root", type=Path, default=None)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - CLI env concern
        raise RuntimeError(
            "uvicorn is not installed; install with `pip install context-manager[sidecar]`"
        ) from exc

    socket_path = _prepare_socket_path(args.socket)
    app = create_app(db_path=args.db, offload_root=args.offload_root)
    uvicorn.run(app, uds=str(socket_path), log_level=args.log_level)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
