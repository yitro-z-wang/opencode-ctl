> Historical (superseded 2026-09-30 by DESIGN-opencode-ctl.md) — describes the removed MCP tool surface; kept for the decision record.

# Connection model: MCP-spawned local + multi-server

**English** | [中文](connection-model.zh-CN.md)

Part of the [opencode-mcp](../README.md) documentation.

**No inferential service discovery (including `service.json`).** The local connection takes one of two forms:

1. **Explicit direct connect**: when `OPENCODE_URL` is set, `local` points at that address (password from `OPENCODE_PASSWORD`, default `opencode`).
2. **MCP-spawned serve** (default): the first time `local` is needed, the MCP spawns its own `opencode serve` — **random high port + random password** injected via `OPENCODE_SERVER_PASSWORD`. It runs as a child process and **dies with the MCP instance**: exactly one serve is spawned per MCP process (a per-process singleton), and it is killed and reaped on stdin EOF (normal host shutdown) and on `SIGTERM` / `SIGINT` / `SIGHUP`. Multiple MCP instances never collide thanks to the random ports — and never accumulate, since each restart cleans up after itself. The one gap is `SIGKILL`, which no process can intercept on any platform: a hard-killed MCP can leave one stale serve behind (it is not adopted later — no inferential discovery, by design). If `opencode` is not on `PATH`, the call fails with an availability error (user environment issue, no retries).

**Multi-server**: `connect_server(name, url, password_file?/password_env?/password?)` registers a remote connection (process-lifetime only, never persisted). Credential priority: **file > env > plaintext**; when no credential source is given, no `Authorization` header is sent (some remotes accept no auth).

**Version baseline and failure classification**: every connection is hard-gated at creation (unreachable = `[availability]`; reachable but not an opencode API = `[compatibility]`). After a request failure the version is re-queried and the failure is classified as `[availability] / [compatibility] / [other]` — `other` carries the full original error for reporting. When a server version differs from the development baseline, a single `api_version_warning` is injected into the first tool result touching each (connection, session, version) pair — new sessions see it, the same session is never spammed.

**Permission defaults**: local `chat` defaults to `auto_permission="once"`; **remote connections default to `manual`** (approval must live with the caller); `once/always/reject` can always be chosen explicitly.

### Environment variables

- `OPENCODE_URL`: explicit local address (skips spawning), e.g. `http://127.0.0.1:4096`.
- `OPENCODE_PASSWORD`: HTTP Basic password for the explicit local connection; username is always `opencode`, default password `opencode`.
- `OPENCODE_MCP_WORKERS`: worker threads for request handling, default `4` (set to `1` for strict serialization).
- `OPENCODE_MCP_BASELINE_VERSION`: overrides the development baseline (default `2.0.12`, mainly for testing).

## Concurrency and cancellation

- Each JSON-RPC request is handled in its own worker thread (`OPENCODE_MCP_WORKERS`, default 4). Long-blocking calls such as `chat` / `wait_session` never block other tool calls.
- MCP-standard `notifications/cancelled` is honored: cancelling `chat` / `wait_session` stops polling within 1 second (a single in-flight HTTP request can take up to its 30-second timeout to unwind).
