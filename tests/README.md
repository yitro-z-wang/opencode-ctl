# opencode-mcp live tests

**English** | [中文](README.zh-CN.md)

Live, end-to-end suites for `server.py` (the MCP) and `octl` (the CLI). The
MCP suites spawn the real MCP over stdio and exercise the connection layer, the
permission/form loops, the subagent subtree semantics and the spawned-serve
lifecycle. The CLI suites exercise the `octl` contract offline and against a
real `opencode serve`.

## Prerequisites

- Python 3.10 or newer. The suites use the standard library only: no pytest and
  no third-party packages.
- `opencode` on `PATH` for the local/spawn scenarios and for the whole
  lifecycle suite. If you would rather test against an already running server,
  set `OPENCODE_TEST_URL` instead.
- For `live_subagent_test.py`: an agent configuration that can delegate to a
  child session. The suite probes this politely and skips when it cannot.

## Running

Each suite is standalone and prints one line per scenario plus a summary line:

```sh
python3 tests/live_core_test.py
python3 tests/live_subagent_test.py
python3 tests/live_lifecycle_test.py
python3 tests/cli_offline_test.py
python3 tests/live_cli_test.py
```

The process exit code is `1` only when at least one scenario **fails**. Skips
keep the exit code at `0`, so an environment that cannot support a scenario
never turns into a red build.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `OPENCODE_TEST_URL` | Point the MCP at an existing opencode server (for example `http://127.0.0.1:4096`). When unset, the MCP spawns its own private `serve`. |
| `OPENCODE_TEST_PASSWORD` | Password for `OPENCODE_TEST_URL`. When unset, no password is forwarded. |
| `OPENCODE_TEST_REMOTE_URL` | A remote opencode instance for the remote scenarios. When unset, those scenarios report SKIP. |
| `OPENCODE_TEST_REMOTE_PASSWORD` | Password for `OPENCODE_TEST_REMOTE_URL` (optional; only sent when set). |
| `OPENCODE_TEST_AGENT` | Agent used when creating sessions. When unset, the server's own default is used. |
| `OPENCODE_TEST_MODEL` | Optional model in `providerID/modelID` form. When unset, no model is pinned. |
| `OPENCODE_TEST_TIMEOUT` | Per-scenario wait budget in seconds. Default `60`. |

The helper maps `OPENCODE_TEST_URL` / `OPENCODE_TEST_PASSWORD` onto the MCP's
own `OPENCODE_URL` / `OPENCODE_PASSWORD` when it spawns the server, so the
suite exercises the product's normal env-connection path.

## Coverage

### `live_core_test.py`

- Explicit-env connection vs MCP-spawned local connection (`source=env` vs
  `source=spawned`).
- Failure classification: an unreachable port is an `[availability]` error, a
  reachable non-opencode HTTP endpoint is a `[compatibility]` error, and wrong
  credentials are an `[availability]` error.
- Duplicate connection name and unknown connection name errors; the local
  connection cannot be disconnected.
- Session auto-routing across two registered connections.
- `chat` happy path; `wait_session` terminal state and the incremental
  `get_messages` cursor.
- Manual permission loop (`chat` → `needs_permission` → `permission_reply` →
  `wait_session`); form loop (`pending_interactions` → `form_reply`); automatic
  permission.
- `wait_session` timeout while still generating, and `interrupted` after an
  interrupt.
- `notifications/cancelled` returning promptly and discarding the chat
  response; concurrent `pending_interactions` while a chat is in flight.
- `get_context` and `compact`.
- Remote end-to-end loop (connect → create → chat → manual permission → reply →
  wait → incremental `get_messages` → disconnect), only with the remote env set.

### `live_subagent_test.py`

All scenarios require a delegation-capable environment; each reports SKIP when
no child session appears within the bounded probe window.

- Manual mode: `wait_session(wait_for_subagents=true)` does not report a
  premature `succeeded`; it returns `needs_permission` whose `session_id` is the
  subagent and which carries `root_session_id`; the reply uses the subagent id.
- `chat` default (`wait_for_subagents` unset/false): returns `succeeded` but
  reports `pending_subagents >= 1` and a non-empty `subagents` list.
- Automatic mode: `auto_permission="once"` answers the subagent's request and
  the wait eventually reaches `succeeded`.
- Form ownership: a subagent blocked on a form yields `needs_form` whose
  `session_id` and `forms[].sessionID` are the subagent; `form_reply` uses the
  subagent id.
- Multiple parallel subagents: each pending request is reported with its own
  owning session id.
- Remote (only with the remote env set): `needs_permission` names the remote
  subagent and a `permission_reply` **without** an explicit `server` still
  reaches the right connection.

### `live_lifecycle_test.py`

With a clean environment the MCP spawns its own `serve`. The suite forces that
path via `list_servers`, locates the child process and asserts it is gone within
a few seconds for each shutdown path: stdin EOF, `SIGTERM` and `SIGINT`. The
whole suite reports SKIP when `opencode` is not on `PATH`.

### `cli_offline_test.py`

The offline suite for the `octl` CLI (Phase 2 of the opencode-ctl refactor).
No external network: it exercises config parsing (`endpoints.toml`, both
`password` and `password_command`, plus the loose-permissions warning), the
routes database (write / lookup / miss), URL-as-alias rejection, the
`OpenCodeError` kind -> exit-code mapping (monkeypatched), stdin JSON argument
handling for `chat` / `form-reply`, and the credential non-leak invariant
(wrong password and a failing `password_command`) against a throwaway HTTP
server bound to `127.0.0.1`.

### `live_cli_test.py`

The harness (not `octl`) spawns `opencode serve` on a free port with a random
`OPENCODE_SERVER_PASSWORD`, waits for readiness and writes a temporary
`endpoints.toml` into a throwaway `XDG_CONFIG_HOME`, then drives the normal
agent loop: `doctor` -> `create` -> `chat` (async) -> `wait` -> `messages
--after` -> `delete`. Each step asserts the exit code and JSON keys and that the
server password never leaks into stdout+stderr; `api_version_warning` is
tolerated when present. Reports SKIP when `opencode` is not on `PATH`.

## Skip behaviour

- `opencode` not on `PATH` and no `OPENCODE_TEST_URL` → the local scenarios and
  the lifecycle suite report SKIP.
- No `OPENCODE_TEST_URL` → the explicit-env scenario reports SKIP; the
  MCP-spawned scenario still runs.
- No local password available → scenarios that need direct HTTP API access
  (permission loop, form loop, session routing, duplicate name, remote manual
  permission) report SKIP.
- No remote environment → the remote scenarios report SKIP.
- Subagent environment cannot delegate → the subagent scenarios report SKIP
  with a hint to set `OPENCODE_TEST_AGENT` to a delegation-capable agent.

When `OPENCODE_TEST_URL` is not set, the helper discovers the MCP-spawned local
server from `list_servers` and recovers the random child password from the
spawned process environment (best effort) so the direct-API scenarios can run
without any extra setup. When that recovery is not possible, those scenarios
simply SKIP.
