> Written for the retired MCP surface (superseded by DESIGN-opencode-ctl.md); the interaction/session/subtree semantics carry over to the octl CLI.

# Verified flows

**English** | [中文](verification.zh-CN.md)

Part of the [opencode-mcp](../README.md) documentation.

All verified live against opencode v2.0.12:

1. **Create + chat**: `create_session` → `chat` → `succeeded` with `assistant_text` / `tools_used`.
2. **Manual permission loop**: `chat(auto_permission="manual")` → `needs_permission` → `permission_reply(decision="once")` → `wait_session` → terminal.
3. **Form pipeline**: `chat` / `wait_session` returns `needs_form` (with field details) → `form_reply` → `wait_session` → terminal.
4. **Automatic permission**: default `auto_permission="once"` approves and continues; a single `chat` returns `succeeded`.
5. **Connection layer**: explicit-env and MCP-spawned local paths; spawned serve carries a real conversation and dies with its parent; failure classification (dead port = availability, HTML-only service = compatibility, 401 = credentials); duplicate-name and unknown-server errors; session auto-routing; disconnect rules.
6. **Real remote end-to-end**: `connect_server` over the network → remote `create_session` → remote `chat` → remote manual-permission default (blocked, not auto-approved) → `permission_reply` → `wait_session` → incremental `get_messages` → `disconnect_server`.
7. **Long-session window**: on a 200+ message session, tail-window fetching keeps gate lookup, incremental cursors and `last_message_id` correct.
8. **Cancellation & concurrency**: `notifications/cancelled` stops polling within 1s; concurrent `pending_interactions` returns in milliseconds while `chat` is in flight.
9. **Host integrations**: mounted as a tool provider in the Hermes agent gateway; hosted by the oh-my-opencode-slim orchestration framework to drive nested opencode sessions.
10. **Subagent-aware waiting**: a session delegating a background subagent that blocks on a `shell` approval — `wait_session` (default) returns `needs_permission` naming the **subagent's** session (plus `root_session_id`) instead of a premature `succeeded`, while `chat` (default) returns `succeeded` but reports `pending_subagents: 1`. Also verified: three parallel subagents each awaiting approval, a subagent blocked on a form, `auto_permission="once"` answering a subagent's request and then reaching `succeeded`, and the same flow over a remote connection where the reply routes correctly without an explicit `server`.

## Testing

```bash
python3 test_client.py                 # handshake + tools/list assertion (16 tools)
python3 test_client.py --chat "hello"  # adds a real create_session + chat round-trip
```

The scenario suite lives in [`tests/`](../tests/README.md): the core interaction flows, the
subagent/subtree scenarios, and the spawned-serve lifecycle paths. It is plain stdlib Python
and reports `PASS` / `FAIL` / `SKIP`; scenarios that need an extra environment (an existing
opencode server, a remote instance, a delegation-capable agent) are skipped rather than failed,
and are enabled through `OPENCODE_TEST_*` variables documented in `tests/README.md`.

The smoke test spawns its own server process; with `OPENCODE_URL`/`OPENCODE_PASSWORD` unset it exercises the MCP-spawned local path directly.
