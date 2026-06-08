"""CompressEngine — pure, stateless transforms over message lists.

No I/O, no DB, no LLM. All functions take and return plain dicts
(OpenAI chat-completions message format) so they work with any backend
that uses the standard role/content/tool_calls shape.

Design principles:
- Immutability: inputs are never mutated; return new lists.
- Totality: every function returns something sensible for any valid input.
- Determinism: same inputs → same output (canonical JSON for dedup).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any, Dict, List, Optional, Set, Tuple

from .config import DCPProtectionsConfig
from .placeholders import Placeholder


@dataclass
class EngineError:
    """Structured error returned by engine operations (not raised)."""
    code: str
    detail: str


@dataclass
class CompressResult:
    messages: List[Dict[str, Any]]
    placeholder_summary: str  # what was written to PlaceholderStore
    compressed_ids: List[str]  # store message ids that were replaced
    error: Optional[EngineError] = None


# ── Protected-content helpers ─────────────────────────────────────────────────

def _tool_names_in_call(msg: Dict[str, Any]) -> Set[str]:
    """Return the set of tool names invoked by an assistant message."""
    calls = msg.get("tool_calls")
    if not calls:
        return set()
    if isinstance(calls, str):
        try:
            calls = json.loads(calls)
        except Exception:
            return set()
    if not isinstance(calls, list):
        return set()
    names: Set[str] = set()
    for c in calls:
        if isinstance(c, dict):
            fn = c.get("function") or {}
            name = fn.get("name") if isinstance(fn, dict) else None
            if name:
                names.add(str(name))
    return names


def _file_path_in_call(msg: Dict[str, Any]) -> Optional[str]:
    """Extract the primary file path from a tool-call message's arguments."""
    calls = msg.get("tool_calls")
    if not calls:
        return None
    if isinstance(calls, str):
        try:
            calls = json.loads(calls)
        except Exception:
            return None
    if not isinstance(calls, list) or not calls:
        return None
    first_call = calls[0] if isinstance(calls[0], dict) else {}
    fn = first_call.get("function") or {}
    if not isinstance(fn, dict):
        return None
    args_raw = fn.get("arguments")
    if not args_raw:
        return None
    try:
        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
    except Exception:
        return None
    if not isinstance(args, dict):
        return None
    for key in ("file_path", "path", "filePath", "filename"):
        val = args.get(key)
        if val and isinstance(val, str):
            return val
    return None


def _is_protected(
    msg: Dict[str, Any],
    protections: DCPProtectionsConfig,
    protected_tool_names: Set[str],
    protected_call_ids: Set[str],
) -> bool:
    """Return True if this message must not be replaced."""
    role = msg.get("role", "")

    if role == "user" and protections.protect_user_messages:
        return True

    if role == "assistant":
        call_names = _tool_names_in_call(msg)
        if call_names & protected_tool_names:
            return True
        # Check file-path globs
        fp = _file_path_in_call(msg)
        if fp and any(fnmatch(fp, g) for g in protections.file_globs):
            return True

    if role == "tool":
        cid = msg.get("tool_call_id")
        if cid and str(cid) in protected_call_ids:
            return True

    return False


def _build_protected_sets(
    messages: List[Dict[str, Any]],
    protections: DCPProtectionsConfig,
) -> Tuple[Set[str], Set[str]]:
    """Pre-compute sets of protected tool names and call_ids for O(1) lookup."""
    protected_tool_names = set(protections.tool_names)
    protected_call_ids: Set[str] = set()
    for msg in messages:
        if msg.get("role") == "assistant":
            names = _tool_names_in_call(msg)
            if names & protected_tool_names:
                # All call_ids in this message are protected
                calls = msg.get("tool_calls")
                if isinstance(calls, str):
                    try:
                        calls = json.loads(calls)
                    except Exception:
                        calls = []
                if isinstance(calls, list):
                    for c in calls:
                        if isinstance(c, dict) and c.get("id"):
                            protected_call_ids.add(str(c["id"]))
    return protected_tool_names, protected_call_ids


def _msg_id(msg: Dict[str, Any]) -> Optional[str]:
    """Return the message's store id (set by adapters from the DB rowid)."""
    return str(msg["_ctx_id"]) if msg.get("_ctx_id") is not None else None


# ── Range compression ─────────────────────────────────────────────────────────

def apply_range_compress(
    messages: List[Dict[str, Any]],
    start_id: str,
    end_id: str,
    summary: str,
    *,
    protections: DCPProtectionsConfig,
    existing_placeholders: List[Placeholder],
) -> CompressResult:
    """Replace messages in [start_id, end_id] with a placeholder summary.

    Protected messages within the range are appended to the summary as
    labelled blocks rather than dropped (up to protected_append_budget bytes).

    Returns CompressResult with error set if the range is invalid.
    """
    protected_tool_names, protected_call_ids = _build_protected_sets(messages, protections)

    # Validate: start ≤ end
    try:
        if int(start_id) > int(end_id):
            return CompressResult(
                messages=list(messages),
                placeholder_summary=summary,
                compressed_ids=[],
                error=EngineError("invalid_range", f"start_id {start_id} > end_id {end_id}"),
            )
    except (ValueError, TypeError):
        if start_id > end_id:
            return CompressResult(
                messages=list(messages),
                placeholder_summary=summary,
                compressed_ids=[],
                error=EngineError("invalid_range", f"start_id {start_id!r} > end_id {end_id!r}"),
            )

    # Validate: no partial overlap with existing placeholders
    for ph in existing_placeholders:
        if ph.kind != "range" or not ph.span_start or not ph.span_end:
            continue
        # [ph.span_start, ph.span_end] vs [start_id, end_id]
        try:
            ph_s, ph_e = int(ph.span_start), int(ph.span_end)
            r_s, r_e = int(start_id), int(end_id)
        except (ValueError, TypeError):
            continue
        # Fully nested (ph ⊆ new range) → OK (will embed)
        # Disjoint → OK
        # Partial overlap → reject
        nested = r_s <= ph_s and ph_e <= r_e
        disjoint = ph_e < r_s or r_e < ph_s
        if not nested and not disjoint:
            return CompressResult(
                messages=list(messages),
                placeholder_summary=summary,
                compressed_ids=[],
                error=EngineError(
                    "partial_overlap",
                    f"ranges must be disjoint or fully nest existing placeholder #{ph.id}",
                ),
            )

    # Two-pass approach:
    # Pass 1 — scan in-range messages to collect protected blocks and compressed ids.
    protected_blocks: List[str] = []
    compressed_ids: List[str] = []
    protected_budget_remaining = protections.protected_append_budget
    found_any = False

    for msg in messages:
        mid = _msg_id(msg)
        in_range = False
        if mid is not None:
            try:
                in_range = int(start_id) <= int(mid) <= int(end_id)
            except (ValueError, TypeError):
                in_range = start_id <= mid <= end_id
        if not in_range:
            continue
        found_any = True
        if _is_protected(msg, protections, protected_tool_names, protected_call_ids):
            role = msg.get("role", "?")
            content = msg.get("content") or ""
            if not isinstance(content, str):
                content = json.dumps(content)
            block = f"--- protected ({role}, id={mid}) ---\n{content}"
            block_bytes = block.encode()
            if protected_budget_remaining >= len(block_bytes):
                protected_blocks.append(block)
                protected_budget_remaining -= len(block_bytes)
            else:
                protected_blocks.append(f"[protected output omitted, see message #{mid}]")
        else:
            if mid is not None:
                compressed_ids.append(mid)

    if not found_any:
        return CompressResult(
            messages=list(messages),
            placeholder_summary=summary,
            compressed_ids=[],
            error=EngineError("range_not_found", f"no messages found in [{start_id}, {end_id}]"),
        )

    full_summary = _build_full_summary(summary, protected_blocks)
    placeholder_msg = _make_placeholder_message(full_summary, start_id, end_id)

    # Pass 2 — rebuild the message list: replace the entire range with the placeholder.
    out_messages: List[Dict[str, Any]] = []
    placeholder_inserted = False

    for msg in messages:
        mid = _msg_id(msg)
        in_range = False
        if mid is not None:
            try:
                in_range = int(start_id) <= int(mid) <= int(end_id)
            except (ValueError, TypeError):
                in_range = start_id <= mid <= end_id

        if in_range:
            if not placeholder_inserted:
                out_messages.append(placeholder_msg)
                placeholder_inserted = True
            # Drop the original in-range message (represented by placeholder).
            continue

        out_messages.append(msg)

    return CompressResult(
        messages=out_messages,
        placeholder_summary=full_summary,
        compressed_ids=compressed_ids,
    )


def apply_message_compress(
    messages: List[Dict[str, Any]],
    msg_ids: List[str],
    summary: str,
    *,
    protections: DCPProtectionsConfig,
) -> CompressResult:
    """Replace specific messages (non-contiguous) with a placeholder summary."""
    id_set = set(str(i) for i in msg_ids)
    protected_tool_names, protected_call_ids = _build_protected_sets(messages, protections)

    out_messages: List[Dict[str, Any]] = []
    compressed_ids: List[str] = []
    placeholder_inserted = False
    found_ids: Set[str] = set()

    for msg in messages:
        mid = _msg_id(msg)
        if mid is None or mid not in id_set:
            out_messages.append(msg)
            continue

        found_ids.add(mid)
        if not _is_protected(msg, protections, protected_tool_names, protected_call_ids):
            compressed_ids.append(mid)

        if not placeholder_inserted:
            out_messages.append(_make_placeholder_message(summary, None, None))
            placeholder_inserted = True

    missing = id_set - found_ids
    error = None
    if missing:
        error = EngineError("missing_ids", f"ids not found: {sorted(missing)}")

    return CompressResult(
        messages=out_messages,
        placeholder_summary=summary,
        compressed_ids=compressed_ids,
        error=error,
    )


# ── Middleware: apply active placeholders to an outbound message list ─────────

def apply_placeholders(
    messages: List[Dict[str, Any]],
    placeholders: List[Placeholder],
) -> List[Dict[str, Any]]:
    """Substitute active placeholders into a message list.

    Processes active placeholders in creation order (id ascending, which is
    how PlaceholderStore.active_for returns them). Nested placeholders were
    deactivated at compress time, so they never appear in the active list.

    Idempotent: calling twice produces the same result.
    """
    if not placeholders:
        return list(messages)

    out: List[Dict[str, Any]] = []
    placeholder_inserted: Set[int] = set()  # ph.id values already emitted

    for msg in messages:
        mid = _msg_id(msg)
        new_ph = None     # a placeholder to insert *before* this message
        drop_msg = False  # whether to drop this message (covered by a placeholder)

        if mid is not None:
            for ph in placeholders:
                if ph.covers(mid):
                    if ph.id not in placeholder_inserted:
                        # First covered message → emit placeholder here
                        new_ph = ph
                    # Either way, the original message is replaced — drop it
                    drop_msg = True
                    break

        if new_ph is not None:
            out.append(_make_placeholder_message(
                new_ph.summary,
                new_ph.span_start,
                new_ph.span_end,
            ))
            placeholder_inserted.add(new_ph.id)

        if not drop_msg:
            out.append(msg)

    return out


# ── Tool-call deduplication ───────────────────────────────────────────────────

def dedupe_tool_calls(
    messages: List[Dict[str, Any]],
    *,
    protections: DCPProtectionsConfig,
) -> List[Dict[str, Any]]:
    """Keep the latest occurrence of each (tool_name, canonical_args) pair.

    Earlier duplicates are replaced with a stub referencing the kept message.
    Protected tool names are exempt.
    Runs in O(n) by scanning once to find last occurrences, then scanning again
    to emit.
    """
    protected_names = set(protections.tool_names)

    # First pass: find the last message index for each canonical key
    last_index: Dict[Tuple[str, str], int] = {}
    call_id_to_canonical: Dict[str, Tuple[str, str]] = {}

    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        for name, canonical_args, call_id in _iter_call_canonical(msg, protected_names):
            key = (name, canonical_args)
            last_index[key] = i
            if call_id:
                call_id_to_canonical[call_id] = key

    # Second pass: emit, replacing earlier duplicates
    out: List[Dict[str, Any]] = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            out.append(msg)
            continue
        calls = _parse_tool_calls(msg)
        if calls is None:
            out.append(msg)
            continue
        new_calls = []
        changed = False
        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name") if isinstance(fn, dict) else None
            if not name or name in protected_names:
                new_calls.append(call)
                continue
            canonical_args = _canonical_json(fn.get("arguments"))
            key = (name, canonical_args)
            if last_index.get(key, i) != i:
                # This is not the last occurrence — replace with stub
                stub = dict(call)
                stub_fn = dict(fn)
                stub_fn["arguments"] = json.dumps(
                    {"_deduped": f"see message #{last_index[key]}"}
                )
                stub["function"] = stub_fn
                new_calls.append(stub)
                changed = True
            else:
                new_calls.append(call)
        if changed:
            new_msg = dict(msg)
            new_msg["tool_calls"] = new_calls
            out.append(new_msg)
        else:
            out.append(msg)

    return out


def _iter_call_canonical(msg: Dict[str, Any], protected_names: Set[str]):
    """Yield (name, canonical_args_str, call_id) for each non-protected tool call."""
    calls = _parse_tool_calls(msg)
    if not calls:
        return
    for call in calls:
        fn = call.get("function") or {}
        name = fn.get("name") if isinstance(fn, dict) else None
        if not name or name in protected_names:
            continue
        canonical_args = _canonical_json(fn.get("arguments"))
        call_id = call.get("id")
        yield name, canonical_args, call_id


# ── Error-input purging ───────────────────────────────────────────────────────

def purge_errored_inputs(
    messages: List[Dict[str, Any]],
    *,
    turn_threshold: int,
    protections: DCPProtectionsConfig,
    now_turn: int,
) -> List[Dict[str, Any]]:
    """Replace arguments of errored tool calls that are older than turn_threshold turns.

    "Error" is detected by looking for a matching tool-result message with
    is_error=True in its metadata or a content that contains typical error
    markers.  Keeps the error message itself; only the large args blob is slimmed.
    """
    protected_names = set(protections.tool_names)

    # Find call_ids whose results indicate error, and their first-seen turn
    error_call_ids: Dict[str, int] = {}  # call_id -> turn number when seen
    turn = 0
    for msg in messages:
        if msg.get("role") == "user":
            turn += 1
        if msg.get("role") == "tool":
            cid = msg.get("tool_call_id")
            meta = msg.get("metadata") or {}
            is_error = meta.get("is_error") or _content_looks_like_error(msg.get("content"))
            if is_error and cid:
                error_call_ids[str(cid)] = turn

    # Build set of call_ids eligible for purging (old enough + not protected)
    purgeable: Set[str] = {
        cid
        for cid, t in error_call_ids.items()
        if now_turn - t >= turn_threshold
    }

    # Build map of protected call_ids
    _, protected_call_ids = _build_protected_sets(messages, protections)
    purgeable -= protected_call_ids

    if not purgeable:
        return list(messages)

    out: List[Dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") != "assistant":
            out.append(msg)
            continue
        calls = _parse_tool_calls(msg)
        if not calls:
            out.append(msg)
            continue
        new_calls = []
        changed = False
        for call in calls:
            cid = call.get("id")
            if cid and str(cid) in purgeable:
                fn = call.get("function") or {}
                name = fn.get("name") if isinstance(fn, dict) else None
                if name in protected_names:
                    new_calls.append(call)
                    continue
                orig_args = (fn.get("arguments") or "")[:200]
                stub_fn = dict(fn)
                stub_fn["arguments"] = json.dumps(
                    {"_purged": f"error after {turn_threshold} turns; args: {orig_args}"}
                )
                stub = dict(call)
                stub["function"] = stub_fn
                new_calls.append(stub)
                changed = True
            else:
                new_calls.append(call)
        if changed:
            new_msg = dict(msg)
            new_msg["tool_calls"] = new_calls
            out.append(new_msg)
        else:
            out.append(msg)

    return out


# ── Nudge injection ───────────────────────────────────────────────────────────

_NUDGE_TEXT = (
    "[context-manager DCP] Context is filling up. "
    "If there is a closed portion of the conversation (finished research, "
    "completed sub-task, resolved error) that is unlikely to be needed immediately, "
    "call the `compress` tool to summarise it now. "
    "Each visible message is prefixed with `[#N]` where N is its message id; "
    "pass these ids as `start_message_id` and `end_message_id` to compress a range. "
    "Write a high-fidelity technical summary covering: Goal, Progress, "
    "Key Decisions, Next Steps, Relevant Files."
)


def maybe_inject_nudge(
    messages: List[Dict[str, Any]],
    *,
    fill_ratio: float,
    turns_since_compress: int,
    cooldown_turns: int,
    repeat_every_turns: int,
    fill_threshold: float,
) -> List[Dict[str, Any]]:
    """Append a nudge system message when context is filling and cooldown allows."""
    if fill_ratio < fill_threshold:
        return list(messages)
    if turns_since_compress < cooldown_turns:
        return list(messages)
    if turns_since_compress > cooldown_turns and (
        (turns_since_compress - cooldown_turns) % repeat_every_turns != 0
    ):
        return list(messages)
    nudge: Dict[str, Any] = {
        "role": "system",
        "content": _NUDGE_TEXT,
        "_dcp_nudge": True,
    }
    return list(messages) + [nudge]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_placeholder_message(
    summary: str,
    start_id: Optional[str],
    end_id: Optional[str],
) -> Dict[str, Any]:
    span = f"[{start_id}–{end_id}]" if start_id and end_id else "[selected]"
    return {
        "role": "system",
        "content": (
            f"[DCP placeholder {span}]\n"
            f"{summary}"
        ),
        "_dcp_placeholder": True,
    }


def _build_full_summary(summary: str, protected_blocks: List[str]) -> str:
    if not protected_blocks:
        return summary
    blocks_text = "\n\n".join(protected_blocks)
    return f"{summary}\n\n--- protected context ---\n{blocks_text}"


def _parse_tool_calls(msg: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    calls = msg.get("tool_calls")
    if not calls:
        return None
    if isinstance(calls, str):
        try:
            calls = json.loads(calls)
        except Exception:
            return None
    if not isinstance(calls, list):
        return None
    return calls


def _canonical_json(value: Any) -> str:
    """Produce a stable, whitespace-free JSON string for dedup comparison."""
    if value is None:
        return "null"
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        except Exception:
            return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _content_looks_like_error(content: Any) -> bool:
    if not content:
        return False
    text = content if isinstance(content, str) else json.dumps(content)
    return bool(re.search(r"\b(error|Error|exception|Exception|traceback|Traceback)\b", text))
