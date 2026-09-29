# DESIGN — opencode-ctl (`octl`)

Status: **finalized** (2026-09-30). This document takes over the standing of `DESIGN-remote-connections.md` (which is demoted to a historical record). The full decision process of the refactor discussion is in the repo session records; this document records only the conclusions and their basis.

---

## 0. One-sentence positioning

`octl` is an agent-facing opencode command-line client: the agent calls the CLI following the skill's guidance, and the CLI drives an operator-pre-deployed `opencode serve`. **No MCP server, no self-built daemon** — the daemon is `opencode serve` itself.

## 1. Architecture

```
agent ──read──> skills (SKILL.md)                  teaches "how to use"
      ──call──> octl (CLI, one process per call)   session surface: 14 verbs
                │  core lib (stdlib-only)          domain logic: connection/classification/subtree/wait
                └──HTTP──> opencode serve (operator-deployed, multi-client, multi-directory)
                          ~/.config/octl/endpoints.toml (operator-private)
```

Three-layer responsibilities:

- **core lib**: domain logic extracted from the existing `server.py` (pure stdlib, no process-state assumptions);
- **CLI (`octl`)**: one process per command, JSON→stdout, logs→stderr, exit code = result class;
- **skills**: standard SKILL.md, teaches the interaction loop, carries no credentials and no URLs.

What is NOT built (YAGNI, with triggers):

- event fan-out / custom daemon — trigger: single-machine multi-agent concurrency with mutual distrust, or a shared team host;
- per-session ACL / multi-tenancy — same as above;
- project-level config layering — no requirement (v1 is global config + env only);
- any form of automatic permission answering — see §5;
- the MCP surface — the existing `server.py` is deleted wholesale once the CLI reaches feature parity;
- `octl` spawning `opencode serve` — serve is deployed by the operator, `_spawn_local_serve` dies wholesale.

Basis (external evidence, 2026-09 research): MCP tool-schema token overhead is 4–32× that of an equivalent CLI; Skills have become an open standard (agentskills.io, ~40 compatible vendors); opencode v2 natively supports a shared serve across multiple clients (the TUI is one client). The parts MCP keeps (OAuth/audit/structured IO) have no requirement in this project's scenario.

## 2. Threat model and security boundary

**In scope** (this project's responsibility):

- the CLI verb surface exposes no credentials: no credential arguments, no endpoint-writing verbs, no credential echo (including error and debug output);
- the agent cannot enumerate sessions: no list verb; pending-type queries are filtered precisely by session subtree, and return an empty set when `verified=false` (fail closed — better to under-report than to overstep);
- the agent cannot specify a URL: only aliases, and the alias→endpoint mapping exists only in the operator config;
- HTTP transport hygiene: response-body limits (see §7), follow redirects but strip `Authorization` across origins.

**Out of scope** (explicitly not defended, documented so no future "fix" tries):

- **same-user malicious agent**: a same-user agent with a bare shell can read the config file and the opencode state directory directly. That is the host's permission-gating responsibility; the CLI layer cannot backstop it;
- **path intrusion** (http cleartext link eavesdropping, TLS-intercepting proxy): on the same level as "the endpoint is compromised", not defended separately;
- **the operator configured a malicious endpoint**: the endpoint receives its own credentials, no incremental leakage surface.

Credential-channel principle (inherited from the PR #1 lesson and strengthened): **secrets enter only through the operator channel (config file / env / password_command); the agent holds only non-secret references (aliases, ses_).**

## 3. Endpoints and credentials

Config file `~/.config/octl/endpoints.toml` (0600, hand-edited by the operator, no CLI write verbs):

```toml
default = "main"

[endpoints.main]
url = "http://127.0.0.1:4096"
username = "opencode"                      # optional; v2 basic-auth default user
password = "..."                           # one of the two

[endpoints.lab]
url = "https://build-box.example.com:4096"
password_command = ["pass", "show", "opencode/lab"]   # alternative channel: secret never on disk
```

Resolution order: explicit `--endpoint <alias>` → the configured `default` → `OPENCODE_URL` / `OPENCODE_PASSWORD` env. **No service discovery** (users figure out for themselves which instances exist; `doctor` does not discover either).

The CLI surface has only: `endpoints` (enumerate aliases + URL + version-baseline status, no liveness probe), `endpoints --check` (per-endpoint liveness + auth check + version baseline). Add/remove/change = hand-edit the file.

## 4. Session location semantics

Basis (source + official docs verified, 2026-09-30):

- `POST /api/session` without `location`: directory = **the serve process's cwd** (not the client cwd), uncontrollable under a shared deployment;
- a single serve natively carries multi-directory sessions; config/skills/AGENTS.md load per-location by session directory (service graph 60min idle TTL);
- all official clients (desktop/TUI/SDK) explicitly pass their own directory.

Rules:

- `octl create` **unconditionally sends** `location: {directory: <absolute path>}`; default = cwd at call time, `--directory` overrides;
- subsequent commands address by `ses_`; serve resolves the directory from the session row itself, and the CLI no longer passes location;
- multi-repo work does not need multiple endpoints: one shared serve + a directory per create;
- core uniformly unwraps the runtime-routed `{location, data}` response envelope (the existing server.py already handles the measured shape, carried along with the extraction).

## 5. CLI contract

### Verb table (relative to the existing 16 MCP tools)

| Deleted | Reason |
|---|---|
| `connect_server` / `disconnect_server` | dynamic registration ends; the config file is the only source of endpoints |
| `list_servers` | → `endpoints` |
| `list_sessions` | session enumeration is not allowed (multiple agents rely on `ses_` being unpredictable for isolation) |

Retained mapping: `doctor` `endpoints` `agents` `create` `chat` `wait` `messages` `permission-reply` `form-reply` `pending` `interrupt` `compact` `context` `delete`.

### Parameter naming conventions (contract refinements crystallized during the Phase 3 implementation)

- session addressing flag: `-s SES` / `--session SES`;
- `permission-reply -s SES --request-id RID --decision once|always|reject [--message M]`;
- `form-reply -s SES --form-id FID`, field answers passed as a JSON object on stdin (`{"fieldKey": value}`);
- cursor field name: `last_message_id` (consistent with the existing MCP `get_messages`/`wait` output);
- wait flag: `--timeout` (seconds, default 300), not `timeout_secs`;
- `create` returns JSON with top-level key `session_id`;
- `chat` v1 parameter surface: `--text` (or stdin JSON `{"text": ...}`); v2 `files`/`delivery` do not enter v1;
- verbs not listed with flags (`pending`/`interrupt`/`compact`/`context`/`delete`) use only `-s SES` (+ global `--endpoint`).

### Key semantics

- **Output**: JSON→stdout, human logs→stderr; exit codes: 0 success / 2 usage / 3 `[availability]` / 4 `[compatibility]` / 5 `[other]` / 6 timeout / 7 needs_permission / 8 needs_form. The `status` field is always in the JSON too;
- **`chat`**: enqueue asynchronously and return (v2 prompt semantics), does not block; complex/long text parameters go through stdin JSON (prevents quote injection);
- **`wait`**: `--timeout` default **300s** (blocking long-poll, loops within a single process); `--once` = single snapshot + cursor (the agent manages its own loop); **manual permission mode only, no automatic answering of any kind** — permissions must be approved by the agent. The skill explicitly instructs: answer `always` to safe requests to reduce turns;
- **`pending` / pending-type queries**: subtree-scoped (precise idset membership filtering over the `parentID` tree; a subagent request is reported to the parent-session controller, with the payload carrying the real owner `sessionID`); fall back to the per-session endpoint when the global endpoint is unavailable; failing that, `verified=false` empty set, fail closed;
- **`permission-reply` / `form-reply`**: addressed within the session (the `/api/session/{sid}/permission/{rid}/reply` path itself is bound to the session);
- **pre-trust path** (the proper way to reduce permission turns): after implementation is complete, explore opencode's native permissions config for a pre-authorization allowlist (project directories, `/tmp/opencode/`, etc.), as a follow-up enhancement, not part of v1.

### Route cache

`~/.local/state/octl/routes.db` (**sqlite**, table `routes(ses_ TEXT PRIMARY KEY, endpoint TEXT, created_at)`). Written at `create`; subsequent commands resolve the endpoint automatically by ses_; on a miss, error asking for an explicit `--endpoint`. sqlite rather than JSON+atomic write: transactions and corruption detection are built in, minimizing corruption probability and rebuild logic.

### Version policy

- `octl doctor`: hard gate — reachability, `/api/info` shape (only the v2 API passes), auth, version-baseline report; no discovery;
- regular commands: trust the config, zero probe overhead; on failure classify into `[availability]/[compatibility]/[other]` exit codes (re-check the version after failure and then classify, inheriting the existing mechanism);
- version/baseline drift: warn, do not block (opencode iterates fast; hard blocking would break frequently).

## 6. skills

- repo `skills/octl/SKILL.md` single file (standard SKILL.md format, compatible with agentskills.io hosts);
- teaches the loop: `doctor` (first session step) → `create` → `chat` → `wait` → branch [needs_permission → approve, answer `always` to safe requests → `permission-reply` → `wait`] → terminal state → `messages --after <cursor>`;
- advanced content (forms/subagents/compact) is a same-level section, not split into files;
- lock the CLI major version; bilingual (en / zh-CN).

## 7. core extraction and the disposition of PR #1

Extract from `server.py`: the connection object, `http_request`, probe/version gate, failure classification, subtree resolution, the `poll_once` split (poll body = state snapshot + cursor; the loop shell stays in the surface layer), data shaping.

From PR #1 **take only two things** (response-body caps re-estimated from measurement: the cap applies to the **raw message**; the messages endpoint embeds full tool I/O, and 1 MiB would hurt tool-I/O-heavy sessions):

1. **response-body cap, two tiers**: default endpoints (info/session/permission/form, normally KB-scale) **4 MiB**; message fetching (messages / the `assistant_text` path of wait) **64 MiB**; HTTP error body 64 KiB read cap. A declared Content-Length is pre-checked at the same tier (rejected before reading); chunked transfer is bounded by the actual-read cap — a general client hygiene independent of URL origin;
2. **redirect policy**: follow redirects, but **strip `Authorization` when Location crosses origins** (the common posture of curl `--location` / browser fetch / Go `net/http`; urllib and requests replaying it as-is by default is a known footgun).

Not ported: the SSRF URL gate (the agent no longer submits URLs; porting it wholesale would hurt legitimate private-network endpoints), credential-channel changes (replaced by the architecture itself: there are simply no credential parameters on the agent surface), MCP stdio hardening (stdin line cap/workers/params tolerance, dies with the server).

PR #1 closed, labelled superseded by opencode-ctl refactor; its review findings F1 (`::1` unwrap ordering) / F2 (probe-path re-verification gap) / F3 (stdin fake cap) die with the gate, no fix needed.

## 8. Migration plan (all green each step)

| Phase | Content |
|---|---|
| 0 | GitHub rename to `opencode-ctl` (old URL auto-redirects); settle Python ≥3.11 / TOML |
| 1 | core extraction; `server.py` changed to reference core, behavior unchanged; live tests (MCP driver) all green; fold in the two §7 hardenings |
| 2 | CLI (`octl`) implements the full §5 contract; test suite driver parameterized (MCP/CLI dual-run) |
| 3 | skills (`skills/octl/SKILL.md` bilingual) + install matrix (each host's skill directory) |
| 4 | delete `server.py`, the MCP tests and the `mcp_client.py` harness; close PR #1; rewrite the README bilingually; mark `DESIGN-remote-connections.md` as a historical record |

## 9. Language and documentation conventions

- code/CLI machine output: English; documentation: en + zh-CN bilingual (existing project convention);
- the English version of this document follows after the Chinese draft is finalized; the README is rewritten in Phase 4.
