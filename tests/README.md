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

### `live_cli_test.py`

The harness (not `octl`) spawns `opencode serve` on a free port with a random
`OPENCODE_SERVER_PASSWORD`, waits for readiness and writes a temporary
`endpoints.toml` into a throwaway `XDG_CONFIG_HOME`, then drives the normal
agent loop: `doctor` -> `create` -> `chat` (async) -> `wait` -> `messages
--after` -> `delete`. Each step asserts the exit code and JSON keys and that the
server password never leaks into stdout+stderr; `api_version_warning` is
tolerated when present. Reports SKIP when `opencode` is not on `PATH`.

## Skip behaviour

- `opencode` not on `PATH` → the live suite reports SKIP (exit 0).
- The offline suite has no external dependency and never skips.
