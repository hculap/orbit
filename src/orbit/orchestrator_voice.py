"""Orchestrator voice transcription — proxy to Groq Whisper turbo.

POST /api/orchestrator/transcribe accepts a multipart audio blob and forwards
it to Groq's OpenAI-compatible transcription endpoint. We proxy server-side so
GROQ_API_KEY stays out of the PWA bundle and so usage is logged in one place.
The frontend sends overlapping full-audio chunks every 2s during recording
(Mode B); each request is independent and stateless from Groq's perspective.
"""
from __future__ import annotations
import os
import sys
import time

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB Groq free-tier cap
WHISPER_PROMPT_CAP = 224            # Whisper's context window cap

# Whisper has a well-documented hallucination pattern where short / silent
# audio gets transcribed to common stock phrases — "Thanks for watching",
# "Subscribe", "Dziękuję" (PL). When VAD trips on background noise (a cough,
# door slam, road noise in a car) the captured ~1s blob arrives at Groq and
# comes back as one of these phrases, which the conversation loop then
# sends to Claude as a "ghost prompt".
#
# Filter strategy: substring match on a known prefix list, gated on audio
# size. The byte threshold was tuned for opus/webm (~16 KB/s) but with
# WAV at 16 kHz/16-bit/mono, 1 s is ~32 KB — empirical battery 2026-05-04
# showed 0.7 s of WAV silence (22 KB) bypassed the original 16 KB filter.
# Bumped to 32 KB so WAV ≤ 1 s and opus/webm ≤ 2 s both fall under it; real
# user speech of "tak"/"nie"/"dzięki" in Polish is typically 0.5-1 s and
# will still pass through if encoded as opus/webm. The substring matching
# also catches the longer hallucinations like "Dziękuję za oglądanie".
_PL_DIACRITIC_FOLD = str.maketrans("ąćęłńóśźż", "acelnoszz")


def _fold(text: str) -> str:
    """Lowercase + strip Polish diacritics so 'Dziękuje'/'dziękuję'/'dzieki'
    all compare equal. The in-car miss (2026-06-13) was exactly this: Whisper
    returned 'Dziękuje za oglądanie' (no ę) while the filter listed 'dziękuję'
    (ę), so startswith() never matched and the hallucination reached claude."""
    return text.lower().translate(_PL_DIACRITIC_FOLD)


# Tier 2 — unmistakable YouTube/outro hallucinations (already diacritic-folded
# ASCII). Matched as a SUBSTRING and at ANY audio size: a few seconds of cabin
# noise can yield a full outro sentence that blows past the small-audio gate,
# and these phrases are never a real command driving Claude Code.
_HALLUCINATION_ALWAYS = (
    "za ogladanie", "dziekuje za ogladanie", "do zobaczenia",
    "napisy stworzone", "subskrybuj", "zasubskrybuj",
    "thanks for watching", "thank you for watching",
    "please subscribe", "subscribe to", "see you next time",
    "like and subscribe", "don't forget to subscribe", "dont forget to subscribe",
)
# Tier 1 — short fillers that COULD be a real terse reply on a clear short clip
# but are usually noise on a tiny one: only dropped (as a prefix) when the
# audio is small. Diacritic-folded ASCII.
_HALLUCINATION_SHORT = (
    "dziekuje", "dzieki", "tak.", "nie.", "ok.", "thank you", "goodbye", "see you",
)
_HALLUCINATION_AUDIO_BYTES_MAX = 32 * 1024  # ~1 s WAV / ~2 s opus


def _is_hallucination(text: str, audio_len: int) -> bool:
    """True when ``text`` is a Whisper noise/silence hallucination to drop.

    Tier 2 (outro phrases) match as a substring at ANY size; Tier 1 (short
    fillers) match as a prefix only on small audio. Both are diacritic-folded.
    """
    if not text:
        return False
    folded = _fold(text.strip())
    if any(p in folded for p in _HALLUCINATION_ALWAYS):
        return True
    return audio_len < _HALLUCINATION_AUDIO_BYTES_MAX and any(
        folded.startswith(p) for p in _HALLUCINATION_SHORT
    )


def _warn(msg: str) -> None:
    print(f"[voice] {msg}", file=sys.stderr)


def register_routes(app: FastAPI) -> None:
    @app.post("/api/orchestrator/transcribe")
    async def api_transcribe(
        audio: UploadFile = File(...),
        language: str = Form("auto"),
        prompt: str | None = Form(None),
    ) -> dict:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise HTTPException(503, detail="GROQ_API_KEY not configured")
        data = await audio.read()
        if not data:
            raise HTTPException(400, detail="empty audio")
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(413, detail=f"audio exceeds {MAX_AUDIO_BYTES} bytes")
        # Groq accepts: flac, m4a, mp3, mp4, mpeg, mpga, ogg, opus, wav, webm.
        files = {
            "file": (
                audio.filename or "audio.webm",
                data,
                audio.content_type or "audio/webm",
            ),
        }
        form: dict[str, str] = {
            "model": GROQ_MODEL,
            "response_format": "json",
            "temperature": "0",
        }
        # `auto` (or empty) lets Whisper auto-detect from the audio. A specific
        # ISO 639-1 code (`pl`/`en`/`de`/...) constrains detection — useful for
        # short clips or noisy audio where auto-detect drifts.
        lang_norm = (language or "").strip().lower()
        if lang_norm and lang_norm != "auto":
            form["language"] = lang_norm
        if prompt:
            form["prompt"] = prompt[:WHISPER_PROMPT_CAP]
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    files=files,
                    data=form,
                )
        except httpx.HTTPError as exc:
            _warn(f"groq request failed: {exc}")
            raise HTTPException(502, detail=f"groq request failed: {exc}") from exc
        duration_ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code != 200:
            body = resp.text[:200]
            _warn(f"groq {resp.status_code}: {body}")
            # Groq 400 "audio too short" / "no speech" fires whenever the
            # user didn't speak or background noise tripped VAD without real
            # speech. Surface as empty transcript (200) so the conversation
            # loop's silence-only branch re-arms cleanly instead of bubbling
            # up a 502 that paints the UI red. Keep real failures (5xx,
            # auth) as 502.
            body_l = body.lower()
            if resp.status_code == 400 and (
                "too short" in body_l or "no speech" in body_l
            ):
                return {"text": "", "duration_ms": duration_ms}
            raise HTTPException(502, detail=f"groq {resp.status_code}: {body}")
        try:
            text = (resp.json().get("text") or "").strip()
        except ValueError as exc:
            raise HTTPException(502, detail=f"groq returned non-json: {exc}") from exc
        # Breadcrumb (single-user box): the raw transcript + audio size makes a
        # future hallucination/empty-command bypass self-evident in the journal
        # — the size drives the small-audio gate, the text shows what reached
        # the conversation loop. Diagnosed 2026-06-13 when a ghost command
        # ("Dziękuje za oglądanie") slipped through invisibly.
        _warn(f"transcript len={len(data)}B dur={duration_ms}ms text={text[:120]!r}")
        # Strip Whisper hallucinations (see _HALLUCINATION_* above). User says
        # nothing or background/road noise trips VAD → Whisper invents a stock
        # outro phrase → the conversation loop would forward it as a real
        # command. Two tiers: outro phrases die at ANY size (substring); short
        # fillers only on small audio (prefix). Diacritic-folded so the e/ę
        # variants Whisper emits can't slip through.
        if _is_hallucination(text, len(data)):
            _warn(
                f"hallucination filter dropped {text!r} "
                f"(audio={len(data)}B, dur={duration_ms}ms)"
            )
            return {"text": "", "duration_ms": duration_ms}
        return {"text": text, "duration_ms": duration_ms}
