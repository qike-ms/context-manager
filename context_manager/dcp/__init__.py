"""context_manager.dcp — DCP (Dynamic Context Pruning) for context-manager.

An independent reimplementation of the DCP ideas (model-invoked compress tool,
placeholder-swap middleware, store-immutable) in Python under MIT, developed
from the public DCP README description.  No DCP source code was consulted.

Public API:

    DCPConfig           — configuration dataclass (from_env() for env-var loading)
    DCPMiddleware       — per-session coordinator; main entry point for dispatchers
    CompressTool        — the `compress` tool schema + invocation handler
    PlaceholderStore    — SQLite-backed placeholder records
    tag_ctx_ids         — utility to tag ContextStore messages with _ctx_id

Relationship to Compactor:

    Compactor (context_manager.compactor) is a background async worker that
    calls a summarize_fn LLM periodically, writing a rolling watermark-based
    text summary into the session row.  It mutates store state.

    DCPMiddleware is a request-build-time transform layer: it applies active
    placeholders to the message list *after* the store is read, without ever
    changing the stored rows.  The model itself invokes compression via the
    `compress` tool.

    The two layers compose: Compactor provides background rolling summaries;
    DCP adds model-driven, fine-grained, reversible compression on top.
    When both are active, the Compactor summary appears as a system message
    at the start of the context, and DCP placeholders apply within the
    verbatim tail.

Prior art: @tarquinen/opencode-dcp (AGPL-3.0, TypeScript).
This module is MIT, independently implemented.
"""

from .config import DCPConfig, DCPNudgeConfig, DCPProtectionsConfig, DCPPurgeErrorsConfig, DCPDedupeConfig
from .middleware import DCPMiddleware, tag_ctx_ids
from .placeholders import Placeholder, PlaceholderStore
from .tool import CompressTool, InvokeResult, TOOL_NAME, TOOL_SCHEMA_OPENAI
from .engine import (
    apply_range_compress,
    apply_message_compress,
    apply_placeholders,
    dedupe_tool_calls,
    purge_errored_inputs,
    maybe_inject_nudge,
    CompressResult,
    EngineError,
)

__all__ = [
    # Config
    "DCPConfig",
    "DCPNudgeConfig",
    "DCPProtectionsConfig",
    "DCPPurgeErrorsConfig",
    "DCPDedupeConfig",
    # Middleware (main entry point)
    "DCPMiddleware",
    "tag_ctx_ids",
    # Placeholders
    "Placeholder",
    "PlaceholderStore",
    # Tool
    "CompressTool",
    "InvokeResult",
    "TOOL_NAME",
    "TOOL_SCHEMA_OPENAI",
    # Engine (pure functions, for testing / advanced use)
    "apply_range_compress",
    "apply_message_compress",
    "apply_placeholders",
    "dedupe_tool_calls",
    "purge_errored_inputs",
    "maybe_inject_nudge",
    "CompressResult",
    "EngineError",
]
