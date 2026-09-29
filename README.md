# opencode-ctl (`octl`)

**English** | [中文](README.zh-CN.md)

`octl` is an agent-facing command-line client plus a skill for driving an **operator-deployed**
`opencode serve` over its v2 HTTP API. Each call is a one-shot process: JSON goes to **stdout**,
human logs go to **stderr**, and the **exit code** is the result class.

This repo was renamed from `opencode-mcp`. The original MCP server has been retired now that the
CLI reaches feature parity — see [DESIGN-remote-connections.md](DESIGN-remote-connections.md) for
the decision record behind it.

## Requirements

- **Python ≥ 3.11** (standard library only; no third-party packages).
- An **`opencode serve` that the operator runs**. `octl` never spawns a server — it only talks to
  one that is already up.
- Optional: a `pass`-style helper on `PATH` if you configure credentials via `password_command`.

## Install

```sh
git clone https://github.com/yitro-z-wang/opencode-ctl ~/opencode-ctl
ln -s ~/opencode-ctl/octl ~/.local/bin/octl     # ensure ~/.local/bin is on PATH
```

Install the skill by copying (or symlinking) `skills/octl/` into your agent host's skill directory,
for example:

- `~/.opencode/skills/octl/` (project-local: `.opencode/skills/`)
- `~/.claude/skills/octl/`
- the equivalent skill directory for any other SKILL.md-compatible host

## Configuration

`octl` reads a single operator-owned file at `~/.config/octl/endpoints.toml` (mode **0600**). There
are no CLI verbs that write it — edit it by hand.

```toml
default = "main"

[endpoints.main]
url = "http://127.0.0.1:4096"
username = "opencode"                      # optional; v2 basic-auth default user
password = "..."                           # either password ...

[endpoints.lab]
url = "https://build-box.example.com:4096"
password_command = ["pass", "show", "opencode/lab"]   # ... or a secret that never hits disk
```

Resolution order: explicit `--endpoint <alias>` → the configured `default` →
`OPENCODE_URL` / `OPENCODE_PASSWORD` environment variables. Endpoints are addressed by **alias
only**; there is no service discovery.

## Quickstart

```sh
octl doctor                                    # hard gate: reachability, v2 shape, auth, baseline
octl create                                    # -> JSON with a ses_... session id
octl chat -s ses_... --text "your instruction" # async: enqueues and returns immediately
octl wait -s ses_...                           # blocks up to --timeout (default 300s)
octl messages -s ses_... --after <cursor>      # incremental read from the last_message_id cursor
```

`create` always sends an explicit location (the current directory unless `--directory` is given);
after that every command addresses the session by its `ses_` id. For long or complex prompts, pass
the body as JSON on stdin (`{"text": ...}`) instead of argv to avoid quote injection.

**Exit codes**

| Code | Status | Meaning |
| --- | --- | --- |
| 0 | success | command completed; for `wait`/`chat`, a terminal outcome |
| 2 | usage | bad invocation or arguments |
| 3 | `[availability]` | endpoint unreachable / unavailable |
| 4 | `[compatibility]` | API or version incompatible (requires the v2 API) |
| 5 | `[other]` | unclassified error |
| 6 | timeout | `wait`/`chat` timed out; the session may still be generating |
| 7 | needs_permission | blocked, awaiting permission approval |
| 8 | needs_form | blocked, awaiting form input |

The `status` field is also always present in the JSON.

## Security model

- **Credentials enter only through the operator channel** (config file / env / `password_command`).
  The agent surface has no credential arguments and no endpoint-writing verbs.
- **No URLs on the agent surface** — only aliases; the alias → endpoint mapping lives solely in the
  operator's config.
- **No session enumeration** — there is no `list` verb. Pending-interaction queries are scoped to a
  session subtree and fail closed (empty set) when verification is not possible.
- **Operator pre-trust:** operators can additionally pre-trust paths/commands globally through
  opencode's own permission configuration (`permissions` rules in `opencode.json`) — see
  <https://opencode.ai/v2/docs/permissions>.
- **Out of scope:** a malicious agent running as the same user can read the config and opencode
  state directly. That is the host's permission-gating responsibility, not something the CLI layer
  can backstop.

## Docs

- [Design: opencode-ctl (`octl`)](DESIGN-opencode-ctl.md) · [中文](DESIGN-opencode-ctl.zh-CN.md)
- [Agent skill](skills/octl/SKILL.md) · [中文](skills/octl/SKILL.zh-CN.md)
- [Testing](tests/README.md) · [中文](tests/README.zh-CN.md)
- [Historical: design record for remote connections (retired MCP)](DESIGN-remote-connections.md) · [中文](DESIGN-remote-connections.zh-CN.md)
