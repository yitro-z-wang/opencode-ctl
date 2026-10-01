#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live end-to-end scenarios for the ``octl`` CLI.

Run directly::

    python3 tests/live_cli_test.py

This harness (not ``octl``) spawns ``opencode serve`` on a free port with a
random ``OPENCODE_SERVER_PASSWORD``, waits for readiness, writes a temporary
``endpoints.toml`` into a throwaway ``XDG_CONFIG_HOME`` and then drives the CLI
through the normal agent loop: ``doctor`` -> ``create`` -> ``chat`` (async) ->
``wait`` -> ``messages --after`` -> ``delete``, then a two-round smoke where a
second ``chat`` with a unique marker must return the second round's text (never
the stale first-round outcome). Every step asserts the exit code and the JSON
keys, and every step asserts that the server password never leaks into
stdout+stderr. ``api_version_warning`` is tolerated when present.

The whole suite reports SKIP (exit 0) when ``opencode`` is not on PATH. See
``tests/README.md``.
"""

import base64
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cli_common import Reporter, opencode_on_path  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
OCTL_PATH = os.path.join(REPO_ROOT, "octl")


def free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def auth_header(password):
    token = base64.b64encode(("opencode:" + password).encode("utf-8")).decode("ascii")
    return "Basic " + token


class Harness:
    def __init__(self, reporter):
        self.reporter = reporter
        self.root = tempfile.mkdtemp(prefix="octl-live-")
        self.config_home = os.path.join(self.root, "config")
        self.state_home = os.path.join(self.root, "state")
        self.project = os.path.join(self.root, "project")
        os.makedirs(self.config_home, exist_ok=True)
        os.makedirs(self.state_home, exist_ok=True)
        os.makedirs(self.project, exist_ok=True)

        self.password = secrets.token_urlsafe(24)
        self.port = free_port()
        self.url = "http://127.0.0.1:%d" % self.port
        self.proc = None
        self.session_id = None
        self.secret_leaked = []

        self._write_config()

    def _write_config(self):
        directory = os.path.join(self.config_home, "octl")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "endpoints.toml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                'default = "local"\n'
                '\n'
                '[endpoints.local]\n'
                'url = "%s"\n'
                'password = "%s"\n' % (self.url, self.password)
            )
        os.chmod(path, 0o600)

    def start_serve(self):
        env = dict(os.environ)
        env["OPENCODE_SERVER_PASSWORD"] = self.password
        self.proc = subprocess.Popen(
            ["opencode", "serve", "--port", str(self.port)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=self.project,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return False
            request = urllib.request.Request(self.url + "/api/info")
            request.add_header("Authorization", auth_header(self.password))
            try:
                with urllib.request.urlopen(request, timeout=2) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if isinstance(payload, dict) and payload.get("version"):
                    return True
            except Exception:
                pass
            time.sleep(0.3)
        return False

    def env(self):
        env = dict(os.environ)
        env.pop("OPENCODE_URL", None)
        env.pop("OPENCODE_PASSWORD", None)
        env["XDG_CONFIG_HOME"] = self.config_home
        env["XDG_STATE_HOME"] = self.state_home
        return env

    def run(self, args, stdin=None, cwd=None):
        proc = subprocess.run(
            [sys.executable, OCTL_PATH] + list(args),
            input="" if stdin is None else stdin,
            capture_output=True,
            text=True,
            env=self.env(),
            cwd=cwd or self.project,
            timeout=300,
        )
        if self.password in (proc.stdout or "") or self.password in (proc.stderr or ""):
            self.secret_leaked.append(" ".join(args))
        return proc

    def api_delete(self):
        if not self.session_id:
            return
        request = urllib.request.Request(
            self.url + "/api/session/" + self.session_id, method="DELETE"
        )
        request.add_header("Authorization", auth_header(self.password))
        try:
            urllib.request.urlopen(request, timeout=10).read()
        except Exception:
            pass

    def stop_serve(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
                except Exception:
                    pass

    def close(self):
        self.api_delete()
        self.stop_serve()
        shutil.rmtree(self.root, ignore_errors=True)


def parse_json(proc):
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def run(reporter, harness):
    reporter.header("octl CLI live loop")

    doctor = harness.run(["--endpoint", "local", "doctor"])
    doctor_payload = parse_json(doctor)
    row = ((doctor_payload or {}).get("endpoints") or [{}])[0]
    reporter.check(
        "doctor: endpoint is reachable and authenticated",
        doctor.returncode == 0
        and doctor_payload is not None
        and doctor_payload.get("ok") is True
        and row.get("reachable") is True
        and row.get("auth_ok") is True
        and bool(row.get("version"))
        and row.get("baseline_check") is not None,
        "rc=%s version=%s check=%s detail=%s" % (
            doctor.returncode, row.get("version"), row.get("baseline_check"), row.get("detail"),
        ),
    )

    create = harness.run(["--endpoint", "local", "create", "--title", "live-cli", "--directory", harness.project])
    create_payload = parse_json(create)
    harness.session_id = (create_payload or {}).get("session_id")
    reporter.check(
        "create: session_id at the top level",
        create.returncode == 0
        and isinstance(harness.session_id, str)
        and harness.session_id.startswith("ses_"),
        "rc=%s session_id=%s" % (create.returncode, harness.session_id),
    )

    trust_create = harness.run([
        "--endpoint", "local", "create", "--title", "live-trust",
        "--directory", harness.project, "--trust", "/tmp/opencode/octl-*",
    ])
    trust_payload = parse_json(trust_create)
    trust_session_id = (trust_payload or {}).get("session_id")
    reporter.check(
        "create --trust: serve accepts the permission ruleset",
        trust_create.returncode == 0
        and isinstance(trust_session_id, str)
        and trust_session_id.startswith("ses_"),
        "rc=%s session_id=%s" % (trust_create.returncode, trust_session_id),
    )
    if trust_session_id:
        trust_delete = harness.run(["delete", "-s", trust_session_id])
        reporter.check(
            "create --trust: the trusted session deletes",
            trust_delete.returncode == 0
            and (parse_json(trust_delete) or {}).get("ok") is True,
            "rc=%s" % trust_delete.returncode,
        )

    if not harness.session_id:
        return

    chat = harness.run(["chat", "-s", harness.session_id, "--endpoint", "local",
                        "--text", "Reply with exactly: ok"])
    chat_payload = parse_json(chat)
    reporter.check(
        "chat: async submit returns immediately",
        chat.returncode == 0
        and chat_payload is not None
        and chat_payload.get("status") == "submitted"
        and chat_payload.get("session_id") == harness.session_id,
        "rc=%s status=%s" % (chat.returncode, (chat_payload or {}).get("status")),
    )

    wait = harness.run(["wait", "-s", harness.session_id, "--timeout", "240"])
    wait_payload = parse_json(wait)
    text = (wait_payload or {}).get("assistant_text") or ""
    reporter.check(
        "wait: reaches succeeded with the assistant reply",
        wait.returncode == 0
        and wait_payload is not None
        and wait_payload.get("status") == "succeeded"
        and "ok" in text.lower()
        and "last_message_id" in wait_payload,
        "rc=%s status=%s text=%r" % (wait.returncode, (wait_payload or {}).get("status"), text[:40]),
    )

    cursor = (wait_payload or {}).get("last_message_id")
    messages = harness.run(["messages", "-s", harness.session_id, "--after", cursor or "", "--limit", "20"])
    messages_payload = parse_json(messages)
    reporter.check(
        "messages: incremental shape after the wait cursor",
        messages.returncode == 0
        and messages_payload is not None
        and messages_payload.get("session_id") == harness.session_id
        and isinstance(messages_payload.get("count"), int)
        and isinstance(messages_payload.get("messages"), list)
        and "last_message_id" in messages_payload,
        "rc=%s count=%s" % (messages.returncode, (messages_payload or {}).get("count")),
    )

    # Round gate regression: a second turn must never come back as the first turn's
    # stale succeeded outcome with the previous round's text.
    marker = "OCTL-ROUND2-MARKER-" + secrets.token_hex(4)
    chat2 = harness.run(["chat", "-s", harness.session_id, "--endpoint", "local",
                         "--text", "Reply with exactly: " + marker])
    chat2_payload = parse_json(chat2)
    once = harness.run(["wait", "-s", harness.session_id, "--endpoint", "local", "--once"])
    once_payload = parse_json(once)
    once_ok = once.returncode == 0 and once_payload is not None
    if once_ok and once_payload.get("status") == "succeeded":
        # If the single poll already claims success it must be this round's text.
        once_ok = marker in (once_payload.get("assistant_text") or "")
    wait2 = harness.run(["wait", "-s", harness.session_id, "--endpoint", "local", "--timeout", "240"])
    wait2_payload = parse_json(wait2)
    text2 = (wait2_payload or {}).get("assistant_text") or ""
    reporter.check(
        "wait: round 2 returns the new turn's text (not the stale round-1 outcome)",
        chat2.returncode == 0
        and (chat2_payload or {}).get("status") == "submitted"
        and once_ok
        and wait2.returncode == 0
        and (wait2_payload or {}).get("status") == "succeeded"
        and marker in text2,
        "chat2_rc=%s once_rc=%s/%s wait2_rc=%s/%s text2=%r" % (
            chat2.returncode, once.returncode, (once_payload or {}).get("status"),
            wait2.returncode, (wait2_payload or {}).get("status"), text2[-80:],
        ),
    )

    delete = harness.run(["delete", "-s", harness.session_id])
    delete_payload = parse_json(delete)
    reporter.check(
        "delete: session removed",
        delete.returncode == 0 and (delete_payload or {}).get("ok") is True,
        "rc=%s ok=%s" % (delete.returncode, (delete_payload or {}).get("ok")),
    )
    harness.session_id = None  # already deleted; skip the API cleanup

    reporter.check(
        "no step leaked the server password",
        not harness.secret_leaked,
        "leaked in: %s" % harness.secret_leaked,
    )


def main():
    reporter = Reporter("live_cli")
    if not opencode_on_path():
        reporter.skip("octl CLI live loop", "opencode is not on PATH")
        return reporter.summary()

    harness = Harness(reporter)
    try:
        if not harness.start_serve():
            reporter.skip("octl CLI live loop", "opencode serve did not become ready")
            return reporter.summary()
        run(reporter, harness)
    except Exception as exc:
        reporter.fail("octl CLI live loop", "unexpected: %s: %s" % (type(exc).__name__, exc))
    finally:
        harness.close()
    return reporter.summary()


if __name__ == "__main__":
    sys.exit(main())
