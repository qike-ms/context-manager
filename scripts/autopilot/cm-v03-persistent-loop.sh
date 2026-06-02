#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${CM_LOOP_REPO:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
PROMPT="${CM_LOOP_PROMPT:-$REPO/prompts/autopilot/cm-v03-persistent-loop.md}"
RUNTIME_DIR="${CM_LOOP_RUNTIME_DIR:-$REPO/.swe-supervisor/runtime/harness}"
DONE="${CM_LOOP_DONE:-$RUNTIME_DIR/cm-v03-DONE}"
LOCK_DIR="${CM_LOOP_LOCK_DIR:-$RUNTIME_DIR}"
LOCK="$LOCK_DIR/loop.lock"
GUARD="$LOCK_DIR/loop.lock.d"
HIST="$LOCK_DIR/loop-lock-history.log"
STALE_SECONDS="${CM_LOOP_LOCK_STALE_SECONDS:-600}"
ISSUE_URL="https://github.com/qike-ms/context-manager/issues/8"
SESSION_ID="${CM_LOOP_SESSION_ID:-telegram-context-compactor-cm-v03}"
CMD_NAME="cm-v03-persistent-loop"
LOCK_ACQUIRED=0

json_get() { python3 - "$1" "$2" "${3:-}" <<'PY'
import json, sys
try: print(json.load(open(sys.argv[1])).get(sys.argv[2], sys.argv[3]))
except Exception: print(sys.argv[3])
PY
}
pid_alive() { [[ -n "${1:-}" ]] && kill -0 "$1" 2>/dev/null; }
write_lock() { python3 - "$LOCK" "$$" "$ISSUE_URL" "$SESSION_ID" "$CMD_NAME" "$REPO" <<'PY'
import json, sys, time, datetime, socket
lock,pid,issue,session,cmd,repo=sys.argv[1:7]
open(lock,'w').write(json.dumps({'pid':int(pid),'host':socket.gethostname(),'started_at':datetime.datetime.utcnow().isoformat()+'Z','issue':issue,'session_id':session,'heartbeat_at':time.time(),'cmd':cmd,'repo':repo}, indent=2))
PY
}
append_takeover() { python3 - "$1" "$HIST" "$2" <<'PY'
import json, sys, datetime, os
lock,hist,age=sys.argv[1:4]
try: old=json.load(open(lock))
except Exception: old={'raw': open(lock).read() if os.path.exists(lock) else None}
os.makedirs(os.path.dirname(hist), exist_ok=True)
open(hist,'a').write(json.dumps({'event':'takeover','at':datetime.datetime.utcnow().isoformat()+'Z','age_seconds':age,'old':old})+'\n')
PY
}
acquire_lock() {
  mkdir -p "$LOCK_DIR"
  while true; do
    if mkdir "$GUARD" 2>/dev/null; then
      trap 'rm -rf "$GUARD"' RETURN
      if [[ -f "$LOCK" ]]; then
        old_pid="$(json_get "$LOCK" pid '')"; old_hb="$(json_get "$LOCK" heartbeat_at 0)"
        age="$(python3 - "$old_hb" <<'PY'
import sys,time
try: hb=float(sys.argv[1])
except Exception: hb=0
print(time.time()-hb)
PY
)"
        if pid_alive "$old_pid"; then echo "BLOCKED: existing cm loop alive $(cat "$LOCK")" >&2; exit 12; fi
        if ! python3 - "$age" "$STALE_SECONDS" <<'PY'
import sys
raise SystemExit(0 if float(sys.argv[1]) >= float(sys.argv[2]) else 1)
PY
        then echo "BLOCKED: dead loop heartbeat not stale yet age=$age" >&2; exit 13; fi
        append_takeover "$LOCK" "$age"
      fi
      cd "$REPO"; write_lock; LOCK_ACQUIRED=1; return 0
    fi
    sleep 1
  done
}
heartbeat() { python3 - "$LOCK" <<'PY'
import json, sys, time
p=sys.argv[1]; d=json.load(open(p)); d['heartbeat_at']=time.time(); open(p,'w').write(json.dumps(d, indent=2))
PY
}
cleanup() { rc=$?; rm -rf "$GUARD"; if [[ $rc -eq 0 && -f "$DONE" && "$LOCK_ACQUIRED" == "1" ]]; then rm -f "$LOCK"; fi; exit $rc; }
trap cleanup EXIT
mkdir -p "$RUNTIME_DIR"
if [[ -f "$DONE" ]]; then cat "$DONE"; exit 0; fi
acquire_lock
while [[ ! -f "$DONE" ]]; do
  heartbeat
  cd "$REPO"
  hermes chat -q "$(cat "$PROMPT")" --toolsets terminal,file,delegation,skills --source cm-v03-swe-supervisor-loop || true
  heartbeat
  [[ -f "$DONE" ]] && break
  sleep 30
done
cat "$DONE"
