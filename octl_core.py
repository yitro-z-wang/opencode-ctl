#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""octl core — stdlib-only domain library for driving an opencode server.

Extracted from ``server.py`` in Phase 1 of the opencode-ctl refactor. It holds the
connection object, the HTTP layer (two-tier response caps, cross-origin auth-strip
redirects), version gating / failure classification, subtree (subagent) resolution,
the wait loop (``poll_once`` + ``run_until_terminal``) and message/data shaping.

Hard rules:

* standard library only; no third-party imports;
* it must not import ``server`` or reference MCP concepts;
* ``server.py`` imports this module (one direction only).
"""

import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HTTP_TIMEOUT = 30.0
POLL_INTERVAL = 1.0

VALID_AUTO_PERMISSION = ("once", "always", "reject", "manual")

# --- Response-body caps (raw wire bytes) -----------------------------------
# Local client hygiene, independent of the URL origin: an endpoint (or a hostile
# redirect target) can otherwise answer any request with an unbounded body.
CAP_DEFAULT = 4 * 1024 * 1024  # info / session / permission / form (normally KB-scale)
CAP_MESSAGES = 64 * 1024 * 1024  # message fetch: embedded tool I/O can be large
CAP_ERROR_BODY = 64 * 1024  # HTTP error detail body

DEVELOPMENT_BASELINE_VERSION = (
    os.environ.get("OCTL_BASELINE_VERSION") or "2.0.18"
)

# --- Subtree (subagent) resolution -----------------------------------------
# Structure only: the subtree is the reverse closure of parentID via ?parentID= (never any
# message-content heuristic). Depth/node caps keep one runaway tree from costing unbounded work.
SUBTREE_MAX_DEPTH = 3
SUBTREE_MAX_NODES = 64
SUBTREE_AUTOREPLY = ("once", "always", "reject")

# Per-connection capability keys: marked unsupported (once, permanently for that connection) when
# the endpoint genuinely does not exist (404 / reworked API), so we fall back instead of hanging.
CAP_SESSION_ACTIVE = "session_active"
CAP_GLOBAL_PERMISSION = "global_permission"
CAP_GLOBAL_FORM = "global_form"
CAP_SESSION_PARENTID = "session_parentid"

SUBTREE_UNVERIFIED_NOTE = (
    "Subagent state could not be verified (fail-closed); the reported status is this session's own "
    "outcome only and may be stale with respect to child sessions."
)
SUBTREE_UNSUPPORTED_NOTE = (
    "This opencode server does not support subagent verification (/api/session/active or ?parentID=); "
    "the reported status is this session's own outcome only and may be stale with respect to child sessions."
)

# Lock guarding the per-connection capability marks (independent of the surface registry lock).
_CAP_LOCK = threading.Lock()


def log(*parts):
    """Write logs to stderr to avoid polluting any stdout protocol stream."""
    try:
        sys.stderr.write(" ".join(str(p) for p in parts) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


class OpenCodeError(Exception):
    """Interaction with opencode failed. kind ∈ {availability, compatibility, other}.

    other must carry the raw error so it can be reported to the developer verbatim.
    """

    def __init__(self, message, kind="other"):
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------------------
# Connection layer: one opencode server
# ---------------------------------------------------------------------------

class Connection:
    """A single opencode server connection."""

    def __init__(self, name, base_url, password, is_local=False, source="dynamic"):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.password = password or None
        self.is_local = is_local
        self.source = source  # spawned / env / dynamic
        self.server_version = None
        self.spawned_proc = None
        self.subtree_unsupported = set()  # subtree capability keys proven unavailable on this server

    def auth_header(self):
        if not self.password:
            return None  # No credential source: do not send Authorization (some remotes use an empty username/password)
        token = base64.b64encode(
            ("opencode:" + self.password).encode("utf-8")
        ).decode("ascii")
        return "Basic " + token

    def default_auto_permission(self):
        return "once" if self.is_local else "manual"

    def describe(self):
        if self.server_version == DEVELOPMENT_BASELINE_VERSION:
            check = "ok"
        elif self.server_version:
            check = "mismatch(%s)" % self.server_version
        else:
            check = "unknown"
        return {
            "name": self.name,
            "url": self.base_url,
            "local": self.is_local,
            "source": self.source,
            "version": self.server_version,
            "baseline": DEVELOPMENT_BASELINE_VERSION,
            "baseline_check": check,
        }


class VersionWarnings:
    """Per-(connection, session, version) deduplicated baseline-mismatch warning latch.

    Replaces the former module-global state in ``server.py``; the surface instantiates one
    and calls ``warning_for`` / ``attach``. Behavior is unchanged: a session is warned once
    per server version, visible to new sessions, without flooding within one session.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._warned = {}  # connection object -> {session_id: server_version}

    def warning_for(self, conn):
        if not conn.server_version:
            return None
        if conn.server_version == DEVELOPMENT_BASELINE_VERSION:
            return None
        major = (
            conn.server_version.split(".")[0]
            != DEVELOPMENT_BASELINE_VERSION.split(".")[0]
        )
        return {
            "server": conn.name,
            "baseline": DEVELOPMENT_BASELINE_VERSION,
            "current": conn.server_version,
            "severity": "high" if major else "low",
            "message": "opencode server (%s) version %s does not match the development baseline %s; %s, behavior may differ."
            % (
                conn.name,
                conn.server_version,
                DEVELOPMENT_BASELINE_VERSION,
                "different major version, high compatibility risk" if major else "minor version difference",
            ),
        }

    def attach(self, conn, session_id, result):
        """Attach the warning to a result dict once per (session, version)."""
        if session_id is None or not isinstance(result, dict):
            return result
        warning = self.warning_for(conn)
        if not warning:
            return result
        with self._lock:
            warned = self._warned.setdefault(conn, {})
            if warned.get(session_id) == conn.server_version:
                return result
            warned[session_id] = conn.server_version
            while len(warned) > 2000:
                warned.pop(next(iter(warned)))
        result = dict(result)
        result["api_version_warning"] = warning
        return result


# Session routing is owned by the surface (the connection registry in server.py). Core only
# needs to report child/root sessions discovered during a subtree walk so replies reach the
# right server; the surface registers its router here, keeping imports one-directional.
_ROUTE_SESSION = None


def set_route_session(fn):
    """Surface hook: register the session -> connection router (best effort)."""
    global _ROUTE_SESSION
    _ROUTE_SESSION = fn


def _route_session(session_id, conn):
    if _ROUTE_SESSION is not None:
        _ROUTE_SESSION(session_id, conn)


# ---------------------------------------------------------------------------
# HTTP (with connection context and failure classification)
# ---------------------------------------------------------------------------

def _origin(url):
    """Normalized (scheme, host, port) of a URL; default ports 80/443 are implicit."""
    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is None:
        port = {"http": 80, "https": 443}.get(parts.scheme.lower())
    return (parts.scheme.lower(), (parts.hostname or "").lower(), port)


def _strip_authorization(req):
    """Remove every Authorization header variant from a urllib request (headers + unredirected)."""
    for store in (getattr(req, "headers", None), getattr(req, "unredirected_hdrs", None)):
        if not store:
            continue
        for key in [k for k in store if k.lower() == "authorization"]:
            del store[key]


class _AuthStrippingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but strip Authorization when Location crosses origins.

    CPython's default redirect handler replays every request header except
    Content-Length / Content-Type to the unvalidated Location target -- including
    Authorization: Basic ***. curl --location, browser fetch and Go net/http all drop
    credentials on a cross-origin redirect; urllib (and requests) do not. Same-origin
    redirects keep the header; only a differing (scheme, hostname, port) drops it.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        if _origin(req.full_url) != _origin(new.full_url):
            _strip_authorization(new)
        return new


_OPENER = urllib.request.build_opener(_AuthStrippingRedirectHandler)


def check_response_size(size, cap=CAP_DEFAULT):
    """Raise OpenCodeError for a body beyond *cap* bytes (raw wire bytes).

    kind="other": a cap abort is a local protection event, not a server state, and must be
    reported verbatim (never reclassified as availability/compatibility).
    """
    if size > cap:
        raise OpenCodeError(
            "[other] Response body exceeds the %d-byte cap (got %d bytes); refusing to read it."
            % (cap, size)
        )


def _read_bounded(resp, cap):
    """Pre-check the declared Content-Length against *cap*, then read at most cap + 1 bytes.

    A non-numeric Content-Length skips the pre-check but the actual read stays bounded, and
    the final size is verified by the caller (check_response_size).
    """
    length = resp.headers.get("Content-Length")
    if length:
        try:
            check_response_size(int(length), cap)
        except ValueError:
            pass  # non-numeric: skip the pre-check, still bound the read below
    return resp.read(cap + 1)


def _raw_probe(conn, timeout=8.0, cap=CAP_DEFAULT):
    """Raw GET /api/info, returns the parsed dict; network failure raises a connection exception, a non-JSON response raises ValueError."""
    req = urllib.request.Request(conn.base_url + "/api/info")
    header = conn.auth_header()
    if header:
        req.add_header("Authorization", header)
    req.add_header("Accept", "application/json")
    with _OPENER.open(req, timeout=timeout) as resp:
        raw = _read_bounded(resp, cap)
    check_response_size(len(raw), cap)
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("Response is not JSON: %s" % exc)


def _ensure_version(conn):
    """Creation-time check (hard gate).

    Unreachable = availability; reachable but /api/info is non-JSON or returns 404/5xx = compatibility (not the opencode API);
    401 = authentication problem (wrong password).
    A size-cap abort (OpenCodeError) is a local protection event and is re-raised verbatim
    rather than reclassified as a connectivity problem.
    """
    try:
        info = _raw_probe(conn)
    except OpenCodeError:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise OpenCodeError(
                "[availability] %s(%s) authentication rejected (401): wrong password or password changed"
                % (conn.name, conn.base_url),
                kind="availability",
            )
        raise OpenCodeError(
            "[compatibility] %s(%s) is reachable but /api/info returned HTTP %s, which is not the opencode API"
            " (looks like some other service; baseline v%s)"
            % (conn.name, conn.base_url, exc.code, DEVELOPMENT_BASELINE_VERSION),
            kind="compatibility",
        )
    except ValueError:
        raise OpenCodeError(
            "[compatibility] %s(%s) is reachable but /api/info did not return opencode v2 API JSON"
            " (observed cases: without correct credentials the request falls back to the Web UI, or this is another service; baseline v%s. "
            "If this is confirmed to be opencode, check the password)"
            % (conn.name, conn.base_url, DEVELOPMENT_BASELINE_VERSION),
            kind="compatibility",
        )
    except Exception as exc:
        raise OpenCodeError(
            "[availability] Cannot connect to opencode server %s(%s): %s"
            % (conn.name, conn.base_url, exc),
            kind="availability",
        )
    if not isinstance(info, dict) or not isinstance(info.get("version"), str):
        raise OpenCodeError(
            "[compatibility] %s is reachable but /api/info has no version field; the API looks completely reworked"
            " (development baseline v%s)" % (conn.name, DEVELOPMENT_BASELINE_VERSION),
            kind="compatibility",
        )
    conn.server_version = info["version"]
    return info


def http_request(conn, method, path, body=None, query=None, cap=CAP_DEFAULT):
    """Perform a request against the given connection; on failure classify as availability / compatibility / other (dump the raw error)."""
    url = conn.base_url + path
    if query:
        clean = {k: v for k, v in query.items() if v is not None}
        if clean:
            url = url + "?" + urllib.parse.urlencode(clean)

    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(url, data=data, method=method)
    header = conn.auth_header()
    if header:
        req.add_header("Authorization", header)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")

    try:
        with _OPENER.open(req, timeout=HTTP_TIMEOUT) as resp:
            raw = _read_bounded(resp, cap)
        # Outside the read block on purpose: a cap abort is a local protection event and must
        # surface as [other] verbatim -- it is not a server failure and must not be
        # reclassified (or re-probed) by _classify_failure.
        check_response_size(len(raw), cap)
    except OpenCodeError:
        # A cap abort (declared or actual body size) must propagate verbatim as [other].
        raise
    except urllib.error.HTTPError as exc:
        try:
            # Capped: the error body is an error detail, not the payload.
            detail = exc.read(CAP_ERROR_BODY).decode("utf-8", "replace")
        except Exception:
            detail = ""
        raise _classify_failure(
            conn,
            method,
            path,
            "HTTP %s %s %s -> %s %s"
            % (exc.code, method, path, exc.reason, detail[:1500]),
            http_status=exc.code,
        )
    except Exception as exc:  # URLError / timeout, etc.
        raise _classify_failure(
            conn, method, path, "%s: %s" % (type(exc).__name__, exc)
        )

    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise OpenCodeError(
            "[other] Response is not valid JSON (%s %s): %s" % (method, path, exc)
        )


def _classify_failure(conn, method, path, original, http_status=None):
    """After a failure, re-probe the version and classify as availability / compatibility / other accordingly.

    The primary criterion is the shape of the original failure (GET 404 = endpoint gone = compatibility; network layer = availability),
    the version re-probe corroborates and updates the connection's version record; other must preserve the full raw error for reporting.
    """
    probe = None
    probe_err = None
    try:
        probe = _raw_probe(conn, timeout=5.0)
    except Exception as exc:
        probe_err = exc
    if isinstance(probe, dict) and probe.get("version"):
        conn.server_version = probe["version"]
    ctx = "server=%s version=%s baseline=%s" % (
        conn.name,
        conn.server_version,
        DEVELOPMENT_BASELINE_VERSION,
    )
    if http_status in (401, 403):
        raise OpenCodeError(
            "[availability] %s authentication rejected (HTTP %s): wrong or expired password (%s). Original: %s"
            % (conn.name, http_status, ctx, original),
            kind="availability",
        )
    if http_status == 404 and method == "GET":
        raise OpenCodeError(
            "[compatibility] Endpoint gone (GET %s -> 404); the API looks reworked (%s). Original: %s"
            % (path, ctx, original),
            kind="compatibility",
        )
    if probe is None:
        raise OpenCodeError(
            "[availability] Service unreachable (%s); re-probing /api/info also failed: %s. Original: %s"
            % (conn.base_url, probe_err, original),
            kind="availability",
        )
    if not (isinstance(probe, dict) and probe.get("version")):
        raise OpenCodeError(
            "[compatibility] Re-probe of /api/info has no version field; the API looks reworked (%s). Original: %s"
            % (ctx, original),
            kind="compatibility",
        )
    raise OpenCodeError(
        "[other] Request failed (%s). Raw error: %s. Can be reported to the developer verbatim."
        % (ctx, original),
        kind="other",
    )


def unwrap(payload):
    """opencode responses are usually {"data": ...}; uniformly extract data."""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


# ---------------------------------------------------------------------------
# Data shaping helpers
# ---------------------------------------------------------------------------

def _text_parts(message):
    """Get the text of all text parts in an assistant message."""
    out = []
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if text:
                    out.append(text)
    return out


def _reasoning_parts(message):
    out = []
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "reasoning":
                text = part.get("text")
                if text:
                    out.append(text)
    return out


def _tool_parts(message):
    out = []
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "tool":
                state = part.get("state") or {}
                out.append(
                    {
                        "name": part.get("name"),
                        "status": state.get("status"),
                    }
                )
    return out


def _message_text(message):
    """Get the readable text of any message."""
    if isinstance(message.get("text"), str):
        return message["text"]
    return "\n".join(_text_parts(message))


def _is_pending_form(item):
    """List items may carry state.status; only pending counts as outstanding; no state counts as pending."""
    state = item.get("state")
    if not isinstance(state, dict):
        return True
    return state.get("status") == "pending"


def _form_summary(item):
    fields = []
    for field in item.get("fields") or []:
        if not isinstance(field, dict):
            continue
        fields.append(
            {
                "key": field.get("key"),
                "title": field.get("title"),
                "type": field.get("type"),
                "required": field.get("required", False),
                "options": field.get("options"),
                "description": field.get("description"),
            }
        )
    return {
        "id": item.get("id"),
        "sessionID": item.get("sessionID"),
        "title": item.get("title"),
        "fields": fields,
    }


def _format_message(message):
    mtype = message.get("type")
    created = (message.get("time") or {}).get("created")
    result = {
        "id": message.get("id"),
        "type": mtype,
        "time": created,
    }
    if mtype == "assistant":
        result["agent"] = message.get("agent")
        result["model"] = message.get("model")
        result["text"] = "\n".join(_text_parts(message))
        reasoning = _reasoning_parts(message)
        if reasoning:
            result["reasoning"] = reasoning
        tools = _tool_parts(message)
        if tools:
            result["tools"] = tools
        result["completed"] = bool((message.get("time") or {}).get("completed"))
    else:
        result["text"] = _message_text(message)
        if mtype == "shell":
            result["command"] = message.get("command")
    return result


def fetch_messages(conn, session_id, limit=100):
    """Fetch the **latest** limit messages of a session, returned in ascending time order.

    In practice opencode's order=asc&limit returns the "earliest N" (verified on 200+ message sessions),
    which misaligns gate lookup / incremental cursors on long sessions; so we uniformly use order=desc to take the tail window and then reverse it.

    Message bodies embed full tool I/O, so this is the one path that gets the larger CAP_MESSAGES tier.
    """
    payload = http_request(
        conn,
        "GET",
        "/api/session/%s/message" % urllib.parse.quote(session_id, safe=""),
        query={"order": "desc", "limit": limit},
        cap=CAP_MESSAGES,
    )
    data = unwrap(payload)
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        data = data["messages"]
    if not isinstance(data, list):
        return []
    data.reverse()
    return data


def fetch_permissions(conn, session_id):
    payload = http_request(
        conn,
        "GET",
        "/api/session/%s/permission" % urllib.parse.quote(session_id, safe=""),
    )
    data = unwrap(payload)
    return data if isinstance(data, list) else []


def fetch_forms(conn, session_id, pending_only=True):
    payload = http_request(
        conn,
        "GET",
        "/api/session/%s/form" % urllib.parse.quote(session_id, safe=""),
    )
    data = unwrap(payload)
    if not isinstance(data, list):
        return []
    if pending_only:
        data = [item for item in data if _is_pending_form(item)]
    return data


def _session_time(raw):
    """Get updated / idle from the time field of Session.Info."""
    info = raw.get("time") or {}
    result = {"updated": info.get("updated")}
    if "idle" in info:
        result["idle"] = info.get("idle")
    return result


# ---------------------------------------------------------------------------
# Subtree (subagent) awareness
#
# A parent's own outcome is per-turn and can be succeeded/idle while child sessions are still
# working (background delegation), so `succeeded` is only accepted once the whole subtree is
# quiescent. Structure is resolved purely by reverse parentID closure; activity and pending
# interactions are re-read every poll. Everything is fail-closed: an unexpected shape, missing
# key, 404 or request failure means UNKNOWN and success is never declared on UNKNOWN.
# ---------------------------------------------------------------------------


def _capability_unsupported(conn, cap):
    return cap in conn.subtree_unsupported


def _mark_capability_unsupported(conn, cap, exc):
    """Record (once) that a server cannot support a subtree capability; returns True if newly marked."""
    with _CAP_LOCK:
        if cap in conn.subtree_unsupported:
            return False
        conn.subtree_unsupported.add(cap)
    log(
        "[octl] subtree capability %s unsupported on %s, falling back to legacy: %s"
        % (cap, conn.name, exc)
    )
    return True


def _is_capability_error(exc):
    """A compatibility classification (404 GET / reworked API) means the endpoint genuinely does not exist."""
    return isinstance(exc, OpenCodeError) and exc.kind == "compatibility"


def _active_session_ids(conn):
    """Return (set_of_active_session_ids, verified).

    Server-wide map of sessions with a live foreground drain. A blocked-on-permission session still
    appears as running; an idle parent is absent. verified=False is fail-closed: a missing or odd
    payload must never be read as "nothing is active".
    """
    if _capability_unsupported(conn, CAP_SESSION_ACTIVE):
        return None, False
    try:
        payload = http_request(conn, "GET", "/api/session/active")
    except OpenCodeError as exc:
        if _is_capability_error(exc):
            _mark_capability_unsupported(conn, CAP_SESSION_ACTIVE, exc)
        return None, False
    # The measured shape is {"data": {session_id: {"type": "running"}}}; a payload without that
    # wrapper is UNKNOWN (fail-closed), never an empty active set.
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return None, False
    active = set()
    for sid, value in payload["data"].items():
        if not isinstance(sid, str) or not isinstance(value, dict):
            return None, False
        active.add(sid)
    return active, True


def _subtree_ids(conn, root_id):
    """Reverse closure of parentID (direct children via ?parentID= only), with caps and a cycle guard.

    Returns {"root", "ids", "info", "truncated"} or None when the structure cannot be verified
    (fail-closed). Structure only: no activity is cached, no message content is inspected.
    """
    if not root_id:
        return None
    if _capability_unsupported(conn, CAP_SESSION_PARENTID):
        return None
    ids = [root_id]
    info = {root_id: None}
    depth = {root_id: 0}
    seen = {root_id}
    frontier = [root_id]
    truncated = False
    cursor = 0
    while cursor < len(frontier):
        current = frontier[cursor]
        cursor += 1
        cur_depth = depth[current]
        try:
            payload = http_request(
                conn, "GET", "/api/session", query={"parentID": current}
            )
        except OpenCodeError as exc:
            if _is_capability_error(exc):
                _mark_capability_unsupported(conn, CAP_SESSION_PARENTID, exc)
            return None
        data = unwrap(payload)
        if isinstance(data, dict) and isinstance(data.get("sessions"), list):
            data = data["sessions"]  # tolerate the sessions-wrapped variant list_sessions also accepts
        if not isinstance(data, list):
            return None
        for child in data:
            if not isinstance(child, dict):
                return None
            child_id = child.get("id")
            if not isinstance(child_id, str) or not child_id:
                return None
            if child_id in seen:
                continue  # cycle / duplicate guard
            if cur_depth + 1 > SUBTREE_MAX_DEPTH:
                truncated = True  # a deeper node exists but is outside the depth cap
                continue
            if len(seen) >= SUBTREE_MAX_NODES:
                truncated = True  # node cap hit
                break
            seen.add(child_id)
            ids.append(child_id)
            frontier.append(child_id)
            depth[child_id] = cur_depth + 1
            info[child_id] = child
            _route_session(child_id, conn)  # route replies for this child to the right server
        if truncated:
            break
    _route_session(root_id, conn)
    return {"root": root_id, "ids": ids, "info": info, "truncated": truncated}


def _subagent_entries(tree, active):
    """Payload-ready subagent list (subtree nodes excluding the root). active=None means unknown."""
    entries = []
    root = tree.get("root")
    for sid in tree.get("ids") or []:
        if sid == root:
            continue
        node = (tree.get("info") or {}).get(sid) or {}
        entries.append(
            {
                "session_id": sid,
                "agent": node.get("agent"),
                "model": node.get("model"),
                "title": node.get("title"),
                "outcome": node.get("outcome"),
                "active": (sid in active) if active is not None else None,
                "parentID": node.get("parentID"),
            }
        )
    return entries


def _fetch_global_pending(conn, path, cap):
    """Global (location-wide) pending list. Returns (items, status), status in ok/unsupported/unknown.

    The global list may contain requests from other callers/MCPs on a shared or remote server;
    callers must filter by exact subtree membership.
    """
    if _capability_unsupported(conn, cap):
        return None, "unsupported"
    try:
        payload = http_request(conn, "GET", path)
    except OpenCodeError as exc:
        if _is_capability_error(exc):
            _mark_capability_unsupported(conn, cap, exc)
            return None, "unsupported"
        return None, "unknown"
    # The measured shape is {"location": ..., "data": [...]}; a payload without that wrapper is
    # UNKNOWN (fail-closed), never "nothing pending".
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return None, "unknown"
    for item in payload["data"]:
        if not isinstance(item, dict):
            return None, "unknown"
    return payload["data"], "ok"


def _session_pending(conn, sid, kind):
    """Strict per-session pending list for one node: (items, verified).

    The forgiving fetch_permissions/fetch_forms helpers coerce anything to [], which would turn a
    malformed or empty-bodied response into "nothing pending" and let a false success through, so
    this fallback path checks the shape itself and re-adds the owner id that per-session entries omit.
    """
    try:
        payload = http_request(
            conn,
            "GET",
            "/api/session/%s/%s" % (urllib.parse.quote(sid, safe=""), kind),
        )
    except OpenCodeError as exc:
        log("[octl] per-session %s lookup failed" % kind, sid, exc)
        return None, False
    data = unwrap(payload)
    if not isinstance(data, list):
        return None, False
    items = []
    for item in data:
        if not isinstance(item, dict):
            return None, False
        if kind == "form" and not _is_pending_form(item):
            continue
        if not item.get("sessionID"):
            item = dict(item, sessionID=sid)
        items.append(item)
    return items, True


def _pending_interactions_tree(conn, session_ids):
    """Aggregate pending permissions/forms over a subtree, filtered by exact session-id membership.

    Global endpoints are preferred for the sweep, with per-session endpoints as the fallback.
    Returns {"permissions", "forms", "verified"}; verified=False is fail-closed.
    """
    idset = set(session_ids)
    verified = True

    perms, pstat = _fetch_global_pending(
        conn, "/api/permission/request", CAP_GLOBAL_PERMISSION
    )
    if pstat == "ok":
        perms = [p for p in perms if p.get("sessionID") in idset]
    else:
        perms = []
        if pstat == "unknown":
            verified = False
        else:
            for sid in session_ids:
                items, ok = _session_pending(conn, sid, "permission")
                if not ok:
                    verified = False
                    break
                perms.extend(p for p in items if p.get("sessionID") in idset)

    forms, fstat = _fetch_global_pending(conn, "/api/form", CAP_GLOBAL_FORM)
    if fstat == "ok":
        forms = [f for f in forms if f.get("sessionID") in idset]
    else:
        forms = []
        if fstat == "unknown":
            verified = False
        else:
            for sid in session_ids:
                items, ok = _session_pending(conn, sid, "form")
                if not ok:
                    verified = False
                    break
                forms.extend(f for f in items if f.get("sessionID") in idset)

    if not verified:
        return {"permissions": [], "forms": [], "verified": False}
    return {"permissions": perms, "forms": forms, "verified": True}


def _subtree_snapshot(conn, root_id):
    """Fresh full subtree state for gating/reporting (structure is never cached).

    verified=False -> UNKNOWN (fail-closed, the caller must not declare success).
    legacy=True -> the server cannot support verification at all; the caller may use the legacy path.
    """
    base = {
        "verified": False,
        "legacy": False,
        "truncated": False,
        "ids": [root_id],
        "subagents": [],
        "pending_subagents": None,
        "permissions": [],
        "forms": [],
    }
    tree = _subtree_ids(conn, root_id)
    if tree is None:
        if _capability_unsupported(conn, CAP_SESSION_PARENTID):
            base["legacy"] = True
        return base
    ids = tree["ids"]
    base["ids"] = ids
    base["truncated"] = tree["truncated"]

    active, active_ok = _active_session_ids(conn)
    if not active_ok:
        base["subagents"] = _subagent_entries(tree, None)
        if _capability_unsupported(conn, CAP_SESSION_ACTIVE):
            base["legacy"] = True
        return base
    if tree["truncated"]:
        # Never declare success on a truncated tree; still report what we saw for diagnostics.
        base["subagents"] = _subagent_entries(tree, active)
        base["pending_subagents"] = sum(
            1 for sid in ids if sid != root_id and sid in active
        )
        return base

    inter = _pending_interactions_tree(conn, ids)
    base["subagents"] = _subagent_entries(tree, active)
    if not inter["verified"]:
        return base
    base["verified"] = True
    base["permissions"] = inter["permissions"]
    base["forms"] = inter["forms"]
    base["pending_subagents"] = sum(
        1 for sid in ids if sid != root_id and sid in active
    )
    return base


def _subtree_quiescent(snap):
    """True only when verification succeeded and nothing in the subtree is live or waiting."""
    return bool(
        snap.get("verified")
        and not snap.get("truncated")
        and (snap.get("pending_subagents") or 0) == 0
        and not snap.get("permissions")
        and not snap.get("forms")
    )


def _attach_subtree(payload, snap):
    """Attach subtree fields to a payload without touching existing fields."""
    payload["subagents"] = snap.get("subagents") or []
    payload["pending_subagents"] = snap.get("pending_subagents")
    payload["subtree_truncated"] = bool(snap.get("truncated"))
    payload["subtree_verified"] = bool(snap.get("verified"))
    if not snap.get("verified"):
        payload.setdefault(
            "note",
            SUBTREE_UNSUPPORTED_NOTE if snap.get("legacy") else SUBTREE_UNVERIFIED_NOTE,
        )
    return payload


def _autoreply_subtree_permissions(conn, snap, decision, replied):
    """Answer every pending permission in the subtree by POSTing to each request's own session.

    `replied` carries the ids already answered in this wait, so a request that stays visible for
    more than one poll is not re-POSTed every second.
    """
    for req in snap.get("permissions") or []:
        rid = req.get("id")
        owner = req.get("sessionID")
        if not rid or not owner or rid in replied:
            continue
        replied.add(rid)
        try:
            http_request(
                conn,
                "POST",
                "/api/session/%s/permission/%s/reply"
                % (urllib.parse.quote(owner, safe=""), urllib.parse.quote(rid, safe="")),
                body={"decision": decision},
            )
        except OpenCodeError as exc:
            log("[octl] subtree permission auto-reply failed", owner, rid, exc)


def _enrich_forms(conn, forms):
    """Defensive fallback: refetch per owning session when the global list lacks field detail.

    Measured on the development baseline: /api/form already returns `fields` and `sessionID`, and
    /api/session/{id}/form does the same, so the early return below is the path actually taken and
    the refetch is unreachable there. It is kept for other builds whose global list is thinner, and
    it re-adds the owner id because the per-session shape may omit it (this baseline happens to
    include it as well). Best-effort: on any failure the original list is returned unchanged.
    """
    if not forms:
        return forms
    if all(isinstance(f.get("fields"), list) and f.get("fields") for f in forms):
        return forms
    owners = sorted({f.get("sessionID") for f in forms if f.get("sessionID")})
    if not owners:
        return forms
    out = []
    for owner in owners:
        try:
            node_forms = fetch_forms(conn, owner, pending_only=True)
        except OpenCodeError:
            return forms
        for form in node_forms:
            if isinstance(form, dict) and not form.get("sessionID"):
                form = dict(form, sessionID=owner)  # per-session forms omit the owner id
            out.append(form)
    return out or forms


def _subtree_needs_payload(conn, root_id, snap, kind):
    """needs_permission/needs_form payload whose session_id is the request's actual owning session."""
    if kind == "permission":
        items = snap.get("permissions") or []
        owners = [p.get("sessionID") for p in items if p.get("sessionID")]
        return {
            "status": "needs_permission",
            "server": conn.name,
            "session_id": owners[0] if owners else root_id,
            "root_session_id": root_id,
            "requests": [
                {
                    "id": p.get("id"),
                    "sessionID": p.get("sessionID"),
                    "action": p.get("action"),
                    "resources": p.get("resources"),
                    "save": p.get("save"),
                }
                for p in items
            ],
            "subagents": snap.get("subagents") or [],
            "pending_subagents": snap.get("pending_subagents"),
            "subtree_truncated": bool(snap.get("truncated")),
            "subtree_verified": bool(snap.get("verified")),
            "note": "Reply with permission_reply, then call wait_session to keep waiting "
            "(the request may belong to a subagent session; use its sessionID).",
        }
    items = _enrich_forms(conn, snap.get("forms") or [])
    owners = [f.get("sessionID") for f in items if f.get("sessionID")]
    return {
        "status": "needs_form",
        "server": conn.name,
        "session_id": owners[0] if owners else root_id,
        "root_session_id": root_id,
        "forms": [_form_summary(f) for f in items],
        "subagents": snap.get("subagents") or [],
        "pending_subagents": snap.get("pending_subagents"),
        "subtree_truncated": bool(snap.get("truncated")),
        "subtree_verified": bool(snap.get("verified")),
        "note": "Reply with form_reply, then call wait_session to keep waiting "
        "(the form may belong to a subagent session; use its sessionID).",
    }


# ---------------------------------------------------------------------------
# Unified wait core (shared by chat / wait_session / compact)
# Terminal determination trusts only the authoritative field Session.outcome (succeeded/failed/interrupted) + the gate message
# timestamp; no message-shape inference (five historical rounds of bugs all came from shape heuristics, now fully removed).
# On top of that, `succeeded` is gated on full-subtree quiescence when wait_for_subagents is set.
#
# poll_once is the single-step state snapshot (also usable by a non-blocking CLI --once mode);
# run_until_terminal is the loop shell. Cancellation is a surface concern: it is injected as a
# cancel_check callable, so core stays free of any MCP/process concept.
# ---------------------------------------------------------------------------


def _build_result(messages):
    assistant_texts = []
    reasoning_texts = []
    tools_used = []
    for message in messages:
        if message.get("type") != "assistant":
            continue
        text = "\n".join(_text_parts(message))
        if text:
            assistant_texts.append(text)
        reasoning_texts.extend(_reasoning_parts(message))
        tools_used.extend(_tool_parts(message))
    result = {
        "assistant_text": "\n\n".join(assistant_texts),
        "tools_used": tools_used,
    }
    if reasoning_texts:
        result["reasoning"] = reasoning_texts
    return result


def _result_payload(
    conn,
    session_id,
    status,
    baseline=None,
    with_result=False,
    time_idle=None,
    subtree=None,
):
    """Terminal response body; last_message_id always provides the incremental cursor, and with_result attaches this round's new replies."""
    try:
        messages = fetch_messages(conn, session_id)
    except OpenCodeError:
        messages = []
    payload = {
        "status": status,
        "server": conn.name,
        "session_id": session_id,
        "time_idle": time_idle,
        "last_message_id": messages[-1].get("id") if messages else None,
    }
    if with_result:
        new = [
            m
            for m in messages
            if baseline is None or m.get("id") not in (baseline or set())
        ]
        payload.update(_build_result(new))
        if not any(m.get("type") == "assistant" for m in new):
            payload.setdefault(
                "note", "Session is already completed/idle; no new replies this round."
            )
    if subtree is not None:
        _attach_subtree(payload, subtree)
    return payload


def poll_once(
    conn,
    session_id,
    auto_permission="manual",
    gate_message_id=None,
    gate_is_compaction=False,
    gate_created=None,
    round_gate=None,
    baseline=None,
    with_result=False,
    wait_for_subagents=False,
    replied_permissions=None,
):
    """One polling step; returns a snapshot dict.

    Snapshot keys:

    * ``status`` / ``payload``: a terminal / needs-interaction / compaction result, or both None
      meaning "still running, keep polling";
    * ``gate_created``: gate message created timestamp, cached across polls (pass it back in);
    * ``round_gate``: the round-gate evaluation dict when one was supplied, else None;
    * ``permissions`` / ``forms`` / ``outcome``: this step's root-session view, for timeout diagnostics;
    * ``subtree``: the subtree snapshot when one was computed, else None.

    ``round_gate`` (``{"watermark": float|None, "message_id": str|None}``, at least one set)
    is the stale-outcome defense for the chat->wait flow: v2 servers keep ``Session.outcome``
    frozen at the last completed execution while the next one runs, so a terminal outcome is
    only accepted once ``session.time.idle`` advanced past the pre-prompt watermark AND (when
    the enqueued message id is known) a ``type:"idle"`` turn-end message exists after it. Both
    are server-authoritative round markers, not message-shape heuristics.

    ``replied_permissions`` is per-wait ephemeral state (ids already auto-replied); the caller owns
    the set and passes it back across polls. This is deliberately not global state.
    """
    if round_gate and not (
        round_gate.get("watermark") is not None
        or round_gate.get("message_id") is not None
    ):
        round_gate = None
    if replied_permissions is None:
        replied_permissions = set()
    snapshot = {
        "status": None,
        "payload": None,
        "gate_created": gate_created,
        "round_gate": None,
        "permissions": [],
        "forms": [],
        "outcome": None,
        "subtree": None,
    }

    # a. Permission requests (the root session is always covered)
    try:
        permissions = fetch_permissions(conn, session_id)
    except OpenCodeError:
        permissions = []
    snapshot["permissions"] = permissions
    if permissions:
        if auto_permission in SUBTREE_AUTOREPLY:
            for req in permissions:
                rid = req.get("id")
                if not rid or rid in replied_permissions:
                    continue
                replied_permissions.add(rid)
                try:
                    http_request(
                        conn,
                        "POST",
                        "/api/session/%s/permission/%s/reply"
                        % (
                            urllib.parse.quote(session_id, safe=""),
                            urllib.parse.quote(rid, safe=""),
                        ),
                        body={"decision": auto_permission},
                    )
                except OpenCodeError as exc:
                    log("[octl] permission auto-reply failed", rid, exc)
        else:
            snapshot["status"] = "needs_permission"
            snapshot["payload"] = {
                "status": "needs_permission",
                "server": conn.name,
                "session_id": session_id,
                "root_session_id": session_id,
                "requests": [
                    {
                        "id": p.get("id"),
                        "sessionID": session_id,
                        "action": p.get("action"),
                        "resources": p.get("resources"),
                        "save": p.get("save"),
                    }
                    for p in permissions
                ],
                "note": "Reply with permission_reply, then call wait_session to keep waiting.",
            }
            return snapshot

    # b. Form requests
    try:
        forms = fetch_forms(conn, session_id, pending_only=True)
    except OpenCodeError:
        forms = []
    snapshot["forms"] = forms
    if forms:
        snapshot["status"] = "needs_form"
        snapshot["payload"] = {
            "status": "needs_form",
            "server": conn.name,
            "session_id": session_id,
            "root_session_id": session_id,
            "forms": [_form_summary(f) for f in forms],
            "note": "Reply with form_reply, then call wait_session to keep waiting.",
        }
        return snapshot

    # c. Authoritative session state
    info = unwrap(
        http_request(
            conn,
            "GET",
            "/api/session/%s" % urllib.parse.quote(session_id, safe=""),
        )
    )
    if not isinstance(info, dict):
        info = {}
    outcome = info.get("outcome")
    snapshot["outcome"] = outcome
    time_idle = (info.get("time") or {}).get("idle")

    # Round gate (stale-outcome defense). Evaluated before the terminal branch below; the
    # idle-message confirmation is only fetched once the watermark has advanced, so the
    # steady-state poll cost is unchanged.
    if round_gate is not None:
        watermark = round_gate.get("watermark")
        rg_message_id = round_gate.get("message_id")
        watermark_passed = watermark is None or (time_idle or 0) > watermark
        idle_message_seen = True
        if rg_message_id is not None:
            idle_message_seen = False
            if watermark_passed:
                try:
                    rg_msgs = fetch_messages(conn, session_id)
                except OpenCodeError:
                    rg_msgs = []
                gate_index = None
                for i, m in enumerate(rg_msgs):
                    if m.get("id") == rg_message_id:
                        gate_index = i
                        break
                if gate_index is not None:
                    for m in rg_msgs[gate_index + 1:]:
                        if m.get("type") == "idle":
                            idle_message_seen = True
                            break
        snapshot["round_gate"] = {
            "watermark": watermark,
            "message_id": rg_message_id,
            "time_idle": time_idle,
            "watermark_passed": watermark_passed,
            "idle_message_seen": idle_message_seen,
        }

    # Gate message: cache the created timestamp; in the compact case accept its message terminal state directly
    if gate_message_id is not None and gate_created is None:
        try:
            gate_msgs = fetch_messages(conn, session_id)
        except OpenCodeError:
            gate_msgs = []
        for gm in gate_msgs:
            if gm.get("id") == gate_message_id:
                gate_created = (gm.get("time") or {}).get("created")
                snapshot["gate_created"] = gate_created
                if gate_is_compaction:
                    gstatus = gm.get("status")
                    if gstatus == "completed":
                        snapshot["status"] = "succeeded"
                        snapshot["payload"] = _result_payload(
                            conn, session_id, "succeeded", baseline, with_result, time_idle
                        )
                        return snapshot
                    if gstatus == "failed":
                        snapshot["status"] = "compaction_failed"
                        snapshot["payload"] = {
                            "status": "compaction_failed",
                            "server": conn.name,
                            "session_id": session_id,
                            "note": "Context compaction failed (compaction status=failed).",
                        }
                        return snapshot
                break

    if outcome in ("succeeded", "failed", "interrupted"):
        gate_ok = gate_message_id is None or (
            gate_created is not None and (time_idle or 0) > gate_created
        )
        if round_gate is not None:
            rg = snapshot["round_gate"]
            gate_ok = gate_ok and rg["watermark_passed"] and rg["idle_message_seen"]
        if gate_ok:
            # failed / interrupted are decided outcomes: return immediately, never delayed.
            # A terminal `succeeded` without subtree gating still reports subagent state.
            if outcome != "succeeded" or not wait_for_subagents:
                payload = _result_payload(
                    conn, session_id, outcome, baseline, with_result, time_idle
                )
                if outcome == "succeeded":
                    snap = _subtree_snapshot(conn, session_id)
                    _attach_subtree(payload, snap)
                    snapshot["subtree"] = snap
                snapshot["status"] = outcome
                snapshot["payload"] = payload
                return snapshot

            # succeeded + wait_for_subagents: the parent outcome is per-turn, so accept it
            # only when the entire subtree is quiescent. The structure and activity map are
            # read fresh here (decision-point refresh), not from an earlier snapshot.
            snap = _subtree_snapshot(conn, session_id)
            snapshot["subtree"] = snap
            if snap["legacy"]:
                # The server cannot support verification: fall back, but never claim it was verified.
                payload = _result_payload(
                    conn, session_id, "succeeded", baseline, with_result, time_idle
                )
                _attach_subtree(payload, snap)
                snapshot["status"] = "succeeded"
                snapshot["payload"] = payload
                return snapshot
            if _subtree_quiescent(snap):
                payload = _result_payload(
                    conn,
                    session_id,
                    "succeeded",
                    baseline,
                    with_result,
                    time_idle,
                    subtree=snap,
                )
                snapshot["status"] = "succeeded"
                snapshot["payload"] = payload
                return snapshot

            # Not quiescent (or UNKNOWN). UNKNOWN must never become success: keep polling
            # until the timeout, then report a timeout diagnostic.
            if snap["verified"]:
                if snap["permissions"]:
                    if auto_permission in SUBTREE_AUTOREPLY:
                        _autoreply_subtree_permissions(
                            conn, snap, auto_permission, replied_permissions
                        )
                    else:
                        snapshot["status"] = "needs_permission"
                        snapshot["payload"] = _subtree_needs_payload(
                            conn, session_id, snap, "permission"
                        )
                        return snapshot
                if snap["forms"]:  # forms are never auto-answered
                    snapshot["status"] = "needs_form"
                    snapshot["payload"] = _subtree_needs_payload(
                        conn, session_id, snap, "form"
                    )
                    return snapshot
            # fall through: active subagents, truncated tree or UNKNOWN -> keep polling

    return snapshot


def _timeout_payload(conn, session_id, timeout_secs, baseline, snapshot):
    """Timeout diagnostics (the diagnostics block includes the last message and this round's partial text)."""
    try:
        msgs = fetch_messages(conn, session_id)
    except OpenCodeError:
        msgs = []
    last = msgs[-1] if msgs else None
    new = [
        m
        for m in msgs
        if baseline is None or m.get("id") not in (baseline or set())
    ]
    snap = _subtree_snapshot(conn, session_id)
    permissions = snapshot.get("permissions") or []
    forms = snapshot.get("forms") or []
    outcome = snapshot.get("outcome")
    if snap.get("truncated"):
        note = (
            "Wait timed out (%s seconds); the session is still generating. "
            "The subagent subtree was truncated by the depth/node caps, so quiescence could not be confirmed."
            % timeout_secs
        )
    elif not snap.get("verified") and not snap.get("legacy"):
        note = (
            "Wait timed out (%s seconds); the session is still generating. "
            "Subagent activity could not be verified (fail-closed): the terminal state was not accepted."
            % timeout_secs
        )
    else:
        note = (
            "Wait timed out (%s seconds); the session is still generating."
            % timeout_secs
        )
    payload = {
        "status": "timeout",
        "server": conn.name,
        "session_id": session_id,
        "partial_text": _build_result(new)["assistant_text"],
        "diagnostics": {
            "server": conn.name,
            "outcome": outcome,
            "last_message": (
                {
                    "id": last.get("id"),
                    "type": last.get("type"),
                    "status": last.get("status"),
                    "completed": bool(
                        (last.get("time") or {}).get("completed")
                    ),
                }
                if isinstance(last, dict)
                else None
            ),
            "pending_permissions": len(permissions),
            "pending_forms": len(forms),
            "active_subagents": [
                {
                    "session_id": s.get("session_id"),
                    "agent": s.get("agent"),
                    "title": s.get("title"),
                    "outcome": s.get("outcome"),
                    "active": s.get("active"),
                }
                for s in (snap.get("subagents") or [])
                if s.get("active")
            ],
            "pending_subtree_permissions": len(snap.get("permissions") or []),
            "pending_subtree_forms": len(snap.get("forms") or []),
            "pending_subagents": snap.get("pending_subagents"),
            "subtree_verified": bool(snap.get("verified")),
            "subtree_truncated": bool(snap.get("truncated")),
            "suggested_actions": [
                "get_messages to check current progress",
                "pending_interactions to check pending interactions",
                "wait_session to keep waiting",
                "interrupt to stop generation",
            ],
        },
        "note": note,
    }
    if snapshot.get("round_gate") is not None:
        payload["diagnostics"]["round_gate"] = snapshot["round_gate"]
    _attach_subtree(payload, snap)
    return payload


def run_until_terminal(
    conn,
    session_id,
    timeout_secs,
    auto_permission="manual",
    gate_message_id=None,
    gate_is_compaction=False,
    round_gate=None,
    baseline=None,
    with_result=False,
    wait_for_subagents=False,
    cancel_check=None,
):
    """Poll the session until terminal / needs interaction / timeout / cancellation (the unified wait core).

    Returns (status, payload). status ∈ {succeeded, failed, interrupted,
    compaction_failed, needs_permission, needs_form, timeout, cancelled}

    wait_for_subagents: when set, `succeeded` additionally requires the whole subtree to be
    quiescent (no active node, no pending permission/form anywhere in the subtree). failed /
    interrupted are always returned immediately. auto_permission in once/always/reject answers
    pending permissions of every subtree node by POSTing to each request's owning session.

    cancel_check (optional callable) is the surface's cancellation probe; timeout and the
    per-wait replied-permission dedup live here, in the loop shell.
    """
    started = time.monotonic()
    gate_created = None
    replied_permissions = set()  # ids already auto-replied: never re-POST the same request each poll
    while True:
        if cancel_check is not None and cancel_check():
            return "cancelled", {"status": "cancelled", "note": "The caller cancelled this request"}

        snapshot = poll_once(
            conn,
            session_id,
            auto_permission=auto_permission,
            gate_message_id=gate_message_id,
            gate_is_compaction=gate_is_compaction,
            gate_created=gate_created,
            round_gate=round_gate,
            baseline=baseline,
            with_result=with_result,
            wait_for_subagents=wait_for_subagents,
            replied_permissions=replied_permissions,
        )
        gate_created = snapshot["gate_created"]
        if snapshot["status"] is not None:
            return snapshot["status"], snapshot["payload"]

        # d. Timeout (the diagnostics block includes the last message and this round's partial text)
        if time.monotonic() - started >= timeout_secs:
            return "timeout", _timeout_payload(
                conn, session_id, timeout_secs, baseline, snapshot
            )

        time.sleep(POLL_INTERVAL)
