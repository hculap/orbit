"""Guards for the shared clipboard util (issue #83).

1. The static asset deploys + serves (catches a missing-file / wrong-path regression).
2. Drift guard: no naked `navigator.clipboard.writeText(` survives outside the
   util itself — every copy site must route through window.HubClipboard.
   (Clipboard READ sites — `.read(` / `.readText(` — are a different feature and
   are left alone.)
"""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src/orbit/static"


def test_hub_clipboard_served(client):
    r = client.get("/static/hub-clipboard.js")
    assert r.status_code == 200
    assert "window.HubClipboard" in r.text
    assert "execCommand" in r.text


def test_no_naked_writetext_outside_util():
    offenders = []
    for path in STATIC.glob("*"):
        if path.name == "hub-clipboard.js":
            continue  # the util is the one allowed home of writeText
        if path.suffix not in (".js", ".jsx"):
            continue
        text = path.read_text(encoding="utf-8")
        # Match writeText( on a (cross-window) clipboard, ignore .read(/.readText(.
        if re.search(r"clipboard\s*\.\s*writeText\s*\(", text):
            offenders.append(path.name)
    assert offenders == [], (
        f"naked clipboard.writeText( found — route through window.HubClipboard: {offenders}"
    )
