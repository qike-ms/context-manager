"""MCP server exposing context-manager sidecar tools.

This is intentionally a lossy integration for hosts such as Claude Code and
Goose: MCP tools can call into the sidecar, but they cannot replace the host's
native outbound message list.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .sidecar_client import SidecarClient


def _client() -> SidecarClient:
    return SidecarClient()


def create_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - install concern
        raise RuntimeError(
            "MCP dependencies are not installed; install with `pip install context-manager[mcp]`"
        ) from exc

    server = FastMCP("context-manager")

    @server.tool()
    def ctx_health() -> Dict[str, Any]:
        """Check whether the context-manager sidecar is reachable."""
        return _client().healthz()

    @server.tool()
    def ctx_append(
        session_id: str,
        role: str,
        content: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Append a turn to the sidecar-owned ContextStore."""
        return _client().append(
            session_id,
            role=role,
            content=content,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            metadata=metadata,
        )

    @server.tool()
    def ctx_build_outbound(
        session_id: str,
        recent_n: int = 200,
        fill_ratio: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Build a DCP-transformed context snapshot.

        Note: MCP hosts must explicitly use the returned messages; this tool
        cannot replace the host's native provider request automatically.
        """
        return _client().build_outbound(session_id, recent_n=recent_n, fill_ratio=fill_ratio)

    @server.tool()
    def compress(
        session_id: str,
        summary: str,
        mode: str = "range",
        start_message_id: Optional[str] = None,
        end_message_id: Optional[str] = None,
        message_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Compress a closed/stale context span using context-manager DCP."""
        return _client().compress(
            session_id,
            mode=mode,
            start_message_id=start_message_id,
            end_message_id=end_message_id,
            message_ids=message_ids,
            summary=summary,
        )

    @server.tool()
    def ctx_usage(session_id: str) -> Dict[str, Any]:
        """Return token usage for a sidecar session."""
        return _client().usage(session_id)

    @server.tool()
    def ctx_set_model(session_id: str, model: Optional[str] = None) -> Dict[str, Any]:
        """Set the model name used for token-window accounting."""
        return _client().set_model(session_id, model)

    @server.tool()
    def ctx_list_placeholders(session_id: str) -> List[Dict[str, Any]]:
        """List active/inactive DCP placeholders for a session."""
        return _client().placeholders(session_id)

    @server.tool()
    def ctx_deactivate_placeholder(session_id: str, placeholder_id: int) -> Dict[str, Any]:
        """Deactivate a DCP placeholder so future outbound builds show verbatim context."""
        return _client().deactivate_placeholder(session_id, placeholder_id)

    @server.tool()
    def ctx_parent_summary(session_id: str) -> Dict[str, Any]:
        """Return a direct parent summary for opt-in subagent inheritance."""
        return _client().parent_summary(session_id)

    @server.resource("ctxmgr://health", mime_type="application/json")
    def health_resource() -> Dict[str, Any]:
        """Sidecar health as an MCP resource."""
        return _client().healthz()

    @server.resource("ctxmgr://sessions/{session_id}/usage", mime_type="application/json")
    def usage_resource(session_id: str) -> Dict[str, Any]:
        """Token usage for one sidecar session."""
        return _client().usage(session_id)

    @server.resource("ctxmgr://sessions/{session_id}/placeholders", mime_type="application/json")
    def placeholders_resource(session_id: str) -> List[Dict[str, Any]]:
        """DCP placeholder history for one sidecar session."""
        return _client().placeholders(session_id)

    @server.resource("ctxmgr://sessions/{session_id}/parent_summary", mime_type="application/json")
    def parent_summary_resource(session_id: str) -> Dict[str, Any]:
        """Direct parent summary for opt-in subagent inheritance."""
        return _client().parent_summary(session_id)

    return server


def main() -> int:
    create_server().run(transport="stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
