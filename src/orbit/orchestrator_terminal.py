"""HTTP + WebSocket reverse-proxy for the inline interactive terminal.

Forwards every request under
``/api/orchestrator/sessions/<sid>/term/...`` to the per-session ttyd
subprocess managed by :class:`orchestrator_ttyd.TtydPool`. Same-origin
proxy means the existing FastAPI/Tailscale auth gate covers ttyd —
ttyd itself binds 127.0.0.1 only and never sees external traffic.

This module introduces the FIRST WebSocket route in the codebase
(everything else is SSE). The pump is intentionally simple: one async
task per direction, both terminate on either side disconnecting, the
client WebSocket is always closed in ``finally``.

The proxy is feature-gated by ``ttyd_enabled`` in
:mod:`orchestrator_settings`. When disabled, every route here returns
HTTP 503 (HTTP) or closes immediately with code 1011 (WS) so the
frontend falls back to the legacy SSE preview without a code change.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import orchestrator_settings as settings_mod
from . import orchestrator_ttyd as ttyd_mod


# Hop-by-hop response headers we must NOT forward to the browser (RFC
# 7230 §6.1). Letting these through breaks chunked transfer + keep-alive
# negotiation because the proxy and upstream disagree on framing.
_HOP_BY_HOP_RESPONSE = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    # ``content-length`` is set by httpx based on body but stays attached
    # to the response; with chunked streaming via ``aiter_raw`` we let
    # uvicorn recompute framing. Forwarding the upstream length would
    # mismatch when the body is gzipped end-to-end.
    "content-length",
})

# Subprotocol ttyd advertises and the browser xterm.js client requests.
_TTYD_WS_SUBPROTOCOL = "tty"

# Per-request upstream timeout. Generous because ttyd serves a few KB
# of JS/CSS on first paint; later requests are small.
_UPSTREAM_HTTP_TIMEOUT_S = 30.0

# Pump read size hint for the WS proxy. Doesn't really matter for ttyd
# (its frames are usually small) but caps any single iteration's memory.
_WS_PUMP_MAX_MSG_BYTES = 1 << 20  # 1 MiB

PoolGetter = Callable[[], ttyd_mod.TtydPool]


def _feature_enabled() -> bool:
    """Single source of truth for the kill-switch flag."""
    return bool(settings_mod.get_flag("ttyd_enabled"))


def _filter_proxy_response_headers(headers: httpx.Headers) -> dict[str, str]:
    """Drop hop-by-hop + framing headers before passing back to the browser."""
    return {
        k: v for k, v in headers.items()
        if k.lower() not in _HOP_BY_HOP_RESPONSE
    }


# ── HTTP catchall ──────────────────────────────────────────────────


async def _proxy_http(
    request: Request,
    session_id: str,
    path: str,
    pool: ttyd_mod.TtydPool,
) -> Response:
    """Proxy one HTTP GET to ttyd's localhost port and stream the body back.

    On any error before the response body starts streaming we 502 with
    a short diagnostic so the iframe's ``onError`` surfaces something
    meaningful. After streaming starts, errors propagate via early EOF
    — which xterm.js's loader handles by retrying.
    """
    port = await pool.acquire(session_id=session_id)
    pool.touch(session_id)

    # Reconstruct the upstream URL with the SAME path the browser sent,
    # because ttyd is configured with ``--base-path
    # /api/orchestrator/sessions/<sid>/term`` so it expects to see that
    # prefix in every request. No prefix stripping in the proxy.
    upstream_path = request.url.path
    upstream_query = request.url.query
    upstream_url = f"http://127.0.0.1:{port}{upstream_path}"
    if upstream_query:
        upstream_url = f"{upstream_url}?{upstream_query}"

    # Forward a minimal header set. We do NOT forward Host (would be the
    # dashboard's host, but ttyd doesn't care), Cookie (no value), or
    # Authorization (auth is enforced at the dashboard layer; ttyd has
    # no notion of our auth). Accept-Encoding stays so ttyd can gzip.
    fwd_headers: dict[str, str] = {}
    for hop in ("accept", "accept-encoding", "accept-language", "user-agent"):
        value = request.headers.get(hop)
        if value is not None:
            fwd_headers[hop] = value

    client = httpx.AsyncClient(timeout=_UPSTREAM_HTTP_TIMEOUT_S)
    try:
        upstream_resp = await client.send(
            client.build_request("GET", upstream_url, headers=fwd_headers),
            stream=True,
        )
    except httpx.ConnectError as exc:
        await client.aclose()
        raise HTTPException(
            status_code=502,
            detail=f"ttyd upstream unreachable on port {port}: {exc}",
        )
    except Exception:
        await client.aclose()
        raise

    async def body() -> "asyncio.AsyncGenerator[bytes, None]":
        try:
            async for chunk in upstream_resp.aiter_raw():
                yield chunk
        finally:
            await upstream_resp.aclose()
            await client.aclose()

    return StreamingResponse(
        body(),
        status_code=upstream_resp.status_code,
        headers=_filter_proxy_response_headers(upstream_resp.headers),
        media_type=upstream_resp.headers.get("content-type"),
    )


# ── WebSocket pump ─────────────────────────────────────────────────


async def _pump_client_to_upstream(
    client_ws: WebSocket,
    upstream: "websockets.WebSocketClientProtocol",
) -> None:
    """Forward every browser → ttyd message until the browser disconnects.

    Starlette delivers WS messages as a tagged dict. ttyd uses binary
    frames exclusively for the input channel but some xterm.js clients
    send text frames for resize messages — we forward whichever type
    arrives.
    """
    try:
        while True:
            message = await client_ws.receive()
            mtype = message.get("type")
            if mtype == "websocket.disconnect":
                return
            if mtype != "websocket.receive":
                continue
            payload_bytes = message.get("bytes")
            payload_text = message.get("text")
            if payload_bytes is not None:
                await upstream.send(payload_bytes)
            elif payload_text is not None:
                await upstream.send(payload_text)
    except WebSocketDisconnect:
        return
    except websockets.ConnectionClosed:
        return


async def _pump_upstream_to_client(
    upstream: "websockets.WebSocketClientProtocol",
    client_ws: WebSocket,
) -> None:
    """Forward every ttyd → browser message until ttyd closes."""
    try:
        async for message in upstream:
            if isinstance(message, (bytes, bytearray)):
                await client_ws.send_bytes(bytes(message))
            else:
                await client_ws.send_text(message)
    except websockets.ConnectionClosed:
        return


async def _proxy_websocket(
    client_ws: WebSocket,
    session_id: str,
    pool: ttyd_mod.TtydPool,
) -> None:
    """Bridge the browser xterm.js WS to ttyd's WS over localhost.

    We accept the client first ONLY after upstream connects so a ttyd
    spawn failure surfaces as a clean close-on-handshake instead of an
    "accepted then closed" client-side error. Both pumps run
    concurrently; whichever side closes first cancels the other.
    """
    if not _feature_enabled():
        # Defensive — the proxy registration calls this guard too, but
        # if a stale tab connects between flag flip and route teardown,
        # close cleanly with 1011 ("internal" — the closest standard
        # code to "feature disabled"). Some browsers ignore the close
        # reason; that's OK, the iframe falls back.
        try:
            await client_ws.close(code=1011)
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        port = await pool.acquire(session_id=session_id)
    except Exception as exc:  # noqa: BLE001 — upstream errors must close cleanly
        ttyd_mod._warn(f"WS proxy acquire failed for {session_id!r}: {exc}")
        try:
            await client_ws.close(code=1011)
        except Exception:
            pass
        return
    pool.touch(session_id)

    upstream_url = (
        f"ws://127.0.0.1:{port}{ttyd_mod.BASE_PATH_PREFIX}"
        f"{session_id}{ttyd_mod.BASE_PATH_SUFFIX}/ws"
    )

    try:
        upstream = await websockets.connect(
            upstream_url,
            subprotocols=[_TTYD_WS_SUBPROTOCOL],
            max_size=_WS_PUMP_MAX_MSG_BYTES,
        )
    except Exception as exc:  # noqa: BLE001
        import traceback as _tb
        ttyd_mod._warn(
            f"WS proxy upstream-connect failed: url={upstream_url!r} "
            f"exc_type={type(exc).__name__} exc={exc!r}\n"
            f"{_tb.format_exc()}"
        )
        try:
            await client_ws.close(code=1011)
        except Exception:
            pass
        return

    try:
        await client_ws.accept(subprotocol=_TTYD_WS_SUBPROTOCOL)
    except Exception:
        await upstream.close()
        raise

    try:
        await asyncio.gather(
            _pump_client_to_upstream(client_ws, upstream),
            _pump_upstream_to_client(upstream, client_ws),
            return_exceptions=True,
        )
    finally:
        # Touch one last time so a tab close right at the end doesn't
        # immediately trigger eviction — the next reopen within the
        # idle TTL window can reuse the still-warm ttyd.
        pool.touch(session_id)
        try:
            await upstream.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await client_ws.close()
        except Exception:  # noqa: BLE001
            pass


# ── public registration ────────────────────────────────────────────


def register_terminal_routes(app: FastAPI, get_pool: PoolGetter) -> None:
    """Mount the HTTP catchall + WebSocket on ``app``.

    ``get_pool`` is a zero-arg callable so the route closures pick up
    the pool lazily (matches the ``_get_tmux_pool()`` pattern in
    :mod:`orchestrator` — no reaching into module globals from inside
    the route, which makes the pool replaceable in tests).
    """

    @app.websocket("/api/orchestrator/sessions/{session_id}/term/ws")
    async def term_ws(client_ws: WebSocket, session_id: str) -> None:
        if not _feature_enabled():
            try:
                await client_ws.close(code=1011)
            except Exception:  # noqa: BLE001
                pass
            return
        await _proxy_websocket(client_ws, session_id, get_pool())

    @app.get("/api/orchestrator/sessions/{session_id}/term")
    @app.get("/api/orchestrator/sessions/{session_id}/term/")
    @app.get("/api/orchestrator/sessions/{session_id}/term/{path:path}")
    async def term_http(
        request: Request,
        session_id: str,
        path: str = "",
    ) -> Response:
        if not _feature_enabled():
            raise HTTPException(
                status_code=503,
                detail="inline terminal disabled (ttyd_enabled=false)",
            )
        try:
            return await _proxy_http(request, session_id, path, get_pool())
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 — surface as 502
            raise HTTPException(
                status_code=502,
                detail=f"ttyd proxy error: {exc}",
            )
