"""Dogfood the listing & drop API on a real ephemeral SQLite store.

Appends 10 mixed rows, then exercises all 5 new methods, printing results.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from pprint import pprint

from context_manager import ContextStore


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ctx-dogfood-"))
    db = tmp / "ctx.db"
    print(f"# DB: {db}")
    s = ContextStore(db)
    sid = "dogfood-session"
    s.set_model(sid, "opus-4.7")

    # 10 mixed rows: system + user/assistant + a couple of tool exchanges.
    ids = []
    ids.append(s.append(sid, "system", content="you are helpful"))
    ids.append(s.append(sid, "user", content="hello there"))
    ids.append(s.append(sid, "assistant", content="hi! how can I help?"))
    ids.append(
        s.append(
            sid,
            "assistant",
            tool_calls=[{"id": "c1", "function": {"name": "search", "arguments": '{"q":"weather"}'}}],
        )
    )
    ids.append(s.append(sid, "tool", content="sunny, 72F", tool_name="search", tool_call_id="c1"))
    ids.append(s.append(sid, "assistant", content="it's sunny and 72F"))
    ids.append(s.append(sid, "user", content="run a calc"))
    ids.append(
        s.append(
            sid,
            "assistant",
            tool_calls=[{"id": "c2", "function": {"name": "calc", "arguments": '{"expr":"2+2"}'}}],
        )
    )
    ids.append(s.append(sid, "tool", content="4", tool_name="calc", tool_call_id="c2"))
    ids.append(s.append(sid, "user", content="thanks!"))
    print(f"# Appended {len(ids)} rows: ids={ids}")

    print("\n## 1) iter_messages(kind='all', limit=20)")
    for r in s.iter_messages(sid, kind="all", limit=20):
        print(f"  id={r.id} role={r.role} kind={r.kind} tool={r.tool_name} "
              f"tok={r.token_estimate} text={r.text_preview!r}")

    print("\n## 2) iter_messages(kind='tool')")
    for r in s.iter_messages(sid, kind="tool"):
        print(f"  id={r.id} role={r.role} kind={r.kind} tool={r.tool_name} args={r.tool_args_preview!r}")

    print("\n## 3) token_usage(model=None) [reads sessions.model='opus-4.7']")
    u = s.token_usage(sid)
    pprint(u)

    print("\n## 4) drop_by_tool('search') → expected 1")
    n = s.drop_by_tool(sid, "search")
    print(f"  deleted={n}")

    print("\n## 5) drop_range(ids[0], ids[1]) → expected 2")
    n = s.drop_range(sid, ids[0], ids[1])
    print(f"  deleted={n}")

    print("\n## 6) drop_messages([ids[-1], 999_999]) → expected 1 (unknown ignored)")
    n = s.drop_messages(sid, [ids[-1], 999_999])
    print(f"  deleted={n}")

    print("\n## 7) post-drop iter_messages")
    for r in s.iter_messages(sid, limit=20):
        print(f"  id={r.id} role={r.role} kind={r.kind} text={r.text_preview!r}")

    print("\n## 8) post-drop token_usage")
    pprint(s.token_usage(sid))

    mc = s._conn.execute("SELECT message_count FROM sessions WHERE id=?", (sid,)).fetchone()[0]
    print(f"\n# sessions.message_count = {mc}")
    s.close()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
