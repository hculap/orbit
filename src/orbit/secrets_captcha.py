"""In-memory captcha for env/secrets destructive + reveal endpoints.

Friction gate, NOT auth. Goal is to defend against fat-finger destroy /
reveal — the network is already fronted by Tailscale. Kept dependency-free
and process-local; restart wipes pending captchas (the user just refreshes
the modal).

Public API:
    issue() -> {"token", "code", "expires_at"}
    verify(token, code) -> bool          # single-use, marks consumed on hit

Implementation notes:
- Code alphabet: ``A-Z0-9`` minus ``OIL01`` (``O``/``0`` and ``I``/``1`` are
  visually ambiguous; ``L`` removed for the same reason). 5 chars.
- Token: ``uuid.uuid4().hex``.
- Store: ``OrderedDict[token, _Entry]`` capped at 64 entries (LRU eviction
  on insert when full). 60 s TTL.
- ``secrets.compare_digest`` for the equality check.
"""
from __future__ import annotations

import secrets
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock

CODE_LEN = 5
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
TTL_SECONDS = 60.0
MAX_ENTRIES = 64


@dataclass
class _Entry:
    code: str
    expires_at: float
    consumed: bool


_store: "OrderedDict[str, _Entry]" = OrderedDict()
_lock = Lock()


def _now() -> float:
    return time.time()


def _gen_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


def _evict_locked() -> None:
    """Drop expired entries first; if still over cap, pop oldest."""
    now = _now()
    expired = [tok for tok, entry in _store.items() if entry.expires_at <= now]
    for tok in expired:
        _store.pop(tok, None)
    while len(_store) >= MAX_ENTRIES:
        _store.popitem(last=False)


def issue() -> dict:
    """Mint a fresh captcha. Caller must echo ``code`` back via UI input.

    Returns ``{"token", "code", "expires_at"}`` (epoch seconds, float).
    """
    token = uuid.uuid4().hex
    code = _gen_code()
    expires_at = _now() + TTL_SECONDS
    with _lock:
        _evict_locked()
        _store[token] = _Entry(code=code, expires_at=expires_at, consumed=False)
        _store.move_to_end(token)
    return {"token": token, "code": code, "expires_at": expires_at}


def verify(token: str, code: str) -> bool:
    """Single-use verification. True iff token exists, not expired, not
    consumed, and code matches. Marks consumed on the first matching call.

    Constant-time comparison via :func:`secrets.compare_digest`.
    """
    if not isinstance(token, str) or not isinstance(code, str):
        return False
    with _lock:
        entry = _store.get(token)
        if entry is None:
            return False
        if entry.expires_at <= _now():
            _store.pop(token, None)
            return False
        if entry.consumed:
            return False
        if not secrets.compare_digest(entry.code, code):
            return False
        entry.consumed = True
        return True


def reset_for_tests() -> None:
    """Wipe the store. Tests only."""
    with _lock:
        _store.clear()
