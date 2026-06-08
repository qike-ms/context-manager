"""DCPMiddleware — coordinates DCP state for a session and builds outbound payloads.

This is the main entry point for agent-dispatcher integration.  One
DCPMiddleware instance lives alongside the ContextStore; it owns the
PlaceholderStore for a session and tracks per-session turn counters.

Typical call sequence per outbound request:

    middleware = DCPMiddleware(conn, config)
    msgs = store.get_recent(sid, ...)           # or assemble_context
    msgs_with_ids = middleware.tag_ctx_ids(msgs) # add _ctx_id from store rowid
    outbound = middleware.build_outbound(
        session_id=sid,
        messages=msgs_with_ids,
        fill_ratio=usage.window_pct or 0.0,
    )
    # outbound is a list of standard OpenAI chat-completions dicts,
    # with placeholders substituted and nudge appended when appropriate.

When the model's response includes a `compress` tool call:

    result = middleware.handle_compress(
        session_id=sid,
        messages=msgs_with_ids,      # the same list sent to the model
        args=tool_call_args,
        current_turn=turn_counter,
    )
    # result.tool_result_text → send back to model as tool result
    # result.messages → updated message list (already has placeholder applied)
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, List, Optional

from .config import DCPConfig
from .engine import apply_placeholders, dedupe_tool_calls, maybe_inject_nudge, purge_errored_inputs
from .placeholders import PlaceholderStore
from .tool import CompressTool, InvokeResult, TOOL_SCHEMA_OPENAI


class DCPMiddleware:
    """Per-session DCP coordinator.

    The same instance can be shared across sessions because all mutable
    state is session-keyed via PlaceholderStore.  Per-session ephemeral
    state (turn counters, last compress turn) is tracked in-memory in
    `_session_state`.  This resets on process restart, which is fine:
    the worst effect is a slightly early nudge.
    """

    def __init__(self, conn: sqlite3.Connection, config: DCPConfig) -> None:
        self._ph_store = PlaceholderStore(conn)
        self._config = config
        self._tool = CompressTool()
        # { session_id: {"turn": int, "last_compress_turn": int} }
        self._session_state: Dict[str, Dict[str, int]] = {}

    # ── Outbound ──────────────────────────────────────────────────────────────

    def build_outbound(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        fill_ratio: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """Apply active placeholders, dedup, error-purge, and nudge to messages.

        Args:
            session_id: ContextStore session id.
            messages: Messages as returned by ContextStore, tagged with _ctx_id.
            fill_ratio: active_tokens / window_size (0.0–1.0). Used for nudge trigger.

        Returns:
            A new list of message dicts safe to send to the provider.
            _ctx_id and _dcp_* keys are stripped before return.
        """
        if not self._config.enabled:
            return _strip_private_keys(messages)

        active_phs = self._ph_store.active_for(session_id)
        out = apply_placeholders(messages, active_phs)

        if self._config.dedupe.enabled:
            out = dedupe_tool_calls(out, protections=self._config.protections)

        state = self._get_state(session_id)
        if self._config.purge_errors.enabled:
            out = purge_errored_inputs(
                out,
                turn_threshold=self._config.purge_errors.after_turns,
                protections=self._config.protections,
                now_turn=state["turn"],
            )

        turns_since = state["turn"] - state["last_compress_turn"]
        out = maybe_inject_nudge(
            out,
            fill_ratio=fill_ratio,
            turns_since_compress=turns_since,
            cooldown_turns=self._config.nudge.cooldown_turns,
            repeat_every_turns=self._config.nudge.repeat_every_turns,
            fill_threshold=self._config.nudge.context_fill_threshold,
        )

        if self._config.render_ctx_ids:
            out = _render_ctx_ids_inline(out)
        return _strip_private_keys(out)

    def tool_schema(self) -> Dict[str, Any]:
        """Return the OpenAI-format tool schema to inject into the system prompt."""
        return TOOL_SCHEMA_OPENAI

    def note_user_turn(self, session_id: str) -> None:
        """Call once per incoming user message to advance the turn counter."""
        state = self._get_state(session_id)
        state["turn"] += 1

    # ── Inbound: handle model's compress call ─────────────────────────────────

    def handle_compress(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        args: Dict[str, Any],
        current_turn: Optional[int] = None,
    ) -> InvokeResult:
        """Invoke the compress tool, write placeholder, update turn state.

        Args:
            session_id: ContextStore session id.
            messages: Current tagged message list (same as sent to model).
            args: The tool_call arguments dict from the model's response.
            current_turn: Override the internal turn counter (optional).

        Returns:
            InvokeResult with tool_result_text and updated messages.
        """
        state = self._get_state(session_id)
        turn = current_turn if current_turn is not None else state["turn"]

        result = self._tool.invoke(
            args,
            session_id=session_id,
            messages=messages,
            ph_store=self._ph_store,
            config=self._config,
            current_turn=turn,
        )

        if result.error is None:
            state["last_compress_turn"] = turn

        return result

    # ── Placeholder inspection ────────────────────────────────────────────────

    def active_placeholder_count(self, session_id: str) -> int:
        return self._ph_store.count_active(session_id)

    def placeholder_history(self, session_id: str, limit: int = 20):
        return self._ph_store.history_for(session_id, limit=limit)

    def deactivate_placeholder(self, session_id: str, placeholder_id: int) -> None:
        """Revert a placeholder to verbatim.  Next build_outbound shows raw messages."""
        self._ph_store.deactivate(placeholder_id)

    def reactivate_placeholder(self, session_id: str, placeholder_id: int) -> None:
        self._ph_store.reactivate(placeholder_id)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _get_state(self, session_id: str) -> Dict[str, int]:
        if session_id not in self._session_state:
            self._session_state[session_id] = {"turn": 0, "last_compress_turn": -999}
        return self._session_state[session_id]


# ── Utility: tag messages with store row ids ──────────────────────────────────

def tag_ctx_ids(messages) -> List[Dict[str, Any]]:
    """Add _ctx_id to each message dict from the store Message's id field.

    Converts context_manager.store.Message objects to dicts (via to_openai)
    with _ctx_id set from the store row id.  Pass-through for plain dicts
    that already have _ctx_id.
    """
    out = []
    for msg in messages:
        if hasattr(msg, "to_openai"):
            d = msg.to_openai()
            if msg.id is not None:
                d["_ctx_id"] = msg.id
        elif isinstance(msg, dict):
            d = dict(msg)
        else:
            d = dict(msg)
        out.append(d)
    return out


def _strip_private_keys(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove _ctx_id and _dcp_* keys before sending to provider."""
    private = {"_ctx_id", "_dcp_placeholder", "_dcp_nudge"}
    out = []
    for msg in messages:
        if any(k in msg for k in private):
            msg = {k: v for k, v in msg.items() if k not in private}
        out.append(msg)
    return out


def _render_ctx_ids_inline(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Prepend `[#<id>] ` to each message content so the model can reference
    messages by id when calling the compress tool.

    Only messages with a `_ctx_id` key are touched.  Placeholders, system
    messages without an id, and the nudge are left alone.  The content must
    already be a string; messages with non-string (e.g. list/multimodal)
    content are skipped to avoid corrupting structured payloads.
    """
    out: List[Dict[str, Any]] = []
    for msg in messages:
        cid = msg.get("_ctx_id")
        content = msg.get("content")
        if cid is None or not isinstance(content, str):
            out.append(msg)
            continue
        prefixed = f"[#{cid}] {content}"
        new_msg = dict(msg)
        new_msg["content"] = prefixed
        out.append(new_msg)
    return out
