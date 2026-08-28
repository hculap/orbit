"""Peer-message helpers for the JSONL transcript reader.

Claude Code 2.x cross-session ("peer") messaging drops two NON-HUMAN shapes
into a session's transcript:

1. the RECEIVING session gets ``{"type":"user","origin":{"kind":"peer",…}}``
   whose ``message.content`` is a ``<cross-session-message>`` wrapper plus a
   paragraph of permission-laundering boilerplate;
2. the SENDING session gets the peer's REPLY as
   ``{"type":"attachment","attachment":{"type":"queued_command",
   "origin":{"kind":"peer",…}}}``.

Neither was typed by the human, so neither may drive ``msg_count`` (the
unread badge), the session preview or the auto-title — but both must stay
VISIBLE in the transcript, as the pinned ``role:"peer"`` row.

Split out of ``orchestrator_jsonl`` (which was over the repo's 800-line cap);
every name here is re-exported there, so ``orchestrator_jsonl`` remains the
single import site for callers.
"""
from __future__ import annotations

import re

# Claude Code wraps the sender's text in this tag before handing it to the
# model. ``origin.body`` carries the same text clean, so the regex is only the
# fallback for entries that lack it.
_CROSS_SESSION_RE = re.compile(
    r"<cross-session-message\b[^>]*>\s*(?P<body>.*?)\s*</cross-session-message>",
    re.DOTALL,
)
# ``origin.kind`` values that mark a ``type:"user"`` entry as NOT typed by the
# human: a message from another Claude session, or a background-task
# completion notice. Both are inbound machine input — they must not drive
# msg_count (the unread badge), the session preview or the auto-title.
#
# This frozenset is the WHOLE contract. Notably ``promptSource == "system"`` is
# NOT a marker: claude stamps it on plenty of genuine prompts (cron follow-ups,
# daily kickoffs, ``origin.kind == "auto-continuation"`` goal-setting), and
# treating those as synthetic blanks the previews and starves the auto-title of
# any input in a loop-driven session.
_SYNTHETIC_ORIGIN_KINDS = frozenset({"peer", "task-notification"})


def _origin_kind(evt: object) -> str | None:
    """``evt["origin"]["kind"]`` when both levels are well-formed, else None.

    Every level is untrusted: the key may be missing, ``None``, a string, or a
    list depending on the claude-cli version that wrote the line.
    """
    if not isinstance(evt, dict):
        return None
    origin = evt.get("origin")
    if not isinstance(origin, dict):
        return None
    kind = origin.get("kind")
    return kind if isinstance(kind, str) and kind else None


def _is_synthetic_user(evt: object) -> bool:
    """True for a ``user`` entry that the human did NOT type.

    Keys on ``origin.kind`` alone (see ``_SYNTHETIC_ORIGIN_KINDS``): a peer
    message or a task notification. Never raises.
    """
    return _origin_kind(evt) in _SYNTHETIC_ORIGIN_KINDS


def _peer_meta(origin: object) -> dict | None:
    """Normalise a peer ``origin`` block into the pinned ``peer`` payload.

    Returns None when ``origin`` is not a peer origin — the caller uses that
    as the "this entry is not a peer message" test. Missing/ill-typed fields
    degrade to None rather than propagating junk to the frontend.
    """
    if not isinstance(origin, dict) or origin.get("kind") != "peer":
        return None

    def _str(key: str) -> str | None:
        value = origin.get(key)
        return value if isinstance(value, str) and value else None

    pid = origin.get("verifiedPeerPid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        pid = None
    return {"from": _str("from"), "from_name": _str("name"),
            "from_pid": pid, "msg_id": _str("msg_id")}


def _peer_text(origin: object, raw: object) -> str:
    """Clean body of a peer message.

    Preference order: ``origin.body`` (already stripped of the wrapper and of
    the permission-laundering boilerplate claude appends) → the inner text of
    the ``<cross-session-message>`` wrapper → the raw prompt/content verbatim.
    """
    if isinstance(origin, dict):
        body = origin.get("body")
        if isinstance(body, str) and body.strip():
            return body.strip()
    if not isinstance(raw, str) or not raw.strip():
        return ""
    match = _CROSS_SESSION_RE.search(raw)
    if match:
        return match.group("body").strip()
    return raw.strip()


def _peer_message(peer: dict, text: str, ts: float, turn_idx: int) -> dict:
    """The pinned ``role:"peer"`` transcript row (empty body ⇒ no blocks)."""
    blocks = [{"kind": "text", "text": text}] if text else []
    return {"role": "peer", "ts": ts, "turn_idx": turn_idx,
            "peer": peer, "blocks": blocks}


def _peer_attachment_origin(evt: dict) -> dict | None:
    """The peer ``origin`` of a ``type:"attachment"`` entry, or None.

    A peer's REPLY lands back at the SENDING session as an attachment of type
    ``queued_command`` (not as a ``user`` entry). Every other attachment type
    (``ultrathink_effort``, ``deferred_tools_delta``, …) — and a
    ``queued_command`` the human typed while the agent was busy — stays
    dropped, so this is deliberately narrow.
    """
    attachment = evt.get("attachment")
    if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
        return None
    origin = attachment.get("origin")
    return origin if isinstance(origin, dict) and origin.get("kind") == "peer" else None
