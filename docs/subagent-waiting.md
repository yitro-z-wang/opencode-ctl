> Written for the retired MCP surface (superseded by DESIGN-opencode-ctl.md); the interaction/session/subtree semantics carry over to the octl CLI.

# Subagent-aware waiting (multi-agent sessions)

**English** | [中文](subagent-waiting.zh-CN.md)

Part of the [opencode-mcp](../README.md) documentation.

A session can delegate to subagent sessions and then end its own turn **while they keep working**. opencode's `outcome` is per-turn — it means "this session has no generation running right now", not "the task is done" — so a naive wait reports success while delegated work is still in flight and can even leave a subagent blocked on an approval nobody will ever see.

This server therefore resolves the session's subagent **subtree** from the authoritative `parentID` links (`GET /api/session?parentID=…`, depth ≤ 3, ≤ 64 nodes, no transcript heuristics) and refuses to report `succeeded` while anything in it is still live:

- `wait_session` defaults to `wait_for_subagents: true` — it returns `succeeded` only once the whole subtree is quiescent: no node generating (per `/api/session/active`) and no pending permission or form anywhere inside it.
- `chat` defaults to `wait_for_subagents: false` — it returns when this round stops streaming, but the payload always carries `subagents`, `pending_subagents`, `subtree_truncated` and `subtree_verified`, so a caller can see that work is still running and follow up with `wait_session`.
- **A subagent's blocking interaction surfaces too**: a child waiting for approval comes back as `needs_permission` whose `session_id` is the **subagent that owns the request** (plus `root_session_id`), and the reply is routed to the connection that owns that session. With `auto_permission` in `once` / `always` / `reject`, the answer is applied to the whole subtree; forms are never auto-answered.
- **Fail-closed**: if the activity map or the pending-interaction state cannot be verified, `succeeded` is never returned — the wait keeps polling to its timeout and says what could not be verified (`subtree_verified: false`). A server missing these endpoints is detected once per connection and falls back to the previous behavior, which is reported rather than passed off as verified.
