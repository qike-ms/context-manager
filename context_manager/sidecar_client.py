"""Small synchronous client for the local context-manager sidecar."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .sidecar import default_socket_path


class SidecarClient:
    """HTTP-over-Unix-socket client used by adapters and MCP tools."""

    def __init__(self, socket_path: Optional[str] = None) -> None:
        self.socket_path = str(Path(socket_path).expanduser()) if socket_path else str(default_socket_path())

    def request(self, method: str, path: str, json: Optional[Dict[str, Any]] = None) -> Any:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - install concern
            raise RuntimeError(
                "httpx is required for sidecar clients; install context-manager[sidecar] or context-manager[mcp]"
            ) from exc

        transport = httpx.HTTPTransport(uds=self.socket_path)
        with httpx.Client(transport=transport, base_url="http://ctxmgr", timeout=30.0) as client:
            response = client.request(method, path, json=json)
            response.raise_for_status()
            return response.json()

    def healthz(self) -> Dict[str, Any]:
        return self.request("GET", "/v1/healthz")

    def append(self, session_id: str, **message: Any) -> Dict[str, Any]:
        return self.request("POST", f"/v1/sessions/{session_id}/append", json=message)

    def build_outbound(
        self,
        session_id: str,
        recent_n: int = 200,
        fill_ratio: Optional[float] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"recent_n": recent_n}
        if fill_ratio is not None:
            body["fill_ratio"] = fill_ratio
        return self.request("POST", f"/v1/sessions/{session_id}/build_outbound", json=body)

    def compress(self, session_id: str, **args: Any) -> Dict[str, Any]:
        return self.request("POST", f"/v1/sessions/{session_id}/compress", json=args)

    def usage(self, session_id: str) -> Dict[str, Any]:
        return self.request("GET", f"/v1/sessions/{session_id}/usage")

    def set_model(self, session_id: str, model: Optional[str]) -> Dict[str, Any]:
        return self.request("POST", f"/v1/sessions/{session_id}/set_model", json={"model": model})

    def placeholders(self, session_id: str) -> Any:
        return self.request("GET", f"/v1/sessions/{session_id}/placeholders")

    def deactivate_placeholder(self, session_id: str, placeholder_id: int) -> Dict[str, Any]:
        return self.request("POST", f"/v1/sessions/{session_id}/placeholders/{placeholder_id}/deactivate")

    def parent_summary(self, session_id: str) -> Dict[str, Any]:
        return self.request("GET", f"/v1/sessions/{session_id}/parent_summary")
