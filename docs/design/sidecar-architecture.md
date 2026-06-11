# Context-Manager Sidecar Architecture

Issue: #20.

## Decision

Run one `context-manager` sidecar per host machine. The sidecar owns one
SQLite database and exposes the existing `ContextStore` + DCP middleware over
HTTP on a Unix domain socket.

Multi-tenancy is handled outside the sidecar with containers or VMs. The v1
sidecar assumes a single local Unix user and relies on socket filesystem
permissions instead of tokens or TLS.

## Paths

Defaults follow XDG paths:

```text
${XDG_RUNTIME_DIR:-~/.local/run}/ctxmgr/ctxmgr.sock
${XDG_DATA_HOME:-~/.local/share}/ctxmgr/ctxmgr.db
${XDG_DATA_HOME:-~/.local/share}/ctxmgr/offload/
```

`CTXMGR_SOCK` overrides the socket path for clients and the server CLI.

## API

All endpoints are versioned under `/v1` and exchange JSON:

```text
GET  /v1/healthz
POST /v1/sessions/{sid}/append
POST /v1/sessions/{sid}/build_outbound
POST /v1/sessions/{sid}/compress
GET  /v1/sessions/{sid}/usage
POST /v1/sessions/{sid}/set_model
GET  /v1/sessions/{sid}/placeholders
POST /v1/sessions/{sid}/placeholders/{pid}/deactivate
GET  /v1/sessions/{sid}/parent_summary
```

The API is intentionally synchronous and non-streaming. Hosts should degrade to
their native context path if the sidecar is unavailable.

## Sessions And Subagents

Parents and subagents share the same sidecar but use different `session_id`
values. A suggested scheme is:

```text
<host>:<root>
<host>:<root>:task:<n>
<host>:<root>:task:<n>:task:<m>
```

Subagent context inheritance is opt-in. `/parent_summary` returns only the
direct parent summary inferred from the `:task:` delimiter. It does not merge
parent context into child prompts automatically.

## Dependencies

The core package remains dependency-free. Sidecar dependencies are optional:

```bash
pip install 'context-manager[sidecar]'
```

The top-level `context_manager` import does not import FastAPI or uvicorn.

## SQLite Concurrency

The sidecar owns a single `ContextStore` instance. The store connection uses
WAL mode and `check_same_thread=False`; store methods serialize access with an
internal re-entrant lock. DCP placeholder tables are colocated in the same DB
through the public `ContextStore.connection()` extension seam.

External code should prefer the HTTP API or `ContextStore` methods for writes.
The raw connection is for colocated extension tables, not for bypassing store
invariants.

## Running

```bash
context-manager-sidecar \
  --socket "${XDG_RUNTIME_DIR:-$HOME/.local/run}/ctxmgr/ctxmgr.sock" \
  --db "$HOME/.local/share/ctxmgr/ctxmgr.db"
```

See `etc/systemd/context-manager-sidecar.service` for a sample user unit.

## MCP-Only Hosts

Claude Code and Goose can connect via the optional stdio MCP server:

```bash
pip install -e '.[sidecar,mcp]'  # MCP extra requires Python 3.10+
context-manager-sidecar \
  --socket "${XDG_RUNTIME_DIR:-$HOME/.local/run}/ctxmgr/ctxmgr.sock" \
  --db "$HOME/.local/share/ctxmgr/ctxmgr.db"
```

```json
{
  "mcpServers": {
    "context-manager": {
      "command": "context-manager-mcp",
      "env": {
        "CTXMGR_SOCK": "/home/qike/.local/run/ctxmgr/ctxmgr.sock"
      }
    }
  }
}
```

Goose recipe shape:

```yaml
extensions:
  - type: stdio
    name: context-manager
    cmd: context-manager-mcp
    timeout: 300
```

This mode exposes tools/resources to the model, but it cannot intercept or
replace the host's outbound request. It is useful for explicit `compress` calls
and inspection, not full automatic DCP placeholder substitution.
