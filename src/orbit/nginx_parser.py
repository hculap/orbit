"""Parse /etc/nginx/conf.d/apps/*.conf for `location` + `proxy_pass` upstream port."""
from __future__ import annotations
import re
from pathlib import Path

APPS_DIR = Path("/etc/nginx/conf.d/apps")

LOCATION_RE = re.compile(r"^\s*location\s+([^\s{]+)\s*{", re.MULTILINE)
PROXY_PASS_RE = re.compile(r"proxy_pass\s+http://(?:127\.0\.0\.1|localhost):(\d+)")


def discover_apps() -> list[dict]:
    if not APPS_DIR.is_dir():
        return []
    out = []
    for cfg in sorted(APPS_DIR.glob("*.conf")):
        try:
            text = cfg.read_text(errors="replace")
        except (OSError, PermissionError):
            continue
        # Find first non-redirect location with proxy_pass
        # First location often `location = /foo { return 301; }` — skip those
        port = None
        m = PROXY_PASS_RE.search(text)
        if m:
            port = int(m.group(1))
        # Find primary path: first `location <path>/ {` (with trailing slash)
        primary_path = None
        for loc_match in LOCATION_RE.finditer(text):
            path = loc_match.group(1)
            if path.startswith("/") and path.endswith("/"):
                primary_path = path.rstrip("/") or "/"
                break
        if not primary_path:
            # Fallback to first
            m2 = LOCATION_RE.search(text)
            if m2:
                primary_path = m2.group(1)
        if not primary_path or not port:
            continue
        out.append({
            "name": cfg.stem,
            "label": cfg.stem,
            "icon": "🌐",
            "path": primary_path,
            "port": port,
            "status": "ok",
            "description": "",
            "config_file": str(cfg),
        })
    return out
