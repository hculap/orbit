"""\
Multi-engine TTS proxy with filesystem caching.

GET /api/orchestrator/tts?text=...&engine=...&voice=...&model=...
  Returns audio bytes (MP3 for elevenlabs/openai, WAV for gemini).

Engines:
  - elevenlabs : Flash v2.5 default (~125 ms TTFA, MP3 streaming)  [DEFAULT]
  - openai     : tts-1 / gpt-4o-mini-tts (MP3 streaming, slower)
  - gemini     : 2.5 Flash TTS (PCM blob, slowest, has free tier)

Cache:
  ~/.orchestrator/tts-cache/{engine}/{voice}/{model}/{sha[0:2]}/{sha}.{ext}
  Key   = SHA256(text).
  Tee   = upstream chunks go to client and disk concurrently. On crash the
          .tmp file is deleted so a partial cache entry can never be served.
  No eviction yet — manual `rm -rf` or future LRU.

Frontend uses GET (not POST) so an `<audio src>` tag can drive playback
directly and the browser HTTP cache helps on top of our disk cache.

Engine adapters and cache helpers live in :mod:`tts_engines` so the
``generate-audio`` skill can import them without booting FastAPI. We
re-export them here under private aliases to keep the historical
diff inside this module minimal.
"""
from __future__ import annotations
import asyncio
import os
import sys
import time
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from .tts_engines import (
    DEFAULTS,
    ENGINE_FNS,
    EXT_FOR,
    cache_path as _cache_path,
    stream_eleven as _stream_eleven,  # noqa: F401 — re-exported for backwards compat
    stream_gemini as _stream_gemini,  # noqa: F401
    stream_openai as _stream_openai,  # noqa: F401
    tee_to_cache as _tee_to_cache,
    wav_header as _wav_header,  # noqa: F401
)

MAX_TTS_CHARS = 2000

VOICES = {
    "elevenlabs": [
        {"id": "21m00Tcm4TlvDq8ikWAM", "name": "Rachel (PL ok, neutral)"},
        {"id": "EXAVITQu4vr4xnSDxMaL", "name": "Bella (PL ok, warm)"},
        {"id": "pNInz6obpgDQGcFmaJgB", "name": "Adam (male, deep)"},
        {"id": "TxGEqnHWrfWFTfGW9XjX", "name": "Josh (male, calm)"},
        {"id": "yoZ06aMxZJJ28mfd3POQ", "name": "Sam (male, casual)"},
    ],
    "openai": [
        {"id": "alloy",   "name": "Alloy (neutral)"},
        {"id": "echo",    "name": "Echo (male)"},
        {"id": "fable",   "name": "Fable (storyteller)"},
        {"id": "nova",    "name": "Nova (female, friendly)"},
        {"id": "shimmer", "name": "Shimmer (female, soft)"},
        {"id": "onyx",    "name": "Onyx (male, deep)"},
    ],
    "gemini": [
        {"id": "Kore",   "name": "Kore (firm)"},
        {"id": "Puck",   "name": "Puck (upbeat)"},
        {"id": "Aoede",  "name": "Aoede (breezy)"},
        {"id": "Charon", "name": "Charon (informative)"},
        {"id": "Fenrir", "name": "Fenrir (excitable)"},
        {"id": "Leda",   "name": "Leda (youthful)"},
    ],
}


# ── route registration ──────────────────────────────────────────────────

def register_routes(app: FastAPI) -> None:
    @app.get("/api/orchestrator/tts")
    async def tts(
        text: str = Query(..., min_length=1, max_length=MAX_TTS_CHARS),
        engine: str = Query("elevenlabs"),
        voice: Optional[str] = Query(None),
        model: Optional[str] = Query(None),
    ):
        if engine not in ENGINE_FNS:
            raise HTTPException(400, f"unknown engine: {engine}")
        text = text.strip()
        if not text:
            raise HTTPException(400, "empty text")
        defaults = DEFAULTS[engine]
        voice = voice or defaults["voice"]
        model = model or defaults["model"]
        ext, mime = EXT_FOR[engine]
        target_cache_path = _cache_path(engine, voice, model, text, ext)

        if target_cache_path.exists():
            return FileResponse(
                str(target_cache_path), media_type=mime,
                headers={"X-TTS-Cache": "hit", "Cache-Control": "public, max-age=2592000"},
            )

        # Cache miss → pre-flight the upstream so any HTTPException (auth,
        # rate-limit, bad voice id) is raised BEFORE the StreamingResponse
        # starts emitting bytes. Without this the upstream's 4xx/5xx would
        # only surface mid-stream, where Starlette can't change headers and
        # logs the noisy "RuntimeError: response already started".
        client = httpx.AsyncClient(timeout=60.0)
        upstream_iter = ENGINE_FNS[engine](client, text, voice, model)
        try:
            first_chunk = await upstream_iter.__anext__()
        except StopAsyncIteration:
            await client.aclose()
            raise HTTPException(502, "upstream returned no audio")
        except HTTPException:
            await client.aclose()
            raise
        except Exception as exc:  # noqa: BLE001 — convert any error to 502
            await client.aclose()
            raise HTTPException(502, f"upstream error: {exc}") from exc

        async def with_first():
            yield first_chunk
            async for chunk in upstream_iter:
                yield chunk

        async def gen():
            try:
                async for chunk in _tee_to_cache(with_first(), target_cache_path):
                    yield chunk
            finally:
                await client.aclose()

        return StreamingResponse(
            gen(), media_type=mime,
            headers={"X-TTS-Cache": "miss", "Cache-Control": "public, max-age=2592000"},
        )

    @app.get("/api/orchestrator/tts/voices")
    async def list_voices(engine: str = Query("elevenlabs")):
        if engine not in VOICES:
            raise HTTPException(400, f"unknown engine: {engine}")
        # ElevenLabs supports user-cloned voices via /v1/voices. Pull the
        # live catalogue (5-min TTL cache) and merge with our hand-curated
        # defaults so personal clones show up next to the stock voices.
        # Other engines have static catalogues — return the hardcoded list.
        voices_list = VOICES[engine]
        if engine == "elevenlabs":
            try:
                live = await _eleven_voices_cached()
                if live:
                    voices_list = _merge_voice_lists(VOICES[engine], live)
            except Exception as exc:  # noqa: BLE001 — defensive
                _voice_warn(f"elevenlabs voice fetch failed: {exc}")
        return {
            "engine": engine,
            "default_voice": DEFAULTS[engine]["voice"],
            "default_model": DEFAULTS[engine]["model"],
            "voices": voices_list,
            "configured": bool(os.environ.get({
                "elevenlabs": "ELEVENLABS_API_KEY",
                "openai":     "OPENAI_API_KEY",
                "gemini":     "GEMINI_API_KEY",
            }[engine])),
        }


# ── ElevenLabs live voice catalogue (with TTL cache) ─────────────────────
_ELEVEN_VOICES_TTL_S = 300.0
_eleven_voices_cache: tuple[float, list[dict]] | None = None
_eleven_voices_lock = asyncio.Lock()


def _voice_warn(msg: str) -> None:
    print(f"[orchestrator_tts] {msg}", file=sys.stderr)


async def _eleven_voices_cached() -> list[dict]:
    """Return ElevenLabs voices from /v1/voices, cached for 5 min.

    On API error returns the previous cached list (if any) or `[]` so the
    caller can gracefully fall back to the hand-curated VOICES table.
    """
    global _eleven_voices_cache
    now = time.time()
    if _eleven_voices_cache and (now - _eleven_voices_cache[0]) < _ELEVEN_VOICES_TTL_S:
        return list(_eleven_voices_cache[1])
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        return []
    async with _eleven_voices_lock:
        # Re-check inside the lock (another task may have just refreshed).
        if _eleven_voices_cache and (now - _eleven_voices_cache[0]) < _ELEVEN_VOICES_TTL_S:
            return list(_eleven_voices_cache[1])
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(
                    "https://api.elevenlabs.io/v1/voices",
                    headers={"xi-api-key": api_key},
                )
            if resp.status_code != 200:
                _voice_warn(f"elevenlabs /v1/voices {resp.status_code}: {resp.text[:200]}")
                # Stale cache is better than nothing — keep serving until TTL.
                if _eleven_voices_cache:
                    return list(_eleven_voices_cache[1])
                return []
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            _voice_warn(f"elevenlabs /v1/voices error: {exc}")
            if _eleven_voices_cache:
                return list(_eleven_voices_cache[1])
            return []
        # Parse + cache write must happen INSIDE the lock so the next coroutine
        # waiting on it sees a fresh entry on its own re-check (line above) and
        # skips the redundant HTTP call. Out-of-lock would defeat the
        # double-checked-locking purpose entirely.
        raw = payload.get("voices") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            return []
        cleaned: list[dict] = []
        for v in raw:
            if not isinstance(v, dict):
                continue
            vid = v.get("voice_id")
            name = v.get("name")
            if not isinstance(vid, str) or not isinstance(name, str):
                continue
            category = v.get("category") or ""
            labels = v.get("labels") or {}
            lang = labels.get("language") if isinstance(labels, dict) else None
            suffix_bits: list[str] = []
            if isinstance(category, str) and category and category not in ("premade", "professional"):
                suffix_bits.append(category)
            if isinstance(lang, str) and lang:
                suffix_bits.append(lang)
            suffix = f" ({', '.join(suffix_bits)})" if suffix_bits else ""
            cleaned.append({"id": vid, "name": f"{name}{suffix}"})
        _eleven_voices_cache = (now, cleaned)
        return list(cleaned)


def _merge_voice_lists(curated: list[dict], live: list[dict]) -> list[dict]:
    """Curated entries first (preserves familiar names + ordering), then any
    live voices not in the curated list. De-dupe by ``id``."""
    seen: set[str] = {v.get("id") for v in curated if isinstance(v.get("id"), str)}
    out = list(curated)
    for v in live:
        vid = v.get("id")
        if isinstance(vid, str) and vid not in seen:
            out.append(v)
            seen.add(vid)
    return out
