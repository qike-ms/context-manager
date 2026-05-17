#!/usr/bin/env python3
"""Measure tokenizer drift: cl100k_base estimate vs real provider usage from OC/CC session dumps.

Outputs JSON fixture (drift pairs) + a summary table.
"""
from __future__ import annotations
import json, sys, glob, os, argparse, statistics
from pathlib import Path

import tiktoken
ENC = tiktoken.get_encoding("cl100k_base")

def cl100k_count(text: str) -> int:
    return len(ENC.encode(text or "", disallowed_special=()))

def _text_of(content) -> str:
    """Flatten message.content (str | list of blocks) to a single string."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                parts.append(str(b)); continue
            t = b.get("type")
            if t in ("text", "input_text", "output_text"):
                parts.append(b.get("text", ""))
            elif t == "tool_use":
                parts.append(b.get("name", "") + " " + json.dumps(b.get("input", {}), ensure_ascii=False))
            elif t == "tool_result":
                c = b.get("content")
                parts.append(_text_of(c))
            else:
                parts.append(json.dumps(b, ensure_ascii=False))
        return "\n".join(parts)
    return str(content)


def harvest_cc(jsonl_path: str, max_pairs: int = 50):
    """Two kinds of pairs:
    - mode='full': prompt = concat of all prior messages (mixes tokenizer drift with system-prompt/tool-schema overhead).
    - mode='delta': per-turn delta = (actual_i - actual_{i-1}) vs cl100k of NEW content added since last turn.
      Isolates tokenizer-level drift from fixed prefix overhead.
    Actual provider input = usage.input_tokens + cache_creation_input_tokens + cache_read_input_tokens.
    """
    msgs = []
    pairs = []
    prev_actual = None
    prev_total_text_len = 0
    new_chunks = []
    for line in open(jsonl_path, encoding="utf-8", errors="ignore"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        m = d.get("message")
        if not isinstance(m, dict):
            continue
        role = m.get("role") or d.get("type")
        text = _text_of(m.get("content"))
        usage = m.get("usage")
        if role == "assistant" and isinstance(usage, dict):
            prompt_text = "\n".join(t for _, t in msgs)
            est = cl100k_count(prompt_text)
            actual = (usage.get("input_tokens", 0) or 0) \
                   + (usage.get("cache_creation_input_tokens", 0) or 0) \
                   + (usage.get("cache_read_input_tokens", 0) or 0)
            if actual > 100 and est > 100:
                pairs.append({"backend": "cc", "mode": "full", "estimate": est, "actual": actual,
                              "chars": len(prompt_text), "src": os.path.basename(jsonl_path)})
                # delta
                if prev_actual is not None and new_chunks:
                    new_text = "\n".join(new_chunks)
                    d_est = cl100k_count(new_text)
                    d_actual = actual - prev_actual
                    if d_est > 50 and d_actual > 50:
                        pairs.append({"backend": "cc", "mode": "delta", "estimate": d_est, "actual": d_actual,
                                      "chars": len(new_text), "src": os.path.basename(jsonl_path)})
                prev_actual = actual
                new_chunks = []
                if len(pairs) >= max_pairs:
                    return pairs
        msgs.append((role or "?", text))
        new_chunks.append(text)
    return pairs


def harvest_oc(jsonl_path: str, max_pairs: int = 50):
    """For each token_count event with last_token_usage:
    - mode='full': cl100k of all messages so far vs last_token_usage.input_tokens.
    - mode='delta': cl100k of NEW content added since previous token_count vs delta in input_tokens.
      Isolates tokenizer drift from fixed system-prompt/tool-schema overhead.
    """
    msgs = []
    pairs = []
    prev_actual = None
    new_chunks = []
    for line in open(jsonl_path, encoding="utf-8", errors="ignore"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        pl = d.get("payload") or {}
        ptype = pl.get("type")
        if ptype == "message":
            t = _text_of(pl.get("content"))
            msgs.append((pl.get("role", "?"), t)); new_chunks.append(t)
        elif ptype in ("function_call", "function_call_output", "reasoning"):
            t = _text_of(pl.get("output") or pl.get("arguments") or pl.get("summary") or "")
            msgs.append((ptype, t)); new_chunks.append(t)
        elif ptype == "token_count":
            info = pl.get("info") or {}
            last = (info.get("last_token_usage") or {})
            actual = last.get("input_tokens")
            if not actual:
                continue
            prompt_text = "\n".join(t for _, t in msgs)
            est = cl100k_count(prompt_text)
            if actual > 100 and est > 100:
                pairs.append({"backend": "oc", "mode": "full", "estimate": est, "actual": actual,
                              "chars": len(prompt_text), "src": os.path.basename(jsonl_path)})
                if prev_actual is not None and new_chunks:
                    new_text = "\n".join(new_chunks)
                    d_est = cl100k_count(new_text)
                    d_actual = actual - prev_actual
                    if d_est > 50 and d_actual > 50:
                        pairs.append({"backend": "oc", "mode": "delta", "estimate": d_est, "actual": d_actual,
                                      "chars": len(new_text), "src": os.path.basename(jsonl_path)})
                prev_actual = actual
                new_chunks = []
                if len(pairs) >= max_pairs:
                    return pairs
    return pairs


def harvest_hermes(jsonl_path: str, max_pairs: int = 50):
    """Hermes session .jsonl + state.db `messages.token_count` column are both empty
    (token_count is NULL for all observed rows; provider usage not persisted as of HEAD dc1926b).
    We emit one cl100k tally per session as a synthetic 'self-estimate' pair so the fixture has
    a row, but flag it `synthetic: True` — drift cannot be measured for Hermes without provider data.
    """
    msgs = []
    for line in open(jsonl_path, encoding="utf-8", errors="ignore"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        role = d.get("role")
        if not role:
            continue
        msgs.append((role, _text_of(d.get("content"))))
    if len(msgs) < 4:
        return []
    prompt_text = "\n".join(t for _, t in msgs)
    est = cl100k_count(prompt_text)
    if est < 100:
        return []
    return [{"backend": "hermes", "mode": "full", "estimate": est, "actual": est,
             "chars": len(prompt_text), "src": os.path.basename(jsonl_path),
             "synthetic": True}]


def drift_stats(pairs):
    if not pairs:
        return {"n": 0}
    ratios = [p["actual"] / p["estimate"] for p in pairs]
    drifts = [(p["actual"] - p["estimate"]) / p["estimate"] for p in pairs]
    abs_drifts = [abs(x) for x in drifts]
    return {
        "n": len(pairs),
        "mean_ratio_actual_over_est": round(statistics.mean(ratios), 4),
        "median_ratio": round(statistics.median(ratios), 4),
        "mean_signed_drift_pct": round(100 * statistics.mean(drifts), 2),
        "median_signed_drift_pct": round(100 * statistics.median(drifts), 2),
        "mean_abs_drift_pct": round(100 * statistics.mean(abs_drifts), 2),
        "p90_abs_drift_pct": round(100 * sorted(abs_drifts)[int(0.9 * (len(abs_drifts) - 1))], 2),
        "max_abs_drift_pct": round(100 * max(abs_drifts), 2),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="tests/fixtures/drift_pairs.json")
    ap.add_argument("--n-sessions", type=int, default=5)
    args = ap.parse_args()

    home = Path.home()
    all_pairs = []

    # CC: largest 5 top-level project sessions
    cc_files = sorted(glob.glob(str(home / ".claude/projects/*/*.jsonl")),
                      key=lambda p: os.path.getsize(p), reverse=True)[:args.n_sessions]
    for f in cc_files:
        all_pairs.extend(harvest_cc(f))

    # OC: largest 5 rollouts
    oc_files = sorted(glob.glob(str(home / ".codex/sessions/2026/**/*.jsonl"), recursive=True),
                      key=lambda p: os.path.getsize(p), reverse=True)[:args.n_sessions]
    for f in oc_files:
        all_pairs.extend(harvest_oc(f))

    # Hermes: largest 5
    h_files = sorted(glob.glob(str(home / ".hermes/sessions/*.jsonl")),
                     key=lambda p: os.path.getsize(p), reverse=True)[:args.n_sessions]
    for f in h_files:
        all_pairs.extend(harvest_hermes(f))

    by = {}
    for p in all_pairs:
        key = f"{p['backend']}/{p.get('mode','full')}"
        by.setdefault(key, []).append(p)

    summary = {k: {"stats": drift_stats(v),
                   "sessions": sorted({p["src"] for p in v})}
               for k, v in by.items()}

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"summary": summary, "pairs": all_pairs}, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"\n[wrote {len(all_pairs)} pairs to {out_path}]")


if __name__ == "__main__":
    main()
