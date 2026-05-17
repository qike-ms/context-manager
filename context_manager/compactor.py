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
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional

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
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if not self.config.enabled:
            log.info("Compactor disabled; not starting worker (stub).")
            return
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="compactor-worker")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            await self._queue.put("__shutdown__")
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    def note_append(self, session_id: str) -> None:
        """Signal that a new turn was appended to `session_id`."""
        if not self.config.enabled:
            return
        try:
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
            return
        msgs = self.store.get_full_for_compaction(session_id)
        if len(msgs) < self.config.min_messages_to_summarize:
            return
        head = msgs[: -self.config.keep_verbatim_n]
        if not head:
            return
        prior = self.store.get_summary(session_id)
        new_summary = await self.summarize_fn(head, prior)
        self.store.set_summary(session_id, new_summary)
        log.info("compactor: refreshed summary for session=%s (%d msgs)", session_id, len(head))
