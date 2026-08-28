"""HTTP surface for device geolocation — ``/api/orchestrator/geo/*``.

Mounted by :func:`orchestrator.register_routes` (the ``voice_mod.register_routes``
precedent) so :mod:`orchestrator_geo` itself stays FastAPI-free and trivially
unit-testable.

THE AUTH IS DELIBERATELY ASYMMETRIC:

* **agent → server** (create / wait / cancel / status / reverse / nearby) is
  gated on the ``X-Geo-Token`` header vs ``~/.orchestrator/artifact_token`` —
  the same shared CLI secret ``_require_a2a_token`` uses, compared with
  :func:`hmac.compare_digest` and failing CLOSED when the file is missing.
* **browser → server** (answer / pending / forget) is UN-gated, like every
  other browser call in this app: the ``request_id`` — 128 bits of
  ``secrets.token_urlsafe``, transmitted only over that session's own SSE — IS
  the capability. Un-gated is not un-validated: every field of an answer is
  rebuilt from an allowlist and range-checked before it can become a fix.

The feature flag is checked INSIDE each route, never at registration, so
flipping it in Settings takes effect on the next request with no restart. With
the flag off every agent-facing route 403s with an actionable detail; that is
the documented level-1 rollback, and the route tests are its documentation.

The kill switch has to DELETE, not merely hide, so the browser routes are
flag-aware too — but with a typed BODY instead of a 403, because a tab that
never learned the switch was thrown must stop, not retry. ``answer`` refuses to
turn a coordinate into a cached fix, ``pending`` reports an empty list, and
:func:`on_feature_disabled` (called from the settings PATCH) aborts the asks
that were already on somebody's screen and drops every coordinate in memory.
``forget`` stays unconditional: deleting must work with the feature off.
"""
from __future__ import annotations

import hmac
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request

from . import orchestrator_artifacts as artifacts_mod
from . import orchestrator_events as events_mod
from . import orchestrator_geo as geo_mod
from . import orchestrator_geocode as geocode_mod
from . import orchestrator_meta as meta_mod
from . import orchestrator_settings as settings_mod
from . import orchestrator_uploads as uploads_mod

FLAG = "geolocation_enabled"
GEOCODE_FLAG = "geolocation_geocode_enabled"
TOKEN_HEADER = "x-geo-token"

_DISABLED_DETAIL = "geolocation disabled — Ustawienia → Serwer"


def _require_geo_token(request: Request) -> None:
    """403 unless the caller presents the shared CLI token. Fails closed."""
    expected = artifacts_mod.read_token()
    got = request.headers.get(TOKEN_HEADER) or ""
    if not expected or not hmac.compare_digest(got, expected):
        raise HTTPException(403, detail="invalid geo token")


def _require_flag() -> None:
    """The kill switch, evaluated per request so Settings needs no restart."""
    if not settings_mod.get_flag(FLAG):
        raise HTTPException(403, detail=_DISABLED_DETAIL)


def on_feature_disabled() -> dict[str, Any]:
    """Make the kill switch actually delete. Called when the flag goes False.

    The per-route gate only stops NEW work: an ask registered a second before
    the switch was thrown would still sit on a phone for its whole TTL, and the
    cached fix would still be servable for 15 minutes — which is not what
    "nothing survives" means. Best-effort by design; a failure here must never
    fail the settings PATCH that the user just made.
    """
    cancelled = 0
    try:
        for req in geo_mod.active_requests():
            geo_mod.cancel(req.request_id, hub=events_mod.get_hub())
            cancelled += 1
        geo_mod.forget()
        geocode_mod.clear_caches()
    except Exception:  # noqa: BLE001 — the toggle itself must still succeed
        pass
    return {"cancelled": cancelled}


def _require_body(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(400, detail="payload must be an object")
    return payload


def _require_session_id(raw: Any) -> str:
    if not isinstance(raw, str) or not uploads_mod.SESSION_ID_RE.fullmatch(raw):
        raise HTTPException(400, detail="invalid session_id")
    return raw


def _require_request_id(raw: str) -> str:
    if not geo_mod.REQUEST_ID_RE.fullmatch(raw or ""):
        raise HTTPException(400, detail="invalid request_id")
    return raw


def _agent_for(session_id: str) -> dict[str, str]:
    """``{lib_id, title}`` for the consent UI — best effort, never fatal.

    Reads the session sidecar directly instead of importing ``orchestrator``
    (which imports THIS module — a cycle). A missing sidecar just yields empty
    strings; the modal copy degrades to the generic "Agent".
    """
    try:
        meta = meta_mod.get_meta(session_id)
    except Exception:  # noqa: BLE001 — identity is cosmetic, never block the ask
        return {"lib_id": "", "title": ""}
    return {
        "lib_id": meta.get("lib_id") or "",
        "title": meta.get("title") or "",
    }


def register_geo_routes(app: FastAPI) -> None:
    """Mount every ``/api/orchestrator/geo/*`` route on ``app``."""

    hub = events_mod.get_hub

    # ── agent → server ────────────────────────────────────────────

    @app.post("/api/orchestrator/geo/request")
    async def api_geo_request(
        request: Request, payload: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        """Ask the user's browser for a fix.

        Body: ``{session_id, reason?, accuracy?: fine|coarse, max_age_s?,
        ttl_s?}``. Answers ``cached`` (no prompt at all), ``no_browser``
        (fast-fail in <100 ms — nothing is registered, so no agent ever blocks
        3 minutes on an empty room), ``rate_limited``, or ``pending`` with the
        ``request_id`` to poll.
        """
        _require_geo_token(request)
        _require_flag()
        body = _require_body(payload)
        session_id = _require_session_id(body.get("session_id"))
        try:
            return geo_mod.create_request(
                session_id=session_id,
                hub=hub(),
                reason=body.get("reason", ""),
                accuracy=body.get("accuracy", "fine"),
                max_age_s=body.get("max_age_s", geo_mod.GEO_MAX_AGE_DEFAULT_S),
                ttl_s=body.get("ttl_s", geo_mod.GEO_REQUEST_TTL_S),
                agent=_agent_for(session_id),
            )
        except geo_mod.GeoSaturated:
            raise HTTPException(429, detail="too many pending geolocation requests")

    @app.get("/api/orchestrator/geo/requests/{request_id}/wait")
    async def api_geo_wait(
        request: Request,
        request_id: str,
        timeout: float = Query(default=geo_mod.GEO_WAIT_TIMEOUT_DEFAULT_S),
    ) -> dict[str, Any]:
        """Block until the ask resolves — the agent-side half of the round trip.

        ``{"ok":true,"status":"timeout","terminal":false}`` means the POLL
        WINDOW elapsed, not the request: re-poll the same id. Anything with
        ``terminal:true`` is final, and ``retryable`` says whether trying again
        this turn is reasonable.
        """
        _require_geo_token(request)
        _require_flag()
        _require_request_id(request_id)
        try:
            return await geo_mod.await_fix_guarded(request_id, timeout, hub=hub())
        except geo_mod.GeoWaitSaturated:
            raise HTTPException(429, detail="too many concurrent geolocation waits")

    @app.delete("/api/orchestrator/geo/requests/{request_id}")
    async def api_geo_cancel(request: Request, request_id: str) -> dict[str, Any]:
        """Withdraw an ask (CLI SIGINT / overall timeout) so the modal closes."""
        _require_geo_token(request)
        _require_flag()
        _require_request_id(request_id)
        try:
            return geo_mod.cancel(request_id, hub=hub())
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))

    @app.get("/api/orchestrator/geo/status")
    async def api_geo_status(
        request: Request, session_id: str | None = Query(default=None)
    ) -> dict[str, Any]:
        """Side-effect-free preflight: is a browser attached, how stale is the
        cache, how many asks are outstanding. SKILL.md tells the agent to call
        this before any SPECULATIVE use, so it never fires a pointless prompt.
        """
        _require_geo_token(request)
        _require_flag()
        sid = _require_session_id(session_id) if session_id else None
        return {
            "ok": True,
            "enabled": True,
            "geocode_enabled": bool(settings_mod.get_flag(GEOCODE_FLAG)),
            **geo_mod.snapshot(hub(), sid),
        }

    @app.get("/api/orchestrator/geo/last")
    async def api_geo_last(request: Request) -> dict[str, Any]:
        """The cached fix, READ-ONLY — never registers an ask, never prompts.

        ``geo last`` used to emulate this with a wide ``max_age_s`` create, but
        a create publishes to the browser BEFORE it can discover the cache is
        empty: a verb documented as side-effect-free would fire a real
        permission prompt every time the cache had just been cleared.
        """
        _require_geo_token(request)
        _require_flag()
        served = geo_mod.cached_payload(None, geo_mod.GEO_CACHE_TTL_S)
        return served if served is not None else {"ok": True, "cached": False, "fix": None}

    # ── browser → server (un-gated; the request_id is the capability) ──

    @app.post("/api/orchestrator/geo/requests/{request_id}/answer")
    async def api_geo_answer(
        request_id: str, payload: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        """One device's answer. Body: ``{status, reason?, coords?, ts_ms?,
        session_id?}``.

        Deliberately un-authenticated (a browser holds no CLI token) and
        therefore fully untrusted: coordinates are REBUILT key by key from an
        allowlist, every number is finite- and range-checked, and an unknown
        ``session_id`` is folded onto a real target instead of being believed.

        With the feature switched off this refuses in a BODY rather than a 403:
        a tab that still has a prompt open must not be able to turn a coordinate
        into a cached fix after the kill switch, and must not retry either.
        """
        if not settings_mod.get_flag(FLAG):
            return {"ok": True, "accepted": False, "status": "disabled"}
        _require_request_id(request_id)
        body = _require_body(payload)
        try:
            return geo_mod.submit_answer(request_id, body, hub=hub())
        except geo_mod.GeoNotFound:
            raise HTTPException(404, detail="unknown or already-resolved request")
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))

    @app.get("/api/orchestrator/geo/requests/pending")
    async def api_geo_pending(session_id: str = Query(...)) -> dict[str, Any]:
        """Asks this session still owes an answer to — the reconnect guarantee.

        A hand-built ``EventSource`` sends no ``Last-Event-ID``, so a two-second
        blip drops the push. The frontend polls this on ``onopen`` and on
        ``visibilitychange → visible``.

        Still a 200 with the feature off (a browser polling this is not doing
        anything wrong) — but an EMPTY list, so a tab whose ``useServerSettings``
        hasn't refetched yet cannot pick up an ask the kill switch killed.
        """
        sid = _require_session_id(session_id)
        if not settings_mod.get_flag(FLAG):
            return {"ok": True, "requests": []}
        return {"ok": True, "requests": geo_mod.pending_for(sid)}

    @app.post("/api/orchestrator/geo/forget")
    async def api_geo_forget() -> dict[str, Any]:
        """Clear the in-memory fix + the geocode caches. The "delete my data"
        button; un-gated on purpose — forgetting must work even with the
        feature switched off."""
        result = geo_mod.forget()
        geocode_mod.clear_caches()
        return result

    # ── geocoding (agent → server; second flag, own failure shape) ──

    @app.post("/api/orchestrator/geo/reverse")
    async def api_geo_reverse(
        request: Request, payload: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        """``{lat, lon}`` → a street address via Nominatim.

        ⚠ Egress: the coordinates leave this box for public OSM servers. That
        is why it has its own flag — off returns ``geocode_disabled`` (a body,
        not a 403) so the CLI degrades to raw lat/lon instead of erroring.
        """
        _require_geo_token(request)
        _require_flag()
        if not settings_mod.get_flag(GEOCODE_FLAG):
            return geo_mod.failure("geocode_disabled")
        body = _require_body(payload)
        try:
            return await geocode_mod.reverse(body.get("lat"), body.get("lon"))
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))

    @app.post("/api/orchestrator/geo/nearby")
    async def api_geo_nearby(
        request: Request, payload: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        """``{lat, lon, query, radius_m?, limit?}`` → POIs, nearest first.

        A curated PL/EN keyword resolves to an exact OSM tag (Overpass);
        anything else falls back to a bounded Nominatim text search.
        """
        _require_geo_token(request)
        _require_flag()
        if not settings_mod.get_flag(GEOCODE_FLAG):
            return geo_mod.failure("geocode_disabled")
        body = _require_body(payload)
        try:
            return await geocode_mod.nearby(
                body.get("lat"), body.get("lon"), body.get("query"),
                body.get("radius_m", geocode_mod.RADIUS_DEFAULT_M),
                body.get("limit", geocode_mod.LIMIT_DEFAULT),
            )
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))
