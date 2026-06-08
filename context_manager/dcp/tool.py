"""CompressTool — the `compress` tool schema and invocation handler.

The model calls this tool to request compression.  The handler:
1. Validates the arguments.
2. Calls CompressEngine pure functions.
3. Writes the Placeholder to PlaceholderStore.
4. Runs dedup + error-purge on the resulting message list.
5. Returns a short ToolResult string the model sees.

This module has no I/O beyond SQLite writes to PlaceholderStore.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .config import DCPConfig
from .engine import (
    apply_message_compress,
    apply_range_compress,
    dedupe_tool_calls,
    purge_errored_inputs,
    _msg_id,
)
from .placeholders import Placeholder, PlaceholderStore


def _estimate_tokens_marginal(messages: List[Dict[str, Any]]) -> int:
    """Marginal token estimate for a list of message dicts (no overhead)."""
    try:
        from context_manager.token_estimator import estimate_messages_tokens
        return estimate_messages_tokens(messages, backend="default", include_overhead=False)
    except Exception:
        # Fallback: sum char/4 across all content strings
        total = 0
        for m in messages:
            c = m.get("content") or ""
            total += max(1, len(str(c)) // 4)
        return total


def _count_items(messages: List[Dict[str, Any]]) -> tuple:
    """Return (n_messages, n_tool_pairs) for compressed messages."""
    tool_call_ids = set()
    tool_result_ids = set()
    n_text = 0
    for m in messages:
        role = m.get("role", "")
        if role in ("user", "assistant"):
            if not m.get("tool_calls"):
                n_text += 1
            else:
                # count tool call ids
                calls = m.get("tool_calls") or []
                if isinstance(calls, str):
                    import json as _j
                    try: calls = _j.loads(calls)
                    except Exception: calls = []
                for c in (calls if isinstance(calls, list) else []):
                    if isinstance(c, dict) and c.get("id"):
                        tool_call_ids.add(c["id"])
        elif role == "tool":
            cid = m.get("tool_call_id")
            if cid:
                tool_result_ids.add(cid)
    n_pairs = len(tool_call_ids & tool_result_ids)
    return n_text, n_pairs


TOOL_NAME = "compress"

TOOL_DESCRIPTION = (
    "Compress a closed or stale portion of the conversation into a high-fidelity "
    "technical summary to reclaim context-window space. "
    "Use when a research phase, sub-task, or error-resolution chain is fully complete "
    "and is unlikely to be needed verbatim. "
    "Write the summary yourself: it must cover Goal, Progress, Key Decisions, "
    "Next Steps (if any), and Relevant Files."
)

# OpenAI-style tool schema for the compress tool (range mode).
TOOL_SCHEMA_OPENAI = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": TOOL_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["range", "message"],
                    "description": (
                        "'range' compresses a contiguous span [start_message_id, end_message_id]. "
                        "'message' compresses a non-contiguous list of message_ids."
                    ),
                    "default": "range",
                },
                "start_message_id": {
                    "type": "string",
                    "description": "(range mode) Store id of the first message to compress.",
                },
                "end_message_id": {
                    "type": "string",
                    "description": "(range mode) Store id of the last message to compress.",
                },
                "message_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "(message mode) List of store message ids to compress.",
                },
                "summary": {
                    "type": "string",
                    "description": (
                        "High-fidelity technical summary.  Must cover: "
                        "Goal, Progress, Key Decisions, Next Steps, Relevant Files."
                    ),
                },
            },
            "required": ["summary"],
        },
    },
}


@dataclass
class InvokeResult:
    """Result of a CompressTool.invoke call."""
    tool_result_text: str       # The string the model sees as the tool's return value.
    messages: List[Dict[str, Any]]  # Updated message list (placeholder substituted).
    placeholder: Optional[Placeholder]  # The stored placeholder, or None on error.
    error: Optional[str] = None
    # Token accounting (estimated, marginal — no overhead).
    tokens_removed: Optional[int] = None   # tokens in the compressed head
    tokens_summary: Optional[int] = None   # tokens in the summary text
    items_messages: int = 0                # number of non-tool messages compressed
    items_tools: int = 0                   # number of tool call/result pairs compressed


class CompressTool:
    """Handler for the model-invoked `compress` tool."""

    name = TOOL_NAME
    description = TOOL_DESCRIPTION
    schema_openai = TOOL_SCHEMA_OPENAI

    def invoke(
        self,
        args: Dict[str, Any],
        *,
        session_id: str,
        messages: List[Dict[str, Any]],
        ph_store: PlaceholderStore,
        config: DCPConfig,
        current_turn: int = 0,
    ) -> InvokeResult:
        """Validate args, run engine, write placeholder, apply dedup/purge."""
        mode = args.get("mode", "range")
        summary = args.get("summary", "").strip()

        if not summary:
            return InvokeResult(
                tool_result_text='{"error": "summary must not be empty"}',
                messages=list(messages),
                placeholder=None,
                error="empty_summary",
            )

        active_phs = ph_store.active_for(session_id)

        if mode == "range":
            start_id = str(args.get("start_message_id", "")).strip()
            end_id = str(args.get("end_message_id", "")).strip()
            if not start_id or not end_id:
                return InvokeResult(
                    tool_result_text=(
                        '{"error": "range mode requires start_message_id and end_message_id"}'
                    ),
                    messages=list(messages),
                    placeholder=None,
                    error="missing_range_ids",
                )

            result = apply_range_compress(
                messages,
                start_id,
                end_id,
                summary,
                protections=config.protections,
                existing_placeholders=active_phs,
            )

            if result.error:
                return InvokeResult(
                    tool_result_text=json.dumps({"error": result.error.detail}),
                    messages=list(messages),
                    placeholder=None,
                    error=result.error.code,
                )

            # Check for existing placeholders fully nested inside this range
            # and deactivate them (they are now embedded in the new summary).
            nested_ids = _find_nested(active_phs, start_id, end_id)
            for nid in nested_ids:
                ph_store.deactivate(nid)

            nested_in = None  # top-level compress
            ph = ph_store.add_range(
                session_id,
                start_id,
                end_id,
                result.placeholder_summary,
                nested_in_id=nested_in,
            )
            out_text = (
                f"compressed messages {start_id}–{end_id} into placeholder #{ph.id} "
                f"({len(result.compressed_ids)} messages removed from active context)"
            )

        elif mode == "message":
            ids_raw = args.get("message_ids")
            if not ids_raw or not isinstance(ids_raw, list):
                return InvokeResult(
                    tool_result_text='{"error": "message mode requires message_ids list"}',
                    messages=list(messages),
                    placeholder=None,
                    error="missing_message_ids",
                )
            msg_ids = [str(i) for i in ids_raw]
            result = apply_message_compress(
                messages,
                msg_ids,
                summary,
                protections=config.protections,
            )
            ph = ph_store.add_message(session_id, msg_ids, result.placeholder_summary)
            out_text = (
                f"compressed {len(result.compressed_ids)} messages into placeholder #{ph.id}"
            )
            if result.error:
                out_text += f" (warning: {result.error.detail})"

        else:
            return InvokeResult(
                tool_result_text=json.dumps({"error": f"unknown mode: {mode!r}"}),
                messages=list(messages),
                placeholder=None,
                error="unknown_mode",
            )

        # Run dedup + error-purge on the post-placeholder message list
        updated = result.messages
        if config.dedupe.enabled:
            updated = dedupe_tool_calls(updated, protections=config.protections)
        if config.purge_errors.enabled:
            updated = purge_errored_inputs(
                updated,
                turn_threshold=config.purge_errors.after_turns,
                protections=config.protections,
                now_turn=current_turn,
            )

        # Token accounting for the notification banner.
        compressed_msgs = [m for m in messages if _msg_id(m) in set(result.compressed_ids)]
        tokens_removed = _estimate_tokens_marginal(compressed_msgs)
        tokens_summary = _estimate_tokens_marginal(
            [{"role": "system", "content": result.placeholder_summary}]
        )
        n_msgs, n_tools = _count_items(compressed_msgs)

        return InvokeResult(
            tool_result_text=out_text,
            messages=updated,
            placeholder=ph,
            tokens_removed=tokens_removed,
            tokens_summary=tokens_summary,
            items_messages=n_msgs,
            items_tools=n_tools,
        )


def _find_nested(
    existing: List[Placeholder],
    new_start: str,
    new_end: str,
) -> List[int]:
    """Return ids of existing placeholders fully contained within [new_start, new_end]."""
    nested: List[int] = []
    try:
        ns, ne = int(new_start), int(new_end)
    except (ValueError, TypeError):
        return nested
    for ph in existing:
        if ph.id is None or ph.kind != "range" or not ph.span_start or not ph.span_end:
            continue
        try:
            ps, pe = int(ph.span_start), int(ph.span_end)
        except (ValueError, TypeError):
            continue
        if ns <= ps and pe <= ne:
            nested.append(ph.id)
    return nested
