# octl CLI tests

**English** | [中文](README.zh-CN.md)

Offline and live suites for the `octl` CLI (`octl_core.py` + `octl`). The offline
suite exercises the CLI contract with no external network; the live suite drives
the normal agent loop against a real `opencode serve`.

## Prerequisites

- Python 3.11 or newer. The suites use the standard library only: no pytest and
  no third-party packages.
- `opencode` on `PATH` for the live suite. It spawns its own `serve` and reports
  SKIP when the binary is absent. The offline suite needs no server.

## Running

Each suite is standalone and prints one line per scenario plus a summary line:

```sh
python3 tests/cli_offline_test.py
python3 tests/live_cli_test.py
```

The process exit code is `1` only when at least one scenario **fails**. Skips
keep the exit code at `0`, so an environment that cannot support a scenario
never turns into a red build.

## Environment

The suites need no `OPENCODE_TEST_*` variables. The live harness (not `octl`)
spawns `opencode serve` on a free port with a random `OPENCODE_SERVER_PASSWORD`
and writes a throwaway `endpoints.toml` into a temporary `XDG_CONFIG_HOME`, so it
is self-contained. The offline suite likewise redirects `XDG_CONFIG_HOME` /
`XDG_STATE_HOME` to a throwaway directory of its own.

## Coverage

### `cli_offline_test.py`

The offline suite for the `octl` CLI. No external network: it exercises config
parsing (`endpoints.toml`, both `password` and `password_command`, plus the
loose-permissions warning), the routes database (write / lookup / miss),
URL-as-alias rejection, the `OpenCodeError` kind -> exit-code mapping
(monkeypatched), stdin JSON argument handling for `chat` / `form-reply`, and the
credential non-leak invariant (wrong password and a failing `password_command`)
against a throwaway HTTP server bound to `127.0.0.1`.

The throwaway server models opencode v2 `Session.outcome` semantics exactly: it
has no running value, is not reset when a new prompt is enqueued and is only
rewritten at a terminal transition together with a monotonic `time.idle` bump.
Five round-gate scenarios (the stale-outcome defense) drive that model via a
`POST /__test/complete` control endpoint:

- stale succeeded: after a completed turn, a new `chat` + `wait --timeout` must
  time out (exit 6, `diagnostics.round_gate.watermark_passed == false`) instead
  of returning the previous round, then succeed once the turn really completes;
- stale failed: a previous `failed` outcome is never returned for the new round;
- legacy fallback: with no recorded `rounds` row, `wait` trusts the raw outcome
  immediately (documented transitional behaviour);
- idle-message gate: a watermark bump without a `type:"idle"` message after the
  gate message still times out (`idle_message_seen == false`);
- `--once`: reports `status:"running"` + `round_open` + `round_gate` while the
  round is open, then `succeeded` and closes the row so a later wait is legacy.

### `live_cli_test.py`

The harness (not `octl`) spawns `opencode serve` on a free port with a random
`OPENCODE_SERVER_PASSWORD`, waits for readiness and writes a temporary
`endpoints.toml` into a throwaway `XDG_CONFIG_HOME`, then drives the normal
agent loop: `doctor` -> `create` -> `chat` (async) -> `wait` -> `messages
--after` -> `delete`. It then runs a two-round smoke: a second `chat` with a
unique marker phrase followed by an optional `--once` and a blocking `wait`,
asserting the second round's text is returned (never the stale first-round
outcome). Each step asserts the exit code and JSON keys and that the server
password never leaks into stdout+stderr; `api_version_warning` is tolerated when
present. Reports SKIP when `opencode` is not on `PATH`.

## Skip behaviour

- `opencode` not on `PATH` → the live suite reports SKIP (exit 0).
- The offline suite has no external dependency and never skips.
