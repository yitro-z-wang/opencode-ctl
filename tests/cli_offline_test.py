#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline scenarios for the ``octl`` CLI.

Run directly::

    python3 tests/cli_offline_test.py

No external network is used: every scenario either exercises pure config/route
logic or talks to a throwaway HTTP server bound to ``127.0.0.1`` that fakes the
handful of opencode v2 API responses the CLI needs. The scenarios cover config
parsing (TOML, ``password_command``, loose-permission warning), the routes
database (write / lookup / miss), URL-as-alias rejection, the ``OpenCodeError``
kind -> exit-code mapping (monkeypatched), stdin JSON argument handling, the
create permission pre-trust surface (``--trust`` expansion, stdin merge order,
omission when unused, and the ``--model`` per-session pin), the credential
non-leak invariant in a wrong-password run, and five round-gate (stale-outcome
defense) scenarios driven by a ``POST /__test/complete`` control endpoint.

Each scenario prints PASS/FAIL; the process exits 1 when any scenario fails.
See ``tests/README.md``.
"""

import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import http.server
from importlib.machinery import SourceFileLoader

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cli_common import Reporter  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
OCTL_PATH = os.path.join(REPO_ROOT, "octl")


# ---------------------------------------------------------------------------
# A tiny fake opencode v2 API on 127.0.0.1
# ---------------------------------------------------------------------------

class FakeOpenCode:
    """v2-shaped fake whose ``Session.outcome`` stays frozen across a new prompt.

    Per session the fake tracks a ``messages`` list, a monotonic ``time_idle``
    (ms float) watermark and the last completed turn's ``idle_outcome``. A prompt
    records its message id but never touches ``time_idle`` / ``idle_outcome`` --
    exactly the v2 behaviour that makes a bare ``outcome`` stale for the whole
    next run. The ``POST /__test/complete`` control endpoint performs the terminal
    transition (append messages + bump ``time.idle``), optionally ``bump_only``
    to model a foreign turn advancing the watermark without our round's idle
    marker.
    """

    def __init__(self, password=None, version="2.0.12"):
        self.password = password
        self.version = version
        self.requests = []
        self._lock = threading.Lock()
        self._sessions = {}
        handler = self._make_handler()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # -- per-session state (call with self._lock held unless noted) ----------
    def _st(self, sid):
        state = self._sessions.get(sid)
        if state is None:
            state = {
                "messages": [],
                "time_idle": 1000.0,
                "idle_outcome": None,
                "prompt_count": 0,
                "last_prompt_id": None,
                "last_prompt_text": None,
            }
            self._sessions[sid] = state
        return state

    def _record_prompt(self, sid, text):
        with self._lock:
            state = self._st(sid)
            state["prompt_count"] += 1
            pid = "msg_prompt_%d" % state["prompt_count"]
            state["last_prompt_id"] = pid
            state["last_prompt_text"] = text if isinstance(text, str) else ""
            return pid

    def _session_view(self, sid):
        with self._lock:
            state = self._st(sid)
            return {
                "id": sid,
                "title": "fake",
                "outcome": state["idle_outcome"],
                "time": {"idle": state["time_idle"], "created": 0, "updated": 0},
            }

    def _messages_view(self, sid, order="desc", limit=None):
        with self._lock:
            msgs = list(self._st(sid)["messages"])
        if order == "desc":
            msgs = list(reversed(msgs))
        if limit is not None:
            try:
                msgs = msgs[: int(limit)]
            except (TypeError, ValueError):
                pass
        return msgs

    def _complete(self, sid, outcome, text, mode):
        """Terminal transition: mode ``full`` appends messages, ``bump_only`` only bumps."""
        with self._lock:
            state = self._st(sid)
            now = int(time.time() * 1000)
            if mode == "full":
                pid = state.get("last_prompt_id") or "msg_prompt_0"
                count = len(state["messages"])
                state["messages"].append({
                    "id": pid,
                    "type": "user",
                    "text": state.get("last_prompt_text") or "",
                    "time": {"created": now},
                })
                state["messages"].append({
                    "id": "msg_asst_%d" % (count + 1),
                    "type": "assistant",
                    "agent": "build",
                    "model": "fake/model",
                    "time": {"created": now, "completed": now},
                    "content": [{"type": "text", "text": text}],
                })
                state["messages"].append({
                    "id": "msg_idle_%d" % (count + 3),
                    "type": "idle",
                    "outcome": outcome,
                })
            state["time_idle"] = max(float(now), state["time_idle"] + 1.0)
            state["idle_outcome"] = outcome

    def complete(self, sid, outcome="succeeded", text="", mode="full"):
        """Drive the /__test/complete control endpoint from the test process."""
        request = urllib.request.Request(
            self.url + "/__test/complete",
            data=json.dumps({
                "sid": sid, "outcome": outcome, "text": text, "mode": mode,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read()

    def last(self, method=None, suffix=None):
        with self._lock:
            items = list(self.requests)
        for item in reversed(items):
            if method and item["method"] != method:
                continue
            if suffix and not item["path"].endswith(suffix):
                continue
            return item
        return None

    def _make_handler(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _auth_ok(self):
                if outer.password is None:
                    return True
                expected = "Basic " + base64.b64encode(
                    ("opencode:" + outer.password).encode("utf-8")
                ).decode("ascii")
                return self.headers.get("Authorization") == expected

            def _record(self, body):
                with outer._lock:
                    outer.requests.append({
                        "method": self.command,
                        "path": self.path,
                        "body": body,
                        "authorization": self.headers.get("Authorization"),
                    })

            def _respond(self, code, obj):
                body = b"" if obj is None else json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _path(self):
                return urllib.parse.urlsplit(self.path).path

            def _query(self):
                return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

            def do_GET(self):
                self._record(None)
                if not self._auth_ok():
                    self._respond(401, {"error": "unauthorized"})
                    return
                path = self._path()
                if path == "/api/info":
                    self._respond(200, {"version": outer.version})
                elif path.endswith("/permission"):
                    self._respond(200, {"data": []})
                elif path.endswith("/form"):
                    self._respond(200, {"data": []})
                elif path == "/api/agent":
                    self._respond(200, {"data": [
                        {"name": "build", "mode": "primary", "model": None}
                    ]})
                elif path.endswith("/message"):
                    sid = path.split("/api/session/", 1)[1].rsplit("/message", 1)[0]
                    query = self._query()
                    order = (query.get("order") or ["asc"])[0]
                    limit = (query.get("limit") or [None])[0]
                    messages = outer._messages_view(
                        urllib.parse.unquote(sid), order=order, limit=limit
                    )
                    self._respond(200, {"messages": messages})
                elif path.startswith("/api/session/"):
                    sid = urllib.parse.unquote(path.split("/api/session/", 1)[1])
                    self._respond(200, outer._session_view(sid))
                else:
                    self._respond(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = None
                self._record(body)
                path = self._path()
                if path == "/__test/complete":
                    # Harness-only control endpoint: bypasses endpoint auth by design.
                    fields = body if isinstance(body, dict) else {}
                    sid = fields.get("sid")
                    if sid:
                        outer._complete(
                            sid,
                            fields.get("outcome", "succeeded"),
                            fields.get("text", ""),
                            fields.get("mode", "full"),
                        )
                    self._respond(200, {"ok": True})
                    return
                if not self._auth_ok():
                    self._respond(401, {"error": "unauthorized"})
                    return
                if path == "/api/session":
                    title = (body or {}).get("title")
                    self._respond(200, {"id": "ses_test123", "title": title})
                elif path.endswith("/prompt"):
                    sid = urllib.parse.unquote(
                        path.split("/api/session/", 1)[1].rsplit("/prompt", 1)[0]
                    )
                    prompt_id = outer._record_prompt(sid, (body or {}).get("text"))
                    self._respond(200, {"id": prompt_id})
                elif path.endswith("/interrupt"):
                    self._respond(200, {})
                elif path.endswith("/reply"):
                    self._respond(200, {"ok": True})
                elif path.endswith("/compact"):
                    self._respond(200, {"data": {"id": "msg_compact_1"}})
                else:
                    self._respond(200, {})

            def do_DELETE(self):
                self._record(None)
                if not self._auth_ok():
                    self._respond(401, {})
                    return
                self._respond(200, {"ok": True})

            def log_message(self, *_args):
                pass

        return Handler


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------

class Sandbox:
    """A temp XDG config/state pair plus per-call octl runner."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="octl-offline-")
        self.config_home = os.path.join(self.root, "config")
        self.state_home = os.path.join(self.root, "state")
        os.makedirs(self.config_home, exist_ok=True)
        os.makedirs(self.state_home, exist_ok=True)
        self.config_file = os.path.join(self.config_home, "octl", "endpoints.toml")

    def write_config(self, text, mode=0o600):
        os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
        with open(self.config_file, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(self.config_file, mode)

    def env(self):
        env = dict(os.environ)
        env.pop("OPENCODE_URL", None)
        env.pop("OPENCODE_PASSWORD", None)
        env["XDG_CONFIG_HOME"] = self.config_home
        env["XDG_STATE_HOME"] = self.state_home
        return env

    def run(self, args, stdin=None):
        return subprocess.run(
            [sys.executable, OCTL_PATH] + list(args),
            input="" if stdin is None else stdin,
            capture_output=True,
            text=True,
            env=self.env(),
            cwd=REPO_ROOT,
            timeout=60,
        )

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


def parse_json(proc):
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def canary_in(proc, canary):
    return canary in (proc.stdout or "") or canary in (proc.stderr or "")


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def scenario_config_and_endpoints(reporter):
    name = "config parsing and endpoints listing"
    box = Sandbox()
    try:
        box.write_config(
            'default = "main"\n'
            '\n'
            '[endpoints.main]\n'
            'url = "http://127.0.0.1:4096"\n'
            'password = "sup3r-secret-main"\n'
            '\n'
            '[endpoints.lab]\n'
            'url = "https://build-box.example.com:4096"\n'
            'username = "builder"\n'
            'password_command = ["sh", "-c", "echo lab-secret"]\n'
        )
        proc = box.run(["endpoints"])
        payload = parse_json(proc)
        ok = (
            proc.returncode == 0
            and payload is not None
            and payload.get("count") == 2
            and {e["name"] for e in payload["endpoints"]} == {"main", "lab"}
            and next(e for e in payload["endpoints"] if e["name"] == "main")["default"] is True
            and next(e for e in payload["endpoints"] if e["name"] == "lab")["username"] == "builder"
            and not canary_in(proc, "sup3r-secret-main")
            and "lab-secret" not in (proc.stdout + proc.stderr)
        )
        reporter.check(
            name, ok,
            "rc=%s count=%s stderr=%r" % (proc.returncode, (payload or {}).get("count"), proc.stderr[:80]),
        )
    finally:
        box.close()


def scenario_password_command(reporter):
    name = "password_command is executed for auth"
    box = Sandbox()
    server = FakeOpenCode(password="cmdsecret")
    try:
        box.write_config(
            'default = "cmd"\n'
            '[endpoints.cmd]\n'
            'url = "%s"\n'
            'password_command = ["sh", "-c", "echo cmdsecret"]\n'
            % server.url
        )
        proc = box.run(["doctor"])
        payload = parse_json(proc)
        row = (payload or {}).get("endpoints", [{}])[0]
        reporter.check(
            name,
            proc.returncode == 0 and row.get("auth_ok") is True and row.get("version") == "2.0.12",
            "rc=%s auth_ok=%s version=%s stderr=%r" % (proc.returncode, row.get("auth_ok"), row.get("version"), proc.stderr[:80]),
        )
    finally:
        server.close()
        box.close()


def scenario_bad_permissions_warning(reporter):
    name = "loose config permissions warn on stderr"
    box = Sandbox()
    try:
        box.write_config(
            'default = "main"\n'
            '[endpoints.main]\n'
            'url = "http://127.0.0.1:4096"\n'
            'password = "permmode-secret"\n',
            mode=0o644,
        )
        proc = box.run(["endpoints"])
        combined = proc.stdout + proc.stderr
        reporter.check(
            name,
            proc.returncode == 0
            and "permissions" in proc.stderr
            and "chmod 600" in proc.stderr
            and "permmode-secret" not in combined,
            "rc=%s stderr=%r" % (proc.returncode, proc.stderr[:80]),
        )
    finally:
        box.close()


def scenario_routes(reporter):
    name = "routes.db write / lookup / miss"
    box = Sandbox()
    server = FakeOpenCode(password="routepw")
    try:
        box.write_config(
            'default = "main"\n'
            '[endpoints.main]\n'
            'url = "%s"\n'
            'password = "routepw"\n'
            % server.url
        )
        created = box.run(["create", "--title", "routed"])
        created_payload = parse_json(created)
        session_id = (created_payload or {}).get("session_id")
        if created.returncode != 0 or session_id != "ses_test123":
            reporter.fail(name, "create failed: rc=%s out=%r" % (created.returncode, created.stdout[:120]))
            return

        hit = box.run(["messages", "-s", session_id])
        hit_payload = parse_json(hit)
        missing = box.run(["messages", "-s", "ses_does_not_exist"])
        db_exists = os.path.exists(os.path.join(box.state_home, "octl", "routes.db"))
        reporter.check(
            name,
            hit.returncode == 0
            and hit_payload is not None
            and hit_payload.get("session_id") == session_id
            and "count" in hit_payload
            and db_exists
            and missing.returncode == 2
            and "unknown session" in missing.stderr,
            "create_rc=%s hit_rc=%s miss_rc=%s db=%s miss=%r" % (
                created.returncode, hit.returncode, missing.returncode, db_exists, missing.stderr[:60],
            ),
        )
    finally:
        server.close()
        box.close()


def scenario_url_as_alias(reporter):
    name = "URL passed as --endpoint is a usage error"
    box = Sandbox()
    try:
        box.write_config(
            'default = "main"\n'
            '[endpoints.main]\n'
            'url = "http://127.0.0.1:4096"\n'
            'password = "x"\n'
        )
        url_proc = box.run(["--endpoint", "http://127.0.0.1:9999", "doctor"])
        after_proc = box.run(["doctor", "--endpoint", "https://elsewhere.example"])
        reporter.check(
            name,
            url_proc.returncode == 2 and "ALIAS" in url_proc.stderr
            and after_proc.returncode == 2 and "ALIAS" in after_proc.stderr,
            "before_rc=%s after_rc=%s" % (url_proc.returncode, after_proc.returncode),
        )
    finally:
        box.close()


def scenario_exit_code_mapping(reporter):
    name = "OpenCodeError kind -> exit code (monkeypatched)"
    box = Sandbox()
    try:
        box.write_config(
            'default = "main"\n'
            '[endpoints.main]\n'
            'url = "http://127.0.0.1:4096"\n'
            'password = "x"\n'
        )
        os.environ["XDG_CONFIG_HOME"] = box.config_home
        os.environ["XDG_STATE_HOME"] = box.state_home
        loader = SourceFileLoader("octl_offline_mod", OCTL_PATH)
        spec = importlib.util.spec_from_loader("octl_offline_mod", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)

        observed = {}
        for kind in ("availability", "compatibility", "other"):
            def raiser(*_a, _kind=kind, **_k):
                raise module.OpenCodeError("[%s] boom" % _kind, kind=_kind)
            module.http_request = raiser
            observed[kind] = module.main(["--endpoint", "main", "interrupt", "-s", "ses_x"])

        def crash(*_a, **_k):
            raise RuntimeError("unexpected")

        module.http_request = crash
        observed["unknown"] = module.main(["--endpoint", "main", "interrupt", "-s", "ses_x"])

        reporter.check(
            name,
            observed == {"availability": 3, "compatibility": 4, "other": 5, "unknown": 5},
            "observed=%s" % observed,
        )
    finally:
        box.close()


def scenario_stdin_json(reporter):
    name = "stdin JSON argument handling"
    box = Sandbox()
    server = FakeOpenCode(password="stdinpw")
    try:
        box.write_config(
            'default = "main"\n'
            '[endpoints.main]\n'
            'url = "%s"\n'
            'password = "stdinpw"\n'
            % server.url
        )
        only_stdin = box.run(["chat", "-s", "ses_x", "--endpoint", "main"],
                             stdin='{"text": "hello from stdin"}')
        stdin_wins = box.run(["chat", "-s", "ses_x", "--endpoint", "main", "--text", "from flag"],
                             stdin='{"text": "stdin wins"}')
        bad_json = box.run(["chat", "-s", "ses_x", "--endpoint", "main"], stdin="{not json")
        form = box.run(["form-reply", "-s", "ses_x", "--endpoint", "main", "--form-id", "frm_1"],
                       stdin='{"city": "Testville"}')
        form_missing = box.run(["form-reply", "-s", "ses_x", "--endpoint", "main", "--form-id", "frm_1"])

        prompt_bodies = []
        with server._lock:
            for item in server.requests:
                if item["path"].endswith("/prompt") and item["body"]:
                    prompt_bodies.append(item["body"].get("text"))
        form_body = server.last("POST", suffix="/form/frm_1/reply")

        reporter.check(
            name,
            only_stdin.returncode == 0
            and parse_json(only_stdin).get("status") == "submitted"
            and prompt_bodies[:1] == ["hello from stdin"]
            and stdin_wins.returncode == 0
            and prompt_bodies[1:2] == ["stdin wins"]
            and bad_json.returncode == 2
            and form.returncode == 0
            and (form_body or {}).get("body") == {"answer": {"city": "Testville"}}
            and form_missing.returncode == 2,
            "stdin_rc=%s wins_rc=%s bad_rc=%s form_rc=%s missing_rc=%s bodies=%s" % (
                only_stdin.returncode, stdin_wins.returncode, bad_json.returncode,
                form.returncode, form_missing.returncode, prompt_bodies,
            ),
        )
    finally:
        server.close()
        box.close()


def scenario_create_permissions(reporter):
    box = Sandbox()
    server = FakeOpenCode(password="createpw")
    try:
        box.write_config(
            'default = "main"\n'
            '[endpoints.main]\n'
            'url = "%s"\n'
            'password = "createpw"\n'
            % server.url
        )

        def create_body():
            item = server.last("POST", suffix="/api/session")
            body = (item or {}).get("body")
            return body if isinstance(body, dict) else {}

        # (a) --trust expands to exactly the three allow rules, in order.
        trusted = box.run(["create", "--title", "t", "--trust", "/tmp/x/*"])
        reporter.check(
            "create --trust sends the three-rule expansion",
            trusted.returncode == 0
            and create_body().get("permissions") == [
                {"action": "external_directory", "resource": "/tmp/x/*", "effect": "allow"},
                {"action": "read", "resource": "/tmp/x/*", "effect": "allow"},
                {"action": "edit", "resource": "/tmp/x/*", "effect": "allow"},
            ],
            "rc=%s body=%s" % (trusted.returncode, json.dumps(create_body())),
        )

        # (b) stdin permissions first, then the --trust expansions appended after.
        stdin_rules = [
            {"action": "read", "resource": "/tmp/y", "effect": "deny"},
            {"action": "shell", "resource": "*", "effect": "ask"},
        ]
        merged = box.run(
            ["create", "--title", "m", "--trust", "/tmp/y"],
            stdin=json.dumps({"permissions": stdin_rules}),
        )
        reporter.check(
            "create merges stdin permissions before --trust rules",
            merged.returncode == 0
            and create_body().get("permissions") == stdin_rules + [
                {"action": "external_directory", "resource": "/tmp/y", "effect": "allow"},
                {"action": "read", "resource": "/tmp/y", "effect": "allow"},
                {"action": "edit", "resource": "/tmp/y", "effect": "allow"},
            ],
            "rc=%s body=%s" % (merged.returncode, json.dumps(create_body())),
        )

        # (c) neither given -> no permissions key in the request body at all.
        plain = box.run(["create", "--title", "p"])
        reporter.check(
            "create omits the permissions key when neither is given",
            plain.returncode == 0 and "permissions" not in create_body(),
            "rc=%s body=%s" % (plain.returncode, json.dumps(create_body())),
        )

        # (d) --model PROVIDER/ID is sent as the Model.Ref body shape.
        pinned = box.run(["create", "--title", "mp", "--model", "yitro/glm-5.3"])
        reporter.check(
            "create --model sends providerID + id",
            pinned.returncode == 0
            and create_body().get("model") == {"providerID": "yitro", "id": "glm-5.3"},
            "rc=%s body=%s" % (pinned.returncode, json.dumps(create_body())),
        )

        # (e) an optional #variant suffix adds the variant field.
        varied = box.run(
            ["create", "--title", "mv", "--model", "yitro/glm-5.3#default"]
        )
        reporter.check(
            "create --model #variant suffix adds variant",
            varied.returncode == 0
            and create_body().get("model") == {
                "providerID": "yitro", "id": "glm-5.3", "variant": "default",
            },
            "rc=%s body=%s" % (varied.returncode, json.dumps(create_body())),
        )

        # (f) a --model value without "/" is a usage error (exit 2).
        bad = box.run(["create", "--title", "mb", "--model", "glm-5.3"])
        reporter.check(
            "create --model without PROVIDER/ID exits 2",
            bad.returncode == 2 and "--model" in bad.stderr and "PROVIDER/ID" in bad.stderr,
            "rc=%s stderr=%r" % (bad.returncode, bad.stderr[:120]),
        )

        # (g) no --model -> the model key is absent (server default applies).
        bare = box.run(["create", "--title", "mn"])
        reporter.check(
            "create omits the model key when --model is absent",
            bare.returncode == 0 and "model" not in create_body(),
            "rc=%s body=%s" % (bare.returncode, json.dumps(create_body())),
        )
    finally:
        server.close()
        box.close()


def scenario_credential_non_leak(reporter):
    name = "credentials never appear in output (wrong password + failing command)"
    box = Sandbox()
    server = FakeOpenCode(password="the-real-password")
    try:
        wrong = "wrong-password-LEAKCANARY"
        box.write_config(
            'default = "main"\n'
            '[endpoints.main]\n'
            'url = "%s"\n'
            'password = "%s"\n'
            '\n'
            '[endpoints.badcmd]\n'
            'url = "http://127.0.0.1:1"\n'
            'password_command = ["sh", "-c", "echo CMDCANARY; exit 1"]\n'
            % (server.url, wrong)
        )
        wrong_proc = box.run(["doctor", "--endpoint", "main"])
        wrong_base64 = base64.b64encode(("opencode:" + wrong).encode("utf-8")).decode("ascii")
        cmd_proc = box.run(["doctor", "--endpoint", "badcmd"])
        combined = wrong_proc.stdout + wrong_proc.stderr + cmd_proc.stdout + cmd_proc.stderr
        reporter.check(
            name,
            wrong_proc.returncode == 3
            and wrong not in combined
            and wrong_base64 not in combined
            and "the-real-password" not in combined
            and cmd_proc.returncode == 2
            and "CMDCANARY" not in combined,
            "wrong_rc=%s auth_detail=%r cmd_rc=%s" % (
                wrong_proc.returncode, (parse_json(wrong_proc) or {}).get("endpoints", [{}])[0].get("detail"), cmd_proc.returncode,
            ),
        )
    finally:
        server.close()
        box.close()


def _gate_config(box, server, password):
    box.write_config(
        'default = "main"\n'
        '[endpoints.main]\n'
        'url = "%s"\n'
        'password = "%s"\n'
        % (server.url, password)
    )


GATE_SID = "ses_test123"


def scenario_round_gate_stale_succeeded(reporter):
    name = "round gate: stale succeeded is not returned before the new round ends"
    box = Sandbox()
    server = FakeOpenCode(password="gatepw")
    try:
        _gate_config(box, server, "gatepw")
        sid = GATE_SID
        # Turn 1 runs to completion, so the fake's outcome is now "succeeded" and frozen.
        c1 = box.run(["chat", "-s", sid, "--endpoint", "main", "--text", "turn one"])
        server.complete(sid, outcome="succeeded", text="reply one", mode="full")
        w1 = box.run(["wait", "-s", sid, "--endpoint", "main", "--timeout", "5"])

        # Turn 2 is enqueued: v2 keeps outcome=succeeded and time.idle frozen, so a bare
        # outcome check would return the previous round immediately (the original bug).
        c2 = box.run(["chat", "-s", sid, "--endpoint", "main", "--text", "turn two"])
        w2 = box.run(["wait", "-s", sid, "--endpoint", "main", "--timeout", "2"])
        p2 = parse_json(w2)
        rg2 = ((p2 or {}).get("diagnostics") or {}).get("round_gate") or {}

        server.complete(sid, outcome="succeeded", text="reply two ROUND2-MARKER", mode="full")
        w3 = box.run(["wait", "-s", sid, "--endpoint", "main", "--timeout", "5"])
        p3 = parse_json(w3)
        reporter.check(
            name,
            c1.returncode == 0
            and w1.returncode == 0
            and c2.returncode == 0
            and w2.returncode == 6
            and (p2 or {}).get("status") == "timeout"
            and rg2.get("watermark_passed") is False
            and w3.returncode == 0
            and (p3 or {}).get("status") == "succeeded"
            and "ROUND2-MARKER" in ((p3 or {}).get("assistant_text") or "")
            and "diagnostics" not in (p3 or {}),
            "c1=%s w1=%s c2=%s w2=%s rg=%s w3=%s text=%r" % (
                c1.returncode, w1.returncode, c2.returncode, w2.returncode,
                json.dumps(rg2), w3.returncode, ((p3 or {}).get("assistant_text") or "")[-60:],
            ),
        )
    finally:
        server.close()
        box.close()


def scenario_round_gate_stale_failed(reporter):
    name = "round gate: stale failed is not returned before the new round ends"
    box = Sandbox()
    server = FakeOpenCode(password="gatepw")
    try:
        _gate_config(box, server, "gatepw")
        sid = GATE_SID
        # Turn 1 ended failed; turn 2 must never inherit that decision.
        c1 = box.run(["chat", "-s", sid, "--endpoint", "main", "--text", "turn one"])
        server.complete(sid, outcome="failed", text="failed one", mode="full")
        c2 = box.run(["chat", "-s", sid, "--endpoint", "main", "--text", "turn two"])
        w2 = box.run(["wait", "-s", sid, "--endpoint", "main", "--timeout", "2"])
        p2 = parse_json(w2)

        server.complete(sid, outcome="succeeded", text="reply two", mode="full")
        w3 = box.run(["wait", "-s", sid, "--endpoint", "main", "--timeout", "5"])
        p3 = parse_json(w3)
        reporter.check(
            name,
            c1.returncode == 0
            and c2.returncode == 0
            and w2.returncode == 6
            and w2.returncode != 5  # a stale failed must not surface as failed
            and (p2 or {}).get("status") == "timeout"
            and w3.returncode == 0
            and (p3 or {}).get("status") == "succeeded",
            "c1=%s c2=%s w2=%s p2=%s w3=%s p3=%s" % (
                c1.returncode, c2.returncode, w2.returncode, (p2 or {}).get("status"),
                w3.returncode, (p3 or {}).get("status"),
            ),
        )
    finally:
        server.close()
        box.close()


def scenario_round_gate_legacy_fallback(reporter):
    name = "round gate: no recorded round falls back to the raw outcome"
    box = Sandbox()
    server = FakeOpenCode(password="gatepw")
    try:
        _gate_config(box, server, "gatepw")
        sid = GATE_SID
        # Completed turn, but no `chat` ever recorded a rounds row in this fresh state dir.
        server.complete(sid, outcome="succeeded", text="legacy reply", mode="full")
        w = box.run(["wait", "-s", sid, "--endpoint", "main", "--timeout", "2"])
        payload = parse_json(w)
        reporter.check(
            name,
            w.returncode == 0
            and (payload or {}).get("status") == "succeeded"
            and "legacy reply" in ((payload or {}).get("assistant_text") or "")
            and "diagnostics" not in (payload or {}),
            "rc=%s status=%s" % (w.returncode, (payload or {}).get("status")),
        )
    finally:
        server.close()
        box.close()


def scenario_round_gate_idle_message(reporter):
    name = "round gate: watermark advance without our idle marker is not enough"
    box = Sandbox()
    server = FakeOpenCode(password="gatepw")
    try:
        _gate_config(box, server, "gatepw")
        sid = GATE_SID
        box.run(["chat", "-s", sid, "--endpoint", "main", "--text", "turn one"])
        server.complete(sid, outcome="succeeded", text="reply one", mode="full")
        w1 = box.run(["wait", "-s", sid, "--endpoint", "main", "--timeout", "5"])

        c2 = box.run(["chat", "-s", sid, "--endpoint", "main", "--text", "turn two"])
        # A foreign turn bumps the watermark but leaves no idle message after our gate id.
        server.complete(sid, outcome="succeeded", text="", mode="bump_only")
        w2 = box.run(["wait", "-s", sid, "--endpoint", "main", "--timeout", "2"])
        p2 = parse_json(w2)
        rg2 = ((p2 or {}).get("diagnostics") or {}).get("round_gate") or {}

        server.complete(sid, outcome="succeeded", text="reply two", mode="full")
        w3 = box.run(["wait", "-s", sid, "--endpoint", "main", "--timeout", "5"])
        reporter.check(
            name,
            w1.returncode == 0
            and c2.returncode == 0
            and w2.returncode == 6
            and rg2.get("watermark_passed") is True
            and rg2.get("idle_message_seen") is False
            and w3.returncode == 0,
            "w1=%s c2=%s w2=%s rg=%s w3=%s" % (
                w1.returncode, c2.returncode, w2.returncode, json.dumps(rg2), w3.returncode,
            ),
        )
    finally:
        server.close()
        box.close()


def scenario_round_gate_once(reporter):
    name = "round gate: --once reports the open round, then closes it on terminal"
    box = Sandbox()
    server = FakeOpenCode(password="gatepw")
    try:
        _gate_config(box, server, "gatepw")
        sid = GATE_SID
        box.run(["chat", "-s", sid, "--endpoint", "main", "--text", "turn one"])
        server.complete(sid, outcome="succeeded", text="reply one", mode="full")
        w1 = box.run(["wait", "-s", sid, "--endpoint", "main", "--timeout", "5"])

        box.run(["chat", "-s", sid, "--endpoint", "main", "--text", "turn two"])
        o1 = box.run(["wait", "-s", sid, "--endpoint", "main", "--once"])
        p1 = parse_json(o1)

        server.complete(sid, outcome="succeeded", text="reply two", mode="full")
        o2 = box.run(["wait", "-s", sid, "--endpoint", "main", "--once"])
        p2 = parse_json(o2)
        # The terminal --once must have deleted the rounds row: a following blocking wait
        # therefore uses the documented legacy fallback and still exits 0.
        w3 = box.run(["wait", "-s", sid, "--endpoint", "main", "--timeout", "5"])
        p3 = parse_json(w3)
        reporter.check(
            name,
            w1.returncode == 0
            and o1.returncode == 0
            and (p1 or {}).get("status") == "running"
            and (p1 or {}).get("round_open") is True
            and isinstance((p1 or {}).get("round_gate"), dict)
            and (p1 or {}).get("round_gate", {}).get("watermark_passed") is False
            and o2.returncode == 0
            and (p2 or {}).get("status") == "succeeded"
            and w3.returncode == 0
            and (p3 or {}).get("status") == "succeeded",
            "w1=%s o1=%s p1=%s/%s o2=%s p2=%s w3=%s p3=%s" % (
                w1.returncode, o1.returncode, (p1 or {}).get("status"), (p1 or {}).get("round_open"),
                o2.returncode, (p2 or {}).get("status"), w3.returncode, (p3 or {}).get("status"),
            ),
        )
    finally:
        server.close()
        box.close()


SCENARIOS = [
    scenario_config_and_endpoints,
    scenario_password_command,
    scenario_bad_permissions_warning,
    scenario_routes,
    scenario_url_as_alias,
    scenario_exit_code_mapping,
    scenario_stdin_json,
    scenario_create_permissions,
    scenario_credential_non_leak,
    scenario_round_gate_stale_succeeded,
    scenario_round_gate_stale_failed,
    scenario_round_gate_legacy_fallback,
    scenario_round_gate_idle_message,
    scenario_round_gate_once,
]


def main():
    reporter = Reporter("cli_offline")
    for scenario in SCENARIOS:
        try:
            scenario(reporter)
        except Exception as exc:  # a crashing scenario is a FAIL, not a traceback
            reporter.fail(scenario.__name__, "unexpected: %s: %s" % (type(exc).__name__, exc))
    return reporter.summary()


if __name__ == "__main__":
    sys.exit(main())
