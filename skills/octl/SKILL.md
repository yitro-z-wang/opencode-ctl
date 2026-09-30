---
name: octl
description: Drive an operator-deployed opencode server from the command line — create a session, send a prompt, wait for the outcome, then handle permission and form interactions. Use when you need to run or control an opencode agent session through the octl CLI.
---

# octl — drive opencode sessions from the CLI

`octl` is a one-shot CLI client for an operator-deployed `opencode serve`. Each call is a
process: JSON goes to **stdout**, human logs go to **stderr**, and the **exit code** is the
result class. Parse stdout only.

## The loop

**a. First use in a session — gate.** Run `octl doctor` and fix everything it reports before
proceeding. `doctor` is the hard gate: reachability, `/api/info` shape (v2 API only), auth, and
version baseline. It does not discover endpoints.

**b. Start a round.**
```
octl create                      # runs from the project dir; or: octl create --directory /abs/path
octl create --trust "/tmp/opencode/*"   # pre-trust scratch paths for this session only
# -> JSON containing a ses_... id
octl chat -s ses_... --text "your instruction"   # async: enqueues and returns immediately
octl wait -s ses_... --timeout 1800 > /tmp/opencode/wait-ses_....json   # via your shell tool's background mode, never foreground (see "Waiting")
```
`create` always sends an explicit location (the current directory unless `--directory` is given);
after that, every command addresses the session only by its `ses_` id. For a long or complex
prompt, pass the body as **JSON on stdin** instead of in argv (avoids quote injection).

**c. Exit 7 = needs_permission.** Inspect the returned request details (request id, tool, args).
Approve **safe, routine** operations with `always` to reduce future interaction turns; use
`once` to allow just this time; use `reject` to deny.
```
octl permission-reply -s ses_... --request-id per_... --decision always
octl wait -s ses_...
```

**d. Exit 8 = needs_form.** Fill the form from the returned `needs_form` payload with
`octl form-reply -s ses_... --form-id FID` (send structured field answers as JSON on stdin), then `wait` again.

**e. Exit 0 = success.** Read the new output incrementally:
```
octl messages -s ses_... --after <last_message_id>
```
Use the `last_message_id` from the wait/messages JSON as the next cursor; do not re-read the
whole conversation.

**f. No background shell? Poll.** `octl wait -s ses_... --once` returns one snapshot plus
a cursor and exits immediately (`--timeout` is ignored). Loop it with a short sleep until
`status` leaves `running` — exit code 0 covers `running` too, so branch on the JSON `status`
field. Also the cheap liveness probe when a background wait's output is missing.

## Waiting — always in the background

`chat` only enqueues; `wait` closes the round. Foreground shell calls are cut off by your
runtime's command timeout (usually minutes) — shorter than a medium/large round — leaving no
JSON, no exit code, and an open round. So:

**Run every `wait` — the first and every re-arm — in the background** through your shell
tool's background mode (run-in-background bash, background terminal, or equivalent), stdout
redirected to a file. The same applies to `compact`, which also blocks.

```
octl wait -s ses_... --timeout 1800 > /tmp/opencode/wait-ses_....json   # background
```

- `--timeout` is a checkpoint, not a deadline: size it to the round (1800–3600s for large
  ones); exit 6 means "still running", with `partial_text` diagnostics.
- When the background call completes, read the file and dispatch on the JSON `status` field
  (always present).
- `succeeded` implies the whole session subtree (subagents too) is quiet — one wait tracks
  all delegated work.

| Outcome | Action |
|---|---|
| `succeeded` (0) | round done — read new output: `messages --after <last_message_id>` |
| `timeout` (6) | still running — re-arm the background `wait` |
| `needs_permission` (7) | `permission-reply`, then re-arm |
| `needs_form` (8) | `form-reply`, then re-arm |
| `failed` / `interrupted` (5) | read the partial output, then decide |
| no JSON / process vanished | assume still running — probe with `wait --once`, then re-arm |

**Never end a turn with an open round:** either hold the verified outcome, or a background
wait is live and the closing message says so (session id + "still running"). If your runtime
has no background mode — or does not notify on completion — use the §f `--once` poll loop
and keep looping within the turn until `status` leaves `running`.

## Exit codes

| Code | Status | Meaning |
|---|---|---|
| 0 | success | command completed; blocking `wait` reached a terminal state (`chat` 0 = enqueued only; `--once` 0 may be `running`) |
| 2 | usage | bad invocation or arguments |
| 3 | `[availability]` | endpoint unreachable / unavailable |
| 4 | `[compatibility]` | API or version incompatible (requires the v2 API) |
| 5 | `[other]` | unclassified error |
| 6 | timeout | wait/chat timed out; the session may still be generating |
| 7 | needs_permission | blocked, awaiting permission approval |
| 8 | needs_form | blocked, awaiting form input |

The `status` field is also always present in the JSON.

## Verbs

| Verb | Purpose |
|---|---|
| `doctor` | Session-first hard gate: reachability, `/api/info` v2 shape, auth, version baseline. |
| `endpoints` | List endpoint aliases + URL + version baseline status (no liveness probe); `--check` adds per-endpoint liveness, auth check, and baseline. |
| `agents` | List agents and their resolved default models (read-only). |
| `create` | Create a session; sends the cwd as location, `--directory` overrides. `--trust PATTERN` (repeatable) pre-authorizes paths for this session. `--model PROVIDER/ID[#variant]` pins this session's model. Returns a `ses_` id. |
| `chat` | Enqueue a prompt asynchronously and return (does not block). |
| `wait` | Wait for a terminal or needs-interaction state; `--timeout` default 300s; `--once` = single snapshot + cursor. Always run in the background — see "Waiting". |
| `messages` | Fetch messages; `--after <id>` returns only new output and the next cursor. |
| `permission-reply` | Answer a permission request: `--request-id` plus `--decision once\|always\|reject`. |
| `form-reply` | Submit a form's answers: `--form-id` plus the answers as JSON on stdin. |
| `pending` | List pending permissions/forms for the session subtree (non-blocking). |
| `interrupt` | Interrupt the generation currently in progress. |
| `compact` | Compact the session context and wait for completion. |
| `context` | Show context usage (tokens/cost) and session metadata. |
| `delete` | Delete the session; irreversible, cascades to child sessions. |

## Notes

- **JSON out, logs aside:** machine-readable results are on stdout; parse stdout only.
- **Session addressing:** pass `-s ses_...`; an explicit endpoint is `--endpoint <alias>`.
  `create` records the route, so later commands resolve the endpoint from the `ses_` id; on a
  miss, pass `--endpoint` explicitly.
- **Pending is subtree-scoped:** a subagent's permission/form is reported to the parent
  controller, and its payload carries the request's real owner `sessionID`. Reply using that
  sessionID, not necessarily the parent's.
- **Pre-trust a scratch dir:** `create --trust PATTERN` (repeatable) pre-authorizes resources for this
  session only — caller-declared, session-scoped, dies with the session — e.g. `/tmp/opencode/*` to
  avoid permission rounds.
- **Pin a model per session:** `create --model PROVIDER/ID[#variant]` overrides the server's default
  model for this session only — e.g. a cheap flash model for simple tasks.
- **Endpoints are operator-configured:** use aliases only — never URLs or passwords.
- **interrupt** cancels a running task (e.g. after a timeout); **compact** trims context when it
  nears its limit; **context** shows tokens/cost before compacting; **delete** removes a session
  and all of its children.

Written for octl v1 (CLI contract v1).
