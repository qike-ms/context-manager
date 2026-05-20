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
    """For each assistant turn, pair cl100k(prompt-text-so-far) with the per-request
    provider input.  Provider input = ``input_tokens + cache_creation + cache_read``
    (all three together = total tokens the model actually processed for this request;
    cache vs fresh is a billing optimization, not a tokenization difference).

    Note: drift methodology is *linear regression*, not delta subtraction. The
    earlier delta approach was incorrect because ``usage.input_tokens`` is a
    per-request total, not a cumulative running counter, so ``a_i - a_{i-1}``
    has no meaningful semantics. We expose ``mode="per_request"`` pairs and let
    ``fit_calibration`` compute ``(slope, intercept)`` over the population.
    """
    msgs = []
    pairs = []
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
                pairs.append({"backend": "cc", "mode": "per_request",
                              "estimate": est, "actual": actual,
                              "chars": len(prompt_text), "src": os.path.basename(jsonl_path)})
                if len(pairs) >= max_pairs:
                    return pairs
        msgs.append((role or "?", text))
    return pairs


def harvest_oc(jsonl_path: str, max_pairs: int = 50):
    """For each ``token_count`` event, pair cl100k(messages-so-far) with
    ``last_token_usage.input_tokens`` (the per-request total the provider
    counted; ``cached_input_tokens`` is a subset, NOT additive).
    """
    msgs = []
    pairs = []
    for line in open(jsonl_path, encoding="utf-8", errors="ignore"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        pl = d.get("payload") or {}
        ptype = pl.get("type")
        if ptype == "message":
            t = _text_of(pl.get("content"))
            msgs.append((pl.get("role", "?"), t))
        elif ptype in ("function_call", "function_call_output", "reasoning"):
            t = _text_of(pl.get("output") or pl.get("arguments") or pl.get("summary") or "")
            msgs.append((ptype, t))
        elif ptype == "token_count":
            info = pl.get("info") or {}
            last = (info.get("last_token_usage") or {})
            actual = last.get("input_tokens")
            if not actual:
                continue
            prompt_text = "\n".join(t for _, t in msgs)
            est = cl100k_count(prompt_text)
            if actual > 100 and est > 100:
                pairs.append({"backend": "oc", "mode": "per_request",
                              "estimate": est, "actual": actual,
                              "chars": len(prompt_text), "src": os.path.basename(jsonl_path)})
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
    return [{"backend": "hermes", "mode": "per_request", "estimate": est, "actual": est,
             "chars": len(prompt_text), "src": os.path.basename(jsonl_path),
             "synthetic": True}]


def fit_calibration(pairs):
    """Least-squares fit ``actual ≈ slope * estimate + intercept`` per backend.
    Returns ``{"slope": float, "intercept": int, "n": int, "r2": float}``.
    """
    if len(pairs) < 3:
        return None
    xs = [float(p["estimate"]) for p in pairs]
    ys = [float(p["actual"]) for p in pairs]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    slope = num / den
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return {"slope": round(slope, 4), "intercept": int(round(intercept)),
            "n": n, "r2": round(r2, 4)}


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
                   "fit": fit_calibration([p for p in v if not p.get("synthetic")]),
                   "sessions": sorted({p["src"] for p in v})}
               for k, v in by.items()}

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"summary": summary, "pairs": all_pairs}, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"\n[wrote {len(all_pairs)} pairs to {out_path}]")


if __name__ == "__main__":
    main()
