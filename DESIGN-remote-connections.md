> Historical (superseded 2026-09-30 by DESIGN-opencode-ctl.md) — describes the retired MCP server; kept for the decision record.

# Design: multiple opencode connections (implemented)

**English** | [中文](DESIGN-remote-connections.zh-CN.md)

Status: **implemented** (2026-09-22). `connect_server` / `list_servers` / `disconnect_server`, the `server` parameter on every tool, session auto-routing, MCP-spawned local serve, and failure classification are all in place and live-verified.

## Decisions (2026-09-22, confirmed with the project owner)

| Decision | Outcome | Notes |
| --- | --- | --- |
| Local connection model | **MCP-spawned dedicated serve** | When no `server` is given: connect to local → if unreachable, spawn `opencode serve --port <random high port>` with a random password injected via `OPENCODE_SERVER_PASSWORD` (measured: respected, and the password is not printed). Child-process lifetime (cleaned up with the MCP instance; multiple instances never collide thanks to random ports). **No inferential service discovery of any kind (including `service.json`).** If `opencode` is not on `PATH`: availability error stating it is a user environment issue, no retries. When `OPENCODE_URL` is set explicitly, skip spawning and connect directly (password from `OPENCODE_PASSWORD`, default `opencode`). |
| Addressing model | Named aliases + session auto-routing | Every tool takes an optional `server` parameter (defaults to `local`); `create_session(server=X)` records the `ses_ → X` mapping, and subsequent calls carrying that `session_id` need no `server`. No implicit "current" connection switching. |
| Handle semantics | No fds/sockets; the alias is the handle | MCP callers (LLMs) can only hold strings; `ses_` ids are globally unique, so sessions do not need to carry connection handles. |
| Persistence | **Dynamic only, never static config** | "Remembering frequently used connections is not the tool's job; the tool must not take the place of memory and skills." Connections are established on demand by the caller, lost on process restart, and re-established by the caller. |
| Credential channels | **file > env > plaintext** | The file channel lets callers use special capabilities to produce credentials (askpass-style secure input); env is awkward for multiple servers (names must be staggered); plaintext is the worst and only a fallback. When no source is given, no `Authorization` header is sent (measured: remotes without auth exist). Note: with wrong credentials, requests may fall through to the Web UI; the connector reports this as a compatibility error hinting to check the password. |
| Remote permission default | **manual** | Remote `chat` defaults to `auto_permission="manual"` (the approval process must live with the caller, strictly stronger than `once`); `once / always / reject` are all preserved and chosen explicitly by the caller — **no extra restriction on `always`**. Local keeps the `once` default. Interactive approval UI is the caller's responsibility; the MCP only provides full `needs_permission` details. |
| Version baseline and drift policy | **Implemented** | ① Creation-time check: `connect_server` and the local first connection are hard-gated (unreachable = availability; no `version` field = compatibility). ② Re-query the version only after a failure and classify it as availability / compatibility / other — `other` carries the full original error for developer reporting. ③ Warnings are per-(connection, session), de-duplicated by the warned version (new sessions see it, the same session is not spammed, a version change re-warns new sessions). ④ Explicit status surface: the `connect_server` return value and `list_servers`. |

## Tool surface

- `connect_server(name, url, password_file?, password_env?, password?)` — registers and validates, returning `{name, version, baseline, baseline_check}`; credentials resolved in the priority order above.
- `list_servers()` — local + dynamic remotes: version and baseline status.
- `disconnect_server(name)` — removes a remote (`local` is not removable).
- All other tools: optional `server` parameter + session auto-routing.

## Internal refactoring scope

Global `CONFIG` became a connection registry `{name: Connection}`; `http_request` / `_run_until_terminal` / all fetchers carry connection context; the version-warning latch is isolated per connection.

## Remote prerequisites

On the remote machine run `opencode serve` (reachable bind address + password) or use an SSH tunnel. Tools executed by remote sessions run on the **remote filesystem**, and permission semantics follow the remote configuration.
