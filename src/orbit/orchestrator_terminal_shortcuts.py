"""Server-side store for the mobile terminal soft-keyboard **layout**.

The orchestrator's mobile soft-keyboard (``_MobileSoftKeyboard`` in
``static/orchestrator-terminal-preview.jsx``) renders a row of buttons that send
keys to the live xterm.js terminal. v2 of this feature lets the user fully
manage that layout — define views (tabs) and, per view, buttons of several
KINDS — via the Settings panel. This module persists the **complete layout
tree** (not a sparse diff): ``~/.orchestrator/terminal_shortcuts.json``.

Authoritative seed: ``DEFAULT_LAYOUT`` below is the single source of truth for
the out-of-the-box layout, transcribed once from the frontend's hard-coded
arrays (``_NAV_KEYS`` / ``_SPECIAL_KEYS`` / ``_ACTION_KEYS`` / ``_SOFT_VIEWS`` in
``orchestrator-terminal-preview.jsx``). ``GET`` always returns a full,
ready-to-render layout (seeded if no file), so a cold toolbar never needs the
heavy preview JS to build anything. The frontend keeps those literal arrays
ONLY as the flag-off render fallback (byte-identical rollback).

Structural anchors (cycler, Esc, Tab) are NOT in the layout — the toolbar always
renders them — so a user can never delete the view-switcher or the agent-stop
key and brick their own toolbar.

Schema (version 1) on disk::

    {
      "version": 1,
      "layout": {
        "views": [
          {"id": "nav", "label": "Nawigacja", "icon": "terminal", "hidden": false,
           "buttons": [Button, ...]}          # array order == render order
        ]
      }
    }

    Button = {id, kind, label, hint?, icon?, iconColor?, bgColor?, pinned?, hidden?, payload}
      iconColor / bgColor = optional hex (#rgb…#rrggbbaa) or var(--token) overrides
      kind=send-key      payload={key,code,keyCode,which, ctrlKey?,altKey?,shiftKey?,metaKey?}
      kind=send-raw      payload={data}   # ESCAPED text (\\n, \\xHH incl \\x00/\\x1b); decoded len capped
      kind=paste-text    payload={text, submit}
      kind=slash-command payload={command, submit}
      kind=special       payload={actionType in _ALLOWED_ACTION_TYPES}
      kind=modifier      payload={modifier in {ctrlKey,altKey,metaKey}}  # sticky modifier

ROLLBACK (opt-in ``terminal_shortcuts_enabled`` feature):
  1. flag off (default) → toolbar uses hard-coded defaults, editor hidden.
  2. drop the manager UI + the ``applyLayout`` branch → toolbar falls back to the
     static arrays.
  3. full removal → delete this module, the routes, the flag,
     ``static/orchestrator-shortcuts.jsx`` and the json file.

Migration: the old v1-feature sparse ``{"overrides": {...}}`` file (and anything
without ``version == 1`` of THIS schema) is discarded and reseeded — there is no
lossless mapping of hide/remap diffs onto add/remove/reorder/pin, the feature is
opt-in with a tiny base, and reseed reuses the seed path that must exist anyway.

Validation is strict and total: the frontend writes this file but a hand-edited
or hostile file must never crash the toolbar, smuggle an unknown button kind, or
exhaust memory. Unknown keys/kinds are dropped, strings length-capped, ints
range-clamped, the file size-gated before parsing, and ``send-raw`` payloads are
validated by DECODING (control bytes are allowed — that is the whole point — but
the decoded length is capped and malformed escapes drop the button).
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

SHORTCUTS_PATH = Path.home() / ".orchestrator" / "terminal_shortcuts.json"

_SCHEMA_VERSION = 1

# Defensive caps so a hand-edited / hostile file can't bloat memory, smuggle an
# unknown kind into a key event, or send half a tmux chord.
_MAX_FILE_BYTES = 128 * 1024  # gate BEFORE json.load
_MAX_VIEWS = 16
_MAX_BUTTONS_PER_VIEW = 40
_MAX_BUTTONS = 100  # global ceiling across all views
_MAX_ID_LEN = 64
_MAX_LABEL_LEN = 40
_MAX_HINT_LEN = 120
_MAX_ICON_LEN = 40
_MAX_KEY_LEN = 24
_MAX_CODE_LEN = 40
_MAX_RAW_LEN = 512  # decoded byte length of a send-raw payload
_MAX_PASTE_LEN = 2000
_MAX_COMMAND_LEN = 64
_KEYCODE_RANGE = (0, 255)

_KINDS: frozenset[str] = frozenset(
    {"send-key", "send-raw", "paste-text", "slash-command", "special", "modifier"}
)
# Closed whitelist for kind=special (D2's special actions + the live switcher).
_ALLOWED_ACTION_TYPES: frozenset[str] = frozenset(
    {"microphone", "upload", "clipboard-paste", "session-switcher"}
)
_ALLOWED_MODIFIERS: frozenset[str] = frozenset({"ctrlKey", "altKey", "metaKey"})
_MODIFIER_FIELDS: tuple[str, ...] = ("ctrlKey", "altKey", "shiftKey", "metaKey")
_COMMAND_RE = re.compile(r"^[A-Za-z0-9:_-]+$")
# Optional per-button color overrides (icon color + button background). Accept
# ONLY a hex literal (#rgb / #rgba / #rrggbb / #rrggbbaa) or a design-system
# `var(--token)` — both are inert as CSS `color`/`background` values. Anything
# else (e.g. `url(...)`, an expression, a stray `;`) is dropped so a hand-edited
# or hostile layout can't smuggle CSS into an inline style.
_MAX_COLOR_LEN = 32
_COLOR_RE = re.compile(r"^(#[0-9a-fA-F]{3,8}|var\(--[a-z0-9-]{1,40}\))$")

# Maps a single escape letter to its byte. \xHH is handled separately.
_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\x00", "\\": "\\"}


# ── canonical default layout (single source of truth) ────────────────
# Transcribed once from orchestrator-terminal-preview.jsx _NAV_KEYS /
# _SPECIAL_KEYS / _ACTION_KEYS / _SOFT_VIEWS. The cycler / Esc / Tab are
# structural (rendered by the toolbar, never in the layout). send-raw `data`
# is the ESCAPED text form (\\x00v == NUL + 'v'). Keep in sync with the
# frontend fallback arrays — guarded by a drift test.
def _sk(key: str, code: str, keycode: int, **mods: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {"key": key, "code": code, "keyCode": keycode, "which": keycode}
    payload.update({m: True for m, v in mods.items() if v})
    return payload


# Esc + Tab were structural anchors in v1; per user request they are now real,
# manageable PINNED buttons in the seed (Esc on every view, Tab on Akcje +
# Nawigacja — matching the old placement). Factories return fresh dicts so each
# view gets its own object. The view-cycler stays structural (it's the only way
# to switch views — never deletable).
def _esc() -> dict[str, Any]:
    return {"id": "esc", "kind": "send-key", "label": "", "hint": "Esc — zatrzymaj agenta",
            "icon": "square", "pinned": True, "payload": _sk("Escape", "Escape", 27)}


def _tab() -> dict[str, Any]:
    return {"id": "tab", "kind": "send-key", "label": "Tab", "hint": "Tab — autouzupełnianie",
            "pinned": True, "payload": _sk("Tab", "Tab", 9)}


DEFAULT_LAYOUT: dict[str, Any] = {
    "version": _SCHEMA_VERSION,
    "layout": {
        "views": [
            {
                "id": "actions", "label": "Akcje", "icon": "sparkle", "hidden": False,
                "buttons": [
                    _esc(), _tab(),
                    {"id": "upload", "kind": "special", "label": "", "hint": "Dodaj plik / obraz", "icon": "attach", "payload": {"actionType": "upload"}},
                    {"id": "voice", "kind": "special", "label": "", "hint": "Dyktuj (głos → tekst)", "icon": None, "payload": {"actionType": "microphone"}},
                    {"id": "clip", "kind": "special", "label": "", "hint": "Wklej ze schowka", "icon": "copy", "payload": {"actionType": "clipboard-paste"}},
                    {"id": "ultra", "kind": "paste-text", "label": "", "hint": "Wklej „ultrathink”", "icon": "bulb", "payload": {"text": "ultrathink ", "submit": False}},
                    {"id": "multi", "kind": "paste-text", "label": "", "hint": "Wklej „multi agents”", "icon": "bot", "payload": {"text": "multi agents ", "submit": False}},
                    {"id": "plan", "kind": "paste-text", "label": "", "hint": "Wklej „plan mode”", "icon": "list-checks", "payload": {"text": "plan mode ", "submit": False}},
                    {"id": "web", "kind": "paste-text", "label": "", "hint": "Wklej „web search”", "icon": "globe", "payload": {"text": "web search ", "submit": False}},
                    {"id": "ask", "kind": "paste-text", "label": "", "hint": "Wklej „ask user question”", "icon": "help", "payload": {"text": "ask user question ", "submit": False}},
                    {"id": "compact", "kind": "slash-command", "label": "", "hint": "Compact (/compact)", "icon": "archive", "payload": {"command": "compact", "submit": True}},
                ],
            },
            {
                "id": "nav", "label": "Nawigacja", "icon": "terminal", "hidden": False,
                "buttons": [
                    _esc(), _tab(),
                    {"id": "arrow-up", "kind": "send-key", "label": "↑", "hint": "W górę", "payload": _sk("ArrowUp", "ArrowUp", 38)},
                    {"id": "arrow-down", "kind": "send-key", "label": "↓", "hint": "W dół", "payload": _sk("ArrowDown", "ArrowDown", 40)},
                    {"id": "arrow-left", "kind": "send-key", "label": "←", "hint": "W lewo", "payload": _sk("ArrowLeft", "ArrowLeft", 37)},
                    {"id": "arrow-right", "kind": "send-key", "label": "→", "hint": "W prawo", "payload": _sk("ArrowRight", "ArrowRight", 39)},
                    {"id": "enter", "kind": "send-key", "label": "⏎", "hint": "Enter", "payload": _sk("Enter", "Enter", 13)},
                    {"id": "line-start", "kind": "send-key", "label": "⇤", "hint": "Początek linii (Ctrl+A)", "payload": _sk("a", "KeyA", 65, ctrlKey=True)},
                    {"id": "line-end", "kind": "send-key", "label": "⇥", "hint": "Koniec linii (Ctrl+E)", "payload": _sk("e", "KeyE", 69, ctrlKey=True)},
                    {"id": "tmux-split-v", "kind": "send-raw", "label": "V", "hint": "tmux: split pionowy (prefix → v)", "payload": {"data": "\\x00v"}},
                    {"id": "tmux-split-h", "kind": "send-raw", "label": "H", "hint": "tmux: split poziomy (prefix → h)", "payload": {"data": "\\x00h"}},
                    {"id": "tmux-kill", "kind": "send-raw", "label": "✕", "hint": "tmux: zamknij pane (prefix → x)", "payload": {"data": "\\x00x"}},
                    {"id": "tmux-zoom", "kind": "send-raw", "label": "Z", "hint": "tmux: zoom pane on/off (prefix → z)", "payload": {"data": "\\x00z"}},
                ],
            },
            {
                "id": "sessions", "label": "Sesje", "icon": "inbox", "hidden": False,
                "buttons": [
                    _esc(),
                    {"id": "session-switcher", "kind": "special", "label": "", "hint": "Przełącz sesję", "icon": "inbox", "payload": {"actionType": "session-switcher"}},
                ],
            },
            {
                "id": "special", "label": "Specjalne", "icon": "cmd", "hidden": False,
                "buttons": [
                    _esc(),
                    {"id": "shift-tab", "kind": "send-key", "label": "ST", "hint": "Shift+Tab", "payload": _sk("Tab", "Tab", 9, shiftKey=True)},
                    {"id": "ctrl-s", "kind": "send-key", "label": "^S", "hint": "Ctrl+S", "payload": _sk("s", "KeyS", 83, ctrlKey=True)},
                    {"id": "ctrl-b", "kind": "send-key", "label": "^B", "hint": "Ctrl+B (tmux prefix)", "payload": _sk("b", "KeyB", 66, ctrlKey=True)},
                    {"id": "ctrl-c", "kind": "send-key", "label": "^C", "hint": "Ctrl+C", "payload": _sk("c", "KeyC", 67, ctrlKey=True)},
                    {"id": "ctrl-space", "kind": "send-key", "label": "^␣", "hint": "Ctrl+Space", "payload": _sk(" ", "Space", 32, ctrlKey=True)},
                    {"id": "mod-ctrl", "kind": "modifier", "label": "Ctrl", "hint": "Ctrl — trzyma do następnego klawisza", "payload": {"modifier": "ctrlKey"}},
                    {"id": "mod-alt", "kind": "modifier", "label": "Alt", "hint": "Alt — trzyma do następnego klawisza", "payload": {"modifier": "altKey"}},
                    {"id": "mod-meta", "kind": "modifier", "label": "⌘", "hint": "Cmd / Meta — trzyma do następnego klawisza", "payload": {"modifier": "metaKey"}},
                    {"id": "ctrl-u", "kind": "send-key", "label": "Clr", "hint": "Wyczyść input (Ctrl+U)", "payload": _sk("u", "KeyU", 85, ctrlKey=True)},
                ],
            },
        ],
    },
}

_data: dict[str, Any] | None = None
_lock = asyncio.Lock()


def _warn(msg: str) -> None:
    print(f"[orchestrator_terminal_shortcuts] {msg}", file=sys.stderr)


def _ensure_dir() -> None:
    SHORTCUTS_PATH.parent.mkdir(parents=True, exist_ok=True)


def _seed() -> dict[str, Any]:
    """Fresh deep copy of the canonical default layout."""
    return copy.deepcopy(DEFAULT_LAYOUT)


def _load_from_disk() -> dict[str, Any]:
    if not SHORTCUTS_PATH.exists():
        return _seed()
    try:
        size = SHORTCUTS_PATH.stat().st_size
    except OSError as exc:
        _warn(f"cannot stat {SHORTCUTS_PATH.name}: {exc}; seeding defaults")
        return _seed()
    if size > _MAX_FILE_BYTES:
        _warn(f"{SHORTCUTS_PATH.name} is {size}B > {_MAX_FILE_BYTES}B cap; seeding defaults")
        return _seed()
    try:
        with SHORTCUTS_PATH.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _warn(f"corrupt or unreadable {SHORTCUTS_PATH.name}: {exc}; seeding defaults")
        return _seed()
    if not isinstance(payload, dict) or payload.get("version") != _SCHEMA_VERSION:
        # Missing/old version (incl. the legacy sparse {"overrides":...} shape):
        # discard + reseed. There is no lossless migration to the layout model.
        _warn(f"{SHORTCUTS_PATH.name} not schema v{_SCHEMA_VERSION}; discarding + seeding defaults")
        return _seed()
    # Re-sanitize on read too — the file may be hand-edited.
    return {"version": _SCHEMA_VERSION, "layout": _sanitize_layout(payload.get("layout"))}


def _ensure_loaded() -> dict[str, Any]:
    global _data
    if _data is None:
        _ensure_dir()
        _data = _load_from_disk()
    return _data


def _atomic_write(payload: dict[str, Any]) -> None:
    _ensure_dir()
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(SHORTCUTS_PATH.parent),
        prefix=".terminal_shortcuts.", suffix=".tmp", delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp_path, SHORTCUTS_PATH)
    except Exception:
        try:
            tmp.close()
        except Exception:
            pass
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def _cap_str(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _sanitize_color(value: Any) -> str | None:
    """A hex / `var(--token)` color, or None if absent or not a safe color."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v or len(v) > _MAX_COLOR_LEN or not _COLOR_RE.match(v):
        return None
    return v


def _decode_raw(data: str) -> str | None:
    """Decode the escaped send-raw text to its real bytes (for length validation
    + frontend parity). Allows control bytes deliberately. Returns None on a
    malformed escape so a typo drops the button instead of sending half a chord.
    """
    out: list[str] = []
    i = 0
    n = len(data)
    while i < n:
        ch = data[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            return None  # lone trailing backslash
        nxt = data[i + 1]
        if nxt == "x":
            hexpart = data[i + 2:i + 4]
            if len(hexpart) != 2 or not all(c in "0123456789abcdefABCDEF" for c in hexpart):
                return None  # malformed \xHH
            out.append(chr(int(hexpart, 16)))
            i += 4
            continue
        if nxt in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[nxt])
            i += 2
            continue
        return None  # unknown escape
    return "".join(out)


def _sanitize_key_descriptor(raw: Any) -> dict[str, Any] | None:
    """Coerce a send-key payload into a minimal, safe KeyboardEvent descriptor."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    key = _cap_str(raw.get("key"), _MAX_KEY_LEN)
    if key is not None:
        out["key"] = key
    code = _cap_str(raw.get("code"), _MAX_CODE_LEN)
    if code is not None:
        out["code"] = code
    lo, hi = _KEYCODE_RANGE
    for field in ("keyCode", "which"):
        value = raw.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if lo <= value <= hi:
            out[field] = value
    for field in _MODIFIER_FIELDS:
        if field in raw:
            out[field] = bool(raw[field])
    if not ({"key", "code", "keyCode"} & out.keys()):
        return None
    return out


def _sanitize_payload(kind: str, raw: Any) -> dict[str, Any] | None:
    """Validate a button payload by kind. None → drop the button."""
    if kind == "send-key":
        return _sanitize_key_descriptor(raw)
    if not isinstance(raw, dict):
        return None
    if kind == "send-raw":
        data = raw.get("data")
        if not isinstance(data, str) or not data:
            return None
        decoded = _decode_raw(data)
        if decoded is None or len(decoded) > _MAX_RAW_LEN:
            return None
        # Persist the escaped form (round-trips, human-editable); also cap the
        # escaped length so the on-disk string can't balloon (4x the decoded cap).
        return {"data": data[: _MAX_RAW_LEN * 4]}
    if kind == "paste-text":
        text = _cap_str(raw.get("text"), _MAX_PASTE_LEN)
        if text is None:
            return None
        return {"text": text, "submit": bool(raw.get("submit"))}
    if kind == "slash-command":
        command = raw.get("command")
        if not isinstance(command, str):
            return None
        command = command.lstrip("/")[:_MAX_COMMAND_LEN]
        if not command or not _COMMAND_RE.match(command):
            return None
        return {"command": command, "submit": bool(raw.get("submit"))}
    if kind == "special":
        action = raw.get("actionType")
        if action in _ALLOWED_ACTION_TYPES:
            return {"actionType": action}
        return None
    if kind == "modifier":
        mod = raw.get("modifier")
        if mod in _ALLOWED_MODIFIERS:
            return {"modifier": mod}
        return None
    return None


def _sanitize_button(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    button_id = _cap_str(raw.get("id"), _MAX_ID_LEN)
    if button_id is None:
        return None
    kind = raw.get("kind")
    if kind not in _KINDS:
        return None  # unknown kind is the main injection vector
    payload = _sanitize_payload(kind, raw.get("payload"))
    if payload is None:
        return None
    out: dict[str, Any] = {"id": button_id, "kind": kind, "payload": payload}
    out["label"] = (raw.get("label") or "")[:_MAX_LABEL_LEN] if isinstance(raw.get("label"), str) else ""
    hint = _cap_str(raw.get("hint"), _MAX_HINT_LEN)
    if hint is not None:
        out["hint"] = hint
    icon = _cap_str(raw.get("icon"), _MAX_ICON_LEN)
    if icon is not None:
        out["icon"] = icon
    icon_color = _sanitize_color(raw.get("iconColor"))
    if icon_color is not None:
        out["iconColor"] = icon_color
    bg_color = _sanitize_color(raw.get("bgColor"))
    if bg_color is not None:
        out["bgColor"] = bg_color
    if raw.get("pinned") is True:
        out["pinned"] = True
    if raw.get("hidden") is True:
        out["hidden"] = True
    return out


def _sanitize_view(raw: Any, counter: list[int], seen_ids: set[str]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    view_id = _cap_str(raw.get("id"), _MAX_ID_LEN)
    if view_id is None or view_id in seen_ids:
        return None
    seen_ids.add(view_id)
    out: dict[str, Any] = {"id": view_id}
    out["label"] = (raw.get("label") or "")[:_MAX_LABEL_LEN] if isinstance(raw.get("label"), str) else ""
    icon = _cap_str(raw.get("icon"), _MAX_ICON_LEN)
    out["icon"] = icon  # may be None
    out["hidden"] = raw.get("hidden") is True
    buttons: list[dict[str, Any]] = []
    seen_btn_ids: set[str] = set()
    raw_buttons = raw.get("buttons")
    if isinstance(raw_buttons, list):
        for raw_btn in raw_buttons:
            if len(buttons) >= _MAX_BUTTONS_PER_VIEW or counter[0] >= _MAX_BUTTONS:
                break
            btn = _sanitize_button(raw_btn)
            if btn is not None and btn["id"] not in seen_btn_ids:
                seen_btn_ids.add(btn["id"])
                buttons.append(btn)
                counter[0] += 1  # count only KEPT buttons against the global cap
    out["buttons"] = buttons
    return out


def _sanitize_layout(raw: Any) -> dict[str, Any]:
    """Total, immutable sanitizer → {'views': [...]}. Never mutates input."""
    if not isinstance(raw, dict):
        return _seed()["layout"]
    raw_views = raw.get("views")
    if not isinstance(raw_views, list):
        return _seed()["layout"]
    counter = [0]  # global button counter (mutable box for recursion)
    seen_ids: set[str] = set()
    views: list[dict[str, Any]] = []
    for raw_view in raw_views:
        if len(views) >= _MAX_VIEWS:
            break
        view = _sanitize_view(raw_view, counter, seen_ids)
        if view is not None:
            views.append(view)
    # Reseed if NO views survived, or if every surviving view is buttonless (a
    # near-blank toolbar is useless — only the structural cycler/Esc/Tab anchors
    # would render). Guards hand-edited / hostile files.
    if not views or not any(v["buttons"] for v in views):
        return _seed()["layout"]
    return {"views": views}


def get_layout() -> dict[str, Any]:
    """Return a deep copy of the full layout {version, layout:{views}} (seeded
    from defaults when no file exists)."""
    return copy.deepcopy(_ensure_loaded())


async def set_layout(payload: dict[str, Any]) -> dict[str, Any]:
    """Replace the layout with a sanitized ``payload['layout']`` (or a bare
    layout). Full replace; atomic write. Returns the persisted layout."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    raw_layout = payload.get("layout", payload)
    cleaned = {"version": _SCHEMA_VERSION, "layout": _sanitize_layout(raw_layout)}
    async with _lock:
        await asyncio.to_thread(_atomic_write, cleaned)
        globals()["_data"] = cleaned
    return copy.deepcopy(cleaned)


async def reset_layout() -> dict[str, Any]:
    """Reseed the canonical default layout (write + cache)."""
    fresh = _seed()
    async with _lock:
        await asyncio.to_thread(_atomic_write, fresh)
        globals()["_data"] = fresh
    return copy.deepcopy(fresh)
