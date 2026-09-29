#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""opencode-mcp — MCP (Model Context Protocol) stdio server implemented with the pure Python standard library.

Drives the conversation capabilities of a local opencode (Session / Prompt / permission / form / interrupt).
Zero third-party dependencies; Python 3 standard library only.

Transport: MCP over stdio, one JSON-RPC 2.0 message per line (newline-delimited, not LSP Content-Length framing).
Logs go to stderr; protocol messages go to stdout.
"""

import atexit
import base64
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from octl_core import (
    Connection,
    OpenCodeError,
    VALID_AUTO_PERMISSION,
    VersionWarnings,
    _attach_subtree,
    _ensure_version,
    _enrich_forms,
    _form_summary,
    _format_message,
    _raw_probe,
    _session_time,
    _subtree_snapshot,
    fetch_forms,
    fetch_messages,
    fetch_permissions,
    http_request,
    log,
    run_until_terminal,
    set_route_session,
    unwrap,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVER_NAME = "opencode-mcp"
SERVER_VERSION = "1.0.0"

DEFAULT_PROTOCOL_VERSION = "2024-11-05"
DEFAULT_BASE_URL = "http://127.0.0.1:4096"
DEFAULT_PASSWORD = "opencode"

# Version-baseline warning latch: one instance per process, owned by this surface.
_VERSION_WARNINGS = VersionWarnings()


# ---------------------------------------------------------------------------
# Connection layer: multiple opencode servers (MCP-spawned local serve + dynamic remotes)
# Local: explicit direct connection via OPENCODE_URL (skips the spawn); otherwise the MCP spawns a dedicated serve
# (random high port + random password; the child process lives as long as this MCP instance).
# No inferential service discovery of any kind (including service.json).
# ---------------------------------------------------------------------------

DEFAULT_LOCAL_NAME = "local"
SESSION_ROUTE_LIMIT = 1000

_CONNECTIONS = {}  # name -> Connection (guarded by _STATE_LOCK)
_SESSION_ROUTE = {}  # session_id -> connection name (insertion order; oldest evicted past the limit)
_LOCAL_LOCK = threading.Lock()
# Serial registration lock: eliminates the check-then-write race for concurrent connects with the same name (registration is infrequent)
_REGISTER_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Spawned-child lifecycle
#
# The spawned `opencode serve` is a child process of this MCP, so this MCP owns its
# lifetime: every code path that ends the process must end the child too, otherwise the
# child is re-parented to init and serves a random port forever — one leaked serve per
# MCP restart. Coverage:
#   - stdin EOF (the normal MCP shutdown) and any other normal exit  -> atexit
#   - SIGTERM / SIGHUP / SIGINT (host-managed kill)                  -> signal handlers
#   - SIGKILL (uncatchable on any platform)                          -> a stale serve may survive;
#     the next MCP start does not adopt it (no inferential discovery, by design).
# ---------------------------------------------------------------------------

_SPAWNED_PROCS = []  # every opencode serve this process started (guarded by _CLEANUP_LOCK)
_CLEANUP_LOCK = threading.Lock()
_CLEANUP_DONE = False


def _kill_proc(proc):
    """Terminate a child and reap it (no zombie). Never raises."""
    try:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)  # Reap the child process to avoid a zombie
    except Exception:
        pass


def _track_spawned_proc(proc):
    with _CLEANUP_LOCK:
        _SPAWNED_PROCS.append(proc)


def _cleanup_spawned_procs():
    """Kill every serve this process spawned; idempotent and safe from any exit path."""
    global _CLEANUP_DONE
    with _CLEANUP_LOCK:
        if _CLEANUP_DONE:
            return
        _CLEANUP_DONE = True
        procs = list(_SPAWNED_PROCS)
        _SPAWNED_PROCS.clear()
    for proc in procs:
        _kill_proc(proc)


def _install_signal_handlers():
    """Kill spawned children before dying from a catchable signal, then exit with the signal's default semantics."""
    def _handler(signum, _frame):
        _cleanup_spawned_procs()
        try:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        except Exception:
            os._exit(128 + signum)

    candidates = [signal.SIGTERM, signal.SIGINT]
    if hasattr(signal, "SIGHUP"):
        candidates.append(signal.SIGHUP)
    for sig in candidates:
        try:
            signal.signal(sig, _handler)
        except Exception:
            pass


atexit.register(_cleanup_spawned_procs)


def _spawn_local_serve():
    """Spawn a dedicated local serve: random high port + random password.

    No opencode on PATH → an availability error that states the user's environment problem; no retry.
    Child-process model: lives as long as this MCP instance; multiple instances use random ports and do not conflict.
    """
    if shutil.which("opencode") is None:
        raise OpenCodeError(
            "[availability] No opencode command on PATH, cannot spawn the local server. "
            "Install opencode or add it to PATH and retry (a user environment problem; the MCP will not try again).",
            kind="availability",
        )
    last_err = None
    for _ in range(3):
        port = random.randint(20000, 60000)
        password = (
            base64.urlsafe_b64encode(os.urandom(24)).decode("ascii").rstrip("=")
        )
        env = dict(os.environ)
        env["OPENCODE_SERVER_PASSWORD"] = password
        try:
            proc = subprocess.Popen(
                ["opencode", "serve", "--port", str(port)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        except Exception as exc:
            last_err = exc
            continue
        conn = Connection(
            DEFAULT_LOCAL_NAME,
            "http://127.0.0.1:%d" % port,
            password,
            is_local=True,
            source="spawned",
        )
        conn.spawned_proc = proc
        _track_spawned_proc(proc)  # Owned by this process: killed on exit (atexit / signals)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break  # Port in use, etc.: retry with another port
            try:
                info = _raw_probe(conn, timeout=2.0)
                if isinstance(info, dict) and info.get("version"):
                    conn.server_version = info["version"]
                    return conn
            except Exception:
                pass
            time.sleep(0.3)
        _kill_proc(proc)  # Failed attempt: kill and reap before trying another port
    raise OpenCodeError(
        "[availability] Failed to spawn the local opencode serve (none of 3 random ports became ready): %s"
        % last_err,
        kind="availability",
    )


def _local_connection():
    """Local connection: explicit env direct connection, otherwise spawn a dedicated serve; a per-process singleton."""
    with _LOCAL_LOCK:
        with _STATE_LOCK:
            conn = _CONNECTIONS.get(DEFAULT_LOCAL_NAME)
        if conn is not None:
            return conn
        url = os.environ.get("OPENCODE_URL")
        if url:
            conn = Connection(
                DEFAULT_LOCAL_NAME,
                url,
                os.environ.get("OPENCODE_PASSWORD") or "opencode",
                is_local=True,
                source="env",
            )
            _ensure_version(conn)
        else:
            conn = _spawn_local_serve()
        with _STATE_LOCK:
            _CONNECTIONS[DEFAULT_LOCAL_NAME] = conn
        log(
            "[opencode-mcp] local connection ready:",
            conn.base_url,
            "version=",
            conn.server_version,
        )
        return conn


def _register_connection(name, base_url, password):
    if not name or not isinstance(name, str):
        raise OpenCodeError("Missing required parameter name")
    if name == DEFAULT_LOCAL_NAME:
        raise OpenCodeError("Connection name %r is reserved and cannot be used" % name)
    # Fully serial: eliminates the check-then-write race in concurrent registration of the same name (registration is infrequent, so serial is fine)
    with _REGISTER_LOCK:
        with _STATE_LOCK:
            if name in _CONNECTIONS:
                raise OpenCodeError(
                    "Connection name already exists: %s (see list_servers)" % name
                )
        conn = Connection(name, base_url, password, is_local=False, source="dynamic")
        _ensure_version(conn)  # Creation-time check (hard gate)
        with _STATE_LOCK:
            _CONNECTIONS[name] = conn
    return conn


def _resolve_connection(args):
    """server parameter > session routing > local. An unknown name errors and lists the existing connections."""
    name = args.get("server")
    session_id = args.get("session_id")
    if not name and session_id:
        with _STATE_LOCK:
            name = _SESSION_ROUTE.get(session_id)
    if not name or name == DEFAULT_LOCAL_NAME:
        return _local_connection()
    with _STATE_LOCK:
        conn = _CONNECTIONS.get(name)
        names = sorted(_CONNECTIONS) or [DEFAULT_LOCAL_NAME]
    if conn is None:
        raise OpenCodeError(
            "Unknown connection %r. Existing connections: %s (a remote must be registered with connect_server first)"
            % (name, ", ".join(names))
        )
    return conn


def _route_session(session_id, conn):
    if not session_id:
        return
    with _STATE_LOCK:
        _SESSION_ROUTE[session_id] = conn.name
        while len(_SESSION_ROUTE) > SESSION_ROUTE_LIMIT:
            _SESSION_ROUTE.pop(next(iter(_SESSION_ROUTE)))


# Core resolves subtree children/roots and must report them so replies route to the right server.
set_route_session(_route_session)


def _remove_connection(name):
    with _STATE_LOCK:
        conn = _CONNECTIONS.pop(name, None)
        if conn is not None:
            stale = [sid for sid, n in _SESSION_ROUTE.items() if n == name]
            for sid in stale:
                _SESSION_ROUTE.pop(sid, None)
    return conn


def _warn_choke(args, result):
    """Unified warning injection point for the tools/call success path: per (connection, session)."""
    if not isinstance(result, dict):
        return result
    session_id = args.get("session_id") or result.get("session_id")
    if not session_id:
        return result
    try:
        conn = _resolve_connection(dict(args, session_id=session_id))
    except OpenCodeError:
        return result
    return _VERSION_WARNINGS.attach(conn, session_id, result)


# ---------------------------------------------------------------------------
# Concurrency and cancellation (MCP: notifications/cancelled + request thread pool)
# ---------------------------------------------------------------------------

# Set of request ids cancelled by the caller (written by the reader thread, read by poll threads)
_CANCELLED = set()
_STATE_LOCK = threading.Lock()
# stdout single-writer lock: multi-threaded responses must be written serially
_OUT_LOCK = threading.Lock()
# Request id currently handled by the worker thread (thread-local)
_CURRENT = threading.local()


def _request_cancelled(request_id):
    with _STATE_LOCK:
        return request_id in _CANCELLED


def _current_request_cancelled():
    request_id = getattr(_CURRENT, "request_id", None)
    return request_id is not None and _request_cancelled(request_id)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _arg_timeout(args, default=120):
    """Parse the optional timeout_secs parameter and clamp it to [1, 3600] seconds."""
    value = int(args.get("timeout_secs", default) or default)
    return max(1, min(value, 3600))


def _arg_bool(args, name, default=False):
    """Parse an optional boolean argument; an explicit value overrides the default."""
    value = args.get(name, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def tool_create_session(args):
    conn = _resolve_connection(args)
    body = {}
    if args.get("title"):
        body["title"] = args["title"]
    if args.get("agent"):
        body["agent"] = args["agent"]
    model_id = args.get("model_id")
    if model_id:
        if "/" not in model_id:
            raise OpenCodeError(
                "model_id must be in 'providerID/modelID' format, got: %r" % model_id
            )
        provider_id, model = model_id.split("/", 1)
        # Model.Ref shape is {"id": ..., "providerID": ...} (observed: the "modelID" key is rejected with 400)
        body["model"] = {"providerID": provider_id, "id": model}

    location = args.get("location")
    if location is not None:
        if not isinstance(location, dict):
            raise OpenCodeError(
                "location must be an object of the form {\"directory\": \"/path/to/project\"}"
            )
        directory = location.get("directory")
        if not isinstance(directory, str) or not directory:
            raise OpenCodeError("location.directory is a required string")
        # Pass through in the Location.PublicRef shape from openapi.json: {directory}
        body["location"] = {"directory": directory}

    data = unwrap(http_request(conn, "POST", "/api/session", body=body))
    if not isinstance(data, dict):
        data = {}
    _route_session(data.get("id"), conn)
    return {
        "session_id": data.get("id"),
        "server": conn.name,
        "title": data.get("title"),
        "agent": data.get("agent"),
        "model": data.get("model"),
    }


def tool_delete_session(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("Missing required parameter session_id")
    http_request(
        conn,
        "DELETE",
        "/api/session/%s" % urllib.parse.quote(session_id, safe=""),
    )
    with _STATE_LOCK:
        _SESSION_ROUTE.pop(session_id, None)
    return {"ok": True, "server": conn.name, "session_id": session_id}


def tool_chat(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("Missing required parameter session_id")
    timeout_secs = _arg_timeout(args)
    # Remote connections default to manual (approval must stay with the caller), local defaults to once
    auto_permission = args.get("auto_permission") or conn.default_auto_permission()
    if auto_permission not in VALID_AUTO_PERMISSION:
        raise OpenCodeError(
            "auto_permission must be one of once/always/reject/manual, got: %r"
            % auto_permission
        )

    baseline = set(m.get("id") for m in fetch_messages(conn, session_id))

    text = args.get("text")
    if text is None or text == "":
        raise OpenCodeError("Missing required parameter text")
    body = {"text": text}

    delivery = args.get("delivery")
    if delivery is not None:
        if delivery not in ("steer", "queue"):
            raise OpenCodeError(
                "delivery must be steer / queue, got: %r" % delivery
            )
        body["delivery"] = delivery

    files = args.get("files")
    if files is not None:
        if not isinstance(files, list):
            raise OpenCodeError("files must be an array, e.g. [{\"uri\": \"...\"}]")
        for idx, item in enumerate(files):
            if not isinstance(item, dict) or not item.get("uri"):
                raise OpenCodeError("files[%d] is missing the required field uri" % idx)
        if files:
            body["files"] = files

    prompt_payload = unwrap(
        http_request(
            conn,
            "POST",
            "/api/session/%s/prompt" % urllib.parse.quote(session_id, safe=""),
            body=body,
        )
    )
    gate_id = (
        prompt_payload.get("id") if isinstance(prompt_payload, dict) else None
    )

    _route_session(session_id, conn)
    wait_for_subagents = _arg_bool(args, "wait_for_subagents", default=False)
    status, payload = run_until_terminal(
        conn,
        session_id,
        timeout_secs,
        auto_permission,
        gate_message_id=gate_id,
        baseline=baseline,
        with_result=True,
        wait_for_subagents=wait_for_subagents,
        cancel_check=_current_request_cancelled,
    )
    return payload


def tool_wait_session(args):
    """Wait for the session to reach a terminal or needs-interaction state (a pure state primitive; returns no message content)."""
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("Missing required parameter session_id")
    timeout_secs = _arg_timeout(args)
    _route_session(session_id, conn)
    wait_for_subagents = _arg_bool(args, "wait_for_subagents", default=True)
    status, payload = run_until_terminal(
        conn,
        session_id,
        timeout_secs,
        auto_permission="manual",
        with_result=False,
        wait_for_subagents=wait_for_subagents,
        cancel_check=_current_request_cancelled,
    )
    if status == "succeeded" and "note" not in payload:
        payload["note"] = "Use get_messages(after_message_id=...) to fetch new replies."
    return payload


def tool_get_messages(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("Missing required parameter session_id")
    limit = int(args.get("limit", 50) or 50)
    after = args.get("after_message_id")
    note = None
    _route_session(session_id, conn)
    if after:
        # Incremental fetch: take at most the latest 200, drop after_message_id and everything before it
        messages = fetch_messages(conn, session_id, limit=200)
        idx = next(
            (i for i, m in enumerate(messages) if m.get("id") == after), -1
        )
        if idx >= 0:
            messages = messages[idx + 1 :]
        else:
            note = "after_message_id is not among the latest 200 messages; returned the full list instead."
        if len(messages) > limit:
            messages = messages[-limit:]
    else:
        messages = fetch_messages(conn, session_id, limit=limit)
    messages = sorted(
        messages, key=lambda m: (m.get("time") or {}).get("created") or 0
    )
    formatted = [_format_message(m) for m in messages]
    result = {
        "server": conn.name,
        "session_id": session_id,
        "count": len(formatted),
        "messages": formatted,
        "last_message_id": messages[-1].get("id") if messages else None,
    }
    if note:
        result["note"] = note
    return result


def tool_permission_reply(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    request_id = args.get("request_id")
    decision = args.get("decision")
    if not session_id or not request_id:
        raise OpenCodeError("Missing required parameter session_id / request_id")
    if decision not in ("once", "always", "reject"):
        raise OpenCodeError(
            "decision must be once / always / reject, got: %r" % decision
        )
    body = {"decision": decision}
    if args.get("message"):
        body["message"] = args["message"]
    http_request(
        conn,
        "POST",
        "/api/session/%s/permission/%s/reply"
        % (
            urllib.parse.quote(session_id, safe=""),
            urllib.parse.quote(request_id, safe=""),
        ),
        body=body,
    )
    return {"ok": True, "server": conn.name, "session_id": session_id, "request_id": request_id, "decision": decision}


def tool_form_reply(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    form_id = args.get("form_id")
    answer = args.get("answer")
    if not session_id or not form_id:
        raise OpenCodeError("Missing required parameter session_id / form_id")
    if not isinstance(answer, dict):
        raise OpenCodeError("answer must be an object, e.g. {\"fieldKey\": value}")
    http_request(
        conn,
        "POST",
        "/api/session/%s/form/%s/reply"
        % (
            urllib.parse.quote(session_id, safe=""),
            urllib.parse.quote(form_id, safe=""),
        ),
        body={"answer": answer},
    )
    return {"ok": True, "server": conn.name, "session_id": session_id, "form_id": form_id, "answer": answer}


def tool_interrupt(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("Missing required parameter session_id")
    http_request(
        conn,
        "POST",
        "/api/session/%s/interrupt" % urllib.parse.quote(session_id, safe=""),
    )
    return {"ok": True, "server": conn.name, "session_id": session_id}


def tool_list_agents(args):
    conn = _resolve_connection(args)
    data = unwrap(http_request(conn, "GET", "/api/agent"))
    if not isinstance(data, list):
        data = []
    agents = []
    for agent in data:
        if not isinstance(agent, dict):
            continue
        agents.append(
            {
                "name": agent.get("name"),
                "mode": agent.get("mode"),
                "model": agent.get("model"),
            }
        )
    return {
        "server": conn.name,
        "count": len(agents),
        "agents": agents,
        "note": "model being null means the agent has no explicitly configured model (it falls back to the position default model at runtime). "
        "If a session should use a particular agent's model, the caller passes providerID/modelID to create_session's model_id.",
    }


def tool_pending_interactions(args):
    """Pending human interactions for the session and its whole subtree (consistent with wait_session)."""
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("Missing required parameter session_id")
    _route_session(session_id, conn)
    snap = _subtree_snapshot(conn, session_id)
    if snap["verified"]:
        permissions = snap["permissions"]
        forms = [_form_summary(f) for f in _enrich_forms(conn, snap["forms"])]
    else:
        # Structure could not be verified: fall back to the root session's own view (legacy shape).
        permissions = fetch_permissions(conn, session_id)
        forms = [
            _form_summary(f)
            for f in fetch_forms(conn, session_id, pending_only=True)
        ]
    result = {
        "server": conn.name,
        "session_id": session_id,
        "root_session_id": session_id,
        "permissions": permissions,
        "forms": forms,
    }
    _attach_subtree(result, snap)
    return result


def tool_list_sessions(args):
    conn = _resolve_connection(args)
    query = {
        "search": args.get("search"),
        "limit": args.get("limit", 20),
        "order": args.get("order", "desc"),
        "directory": args.get("directory"),
        "cursor": args.get("cursor"),
    }
    payload = http_request(conn, "GET", "/api/session", query=query)
    data = unwrap(payload)
    sessions = []
    cursor = {}
    if isinstance(data, list):
        sessions = data
    elif isinstance(data, dict):
        sessions = data.get("sessions") or data.get("data") or []
        cursor = data.get("cursor") or {}
    out = []
    for s in sessions:
        if not isinstance(s, dict):
            continue
        out.append(
            {
                "id": s.get("id"),
                "title": s.get("title"),
                "agent": s.get("agent"),
                "model": s.get("model"),
                "parentID": s.get("parentID"),
                "time": _session_time(s),
            }
        )
    return {
        "server": conn.name,
        "count": len(out),
        "sessions": out,
        "cursor": {
            "previous": cursor.get("previous"),
            "next": cursor.get("next"),
        },
    }


def tool_compact(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("Missing required parameter session_id")
    timeout_secs = _arg_timeout(args)
    auto_permission = args.get("auto_permission") or conn.default_auto_permission()

    baseline = set(m.get("id") for m in fetch_messages(conn, session_id))
    compact_payload = http_request(
        conn,
        "POST",
        "/api/session/%s/compact" % urllib.parse.quote(session_id, safe=""),
        body={},
    )
    gate_id = (
        compact_payload.get("data", {}).get("id")
        if isinstance(compact_payload, dict)
        else None
    )
    _route_session(session_id, conn)
    status, payload = run_until_terminal(
        conn,
        session_id,
        timeout_secs,
        auto_permission,
        gate_message_id=gate_id,
        gate_is_compaction=True,
        baseline=baseline,
        with_result=True,
        cancel_check=_current_request_cancelled,
    )
    return payload


def tool_get_context(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("Missing required parameter session_id")
    data = unwrap(
        http_request(
            conn,
            "GET",
            "/api/session/%s" % urllib.parse.quote(session_id, safe=""),
        )
    )
    if not isinstance(data, dict):
        data = {}
    return {
        "server": conn.name,
        "id": data.get("id"),
        "title": data.get("title"),
        "agent": data.get("agent"),
        "model": data.get("model"),
        "parentID": data.get("parentID"),
        "tokens": data.get("tokens"),
        "cost": data.get("cost"),
        "time": {
            "updated": (data.get("time") or {}).get("updated"),
            "idle": (data.get("time") or {}).get("idle"),
        },
        "revert": data.get("revert"),
    }

def tool_connect_server(args):
    """Register and validate a remote connection (creation-time hard gate). Valid only within this process; not persisted."""
    name = args.get("name")
    url = args.get("url")
    if not name or not url:
        raise OpenCodeError("Missing required parameter name / url")

    password = None
    source = None
    password_file = args.get("password_file")
    password_env = args.get("password_env")
    if password_file:
        try:
            with open(password_file, "r", encoding="utf-8") as fh:
                first = fh.readline().strip()
        except Exception as exc:
            raise OpenCodeError(
                "[other] Failed to read password_file (%s): %s" % (password_file, exc)
            )
        if not first:
            raise OpenCodeError(
                "[other] password_file first line is empty (%s)" % password_file
            )
        password = first
        source = "file"
    if password is None and password_env:
        password = os.environ.get(password_env)
        if not password:
            raise OpenCodeError(
                "[availability] Environment variable %s is not set or is empty" % password_env,
                kind="availability",
            )
        source = "env"
    if password is None and args.get("password"):
        password = args["password"]
        source = "plaintext"

    conn = _register_connection(name, url, password)
    result = conn.describe()
    result["password_source"] = source or "none"
    warning = _VERSION_WARNINGS.warning_for(conn)
    if warning:
        result["api_version_warning"] = warning
    return result


def tool_list_servers(args):
    _local_connection()  # Ensure the local connection is ready (spawn or direct connect)
    with _STATE_LOCK:
        conns = list(_CONNECTIONS.values())
    return {
        "count": len(conns),
        "servers": [c.describe() for c in conns],
    }


def tool_disconnect_server(args):
    name = args.get("name")
    if not name:
        raise OpenCodeError("Missing required parameter name")
    if name == DEFAULT_LOCAL_NAME:
        raise OpenCodeError("The local connection cannot be removed")
    conn = _remove_connection(name)
    if conn is None:
        raise OpenCodeError("Connection does not exist: %s (see list_servers)" % name)
    if conn.spawned_proc is not None:
        try:
            conn.spawned_proc.kill()
        except Exception:
            pass
    return {"ok": True, "removed": name}




# ---------------------------------------------------------------------------
# Tool catalog (schema + descriptions)
# ---------------------------------------------------------------------------

# Shared parameter schemas (read-only reuse; for serialization, never mutated)
_SHARED_SERVER_PARAM = {
    "type": "string",
    "description": "(optional) target connection name, defaults to local; a call with a session_id is auto-routed to the connection that created it",
}
_SHARED_SESSION_ID_PARAM = {"type": "string", "description": "Session ID (ses_...)"}
_SHARED_TIMEOUT_PARAM = {
    "type": "integer",
    "description": "Maximum wait in seconds, default 120",
    "default": 120,
}


TOOLS = [
    {
        "name": "create_session",
        "description": (
            "Create a new conversation session on the local opencode. Returns session_id (ses_...), "
            "which later tools such as chat / get_messages use. Optionally specify a title, agent, model, "
            "and location (to create the session at a given directory/project location)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "title": {"type": "string", "description": "Session title (optional)"},
                "agent": {"type": "string", "description": "Name of the agent to use (optional)"},
                "model_id": {
                    "type": "string",
                    "description": "Model in providerID/modelID format, e.g. \"anthropic/claude-sonnet-4\" (optional)",
                },
                "location": {
                    "type": "object",
                    "description": "Session location (optional), used to create the session in a given directory/project. Passed through in the opencode Location.PublicRef shape.",
                    "properties": {
                        "directory": {
                            "type": "string",
                            "description": "Absolute path of the working directory (required)",
                        }
                    },
                    "required": ["directory"],
                    "additionalProperties": False,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "chat",
        "description": (
            "Send a prompt to the given session and wait for opencode to reply. Internally polls messages, "
            "permission requests and form requests. With auto_permission=once/always/reject it answers permission requests automatically; "
            "with manual it returns immediately on a permission request, and you must call permission_reply + wait_session again. "
            "On a form request it returns needs_form, and you must call form_reply + wait_session. "
            "Optional delivery: steer=steer directly while running (interrupts the current generation direction), "
            "queue=queue it to take effect after this round ends; if omitted the field is not sent. "
            "Optional files: an array of files attached to the prompt, each {uri (required), name?, description?}. "
            "Returns status: succeeded (success, with assistant_text/tools_used/reasoning), "
            "failed (failure), interrupted (interrupted), "
            "needs_permission (waiting for authorization, with a requests list), needs_form (waiting for form input), "
            "timeout (timed out, with partial_text and diagnostics). "
            "Terminal payloads also report subagent state (subagents / pending_subagents / subtree_truncated / subtree_verified)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM,
                "text": {"type": "string", "description": "Prompt text to send"},
                "timeout_secs": _SHARED_TIMEOUT_PARAM,
                "wait_for_subagents": {
                    "type": "boolean",
                    "description": "When true, a succeeded status additionally requires the whole subagent subtree to be quiescent (no active child, no pending permission/form) and auto_permission answers pending permissions of every subtree node. Default false (report subagent state without gating).",
                    "default": False,
                },
                "auto_permission": {
                    "type": "string",
                    "enum": ["once", "always", "reject", "manual"],
                    "description": "How to handle permission requests. Local connections default to once (allow this time); remote connections default to manual (approval must stay with the caller). You may explicitly set once/always/reject/manual.",
                    "default": "once",
                },
                "delivery": {
                    "type": "string",
                    "enum": ["steer", "queue"],
                    "description": "Delivery mode (optional). steer=steer directly while running (interrupts the current generation direction), queue=queue it to take effect after this round ends; if omitted the field is not sent.",
                },
                "files": {
                    "type": "array",
                    "description": "Array of files to send with the prompt (optional).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "uri": {"type": "string", "description": "File URI (required)"},
                            "name": {"type": "string", "description": "File name (optional)"},
                            "description": {"type": "string", "description": "File description (optional)"},
                        },
                        "required": ["uri"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["session_id", "text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "wait_session",
        "description": (
            "Wait for the given session to reach a terminal or needs-interaction state (a pure state primitive; returns no message content). "
            "Terminal status: succeeded (this round ended successfully) / failed (failure) / interrupted (interrupted), "
            "taken from the authoritative session field outcome; blocking states: needs_permission / needs_form (call again after replying); "
            "timeout means it was still generating when the wait timed out. Returns last_message_id as the get_messages incremental cursor, "
            "to be used with get_messages(after_message_id=...) to fetch new replies. "
            "By default a succeeded status is only returned once the whole subagent subtree is quiescent; the payload reports subagents / pending_subagents / subtree_truncated / subtree_verified."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM,
                "timeout_secs": _SHARED_TIMEOUT_PARAM,
                "wait_for_subagents": {
                    "type": "boolean",
                    "description": "When true (default), succeeded additionally requires the whole subagent subtree to be quiescent (no active child session, no pending permission/form anywhere in the subtree); when false, the legacy behaviour is used but subagent state is still reported.",
                    "default": True,
                },
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_messages",
        "description": (
            "Fetch session message records in ascending time order, formatting user/assistant text, tool-call summaries and timestamps. "
            "Supports incremental fetch: pass after_message_id (the last_message_id returned previously, or any message id), "
            "and only messages after it are returned; the response includes last_message_id for the next cursor. "
            "Typical combination: wait_session until terminal, then use this to fetch new replies."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM,
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of messages to return, default 50",
                    "default": 50,
                },
                "after_message_id": {
                    "type": "string",
                    "description": "Incremental cursor (optional): only return new messages after this one",
                },
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "permission_reply",
        "description": (
            "Answer a permission request. decision=once allows this time only, always allows always and saves it, "
            "reject denies it. Optional message is an attached note."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM,
                "request_id": {"type": "string", "description": "Permission request ID (per_...)"},
                "decision": {
                    "type": "string",
                    "enum": ["once", "always", "reject"],
                    "description": "Authorization decision",
                },
                "message": {"type": "string", "description": "Optional explanatory message"},
            },
            "required": ["session_id", "request_id", "decision"],
            "additionalProperties": False,
        },
    },
    {
        "name": "form_reply",
        "description": (
            "Submit the answer for a form. answer is an object whose keys are field keys; values may be "
            "string / number / boolean / string[]."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM,
                "form_id": {"type": "string", "description": "Form ID (frm_...)"},
                "answer": {
                    "type": "object",
                    "description": "Answer object, e.g. {\"name\": \"foo\", \"count\": 3}",
                    "additionalProperties": True,
                },
            },
            "required": ["session_id", "form_id", "answer"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_agents",
        "description": (
            "List all agents of the local opencode and their resolved default models (read-only). "
            "model being null means it is not explicitly configured (it falls back to the position default model). "
            "If a session should match a particular agent's model, the caller passes that model in providerID/modelID "
            "format to create_session's model_id; this tool only provides information and does not pin the model for the caller."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "interrupt",
        "description": "Interrupt the generation currently in progress in the given session. Useful to cancel a long-running task after chat returns timeout.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pending_interactions",
        "description": (
            "Query the human interactions currently pending in the given session, returning lists of permissions (permission requests) and forms. "
            "Use it to learn, without blocking, whether the session is waiting for authorization or form input."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_sessions",
        "description": (
            "Enumerate / search existing sessions; supports keywords, ordering, directory filtering and cursor pagination. "
            "Useful for finding past topics; once you have a session_id, use it with chat to resume the previous conversation (the session_id is the resume handle)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "search": {"type": "string", "description": "Keyword to search by title/content (optional)"},
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results, default 20",
                    "default": 20,
                },
                "order": {
                    "type": "string",
                    "enum": ["asc", "desc"],
                    "description": "Order by update time, default desc (newest first)",
                    "default": "desc",
                },
                "directory": {"type": "string", "description": "Filter by working directory (optional)"},
                "cursor": {"type": "string", "description": "Pagination cursor, taken from the cursor.next returned previously (optional)"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "compact",
        "description": (
            "Compact the context of the given session, wait for the compaction to finish and return the result. "
            "Returns status: succeeded (compaction complete), compaction_failed (compaction failed), "
            "timeout (timed out). Useful to proactively trim when the context nears its limit; you can keep chatting afterwards."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM,
                "timeout_secs": _SHARED_TIMEOUT_PARAM,
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_context",
        "description": (
            "View the context usage (tokens / cost) and metadata of the given session, "
            "to be used with compact to decide whether compaction is needed. tokens / cost default to null."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_session",
        "description": (
            "Delete the given session. Warning: this operation is irreversible and cascades to all of its child sessions "
            "(observed: after deleting the parent, accessing a child returns 404). Confirm these sessions are no longer needed before deleting."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": {"type": "string", "description": "ID of the session to delete (ses_...)"}
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "connect_server",
        "description": (
            "Register and validate a remote opencode connection (valid only within this process; not persisted). "
            "Connecting performs the creation-time check: unreachable = availability error; no version = compatibility error; a version differing from the baseline returns a warning. "
            "Credential priority: password_file (first line) > password_env > plaintext password; "
            "when all are absent, no Authorization is sent (some remotes use an empty username/password). "
            "Returns {name, url, version, baseline, baseline_check}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Connection alias (handle); cannot be local"},
                "url": {"type": "string", "description": "e.g. http://host:4096"},
                "password_file": {"type": "string", "description": "(optional) path to a password file; its first line is used"},
                "password_env": {"type": "string", "description": "(optional) name of the environment variable holding the password"},
                "password": {"type": "string", "description": "(optional) plaintext password, the worst option"},
            },
            "required": ["name", "url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_servers",
        "description": (
            "List all current connections (local + dynamic remotes): name, address, source, version, and baseline check status. "
            "Ensures the local connection is ready (spawning the local serve if necessary)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "disconnect_server",
        "description": "Remove a dynamically registered remote connection (local cannot be removed). Its session routing is cleared as well.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Connection alias"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
]




HANDLERS = {
    "create_session": tool_create_session,
    "chat": tool_chat,
    "wait_session": tool_wait_session,
    "get_messages": tool_get_messages,
    "permission_reply": tool_permission_reply,
    "form_reply": tool_form_reply,
    "list_agents": tool_list_agents,
    "interrupt": tool_interrupt,
    "pending_interactions": tool_pending_interactions,
    "list_sessions": tool_list_sessions,
    "compact": tool_compact,
    "get_context": tool_get_context,
    "delete_session": tool_delete_session,
    "connect_server": tool_connect_server,
    "list_servers": tool_list_servers,
    "disconnect_server": tool_disconnect_server,
}


# ---------------------------------------------------------------------------
# MCP JSON-RPC handling
# ---------------------------------------------------------------------------

def _tool_result(payload):
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}]}


def _tool_error(message):
    return {
        "content": [{"type": "text", "text": str(message)}],
        "isError": True,
    }


def handle_message(message):
    """Handle a single JSON-RPC message; returns a response dict or None (notifications need no response)."""
    if not isinstance(message, dict):
        return None

    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        requested = params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": requested,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": TOOLS},
        }

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        handler = HANDLERS.get(name)
        if handler is None:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": _tool_error("Unknown tool: %s" % name),
            }
        try:
            result = handler(arguments)
            result = _warn_choke(arguments, result)
            return {"jsonrpc": "2.0", "id": msg_id, "result": _tool_result(result)}
        except OpenCodeError as exc:
            return {"jsonrpc": "2.0", "id": msg_id, "result": _tool_error(str(exc))}
        except Exception as exc:  # Any exception becomes a tool error so the server never crashes
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": _tool_error("Tool execution failed (%s): %s" % (name, exc)),
            }

    # Notifications (no id) are always ignored
    if msg_id is None:
        return None

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": "Method not found: %s" % method},
    }


def write_message(response):
    with _OUT_LOCK:
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _handle_request(message):
    """Handle a single request in a worker thread; cancelled requests are no longer written back."""
    msg_id = message.get("id")
    _CURRENT.request_id = msg_id
    try:
        try:
            response = handle_message(message)
        except Exception as exc:  # Catch-all so no exception terminates the process
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32603, "message": "Internal error: %s" % exc},
            }
        if response is None:
            return
        if _request_cancelled(msg_id):
            log("[opencode-mcp] request cancelled, discarding response:", msg_id)
            return
        write_message(response)
    finally:
        with _STATE_LOCK:
            _CANCELLED.discard(msg_id)
        _CURRENT.request_id = None


def main():
    _install_signal_handlers()  # Own the spawned serve's lifetime on host-kill paths too
    workers = max(1, int(os.environ.get("OPENCODE_MCP_WORKERS") or "4"))
    log(
        "[opencode-mcp] started, workers=%d, waiting for JSON-RPC on stdin" % workers
    )
    executor = ThreadPoolExecutor(max_workers=workers)
    # The reader thread only parses and dispatches, so cancellation notifications arrive immediately
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except Exception as exc:
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error: %s" % exc},
                }
            )
            continue
        if not isinstance(message, dict):
            continue
        method = message.get("method")
        msg_id = message.get("id")

        # Cancellation notification: handled immediately in the reader thread, not sent to the thread pool
        if method == "notifications/cancelled":
            cancelled_id = (message.get("params") or {}).get("requestId")
            if cancelled_id is not None:
                with _STATE_LOCK:
                    _CANCELLED.add(cancelled_id)
                log("[opencode-mcp] received cancel request:", cancelled_id)
            continue

        # All other notifications (no id) are ignored
        if msg_id is None:
            continue

        executor.submit(_handle_request, message)


if __name__ == "__main__":
    main()
