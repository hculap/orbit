"""Server-initiated device geolocation — the round-trip core (FastAPI-free).

An agent running on this box cannot see where its human is standing; the
BROWSER can (``navigator.geolocation``). This module is the middle of that
round trip: the agent creates a request, the browser answers it, and the
agent's blocking ``/wait`` poll resolves. The transport is the EXISTING
per-session :class:`~.orchestrator_events.SessionEventHub` (the same channel
artifact toasts ride), so there is no second SSE bus, no lifespan wiring and no
restart needed to toggle the feature.

Why the shape is what it is — each rule closes a concrete failure an
unattended agent would otherwise hit:

* **Register in ``_PENDING`` BEFORE publishing.** A phone that answers in
  40 ms would 404 against a request the server has not stored yet. The publish
  callback is therefore invoked from inside :func:`create_request`, after the
  dict insert, never by the caller.
* **First ``fixed`` wins, compare-and-set with no ``await`` between the check
  and the set.** Single-threaded asyncio makes that atomic; two devices
  answering in the same tick cannot both store a fix.
* **A non-``fixed`` answer does NOT resolve while another ANSWERER is still
  outstanding.** A laptop with no GPS denying must not consume the phone's
  chance — the request only resolves once every target has answered AND at
  least :attr:`GeoRequest.expected_n` answers have arrived (see
  :data:`_AGGREGATE_PRECEDENCE` for how a mixed set collapses to one status).
  Counting TARGETS is not enough: one session can have two tabs open, and they
  share a session id, so the count comes from live EventSources instead. A
  voice that never arrives (a subscriber that has gone away) costs
  :data:`GEO_QUORUM_GRACE_S`, not the whole TTL, and the ask then resolves as
  whatever WAS heard (:func:`_expiry_result`) rather than as ``expired``.
* **The cache is stamped with the MEASUREMENT time, not the receipt time.**
  ``getCurrentPosition`` may answer from the platform's own cache (up to the
  agent's ``max_age_s``), so a position can be minutes old the instant it
  arrives; stamping receipt would re-serve it as fresh for another 15 minutes.
* **Serving the cache is accuracy-aware.** ``coarse`` is a privacy control, so
  a stored ``fine`` fix is re-rounded before it is served, and a stored
  ``coarse`` fix can never answer a ``fine`` ask (that would hand ~1 km of
  error to a caller expecting metres).
* **Coords are REBUILT from a fixed key allowlist** (see
  :mod:`orchestrator_geo_coords`), never merged from the browser payload: the
  answer route is un-authenticated, so the body is fully untrusted.
* **The timeout ladder must not drift:** ``getCurrentPosition`` 20 s < the
  ``/wait`` poll window 45 s (clamped 60) < the request TTL 180 s == the CLI's
  overall budget. The poll window elapsing is NOT terminal (``ok:true``,
  ``status:"timeout"``); the request stays live and the CLI re-polls the same
  id. A terminal browser-side timeout is ``ok:false`` — that is the ONE
  discriminator between the two same-named outcomes.
* **No background task.** Expiry is swept opportunistically from
  :func:`await_fix`'s ``finally`` and from :func:`create_request`, because the
  test suite never enters the app lifespan.

:data:`STATUSES` (re-exported from :mod:`orchestrator_geo_statuses`) is the
SINGLE source of truth for the agent-facing contract — every status carries
``reason`` / ``retryable`` / a Polish ``message`` / a Polish ``hint``.
``retryable: False`` means "do not call geo again this turn"; that one field is
what stops a model blind-retrying a permission denial. The frontend mirrors the
table in ``static/geo-consent.js`` and a drift test pins the two halves
together.

PRIVACY: coordinates live in memory ONLY — ``_last_fix`` is a module global,
never written to disk, and nothing here logs a latitude or a longitude. Log
lines carry the request id, the session id and the status, nothing else. (The
fix does still land in the session transcript when the agent quotes it — that
is the agent's doing, not this module's.)
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

# Re-exported so ``orchestrator_geo`` stays the ONE import site for the whole
# contract — the untrusted-payload scrubbing and the WORDS just live in their
# own files (this module was over the 800-line ceiling with them inline).
from .orchestrator_geo_coords import (  # noqa: F401
    ACCURACIES,
    COORD_KEYS,
    REASON_MAX_CHARS,
    clamp_int,
    degrade_fix,
    normalize_accuracy,
    now_ms,
    rebuild_coords,
    sanitize_reason,
)
from .orchestrator_geo_statuses import (  # noqa: F401
    ANSWER_STATUSES,
    ERROR_CODE_STATUS,
    STATUSES,
    describe,
    failure,
    retryable,
)

_logger = logging.getLogger(__name__)

# ── the timeout ladder (see the module docstring — these must not drift) ──
GEO_REQUEST_TTL_S: float = 180.0        # how long a pending ask survives
GEO_WAIT_TIMEOUT_DEFAULT_S: float = 45.0
GEO_WAIT_TIMEOUT_MAX_S: float = 60.0
GEO_TTL_MIN_S: float = 10.0
GEO_TTL_MAX_S: float = 600.0

# A geo storm must not starve the MCP turn-driving waiters — this is a
# DELIBERATELY separate counter from ``orchestrator_wait.WAIT_MAX_CONCURRENT``.
GEO_WAIT_MAX_CONCURRENT: int = 8
# One human, a handful of tabs: 16 outstanding asks is already pathological.
# Counts ACTIVE asks only — a resolved one no longer blocks a new prompt.
GEO_MAX_PENDING: int = 16
# A resolved request is KEPT this long so a ``/wait`` issued after the answer
# already landed still finds its result. Dropping the entry on resolve turns
# the (very common) answer-before-wait race into a bogus "unknown request".
GEO_RESULT_GRACE_S: float = 120.0
# Absolute ceiling on the map, active + retained, so a pathological client
# can't grow it without bound between sweeps.
GEO_MAX_RETAINED: int = 64
# Anti-nag: two agents asking back-to-back must not double-prompt the phone.
GEO_MIN_PROMPT_INTERVAL_S: float = 5.0
# Fan-out ceiling when the asking session has no browser of its own attached.
GEO_FANOUT_MAX: int = 8
# Ceiling on how many answers a refusal waits for (see _expected_answerers) —
# a stale subscriber count must not park every denial until the TTL.
GEO_EXPECTED_MAX: int = 16
# …and when every target HAS answered but a counted voice never shows up (a
# subscriber that has since gone away), this is how much longer the ask waits:
# just past the browser's own 20 s getCurrentPosition ceiling, after which a tab
# that was going to answer already has.
GEO_QUORUM_GRACE_S: float = 25.0
# A fix measured within this many seconds is reported as ``fresh`` — the
# browser may legitimately answer from its own cache, so "just arrived" and
# "just measured" are different things.
GEO_FRESH_WINDOW_S: float = 2.0
# How long a fix stays reusable in memory (a follow-up "a najbliższa apteka?"
# must not re-prompt). Cleared by ``forget()`` / the Settings button.
GEO_CACHE_TTL_S: float = 900.0
GEO_MAX_AGE_DEFAULT_S: int = 300

# ``secrets.token_urlsafe(16)`` → 22 chars of [A-Za-z0-9_-]; the range is wide
# so a future width change doesn't need a coordinated frontend edit.
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

# SSE event names published on the per-session hub.
EVENT_REQUEST = "geo_request"
EVENT_CANCELLED = "geo_cancelled"
CANCEL_REASONS: tuple[str, ...] = ("answered_elsewhere", "expired", "aborted")

# Wire spellings the browser finds natural, folded onto their dedicated
# :data:`STATUSES` row (see ``orchestrator_geo_statuses``).
_REASON_ALIASES: dict[tuple[str, str], str] = {
    ("denied", "device_opt_out"): "device_opt_out",
    ("unavailable", "no_api"): "no_api",
}

# How a MIXED set of non-fixed answers collapses to one status once every
# target has answered. First match wins, so "all were insecure_context" is the
# only way ``insecure_context`` surfaces — exactly the rule the plan pins.
_AGGREGATE_PRECEDENCE: tuple[str, ...] = (
    "denied", "device_opt_out", "dismissed", "timeout",
    "unavailable", "no_api", "insecure_context",
)


class GeoSaturated(Exception):
    """Too many pending requests (:data:`GEO_MAX_PENDING`) — route → 429."""


class GeoWaitSaturated(Exception):
    """Too many concurrent ``/wait`` calls (:data:`GEO_WAIT_MAX_CONCURRENT`)."""


class GeoNotFound(Exception):
    """Unknown / already-resolved request id — route → 404."""


@dataclass
class GeoRequest:
    """One outstanding ask. Lives in :data:`_PENDING` until answered or swept."""

    request_id: str
    session_id: str
    targets: tuple[str, ...]
    reason: str
    accuracy: str
    max_age_s: int
    ttl_s: float
    created_at: float          # monotonic
    expires_at: float          # monotonic
    expires_at_ms: int         # wall clock, for the browser
    agent: dict[str, str]
    # How many answers a REFUSAL needs before it is final — one per live
    # EventSource, not one per target (two tabs share one session id).
    expected_n: int = 1
    event: asyncio.Event = field(default_factory=asyncio.Event)
    # target session id → every status that session's tabs answered with, in
    # arrival order. A list, not one slot: a second tab must not overwrite (and
    # thereby erase) the first tab's voice.
    answers: dict[str, list[str]] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    resolved_at: float | None = None   # monotonic; drives the retention grace


# Active AND recently-resolved requests (see :data:`GEO_RESULT_GRACE_S`).
_PENDING: dict[str, GeoRequest] = {}
# The single, process-global, in-memory last fix. NEVER persisted.
_last_fix: dict[str, Any] | None = None
_last_prompt_at: float = 0.0
_wait_inflight: int = 0


# ── the in-memory fix cache ───────────────────────────────────────


def store_fix(fix: dict[str, Any], *, now: float | None = None) -> None:
    """Remember the newest fix in memory (process-global, never on disk)."""
    global _last_fix
    _last_fix = {"fix": dict(fix), "at": now if now is not None else time.monotonic()}


def last_fix(max_age_s: float | None = None, *, now: float | None = None) -> dict[str, Any] | None:
    """The cached fix + its age, or ``None`` when absent/stale.

    ``max_age_s=0`` always misses (that is how ``--max-age 0`` forces a fresh
    prompt); ``None`` falls back to :data:`GEO_CACHE_TTL_S`.
    """
    if _last_fix is None:
        return None
    limit = GEO_CACHE_TTL_S if max_age_s is None else float(max_age_s)
    if limit <= 0:
        return None
    age = (now if now is not None else time.monotonic()) - float(_last_fix["at"])
    if age < 0 or age > min(limit, GEO_CACHE_TTL_S):
        return None
    return {"fix": dict(_last_fix["fix"]), "age_s": round(age, 1)}


def cached_payload(
    accuracy: str | None = None,
    max_age_s: float | None = None,
    *,
    now: float | None = None,
) -> dict[str, Any] | None:
    """The cache shaped as a terminal ``cached`` answer, or ``None`` on a miss.

    The ONE place a stored fix becomes an answer, so the accuracy rule cannot be
    forgotten at a second call site: :func:`degrade_fix` re-rounds a ``fine``
    entry for a ``coarse`` ask and refuses to serve a ``coarse`` entry to a
    ``fine`` one (which reads as a miss — the caller prompts for a fresh fix).
    ``accuracy=None`` asks for no precision in particular and so can be refused
    none: the entry is served exactly as stored (the read-only ``geo last``).
    """
    cached = last_fix(max_age_s, now=now)
    if cached is None:
        return None
    fix = dict(cached["fix"]) if accuracy is None else degrade_fix(cached["fix"], accuracy)
    if fix is None:
        return None
    return {
        "ok": True, "status": "cached", "terminal": True,
        "source": "cache", "fresh": False,
        "fix": fix, "age_s": cached["age_s"],
        **describe("cached"),
    }


def forget() -> dict[str, Any]:
    """Drop the cached fix. Idempotent; the only "delete my data" lever."""
    global _last_fix
    had = _last_fix is not None
    _last_fix = None
    return {"ok": True, "forgotten": had}


# ── pending-request lifecycle ─────────────────────────────────────


def resolve_targets(hub: Any, session_id: str) -> tuple[str, ...]:
    """Which sessions' browsers get the ask.

    The asking session's own tab wins outright (that is the tab the user is
    staring at). With nothing attached there we fan out to every session that
    DOES have a live EventSource, capped at :data:`GEO_FANOUT_MAX` — the user
    may well be looking at a different agent. Empty tuple ⇒ nobody is home and
    the caller must fast-fail instead of blocking for the whole TTL.
    """
    try:
        if int(hub.subscriber_count(session_id)) > 0:
            return (session_id,)
        live = sorted(str(sid) for sid in hub.live_session_ids() if sid)
    except Exception as exc:  # noqa: BLE001 — a broken hub is "no browser"
        _logger.warning("geo: hub target lookup failed: %s", exc)
        return ()
    return tuple(live[:GEO_FANOUT_MAX])


def active_requests(*, now: float | None = None) -> list[GeoRequest]:
    """Requests still awaiting an answer (resolved-but-retained excluded)."""
    moment = now if now is not None else time.monotonic()
    return [r for r in _PENDING.values() if r.result is None and moment < r.expires_at]


def _sweep(*, hub: Any = None, now: float | None = None) -> int:
    """Expire overdue asks, then drop results past their retention. Returns
    how many asks were EXPIRED (not how many entries were dropped).

    Opportunistic (called from ``create_request`` and ``await_fix``'s
    ``finally``) because the suite never enters the app lifespan, so a
    background sweeper task would simply never run there.
    """
    moment = now if now is not None else time.monotonic()
    expired = [r for r in list(_PENDING.values())
               if r.result is None and moment >= r.expires_at]
    for req in expired:
        _resolve(req, _expiry_result(req),
                 hub=hub, cancel_reason="expired", now=moment)
    for req in list(_PENDING.values()):
        if req.resolved_at is not None and moment - req.resolved_at > GEO_RESULT_GRACE_S:
            _PENDING.pop(req.request_id, None)
    # Hard ceiling — evict the oldest RETAINED results first; an active ask is
    # never dropped out from under a blocked waiter.
    while len(_PENDING) > GEO_MAX_RETAINED:
        retained = [r for r in _PENDING.values() if r.resolved_at is not None]
        if not retained:
            break
        _PENDING.pop(min(retained, key=lambda r: r.resolved_at or 0.0).request_id, None)
    return len(expired)


def _resolve(
    req: GeoRequest,
    result: dict[str, Any],
    *,
    hub: Any = None,
    cancel_reason: str | None = None,
    skip_target: str | None = None,
    now: float | None = None,
) -> None:
    """Terminally resolve ``req``: store the result, wake waiters, retain.

    Deliberately synchronous and ``await``-free so the caller's compare-and-set
    cannot be interleaved. The entry is KEPT (for :data:`GEO_RESULT_GRACE_S`)
    rather than dropped, so a ``/wait`` issued after the answer already landed
    still finds the result instead of a bogus 404. ``cancel_reason`` publishes
    ``geo_cancelled`` to the targets that never answered, so a stale modal
    disappears from their screen.
    """
    if req.result is not None:
        return
    req.result = result
    req.resolved_at = now if now is not None else time.monotonic()
    req.event.set()
    if hub is not None and cancel_reason in CANCEL_REASONS:
        payload = {"request_id": req.request_id, "reason": cancel_reason}
        for target in req.targets:
            if target == skip_target:
                continue
            try:
                hub.publish(target, EVENT_CANCELLED, payload)
            except Exception as exc:  # noqa: BLE001 — one dead queue ≠ failure
                _logger.warning("geo: cancel publish failed (rid=%s): %s",
                                req.request_id, exc)


def create_request(
    *,
    session_id: str,
    hub: Any,
    reason: Any = "",
    accuracy: Any = "fine",
    max_age_s: Any = GEO_MAX_AGE_DEFAULT_S,
    ttl_s: Any = GEO_REQUEST_TTL_S,
    agent: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Ask the user's browser for a fix. Registers FIRST, then publishes.

    Gate order (each one cheaper than the next): cache hit → delivery targets →
    pending cap → anti-nag. Returns a ``cached`` / ``no_browser`` /
    ``rate_limited`` / ``pending`` payload; raises :class:`GeoSaturated` at the
    cap so the route can answer 429.
    """
    global _last_prompt_at
    moment = now if now is not None else time.monotonic()
    _sweep(hub=hub, now=moment)

    acc = normalize_accuracy(accuracy)
    age_limit = clamp_int(max_age_s, GEO_MAX_AGE_DEFAULT_S, 0, int(GEO_CACHE_TTL_S))
    served = cached_payload(acc, age_limit, now=moment)
    if served is not None:
        return served

    targets = resolve_targets(hub, session_id)
    if not targets:
        return failure("no_browser", session_id=session_id)

    if len(active_requests(now=moment)) >= GEO_MAX_PENDING:
        raise GeoSaturated()

    since_prompt = moment - _last_prompt_at
    if since_prompt < GEO_MIN_PROMPT_INTERVAL_S:
        return failure(
            "rate_limited",
            retry_after_s=round(GEO_MIN_PROMPT_INTERVAL_S - since_prompt, 2),
            terminal=False,
        )

    ttl = float(clamp_int(ttl_s, int(GEO_REQUEST_TTL_S),
                        int(GEO_TTL_MIN_S), int(GEO_TTL_MAX_S)))
    req = GeoRequest(
        request_id=secrets.token_urlsafe(16),
        session_id=session_id,
        targets=targets,
        reason=sanitize_reason(reason),
        accuracy=acc,
        max_age_s=age_limit,
        ttl_s=ttl,
        created_at=moment,
        expires_at=moment + ttl,
        expires_at_ms=now_ms() + int(ttl * 1000),
        agent=_clean_agent(agent),
        expected_n=_expected_answerers(hub, targets),
    )
    # ORDERING INVARIANT: store, THEN publish. A device answering inside the
    # publish call must find the request already registered.
    _PENDING[req.request_id] = req
    _last_prompt_at = moment
    _publish_request(req, hub)
    _logger.info("geo: request %s from session %s → %d target(s)",
                 req.request_id, session_id, len(targets))
    return {
        "ok": True, "status": "pending", "terminal": False,
        "request_id": req.request_id,
        "expires_at_ms": req.expires_at_ms,
        "targets_n": len(targets),
        **describe("pending"),
    }


def _expected_answerers(hub: Any, targets: tuple[str, ...]) -> int:
    """How many devices must answer before a REFUSAL is final.

    ``targets`` is keyed on the SESSION, but a session can have two tabs open —
    a desktop with no GPS and the phone in your hand — and counting targets
    would let the desktop's ``unavailable`` resolve the ask and throw the
    phone's fix away. Count live EventSources instead, floored at one per target
    (a hub that can't say is one voice) and capped so a pathological subscriber
    count can't make every refusal wait out the whole TTL.
    """
    total = 0
    for target in targets:
        try:
            total += max(1, int(hub.subscriber_count(target)))
        except Exception as exc:  # noqa: BLE001 — a broken hub is one voice
            _logger.warning("geo: subscriber count failed: %s", exc)
            total += 1
    return max(len(targets), min(total, GEO_EXPECTED_MAX))


def _clean_agent(agent: dict[str, Any] | None) -> dict[str, str]:
    """Keep exactly ``lib_id`` + ``title``, as short trusted-ish strings."""
    src = agent if isinstance(agent, dict) else {}
    out: dict[str, str] = {}
    for key in ("lib_id", "title"):
        value = src.get(key)
        out[key] = sanitize_reason(value)[:120] if isinstance(value, str) else ""
    return out


def _publish_request(req: GeoRequest, hub: Any) -> None:
    payload = {
        "request_id": req.request_id,
        "reason": req.reason,
        "accuracy": req.accuracy,
        "max_age_s": req.max_age_s,
        "ttl_s": req.ttl_s,
        "expires_at_ms": req.expires_at_ms,
        "agent": dict(req.agent),
    }
    for target in req.targets:
        try:
            hub.publish(target, EVENT_REQUEST, payload)
        except Exception as exc:  # noqa: BLE001 — one dead queue ≠ failure
            _logger.warning("geo: publish failed (rid=%s): %s", req.request_id, exc)


def pending_for(session_id: str, *, now: float | None = None) -> list[dict[str, Any]]:
    """Unexpired asks addressed to ``session_id`` — the reconnect guarantee.

    A manually-constructed ``EventSource`` sends no ``Last-Event-ID``, so a
    two-second network blip silently drops the ``geo_request`` frame. The
    frontend polls this on ``onopen`` and on ``visibilitychange``.

    An ask this session has ALREADY answered is still listed while it is
    unresolved: a session can have two tabs, and the second one — the one whose
    push was dropped — is exactly who this poll exists for. The tab that already
    answered filters the repeat out client-side (its ``handledRef``).
    """
    moment = now if now is not None else time.monotonic()
    out: list[dict[str, Any]] = []
    for req in list(_PENDING.values()):
        if session_id not in req.targets or moment >= req.expires_at:
            continue
        if req.result is not None:
            continue
        out.append({
            "request_id": req.request_id,
            "reason": req.reason,
            "accuracy": req.accuracy,
            "max_age_s": req.max_age_s,
            "expires_at_ms": req.expires_at_ms,
            "agent": dict(req.agent),
        })
    return out


def _normalize_answer(status: Any, reason: Any) -> str:
    """Wire ``(status, reason)`` → one canonical :data:`STATUSES` key."""
    if not isinstance(status, str) or status not in ANSWER_STATUSES:
        raise ValueError(
            "status must be one of: " + ", ".join(sorted(ANSWER_STATUSES))
        )
    if isinstance(reason, str):
        return _REASON_ALIASES.get((status, reason), status)
    return status


def _aggregate(answers: Iterable[str]) -> str:
    seen = set(answers)
    for status in _AGGREGATE_PRECEDENCE:
        if status in seen:
            return status
    return "unavailable"


def _expiry_result(req: GeoRequest) -> dict[str, Any]:
    """What a request that ran out of time resolves to.

    Answers we DID hear beat a bare "nobody reacted": if the laptop denied and
    the phone we were still waiting for never came back, the honest answer is
    ``denied``, not ``expired``. Only a request nobody answered at all is
    ``expired``.
    """
    heard = list(itertools.chain.from_iterable(req.answers.values()))
    return failure(_aggregate(heard) if heard else "expired",
                   request_id=req.request_id)


def submit_answer(
    request_id: str,
    payload: dict[str, Any],
    *,
    hub: Any = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Browser → server: one device's answer to one ask.

    First ``fixed`` wins (the check and the store sit in one synchronous block,
    so two devices in the same tick cannot both win). A NON-``fixed`` answer
    only resolves the request once every target has answered AND
    :attr:`GeoRequest.expected_n` answers have arrived — a laptop denying must
    not consume the phone's chance, not even when both are looking at the SAME
    session (two tabs, one session id, two voices).

    Returns ``{"ok":true,"accepted":bool,"status":…}``; ``accepted:false`` means
    "your answer arrived after somebody else's" — a LOSING device is told so
    politely, only a genuinely unknown id raises :class:`GeoNotFound` (→404).
    :class:`ValueError` (→400) covers every malformed field.
    """
    if not REQUEST_ID_RE.fullmatch(request_id or ""):
        raise ValueError("invalid request_id")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    req = _PENDING.get(request_id)
    if req is None:
        raise GeoNotFound(request_id)
    if req.result is not None:
        return {"ok": True, "accepted": False, "status": req.result.get("status", "")}

    status = _normalize_answer(payload.get("status"), payload.get("reason"))
    moment = now if now is not None else time.monotonic()
    if moment >= req.expires_at:
        _resolve(req, _expiry_result(req), hub=hub, cancel_reason="expired")
        return {"ok": True, "accepted": False,
                "status": (req.result or {}).get("status", "expired")}

    # Which target is speaking? An un-authenticated route can claim any session
    # id, so an unrecognised one is folded onto the first target rather than
    # trusted — it can never invent an extra outstanding voice.
    claimed = payload.get("session_id")
    target = claimed if isinstance(claimed, str) and claimed in req.targets else req.targets[0]

    if status != "fixed":
        req.answers.setdefault(target, []).append(status)
        heard = sum(len(voices) for voices in req.answers.values())
        outstanding = set(req.targets) - set(req.answers)
        if outstanding or heard < req.expected_n:
            if not outstanding:
                # Every target has spoken and we are still a voice short: the
                # subscriber we counted at create time has probably gone away.
                # Bring the deadline forward instead of sitting out the full
                # TTL — and :func:`_expiry_result` will report what we DID hear.
                req.expires_at = min(req.expires_at, moment + GEO_QUORUM_GRACE_S)
            return {"ok": True, "accepted": True, "status": "pending"}
        resolved = _aggregate(itertools.chain.from_iterable(req.answers.values()))
        _resolve(req, failure(resolved, request_id=request_id), hub=hub)
        _logger.info("geo: request %s resolved %s", request_id, resolved)
        return {"ok": True, "accepted": True, "status": resolved}

    fix = rebuild_coords(payload.get("coords"), req.accuracy)
    # The browser's own timestamp, but only when it is PLAUSIBLE: a device with
    # a wrong clock (or a hostile caller) must not be able to backdate a fresh
    # fix by a decade, or post-date it so the cache never looks stale.
    server_ms = now_ms()
    claimed_ms = clamp_int(payload.get("ts_ms"), server_ms, 0, 2 ** 53 - 1)
    fix["ts_ms"] = claimed_ms if abs(claimed_ms - server_ms) <= 86_400_000 else server_ms
    # ``getCurrentPosition`` may answer from the PLATFORM's cache (its
    # ``maximumAge`` mirrors the agent's --max-age), so a reading can already be
    # minutes old on arrival. Age it from when it was MEASURED, or a 5-minute-old
    # position would be re-served as "1 s ago" for the next 15 minutes.
    measured_ago = max(0.0, (server_ms - fix["ts_ms"]) / 1000.0)
    # ── compare-and-set: no await, no I/O between the check and the store ──
    if req.result is not None:
        return {"ok": True, "accepted": False, "status": "answered_elsewhere"}
    store_fix(fix, now=moment - measured_ago)
    req.answers.setdefault(target, []).append("fixed")
    _resolve(
        req,
        {
            "ok": True, "status": "fixed", "terminal": True,
            "source": "browser-gps", "fresh": measured_ago <= GEO_FRESH_WINDOW_S,
            "age_s": round(measured_ago, 1),
            "fix": dict(fix),
            **describe("fixed"),
        },
        hub=hub,
        cancel_reason="answered_elsewhere",
        skip_target=target,
    )
    _logger.info("geo: request %s resolved fixed (precision=%s)",
                 request_id, req.accuracy)
    return {"ok": True, "accepted": True, "status": "fixed"}


def cancel(request_id: str, *, hub: Any = None) -> dict[str, Any]:
    """Agent → server: withdraw an ask (SIGINT / overall timeout). Idempotent."""
    if not REQUEST_ID_RE.fullmatch(request_id or ""):
        raise ValueError("invalid request_id")
    req = _PENDING.get(request_id)
    if req is None:
        return {"ok": True, "cancelled": False, "status": "not_found"}
    if req.result is not None:
        # Already answered — nothing to withdraw, and the CLI's SIGINT path
        # must not overwrite a fix the user already granted.
        return {"ok": True, "cancelled": False,
                "status": req.result.get("status", "")}
    _resolve(req, failure("aborted", request_id=request_id),
             hub=hub, cancel_reason="aborted")
    return {"ok": True, "cancelled": True, "status": "aborted"}


# ── the blocking half ─────────────────────────────────────────────


async def await_fix(
    request_id: str,
    timeout_s: float = GEO_WAIT_TIMEOUT_DEFAULT_S,
    *,
    hub: Any = None,
) -> dict[str, Any]:
    """Block until the ask resolves, the poll window elapses, or the TTL does.

    Three outcomes, told apart by ``terminal``: the stored terminal payload;
    a NON-terminal ``timeout`` (poll window gone, request still live — re-poll
    the same id, the agent never sees this); or a terminal ``expired``.

    ONE monotonic deadline covers the whole call, and ``req.result`` is checked
    BEFORE awaiting so an answer that landed between create and wait is kept.
    """
    req = _PENDING.get(request_id)
    if req is None:
        return failure("not_found", request_id=request_id)
    if req.result is not None:
        return dict(req.result)

    window = max(0.001, min(float(timeout_s), GEO_WAIT_TIMEOUT_MAX_S))
    deadline = time.monotonic() + window
    try:
        while True:
            remaining = min(deadline, req.expires_at) - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(req.event.wait(), remaining)
            except asyncio.TimeoutError:
                break
            if req.result is not None:
                return dict(req.result)
        if time.monotonic() >= req.expires_at:
            _resolve(req, _expiry_result(req), hub=hub, cancel_reason="expired")
            return dict(req.result or _expiry_result(req))
        return {
            "ok": True, "status": "timeout", "terminal": False,
            "request_id": request_id,
            "expires_at_ms": req.expires_at_ms,
            "message": "Okno odpytywania minęło — prośba nadal czeka.",
            "hint": "Odpytaj /wait ponownie tym samym request_id.",
            "retryable": True,
            "reason": "poll_window_elapsed",
        }
    finally:
        _sweep(hub=hub)


async def await_fix_guarded(
    request_id: str,
    timeout_s: float = GEO_WAIT_TIMEOUT_DEFAULT_S,
    *,
    hub: Any = None,
) -> dict[str, Any]:
    """:func:`await_fix` behind a process-wide cap.

    Single-threaded asyncio means the check + increment cannot race, so no lock
    is needed. The counter is deliberately SEPARATE from
    ``orchestrator_wait``'s so a geo storm cannot starve turn-driving waiters.
    """
    global _wait_inflight
    if _wait_inflight >= GEO_WAIT_MAX_CONCURRENT:
        raise GeoWaitSaturated()
    _wait_inflight += 1
    try:
        return await await_fix(request_id, timeout_s, hub=hub)
    finally:
        _wait_inflight -= 1


def snapshot(hub: Any, session_id: str | None = None) -> dict[str, Any]:
    """Side-effect-free preflight: is anyone home, how stale is the cache."""
    _sweep(hub=hub)
    attached = False
    if session_id:
        try:
            attached = int(hub.subscriber_count(session_id)) > 0
        except Exception:  # noqa: BLE001 — a broken hub is "nobody home"
            attached = False
    try:
        browsers_n = len([sid for sid in hub.live_session_ids() if sid])
    except Exception:  # noqa: BLE001 — a broken hub is "nobody home"
        browsers_n = 0
    cached = last_fix(GEO_CACHE_TTL_S)
    return {
        "browser_attached": attached,
        "browsers_n": browsers_n,
        "last_fix_age_s": cached["age_s"] if cached else None,
        "pending_n": len(active_requests()),
        "wait_inflight": _wait_inflight,
    }
