"""TTS engine adapters and on-disk cache helpers.

Extracted from ``orchestrator_tts.py`` so that out-of-process consumers
(notably the ``generate-audio`` skill, which runs as a standalone ``uv run``
script) can import these helpers without dragging in FastAPI / Starlette or
needing ``sys.path`` hacks against the running dashboard.

Public surface:
  - ``stream_eleven``, ``stream_openai``, ``stream_gemini`` — async generators
    yielding upstream audio bytes for one synthesis call.
  - ``ENGINE_FNS`` — engine-name → adapter function.
  - ``DEFAULTS`` — engine-name → ``{"voice": ..., "model": ...}``.
  - ``EXT_FOR`` — engine-name → ``(file-extension, mime-type)``.
  - ``cache_path`` — deterministic path for a (engine, voice, model, text) key.
  - ``tee_to_cache`` — async generator that forwards bytes to the client while
    writing them to the cache atomically (rename-on-success).
  - ``wav_header`` — RIFF/WAVE header for raw s16le PCM (used by the Gemini
    adapter, which only emits PCM blobs).

The HTTP route in ``orchestrator_tts.py`` keeps using these via thin private
aliases so the existing diff stays minimal.
"""
from __future__ import annotations
import base64
import hashlib
import os
import struct
import sys
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import HTTPException

CACHE_ROOT = Path(os.environ.get("HOME", "/tmp")) / ".orchestrator" / "tts-cache"

DEFAULTS = {
    "elevenlabs": {"voice": "21m00Tcm4TlvDq8ikWAM", "model": "eleven_flash_v2_5"},  # Rachel
    "openai":     {"voice": "nova",                "model": "tts-1"},
    "gemini":     {"voice": "Kore",                "model": "gemini-2.5-flash-preview-tts"},
}

EXT_FOR = {
    "elevenlabs": ("mp3", "audio/mpeg"),
    "openai":     ("mp3", "audio/mpeg"),
    "gemini":     ("wav", "audio/wav"),
}


def _warn(msg: str) -> None:
    print(f"[tts] {msg}", file=sys.stderr)


def cache_path(engine: str, voice: str, model: str, text: str, ext: str) -> Path:
    """Deterministic on-disk cache location for a synthesis request.

    Layout: ``<CACHE_ROOT>/<engine>/<voice>/<model>/<sha[:2]>/<sha>.<ext>``.
    The two-char shard prefix keeps directory fan-out reasonable.
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return CACHE_ROOT / engine / voice / model / digest[:2] / f"{digest}.{ext}"


def wav_header(num_pcm_bytes: int, sample_rate: int = 24000, channels: int = 1, bits: int = 16) -> bytes:
    """Minimal RIFF/WAVE header for raw s16le PCM (Gemini's output format)."""
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    return (
        b"RIFF" + struct.pack("<I", 36 + num_pcm_bytes) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits)
        + b"data" + struct.pack("<I", num_pcm_bytes)
    )


# ── upstream adapters ────────────────────────────────────────────────────

async def stream_eleven(client: httpx.AsyncClient, text: str, voice: str, model: str) -> AsyncIterator[bytes]:
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise HTTPException(503, "ELEVENLABS_API_KEY not configured")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream"
    async with client.stream(
        "POST", url,
        headers={"xi-api-key": api_key},
        json={"text": text, "model_id": model, "output_format": "mp3_44100_128"},
    ) as r:
        if r.status_code != 200:
            body = (await r.aread())[:200].decode("utf-8", "ignore")
            _warn(f"elevenlabs {r.status_code}: {body}")
            raise HTTPException(502, f"elevenlabs {r.status_code}: {body}")
        async for chunk in r.aiter_bytes():
            if chunk:
                yield chunk


async def stream_openai(client: httpx.AsyncClient, text: str, voice: str, model: str) -> AsyncIterator[bytes]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(503, "OPENAI_API_KEY not configured")
    url = "https://api.openai.com/v1/audio/speech"
    async with client.stream(
        "POST", url,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "input": text, "voice": voice, "response_format": "mp3"},
    ) as r:
        if r.status_code != 200:
            body = (await r.aread())[:200].decode("utf-8", "ignore")
            _warn(f"openai {r.status_code}: {body}")
            raise HTTPException(502, f"openai {r.status_code}: {body}")
        async for chunk in r.aiter_bytes():
            if chunk:
                yield chunk


async def stream_gemini(client: httpx.AsyncClient, text: str, voice: str, model: str) -> AsyncIterator[bytes]:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(503, "GEMINI_API_KEY not configured")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
        },
    }
    r = await client.post(url, json=payload, timeout=60.0)
    if r.status_code != 200:
        _warn(f"gemini {r.status_code}: {r.text[:200]}")
        raise HTTPException(502, f"gemini {r.status_code}: {r.text[:200]}")
    j = r.json()
    try:
        b64 = j["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
    except (KeyError, IndexError) as e:
        raise HTTPException(502, f"gemini bad response: {e}")
    pcm = base64.b64decode(b64)
    # Yield WAV header first so the browser can decode incrementally.
    yield wav_header(len(pcm))
    yield pcm


ENGINE_FNS = {
    "elevenlabs": stream_eleven,
    "openai":     stream_openai,
    "gemini":     stream_gemini,
}


async def tee_to_cache(upstream: AsyncIterator[bytes], target: Path) -> AsyncIterator[bytes]:
    """Forward upstream bytes to the client while writing them to disk.

    The .tmp file is renamed only on clean completion — an abort/exception
    leaves no partial file behind, so the cache stays correct.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    fp = open(tmp, "wb")
    try:
        async for chunk in upstream:
            fp.write(chunk)
            yield chunk
        fp.close()
        try:
            tmp.replace(target)
        except OSError as e:
            _warn(f"cache rename failed {tmp}: {e}")
    except BaseException:
        fp.close()
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
