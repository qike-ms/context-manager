"""Compactor — continuous background summarizer.

STATUS: STUB. The interface and lifecycle are defined; the actual LLM-call
summarization logic is intentionally not implemented yet — design choices
(prompts, model, throwaway-PoC pruning heuristics) are pending the
compaction-research deliverable (cron 0b0bd07b6e5b / my-ai-skills#5).

Design contract (per architecture.md):
- Continuous background worker, NOT threshold-triggered.
- Per appended turn: score for relevance; enqueue scoreable turns.
- Idle worker runs a local LLM (gemma4:31b on lan-cora via cot-proxy) to
  pre-summarize old turns.
- ALWAYS keep last KEEP_VERBATIM_N messages untouched (no "Dory" surprises).
- Mission-aware: on mission change, re-score retained context.
- Throwaway-PoC pruning: detect dead branches and replace with terse
  conclusion. Like Claude Code's /rewind but automatic.

This stub exposes the wiring so the dispatcher can be built against the final
API today; turn on `enabled=False` (default) until the real logic lands.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional, Sequence, Set

from .store import ContextStore, Message

log = logging.getLogger(__name__)


SummarizeFn = Callable[[List[Message], Optional[str]], Awaitable[str]]
"""Pluggable LLM call: (messages, prior_summary) -> new summary text."""


@dataclass
class CompactorConfig:
    enabled: bool = False
    keep_verbatim_n: int = 20
    idle_interval_sec: float = 30.0
    min_messages_to_summarize: int = 40
    delete_summarized: bool = False
    keep_verbatim_tokens: Optional[int] = None
    keep_verbatim_window_ratio: Optional[float] = None


@dataclass
class TailSelection:
    head: List[Message]
    tail: List[Message]
    strategy: str
    token_budget: Optional[int] = None
    tail_tokens: Optional[int] = None
    fallback_reason: Optional[str] = None
    stopped_reason: Optional[str] = None


@dataclass
class _MessageUnit:
    messages: List[Message]
    complete_tool_history: bool
    start_index: int

    @property
    def token_estimate(self) -> Optional[int]:
        total = 0
        for msg in self.messages:
            est = msg.token_estimate
            if est is None:
                return None
            total += int(est)
        return total


class Compactor:
    """Continuous background context compactor.

    Lifecycle:
        c = Compactor(store, summarize_fn=my_llm)
        await c.start()                 # spawns the worker
        c.note_append(session_id)       # call after each ContextStore.append
        c.on_mission_change(session_id) # call when mission shifts
        await c.stop()
    """

    def __init__(
        self,
        store: ContextStore,
        summarize_fn: Optional[SummarizeFn] = None,
        config: Optional[CompactorConfig] = None,
    ):
        self.store = store
        self.summarize_fn = summarize_fn
        self.config = config or CompactorConfig()
        self._queue: Optional[asyncio.Queue[str]] = None
        self._task: Optional[asyncio.Task] = None
        self._stopping: Optional[asyncio.Event] = None

    async def start(self) -> None:
        if not self.config.enabled:
            log.info("Compactor disabled; not starting worker (stub).")
            return
        if self._task is not None:
            return
        # Bind asyncio primitives to the running loop (Python 3.10+ rejects
        # loop-less Queue()/Event() construction).
        self._queue = asyncio.Queue()
        self._queued: set[str] = set()
        self._stopping = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="compactor-worker")

    async def stop(self) -> None:
        if self._stopping is not None:
            self._stopping.set()
        if self._task is not None and self._queue is not None:
            await self._queue.put("__shutdown__")
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    def note_append(self, session_id: str) -> None:
        """Signal that a new turn was appended to `session_id`."""
        if not self.config.enabled or self._queue is None:
            return
        try:
            if not hasattr(self, "_queued"):
                self._queued = set()
            if session_id in self._queued:
                return
            self._queued.add(session_id)
            self._queue.put_nowait(session_id)
        except asyncio.QueueFull:
            pass

    def on_mission_change(self, session_id: str) -> None:
        """Signal that mission has shifted — re-score retained context."""
        if not self.config.enabled:
            return
        # TODO: invalidate prior summary, requeue for re-summarization.
        log.info("[stub] mission change for session=%s", session_id)
        self.note_append(session_id)

    # ---------- internal ----------
    async def _run(self) -> None:
        assert self._queue is not None and self._stopping is not None
        log.info("Compactor worker started (config=%s)", self.config)
        while not self._stopping.is_set():
            try:
                sid = await asyncio.wait_for(
                    self._queue.get(), timeout=self.config.idle_interval_sec
                )
            except asyncio.TimeoutError:
                continue
            if sid == "__shutdown__":
                break
            if hasattr(self, "_queued"):
                self._queued.discard(sid)
            try:
                await self._compact_one(sid)
            except Exception:
                log.exception("compactor: failed on session=%s", sid)

    async def _compact_one(self, session_id: str) -> None:
        """STUB: summarize older messages, keep last N verbatim.

        TODO (waiting on compaction-research):
        - Score per-turn relevance.
        - Detect throwaway PoCs / abandoned branches; replace with 1-sentence
          conclusion + reason.
        - Mission-aware retention.
        - Choose summarization model + prompt.
        """
        if self.summarize_fn is None:
            log.debug("compactor: no summarize_fn wired; skipping session=%s", session_id)
            self.store.record_event(
                session_id,
                "compaction_skipped",
                {"reason": "summarize_fn_missing"},
            )
            return
        prior, watermark, revision = self.store.get_compaction_state(session_id)
        delta = self.store.get_compaction_delta(session_id, prior, watermark)
        if len(delta) < self.config.min_messages_to_summarize:
            self.store.record_event(
                session_id,
                "compaction_skipped",
                {
                    "reason": "below_threshold",
                    "delta_count": len(delta),
                    "min_messages_to_summarize": self.config.min_messages_to_summarize,
                    "prior_watermark": watermark,
                    "revision": revision,
                },
            )
            return
        token_usage = self.store.token_usage(session_id)
        selection = select_compaction_head_tail(
            delta,
            self.config,
            window_size=token_usage.window_size,
        )
        head = selection.head
        if not head:
            self.store.record_event(
                session_id,
                "compaction_skipped",
                {
                    "reason": "no_head",
                    "delta_count": len(delta),
                    "keep_verbatim_n": self.config.keep_verbatim_n,
                    "tail_selection_strategy": selection.strategy,
                    "tail_token_budget": selection.token_budget,
                    "tail_tokens": selection.tail_tokens,
                    "tail_selection_fallback_reason": selection.fallback_reason,
                    "tail_selection_stopped_reason": selection.stopped_reason,
                    "prior_watermark": watermark,
                    "revision": revision,
                },
            )
            return
        head_ids = [int(m.id) for m in head if m.id is not None]
        if len(head_ids) != len(head):
            log.warning("compactor: cannot compact rows without ids session=%s", session_id)
            self.store.record_event(
                session_id,
                "compaction_skipped",
                {
                    "reason": "missing_ids",
                    "head_count": len(head),
                    "ids_present": len(head_ids),
                    "prior_watermark": watermark,
                    "revision": revision,
                },
            )
            return
        new_watermark = head_ids[-1]
        event_metadata = {
            "prior_watermark": watermark,
            "expected_revision": revision,
            "delta_count": len(delta),
            "summarized_count": len(head),
            "kept_verbatim_count": len(delta) - len(head),
            "target_watermark": new_watermark,
            "delete_summarized": self.config.delete_summarized,
            "tail_selection_strategy": selection.strategy,
            "tail_token_budget": selection.token_budget,
            "tail_tokens": selection.tail_tokens,
            "tail_selection_fallback_reason": selection.fallback_reason,
            "tail_selection_stopped_reason": selection.stopped_reason,
        }
        self.store.record_event(
            session_id,
            "compaction_started",
            event_metadata,
        )
        new_summary = await self.summarize_fn(head, prior)
        if not new_summary or not new_summary.strip():
            log.warning("compactor: empty summary; keeping prior summary session=%s", session_id)
            skipped_metadata = dict(event_metadata)
            skipped_metadata["reason"] = "empty_summary"
            self.store.record_event(
                session_id,
                "compaction_skipped",
                skipped_metadata,
            )
            return
        ok = self.store.commit_compaction_summary(
            session_id,
            new_summary,
            new_watermark,
            revision,
            head_ids,
            delete_summarized=self.config.delete_summarized,
            event_metadata={
                **event_metadata,
                "watermark": new_watermark,
                "deleted_count": len(head_ids) if self.config.delete_summarized else 0,
            },
        )
        if not ok:
            log.info("compactor: state changed during summarize; requeue session=%s", session_id)
            aborted_metadata = dict(event_metadata)
            aborted_metadata["reason"] = "revision_or_guard_changed"
            self.store.record_event(
                session_id,
                "compaction_aborted_revision_changed",
                aborted_metadata,
            )
            self.note_append(session_id)
            return
        log.info("compactor: refreshed summary for session=%s (%d msgs)", session_id, len(head))


def select_compaction_head_tail(
    delta: Sequence[Message],
    config: CompactorConfig,
    *,
    window_size: Optional[int],
) -> TailSelection:
    """Split compaction delta into summarized head and verbatim tail.

    The returned tail is always a contiguous suffix. That keeps the summary
    watermark sound: every live row after the watermark remains verbatim, and
    every live row at or before it is represented by the summary.
    """
    messages = list(delta)
    budget = _resolve_tail_token_budget(config, window_size)
    if budget is None:
        return _select_by_keep_n(messages, config.keep_verbatim_n, strategy="count")

    units = _conversation_units(messages)
    selected_unit_count = 0
    selected_tokens = 0
    stopped_reason: Optional[str] = None

    for unit in reversed(units):
        if not unit.complete_tool_history:
            stopped_reason = "incomplete_tool_history"
            break
        unit_tokens = unit.token_estimate
        if unit_tokens is None:
            fallback = _select_by_keep_n(
                messages,
                config.keep_verbatim_n,
                strategy="count_fallback",
            )
            fallback.token_budget = budget
            fallback.tail_tokens = _sum_token_estimates(fallback.tail)
            fallback.fallback_reason = "missing_token_estimate"
            return fallback
        if selected_tokens + unit_tokens > budget:
            stopped_reason = "token_budget_exhausted"
            break
        selected_tokens += unit_tokens
        selected_unit_count += 1

    if selected_unit_count == 0:
        tail: List[Message] = []
    else:
        tail_units = units[-selected_unit_count:]
        tail = [msg for unit in tail_units for msg in unit.messages]
    head = messages[: len(messages) - len(tail)]
    return _pin_system_messages(
        messages,
        TailSelection(
            head=head,
            tail=tail,
            strategy="token_budget",
            token_budget=budget,
            tail_tokens=selected_tokens,
            stopped_reason=stopped_reason,
        ),
    )


def _pin_system_messages(
    messages: Sequence[Message],
    selection: TailSelection,
) -> TailSelection:
    """Keep active system messages verbatim by moving the tail boundary up."""
    head_len = len(selection.head)
    for idx, msg in enumerate(messages[:head_len]):
        if msg.role != "system":
            continue
        tail_start = _expand_tail_start_for_tool_history(messages, idx)
        tail = list(messages[tail_start:])
        return TailSelection(
            head=list(messages[:tail_start]),
            tail=tail,
            strategy=selection.strategy,
            token_budget=selection.token_budget,
            tail_tokens=_sum_token_estimates(tail)
            if selection.token_budget is not None
            else selection.tail_tokens,
            fallback_reason=selection.fallback_reason,
            stopped_reason="pinned_system_message",
        )
    return selection


def _expand_tail_start_for_tool_history(
    messages: Sequence[Message],
    tail_start: int,
) -> int:
    """Move a pinned tail boundary up until its tool history is complete."""
    units = _conversation_units(messages)
    index_to_unit_start = {
        idx: unit.start_index
        for unit in units
        for idx in range(unit.start_index, unit.start_index + len(unit.messages))
    }
    tail_start = index_to_unit_start.get(tail_start, tail_start)

    while True:
        missing_calls, missing_results = _missing_tool_history(messages[tail_start:])
        if not missing_calls and not missing_results:
            return tail_start

        expanded_start: Optional[int] = None
        for idx, msg in enumerate(messages[:tail_start]):
            if msg.role == "assistant" and msg.tool_calls:
                if _tool_call_ids(msg.tool_calls) & missing_calls:
                    unit_start = index_to_unit_start.get(idx, idx)
                    expanded_start = (
                        unit_start
                        if expanded_start is None
                        else min(expanded_start, unit_start)
                    )
            if (msg.role == "tool" or msg.tool_call_id is not None) and msg.tool_call_id:
                if str(msg.tool_call_id) in missing_results:
                    unit_start = index_to_unit_start.get(idx, idx)
                    expanded_start = (
                        unit_start
                        if expanded_start is None
                        else min(expanded_start, unit_start)
                    )

        if expanded_start is None or expanded_start >= tail_start:
            return tail_start
        tail_start = expanded_start


def _missing_tool_history(messages: Sequence[Message]) -> tuple[Set[str], Set[str]]:
    call_ids: Set[str] = set()
    result_ids: Set[str] = set()

    for msg in messages:
        if msg.role == "assistant" and msg.tool_calls:
            call_ids.update(_tool_call_ids(msg.tool_calls))
        if msg.role == "tool" or msg.tool_call_id is not None:
            if msg.tool_call_id:
                result_ids.add(str(msg.tool_call_id))

    return result_ids - call_ids, call_ids - result_ids


def _sum_token_estimates(messages: Sequence[Message]) -> Optional[int]:
    total = 0
    for msg in messages:
        if msg.token_estimate is None:
            return None
        total += int(msg.token_estimate)
    return total


def _select_by_keep_n(
    messages: Sequence[Message],
    keep_verbatim_n: int,
    *,
    strategy: str,
) -> TailSelection:
    keep_n = max(0, int(keep_verbatim_n))
    units = _conversation_units(messages)
    selected_unit_count = 0
    selected_message_count = 0
    stopped_reason: Optional[str] = None

    if keep_n > 0:
        for unit in reversed(units):
            if not unit.complete_tool_history:
                stopped_reason = "incomplete_tool_history"
                break
            selected_unit_count += 1
            selected_message_count += len(unit.messages)
            if selected_message_count >= keep_n:
                break

    if selected_unit_count == 0:
        tail: List[Message] = []
    else:
        tail_units = units[-selected_unit_count:]
        tail = [msg for unit in tail_units for msg in unit.messages]

    head = list(messages[: len(messages) - len(tail)])
    return _pin_system_messages(
        messages,
        TailSelection(
            head=head,
            tail=tail,
            strategy=strategy,
            stopped_reason=stopped_reason,
        ),
    )


def _resolve_tail_token_budget(
    config: CompactorConfig,
    window_size: Optional[int],
) -> Optional[int]:
    budgets: List[int] = []
    if config.keep_verbatim_tokens is not None:
        budgets.append(max(0, int(config.keep_verbatim_tokens)))
    if config.keep_verbatim_window_ratio is not None and window_size is not None:
        ratio_budget = int(max(0.0, float(config.keep_verbatim_window_ratio)) * window_size)
        budgets.append(max(0, ratio_budget))
    if not budgets:
        return None
    return min(budgets)


def _conversation_units(messages: Sequence[Message]) -> List[_MessageUnit]:
    raw_units: List[tuple[int, List[Message]]] = []
    current: List[Message] = []
    current_start = 0

    for idx, msg in enumerate(messages):
        if msg.role == "system":
            if current:
                raw_units.append((current_start, current))
                current = []
            raw_units.append((idx, [msg]))
            continue
        if msg.role == "user":
            if current:
                raw_units.append((current_start, current))
            current = [msg]
            current_start = idx
            continue
        if not current:
            current = [msg]
            current_start = idx
        else:
            current.append(msg)

    if current:
        raw_units.append((current_start, current))

    return [
        _MessageUnit(unit, _has_complete_tool_history(unit), start_index)
        for start_index, unit in raw_units
    ]


def _has_complete_tool_history(messages: Sequence[Message]) -> bool:
    call_ids: Set[str] = set()
    result_ids: Set[str] = set()

    for msg in messages:
        if msg.role == "assistant" and msg.tool_calls:
            call_ids.update(_tool_call_ids(msg.tool_calls))
        if msg.role == "tool" or msg.tool_call_id is not None:
            if not msg.tool_call_id:
                return False
            result_ids.add(str(msg.tool_call_id))

    if result_ids and not result_ids.issubset(call_ids):
        return False
    if call_ids and not call_ids.issubset(result_ids):
        return False
    return True


def _tool_call_ids(tool_calls: object) -> Set[str]:
    if isinstance(tool_calls, str):
        try:
            tool_calls = json.loads(tool_calls)
        except Exception:
            return set()
    if not isinstance(tool_calls, list):
        return set()
    ids: Set[str] = set()
    for call in tool_calls:
        if isinstance(call, dict) and call.get("id") is not None:
            ids.add(str(call["id"]))
    return ids
