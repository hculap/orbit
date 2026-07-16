#!/usr/bin/env python3
"""
dashboard_mcp — a zero-dependency stdio MCP server (mcp__dashboard__*) giving the
orbit orchestrator agent typed tools to drive THIS system instead of
ad-hoc curl. Issue #95.

It is a thin, robust wrapper over the dashboard's HTTP+SSE API (port 8766) plus a
tmux control plane (sessions run as tmux session `hd-<session_id>`), so the agent
can: list / search / find / create / start / stop the *other* PARA agent
sessions, send a message and wait for the reply (the request/response
primitive), read transcripts, and — for interactive children — capture the live
pane and answer AskUserQuestion / permission prompts by injecting keystrokes.
Plus PARA discovery (`para_overview`) and `notify` (Telegram push).

Single source of truth = the dashboard HTTP API; this is a thin adapter, it does
NOT re-implement session/PARA logic.

Transport: MCP stdio = newline-delimited JSON-RPC 2.0 on stdin/stdout.
NOTHING may be written to stdout except protocol frames; logs go to stderr.

No third-party deps: stdlib urllib for HTTP, subprocess for tmux. Runs on any
python3.11+ (system python or the dashboard venv) — no `mcp`/FastMCP install.

Env (the dashboard injects HD_SESSION_ID/HD_BASE_URL per-spawn via session_env):
  HD_BASE_URL / ORCH_BASE_URL  dashboard base (default http://localhost:8766)
  HD_SESSION_ID                this session's id (self-protection for send_keys)
  ORCH_TMUX_SOCKET             tmux socket (default hd-orch)
"""

import sys
import os
import json
import time
import shlex
import subprocess
import urllib.request
import urllib.parse
import urllib.error

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
BASE_URL = (os.environ.get("HD_BASE_URL") or os.environ.get("ORCH_BASE_URL")
            or "http://localhost:8766").rstrip("/")
API = BASE_URL + "/api"
TMUX_PREFIX = os.environ.get("ORCH_TMUX_PREFIX", "hd-")
# The dashboard runs its session panes on a DEDICATED tmux socket `hd-orch`
# (NOT the default socket). Bare `tmux` only reaches them when $TMUX is
# inherited; pass `-L hd-orch` explicitly so this works from any env.
TMUX_SOCKET = os.environ.get("ORCH_TMUX_SOCKET", "hd-orch")
HTTP_TIMEOUT = float(os.environ.get("ORCH_HTTP_TIMEOUT", "70"))
SERVER_NAME = "dashboard"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"

# This server's own session id, if known — so it refuses to inject keystrokes
# into the pane that is driving it (self-protection). The dashboard injects
# HD_SESSION_ID into every spawned session's env; ORCHESTRATOR_SESSION_ID is the
# global-agent variant. Either lets us recognise "my own pane".
SELF_SESSION_ID = (
    os.environ.get("ORCHESTRATOR_SESSION_ID")
    or os.environ.get("HD_SESSION_ID")
    or ""
).strip()


def log(*a):
    print(f"[orchestrator_mcp]", *a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body[:300]}")


def _http(method, path, query=None, body=None, timeout=None):
    """Call the dashboard API. Returns parsed JSON (or {'_raw': text})."""
    url = API + path
    if query:
        clean = {k: v for k, v in query.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout or HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"_raw": raw}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        # The API uses HTTP 200 + {ok:false} for busy; real errors land here.
        try:
            return {"_http_error": e.code, **json.loads(raw)}
        except json.JSONDecodeError:
            raise ApiError(e.code, raw)
    except urllib.error.URLError as e:
        raise ApiError(0, f"connection error: {e.reason}")


# --------------------------------------------------------------------------- #
# tmux helpers
# --------------------------------------------------------------------------- #
def _tmux_target(session_id):
    return f"{TMUX_PREFIX}{session_id}"


def _tmux(args, timeout=10):
    """Run a tmux command on the dashboard's `hd-orch` socket. Returns (rc, stdout, stderr)."""
    try:
        p = subprocess.run(
            ["tmux", "-L", TMUX_SOCKET, *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "tmux not installed"
    except subprocess.TimeoutExpired:
        return 124, "", "tmux command timed out"


def _tmux_has(session_id):
    rc, _, _ = _tmux(["has-session", "-t", _tmux_target(session_id)])
    return rc == 0


def _tmux_capture(session_id, lines=None):
    target = _tmux_target(session_id)
    args = ["capture-pane", "-p", "-t", target]
    if lines:
        # -S -N gives the last N lines of scrollback+visible
        args = ["capture-pane", "-p", "-S", f"-{int(lines)}", "-t", target]
    rc, out, err = _tmux(args)
    if rc != 0:
        raise ApiError(0, f"capture-pane failed for {target}: {err.strip() or rc}")
    return out


# Friendly key aliases → tmux send-keys tokens
_KEY_ALIASES = {
    "enter": "Enter", "return": "Enter",
    "esc": "Escape", "escape": "Escape",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "tab": "Tab", "backtab": "BTab", "btab": "BTab",
    "space": "Space", "backspace": "BSpace", "bspace": "BSpace",
    "pageup": "PageUp", "pagedown": "PageDown",
    "home": "Home", "end": "End", "delete": "DC",
}


def _tmux_send_keys(session_id, keys, literal_text=None, guard_self=True, key_delay=0.0):
    """
    Inject keystrokes into a session's pane.
      - literal_text: typed verbatim (e.g. a free-text answer).
      - keys: list of named keys / tokens sent after the text (e.g. ["Enter"],
        ["Down","Down","Enter"]). Aliases in _KEY_ALIASES are normalised.
      - key_delay: seconds to pause between keys (a TUI menu can miss a Down that
        is immediately followed by Enter; ~0.3s makes navigation reliable).
    Refuses to target this server's own driving session unless guard disabled.
    """
    if guard_self and SELF_SESSION_ID and session_id == SELF_SESSION_ID:
        raise ApiError(0, "refusing to send keys to my own session (self-protection)")
    target = _tmux_target(session_id)
    if not _tmux_has(session_id):
        raise ApiError(0, f"no tmux session {target} (is it started?)")
    sent = []
    if literal_text:
        rc, _, err = _tmux(["send-keys", "-t", target, "-l", literal_text])
        if rc != 0:
            raise ApiError(0, f"send-keys (text) failed: {err.strip() or rc}")
        sent.append(repr(literal_text))
    for idx, k in enumerate(keys or []):
        if key_delay and (idx or literal_text):
            time.sleep(key_delay)
        tok = _KEY_ALIASES.get(str(k).lower(), str(k))
        rc, _, err = _tmux(["send-keys", "-t", target, tok])
        if rc != 0:
            raise ApiError(0, f"send-keys ({tok}) failed: {err.strip() or rc}")
        sent.append(tok)
    return sent


def _list_orchestrator_tmux():
    rc, out, _ = _tmux(["ls", "-F", "#{session_name}"])
    if rc != 0:
        return []
    res = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith(TMUX_PREFIX):
            res.append({"session_id": line[len(TMUX_PREFIX):], "tmux": line})
    return res


# --------------------------------------------------------------------------- #
# Message / block extraction
# --------------------------------------------------------------------------- #
_TEXT_KINDS = ("markdown", "text")


def _assistant_text(messages):
    """Flatten assistant message blocks into plain text."""
    chunks = []
    for m in messages or []:
        if m.get("role") != "assistant":
            continue
        for b in m.get("blocks", []):
            if b.get("kind") in _TEXT_KINDS:
                t = b.get("text") or b.get("markdown") or ""
                if t:
                    chunks.append(t)
    return "\n\n".join(chunks).strip()


# --------------------------------------------------------------------------- #
# Tool implementations  (each returns a JSON-serialisable object)
# --------------------------------------------------------------------------- #
def t_capabilities(_):
    return _http("GET", "/orchestrator/capabilities")


def t_list_agents(args):
    fresh = args.get("fresh")
    data = _http("GET", "/data", query={"fresh": 1 if fresh else None})
    out = {"areas": [], "projects": []}
    for kind in ("areas", "projects"):
        for it in data.get(kind, []) or []:
            out[kind].append({
                "lib_id": it.get("lib_id"),
                "label": it.get("label"),
                "cwd": it.get("cwd") or it.get("path"),
                "icon": it.get("icon"),
                "description": it.get("description"),
            })
    return out


def t_list_sessions(args):
    return _http("GET", "/orchestrator/sessions",
                 query={"cwd": args.get("cwd"),
                        "include_corpus": "false"})


def t_search_sessions(args):
    return _http("GET", "/orchestrator/sessions/search",
                 query={"q": args["q"], "limit": args.get("limit"),
                        "cwd": args.get("cwd")})


def t_create_session(args):
    body = {k: v for k, v in {
        "title": args.get("title"),
        "cwd": args.get("cwd"),
        "lib_id": args.get("lib_id"),
        "model": args.get("model"),
        "extra_system_prompt": args.get("extra_system_prompt"),
    }.items() if v is not None}
    return _http("POST", "/orchestrator/sessions", body=body)


def t_start_session(args):
    return _http("POST", f"/orchestrator/sessions/{args['session_id']}/start")


def t_stop_session(args):
    return _http("POST", f"/orchestrator/sessions/{args['session_id']}/stop")


def t_cancel_turn(args):
    return _http("POST", f"/orchestrator/sessions/{args['session_id']}/cancel")


def t_delete_session(args):
    return _http("DELETE", f"/orchestrator/sessions/{args['session_id']}")


def t_set_model(args):
    return _http("PATCH", f"/orchestrator/sessions/{args['session_id']}/model",
                 body={"model": args.get("model")})


def t_session_status(args):
    return _http("GET", f"/orchestrator/sessions/{args['session_id']}/status")


def t_session_state(args):
    return _http("GET", f"/orchestrator/sessions/{args['session_id']}/state")


def t_running_turns(_):
    return _http("GET", "/orchestrator/turns/running")


def t_para_overview(args):
    """PARA discovery: areas / projects / resources (each carries lib_id + cwd —
    the cwd is what create_session needs)."""
    data = _http("GET", "/data", query={"fresh": 1 if args.get("fresh") else None})
    out = {}
    for kind in ("areas", "projects", "resources"):
        out[kind] = [{
            "lib_id": it.get("lib_id"), "label": it.get("label"),
            "cwd": it.get("cwd") or it.get("path"),
            "icon": it.get("icon"), "description": it.get("description"),
        } for it in (data.get(kind) or [])]
    out["system"] = data.get("system")
    out["host"] = data.get("host")
    return out


def t_notify(args):
    """Send a Telegram push to the user's phone via the dashboard (no token)."""
    body = {"text": args["text"]}
    for k in ("topic", "title", "url"):
        if args.get(k) is not None:
            body[k] = args[k]
    return _http("POST", "/notify", body=body)


def t_get_messages(args):
    return _http("GET", f"/orchestrator/sessions/{args['session_id']}/messages",
                 query={"after_turn": args.get("after_turn"),
                        "limit": args.get("limit")})


def t_send_message(args):
    body = {"text": args["text"]}
    if args.get("reply_to_turn_idx") is not None:
        body["reply_to_turn_idx"] = args["reply_to_turn_idx"]
    if args.get("interactive_mode") is not None:
        body["interactive_mode"] = args["interactive_mode"]
    return _http("POST", f"/orchestrator/sessions/{args['session_id']}/messages",
                 body=body)


def t_wait_for_reply(args):
    timeout = min(float(args.get("timeout", 25) or 25), 60)
    return _http("GET", f"/orchestrator/sessions/{args['session_id']}/wait",
                 query={"since_turn": args["since_turn"], "timeout": timeout},
                 timeout=timeout + 10)


def t_send_and_wait(args):
    """
    The core request/response primitive: send a message, then long-poll /wait
    until the assistant turn lands (or errors), and return its plain text.
    Handles 'busy' and 429 backoff and re-polls on timeout.
    """
    sid = args["session_id"]
    overall_deadline = time.monotonic() + float(args.get("overall_timeout", 600))
    # 1) send
    send = t_send_message(args)
    if not isinstance(send, dict):
        return {"ok": False, "error": "unexpected send response", "raw": send}
    if send.get("status") == "busy" or (send.get("ok") is False and "flight" in str(send.get("error", ""))):
        return {"ok": False, "status": "busy", "detail": send,
                "hint": "a turn is already running; cancel_turn first or wait"}
    expected = send.get("expected_turn_idx")
    if expected is None:
        return {"ok": False, "error": "no expected_turn_idx in send response", "raw": send}
    # 2) poll
    since = expected
    poll_timeout = min(float(args.get("poll_timeout", 25) or 25), 60)
    while time.monotonic() < overall_deadline:
        try:
            w = _http("GET", f"/orchestrator/sessions/{sid}/wait",
                      query={"since_turn": since, "timeout": poll_timeout},
                      timeout=poll_timeout + 10)
        except ApiError as e:
            if e.status == 429:
                time.sleep(2)
                continue
            raise
        if w.get("_http_error") == 429:
            time.sleep(2)
            continue
        status = w.get("status")
        if status == "done":
            msgs = w.get("new_messages", [])
            return {"ok": True, "status": "done",
                    "latest_turn_idx": w.get("latest_turn_idx"),
                    "text": _assistant_text(msgs),
                    "new_messages": msgs,
                    "cost_usd": w.get("cost_usd")}
        if status == "error":
            return {"ok": False, "status": "error", "error": w.get("error"),
                    "latest_turn_idx": w.get("latest_turn_idx"), "raw": w}
        # timeout → re-poll with same cursor
    return {"ok": False, "status": "timeout",
            "hint": "overall_timeout reached; the turn may still be running — "
                    "call wait_for_reply with since_turn=%s to keep waiting" % expected,
            "since_turn": expected}


# ---- tmux / interactive control plane ----
def t_find_tmux(args):
    sid = args.get("session_id")
    if sid:
        return {"session_id": sid, "tmux": _tmux_target(sid),
                "online": _tmux_has(sid)}
    return {"sessions": _list_orchestrator_tmux()}


def t_capture_pane(args):
    sid = args["session_id"]
    if not _tmux_has(sid):
        return {"ok": False, "online": False,
                "hint": f"no tmux session {_tmux_target(sid)} — call start_session first"}
    text = _tmux_capture(sid, args.get("lines"))
    return {"ok": True, "online": True, "tmux": _tmux_target(sid),
            "pane": text, "pending_question": _detect_pending(text)}


import re as _re

# A pane awaiting input has a strong, specific signature (the AskUserQuestion /
# permission menu footer + a numbered list with the ❯ cursor on a numbered row).
# The plain idle prompt also shows "❯" and "esc to interrupt", so those weak
# signals are deliberately EXCLUDED to avoid false positives.
# The AskUserQuestion / permission menu has a very specific signature that the
# thinking state ("Proofing…") and the prompt echo do NOT: a numbered options
# list (≥2 rows like "❯ 1. APPLE" / "  2. CHERRY") AND a navigation footer
# ("Enter to select · ↑/↓ to navigate · Esc to cancel" / "Enter to confirm").
# Requiring BOTH avoids the false-positive that fires keystrokes into a
# mid-render pane and derails the turn.
_OPTION_ROW = _re.compile(r"^\s*[❯›▶]?\s*(\d+)\.\s+\S", _re.M)
_NAV_FOOTER = _re.compile(r"(↑/↓\s*to navigate|to select\s*·|enter to (select|confirm))", _re.I)
_CURSOR_ROW = _re.compile(r"^\s*[❯›▶]\s*\d+\.\s+\S", _re.M)   # the highlighted option


def _detect_pending(pane):
    """Heuristic: is the pane awaiting an interactive choice? Advisory only —
    always read `pane` to confirm before answering."""
    option_nums = {m.group(1) for m in _OPTION_ROW.finditer(pane)}
    has_footer = bool(_NAV_FOOTER.search(pane))
    has_cursor = bool(_CURSOR_ROW.search(pane))
    signals = []
    if len(option_nums) >= 2:
        signals.append(f"options:{len(option_nums)}")
    if has_footer:
        signals.append("nav-footer")
    if has_cursor:
        signals.append("cursor-on-option")
    # Real menu == a multi-row numbered list AND (footer OR a cursored option).
    likely = (len(option_nums) >= 2) and (has_footer or has_cursor)
    return {"likely_waiting": likely, "signals": signals}


def t_send_keys(args):
    sent = _tmux_send_keys(
        args["session_id"],
        keys=args.get("keys") or [],
        literal_text=args.get("text"),
        guard_self=not args.get("force", False),
    )
    out = None
    if args.get("capture_after", True):
        time.sleep(float(args.get("settle", 0.4)))
        try:
            out = _tmux_capture(args["session_id"], args.get("lines", 40))
        except ApiError:
            out = None
    return {"ok": True, "sent": sent, "pane_after": out}


def t_answer_question(args):
    """
    Answer an AskUserQuestion-style menu in a child's pane.
      mode='option_index': press Down (index) times then Enter to pick option N (0-based visible order).
      mode='text': type free text then Enter (for the 'Other' / free-form field — opens it first if needed).
      mode='keys': pass an explicit key list.
    Always captures the pane before & after so the caller can verify.
    """
    sid = args["session_id"]
    if not _tmux_has(sid):
        return {"ok": False, "error": f"no tmux session {_tmux_target(sid)}"}
    before = _tmux_capture(sid, 40)
    mode = args.get("mode", "keys")
    kd = float(args.get("key_delay", 0.35))
    if mode == "option_index":
        idx = int(args["option_index"])
        # Move the cursor to the Nth option, then Enter — with a delay so the
        # menu registers each Down before the Enter selects.
        keys = ["Down"] * idx + ["Enter"]
        _tmux_send_keys(sid, keys=keys, guard_self=not args.get("force"), key_delay=kd)
    elif mode == "text":
        _tmux_send_keys(sid, literal_text=args["text"], keys=["Enter"],
                        guard_self=not args.get("force"), key_delay=kd)
    elif mode == "keys":
        _tmux_send_keys(sid, keys=args.get("keys", []),
                        literal_text=args.get("text"),
                        guard_self=not args.get("force"), key_delay=kd)
    else:
        return {"ok": False, "error": f"unknown mode {mode}"}
    time.sleep(float(args.get("settle", 0.6)))
    after = _tmux_capture(sid, 40)
    return {"ok": True, "before": before, "after": after}


# --------------------------------------------------------------------------- #
# Tool registry
# --------------------------------------------------------------------------- #
def _S(props, required=()):
    return {"type": "object", "properties": props,
            "required": list(required), "additionalProperties": False}


_SID = {"session_id": {"type": "string", "description": "Orchestrator session UUID"}}

TOOLS = [
    ("capabilities", "Probe the live orchestrator API feature flags + limits.",
     _S({}), t_capabilities),
    ("list_agents", "List PARA agents (areas + projects) with lib_id and cwd. cwd is what you pass to create_session.",
     _S({"fresh": {"type": "boolean", "description": "bypass 30s cache"}}), t_list_agents),
    ("list_sessions", "List sessions, optionally filtered to one agent by cwd (abs dir) or '__global__'.",
     _S({"cwd": {"type": "string", "description": "agent abs dir, or __global__ for unscoped"}}), t_list_sessions),
    ("search_sessions", "Hybrid (BM25+vector) search over session transcripts — 'find the session where we talked about X'.",
     _S({"q": {"type": "string"}, "limit": {"type": "integer"}, "cwd": {"type": "string"}}, ["q"]), t_search_sessions),
    ("create_session", "Create a new session bound to an agent (cwd must be an existing dir under $HOME).",
     _S({"cwd": {"type": "string"}, "lib_id": {"type": "string"}, "model": {"type": "string", "enum": ["opus", "sonnet", "haiku"]},
         "title": {"type": "string"}, "extra_system_prompt": {"type": "string"}}, ["cwd"]), t_create_session),
    ("start_session", "Warm the interactive (tmux) runtime so the session is online & ready to take keystrokes.",
     _S(_SID, ["session_id"]), t_start_session),
    ("stop_session", "Tear down the live runtime (cancel in-flight turn, release tmux), keep the transcript.",
     _S(_SID, ["session_id"]), t_stop_session),
    ("cancel_turn", "Cancel just the in-flight turn of a session.",
     _S(_SID, ["session_id"]), t_cancel_turn),
    ("delete_session", "DELETE transcript + sidecar. Destructive.",
     _S(_SID, ["session_id"]), t_delete_session),
    ("set_session_model", "Set a session's model (opus|sonnet|haiku|null).",
     _S({**_SID, "model": {"type": ["string", "null"]}}, ["session_id"]), t_set_model),
    ("session_status", "Is a turn in flight? {in_flight, started_at_ms, last_seq}.",
     _S(_SID, ["session_id"]), t_session_status),
    ("session_state", "Live {todos[], plan} of the running turn.",
     _S(_SID, ["session_id"]), t_session_state),
    ("running_turns", "All currently-running turns across sessions {session_id, started_at, runner}.",
     _S({}), t_running_turns),
    ("para_overview", "PARA discovery: areas/projects/resources with lib_id + cwd (the cwd feeds create_session), plus system/host snapshot.",
     _S({"fresh": {"type": "boolean", "description": "bypass 30s cache"}}), t_para_overview),
    ("notify", "Send a Telegram push to the user's phone via the dashboard. Use for async results / attention-worthy events — not every reply.",
     _S({"text": {"type": "string"}, "topic": {"type": "string"}, "title": {"type": "string"},
         "url": {"type": "string", "description": "tappable button URL"}}, ["text"]), t_notify),
    ("get_messages", "Read transcript (paginated). after_turn=cursor, limit=last N.",
     _S({**_SID, "after_turn": {"type": "integer"}, "limit": {"type": "integer"}}, ["session_id"]), t_get_messages),
    ("send_message", "Send a message to a session. Returns expected_turn_idx (pass to wait_for_reply). Busy => {ok:false,status:busy}.",
     _S({**_SID, "text": {"type": "string"}, "reply_to_turn_idx": {"type": "integer"},
         "interactive_mode": {"type": "boolean"}}, ["session_id", "text"]), t_send_message),
    ("wait_for_reply", "Long-poll for an assistant turn past since_turn. status done|error|timeout (re-poll on timeout).",
     _S({**_SID, "since_turn": {"type": "integer"}, "timeout": {"type": "number"}}, ["session_id", "since_turn"]), t_wait_for_reply),
    ("send_and_wait", "Send a message AND block until the reply lands, returning the assistant's plain text. The main delegate-and-collect primitive.",
     _S({**_SID, "text": {"type": "string"}, "reply_to_turn_idx": {"type": "integer"},
         "interactive_mode": {"type": "boolean"},
         "poll_timeout": {"type": "number", "description": "per /wait poll, <=60s"},
         "overall_timeout": {"type": "number", "description": "give up after N seconds (default 600)"}}, ["session_id", "text"]), t_send_and_wait),
    # tmux / interactive control plane
    ("find_tmux", "Find the tmux target for a session_id (hd-<id>) and whether it's online; or list all orchestrator tmux sessions.",
     _S({"session_id": {"type": "string"}}), t_find_tmux),
    ("capture_pane", "Capture the live tmux pane of a session (what the child is currently showing). Includes a heuristic 'pending_question' flag — use to detect AskUserQuestion/permission prompts.",
     _S({**_SID, "lines": {"type": "integer", "description": "scrollback lines (default: visible only)"}}, ["session_id"]), t_capture_pane),
    ("send_keys", "Inject raw keystrokes into a session's pane. text=literal typed string; keys=named keys (Enter, Down, Escape, Tab, ...). Use to drive interactive UIs.",
     _S({**_SID, "text": {"type": "string"}, "keys": {"type": "array", "items": {"type": "string"}},
         "capture_after": {"type": "boolean"}, "settle": {"type": "number"}, "lines": {"type": "integer"},
         "force": {"type": "boolean", "description": "bypass self-session guard"}}, ["session_id"]), t_send_keys),
    ("answer_question", "Answer an AskUserQuestion/menu in a child pane. mode=option_index (pick visible option N, 0-based, via Down*N+Enter) | text (type+Enter) | keys (explicit). Captures pane before/after.",
     _S({**_SID, "mode": {"type": "string", "enum": ["option_index", "text", "keys"]},
         "option_index": {"type": "integer"}, "text": {"type": "string"},
         "keys": {"type": "array", "items": {"type": "string"}},
         "settle": {"type": "number"}, "force": {"type": "boolean"}}, ["session_id"]), t_answer_question),
]

TOOL_MAP = {name: fn for (name, _d, _s, fn) in TOOLS}
TOOL_DEFS = [{"name": n, "description": d, "inputSchema": s} for (n, d, s, _f) in TOOLS]


# --------------------------------------------------------------------------- #
# JSON-RPC / MCP loop
# --------------------------------------------------------------------------- #
def _result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": err}


def _send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def handle(msg):
    method = msg.get("method")
    id_ = msg.get("id")
    is_notification = "id" not in msg

    if method == "initialize":
        client_proto = (msg.get("params") or {}).get("protocolVersion")
        return _result(id_, {
            "protocolVersion": client_proto or PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _result(id_, {})
    if method == "tools/list":
        return _result(id_, {"tools": TOOL_DEFS})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        fn = TOOL_MAP.get(name)
        if not fn:
            return _error(id_, -32601, f"unknown tool: {name}")
        try:
            out = fn(arguments)
            text = json.dumps(out, ensure_ascii=False, indent=2, default=str)
            return _result(id_, {"content": [{"type": "text", "text": text}],
                                 "isError": isinstance(out, dict) and out.get("ok") is False})
        except ApiError as e:
            return _result(id_, {"content": [{"type": "text",
                            "text": json.dumps({"ok": False, "error": str(e), "status": e.status})}],
                                 "isError": True})
        except Exception as e:  # noqa: BLE001 — surface everything to the agent
            return _result(id_, {"content": [{"type": "text",
                            "text": json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"})}],
                                 "isError": True})
    if is_notification:
        return None
    return _error(id_, -32601, f"method not found: {method}")


def main():
    log(f"starting; BASE_URL={BASE_URL} self={SELF_SESSION_ID or '(unset)'}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            log("bad json:", e)
            continue
        try:
            resp = handle(msg)
        except Exception as e:  # noqa: BLE001
            log("handler crash:", e)
            resp = _error(msg.get("id"), -32603, f"internal error: {e}")
        if resp is not None:
            _send(resp)
    log("stdin closed; exiting")


if __name__ == "__main__":
    main()
