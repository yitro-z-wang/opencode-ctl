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
kind -> exit-code mapping (monkeypatched), stdin JSON argument handling and the
credential non-leak invariant in a wrong-password run.

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
import urllib.parse
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
    def __init__(self, password=None, version="2.0.12"):
        self.password = password
        self.version = version
        self.requests = []
        self._lock = threading.Lock()
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
                    self._respond(200, {"messages": []})
                elif path.startswith("/api/session/"):
                    sid = path.split("/api/session/", 1)[1]
                    self._respond(200, {"id": sid, "title": "fake"})
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
                if not self._auth_ok():
                    self._respond(401, {"error": "unauthorized"})
                    return
                path = self._path()
                if path == "/api/session":
                    title = (body or {}).get("title")
                    self._respond(200, {"id": "ses_test123", "title": title})
                elif path.endswith("/prompt"):
                    self._respond(200, {"id": "msg_prompt_1"})
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


SCENARIOS = [
    scenario_config_and_endpoints,
    scenario_password_command,
    scenario_bad_permissions_warning,
    scenario_routes,
    scenario_url_as_alias,
    scenario_exit_code_mapping,
    scenario_stdin_json,
    scenario_credential_non_leak,
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
