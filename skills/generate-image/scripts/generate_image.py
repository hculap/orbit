#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["google-genai>=1.0.0", "Pillow>=10.0.0"]
# ///
"""Generate an image via Gemini and save it to the per-session orchestrator uploads dir.

Reads ``GEMINI_API_KEY`` and ``ORCHESTRATOR_SESSION_ID`` from the environment, writes the
PNG to ``~/.orchestrator/uploads/<session_id>/gen-<utc-iso>-<6char-uuid>.png``, prints
exactly one ``MEDIA: <abs-path>`` line to stdout (other diagnostics go to stderr).
"""
from __future__ import annotations

import argparse
import base64
import io
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

PRIMARY_MODEL = "gemini-3.1-flash-image-preview"
FALLBACK_MODEL = "gemini-3-pro-image"
UPLOADS_ROOT = Path.home() / ".orchestrator" / "uploads"
SID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _resolve_session_id() -> str:
    """Resolve the orchestrator session id.

    Primary: ``ORCHESTRATOR_SESSION_ID`` env. Fallback: inside the orchestrator's
    interactive tmux pane the session NAME is ``hd-<uuid>`` — recover it via
    ``tmux display-message -p '#S'`` (uses ``$TMUX``, no socket flag needed) and
    strip the ``hd-`` prefix. The tmux runner doesn't always export the env into
    the Bash subshell, but the session name is always there.
    """
    sid = os.environ.get("ORCHESTRATOR_SESSION_ID", "").strip()
    if SID_RE.match(sid):
        return sid
    if os.environ.get("TMUX"):
        try:
            import subprocess
            out = subprocess.run(
                ["tmux", "display-message", "-p", "#S"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if out.startswith("hd-"):
                out = out[3:]
            if SID_RE.match(out):
                return out
        except Exception:
            pass
    return ""


def _validated_reference(sid_dir: Path, raw_path: str):
    """Resolve a --reference arg, ensure it's INSIDE the session uploads dir
    (defeats `..` traversal AND absolute paths outside the dir, since both
    sides are .resolve()'d), open as RGB PIL.Image."""
    from PIL import Image, UnidentifiedImageError

    p = Path(raw_path).resolve()
    sid_resolved = sid_dir.resolve()
    if sid_resolved not in p.parents:
        raise SystemExit(
            f"ERROR: --reference {raw_path!r} is outside the session uploads dir"
        )
    if not p.is_file():
        raise SystemExit(f"ERROR: --reference {raw_path!r} not found")
    try:
        img = Image.open(p)
        img.load()
    except (FileNotFoundError, PermissionError, UnidentifiedImageError, OSError) as e:
        raise SystemExit(
            f"ERROR: cannot read --reference {raw_path!r}: {type(e).__name__}: {e}"
        ) from e
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


def _extract_image_bytes(response) -> bytes | None:
    """Walk the google-genai response shapes and return the first image's raw bytes.

    Tries ``response.parts`` first, then falls back to
    ``response.candidates[0].content.parts``. Picks the first part with
    ``inline_data.data``; base64-decodes if str. Returns ``None`` if no image.
    """
    parts = getattr(response, "parts", None)
    if not parts:
        candidates = getattr(response, "candidates", None)
        if candidates:
            try:
                content = candidates[0].content
                parts = getattr(content, "parts", None)
            except Exception:
                parts = None

    if not parts:
        return None

    for part in parts:
        if getattr(part, "text", None) is not None:
            print(f"Model response: {part.text}", file=sys.stderr)
            continue

        inline_data = getattr(part, "inline_data", None)
        if inline_data is None:
            continue

        image_data = getattr(inline_data, "data", None)
        if image_data is None:
            continue

        if isinstance(image_data, str):
            image_data = base64.b64decode(image_data)
        return image_data

    return None


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate an image via Gemini, save it under the per-session orchestrator uploads dir."
    )
    ap.add_argument("--prompt", required=True, help="Text description of the image to generate.")
    ap.add_argument("--model", default=PRIMARY_MODEL, help=f"Gemini model (default: {PRIMARY_MODEL}).")
    ap.add_argument("--aspect-ratio", default="1:1", help="Aspect ratio, e.g. 1:1, 3:4, 16:9 (default: 1:1).")
    ap.add_argument("--resolution", default="1K", choices=["1K", "2K", "4K"], help="Output resolution (default: 1K).")
    ap.add_argument(
        "--reference",
        action="append",
        default=[],
        metavar="PATH",
        help="Absolute path to a reference image inside the session uploads dir; repeatable.",
    )
    args = ap.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    sid = _resolve_session_id()
    if not sid:
        print(
            "ERROR: could not resolve session id "
            "(ORCHESTRATOR_SESSION_ID unset and not in an hd-<uuid> tmux session)",
            file=sys.stderr,
        )
        sys.exit(1)

    sid_dir = UPLOADS_ROOT / sid
    sid_dir.mkdir(parents=True, exist_ok=True)

    refs = [_validated_reference(sid_dir, r) for r in args.reference]
    if refs:
        print(f"Loaded {len(refs)} reference image(s)", file=sys.stderr)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_path = sid_dir / f"gen-{ts}-{uuid.uuid4().hex[:6]}.png"

    from google import genai
    from google.genai import types
    from PIL import Image

    client = genai.Client(api_key=api_key)
    contents = [*refs, args.prompt]  # Gemini convention: images first, then the text prompt.

    print(
        f"Generating: model={args.model}, ratio={args.aspect_ratio}, "
        f"res={args.resolution}, refs={len(refs)}",
        file=sys.stderr,
    )

    try:
        response = client.models.generate_content(
            model=args.model,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["TEXT", "IMAGE"],
                image_config=types.ImageConfig(
                    image_size=args.resolution,
                    aspect_ratio=args.aspect_ratio,
                ),
            ),
        )
    except Exception as e:
        msg = str(e).lower()
        if "503" in msg or "unavailable" in msg:
            print(f"Primary model unavailable, retrying on {FALLBACK_MODEL}", file=sys.stderr)
            try:
                response = client.models.generate_content(
                    model=FALLBACK_MODEL,
                    contents=contents,
                    config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"]),
                )
            except Exception as fe:
                print(f"ERROR: fallback model {FALLBACK_MODEL} also failed: {fe}", file=sys.stderr)
                sys.exit(1)
        else:
            print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(1)

    img_bytes = _extract_image_bytes(response)
    if not img_bytes:
        print("ERROR: no image in response", file=sys.stderr)
        sys.exit(1)

    try:
        img = Image.open(io.BytesIO(img_bytes))
        if img.mode == "RGBA":
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        img.save(out_path, "PNG")
    except (OSError, IOError) as e:
        print(f"ERROR: failed to save image to {out_path}: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Image saved: {out_path}", file=sys.stderr)
    print(f"MEDIA: {out_path}")


if __name__ == "__main__":
    main()
