> Written for the retired MCP surface (superseded by DESIGN-opencode-ctl.md); the interaction/session/subtree semantics carry over to the octl CLI.

# Permission and form flows

**English** | [中文](interactions.zh-CN.md)

Part of the [opencode-mcp](../README.md) documentation.

### Automatic mode (default locally)

`chat` with the default `auto_permission="once"` answers permission requests automatically and keeps waiting — usually one call returns `succeeded`.

### Manual mode

1. `chat(auto_permission="manual")` returns `needs_permission` with a `requests` list:
   ```json
   {
     "status": "needs_permission",
     "server": "local",
     "session_id": "ses_abc",
     "requests": [{"id": "per_...", "action": "shell", "resources": ["echo hi"], "save": ["echo *"]}]
   }
   ```
2. Decide per request and call `permission_reply(session_id, request_id, decision, message?)`.
3. Call `wait_session(session_id)` to continue. New permission requests surface as `needs_permission` again; the terminal status is `succeeded / failed / interrupted`, after which `get_messages(after_message_id=...)` pulls the incremental reply.

> If a request was already handled elsewhere, `permission_reply` may error — just call `wait_session` or `pending_interactions` to re-check state.

### Form flow

1. `chat` / `wait_session` returns `needs_form` with field details.
2. Answer with `form_reply(form_id, answer)`.
3. Call `wait_session` to continue until terminal.

## Permission rules and action naming

Permission actions match tool names (measured: `shell`, `bash`, `edit`, `write`, `read`, `glob`, `grep`, `webfetch`, `external_directory`, ...); `resource` is the command text or path pattern (e.g. `*`). Rule `effect` is one of `allow / deny / ask`.

To force `ask` on a session (e.g. to exercise the manual flow), include a ruleset at creation — via the raw API, since `create_session` does not pass permissions through:

```bash
curl -u "opencode:$PASSWORD" -X POST "$URL/api/session" -H 'Content-Type: application/json' \
  -d '{"title": "perm test", "permissions": [{"action": "shell", "resource": "*", "effect": "ask"}]}'
```

Then `chat(..., auto_permission="manual")` deterministically exercises `needs_permission`; answer with `permission_reply` and continue with `wait_session`.
