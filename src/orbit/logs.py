"""Log viewer — whitelisted sources only (file + journalctl)."""
from __future__ import annotations
import subprocess
from typing import Literal, TypedDict


class FileSource(TypedDict):
    type: Literal["file"]
    path: str
    label: str


class JournalSource(TypedDict):
    type: Literal["journal"]
    unit: str
    scope: Literal["system", "user"]
    label: str


# WHITELIST — only these sources can be read. Adding new = edit here.
LOG_SOURCES: dict[str, FileSource | JournalSource] = {
    "nginx-access": {"type": "file", "path": "/var/log/nginx/access.log", "label": "nginx access"},
    "nginx-error":  {"type": "file", "path": "/var/log/nginx/error.log",  "label": "nginx error"},
    "svc:nginx":              {"type": "journal", "unit": "nginx",                  "scope": "system", "label": "nginx (journal)"},
    "svc:tailscaled":         {"type": "journal", "unit": "tailscaled",             "scope": "system", "label": "tailscaled"},
    "svc:orbit":  {"type": "journal", "unit": "orbit",      "scope": "system", "label": "orbit"},
    "svc:fail2ban":           {"type": "journal", "unit": "fail2ban",               "scope": "system", "label": "fail2ban"},
    "svc:ssh":                {"type": "journal", "unit": "ssh",                    "scope": "system", "label": "sshd"},
    "user:syncthing":         {"type": "journal", "unit": "syncthing",              "scope": "user",   "label": "syncthing (user)"},
}

MAX_LINES = 2000


def list_sources() -> list[dict]:
    """Available log sources with labels (for dropdown)."""
    return [{"id": k, "label": v["label"], "type": v["type"]} for k, v in LOG_SOURCES.items()]


def read_source(source_id: str, lines: int = 200) -> dict:
    """Return log content + metadata."""
    n = max(1, min(int(lines), MAX_LINES))
    src = LOG_SOURCES.get(source_id)
    if not src:
        return {"id": source_id, "ok": False, "error": "unknown source", "lines": []}
    if src["type"] == "file":
        return _read_file(src["path"], n, source_id, src["label"])
    if src["type"] == "journal":
        return _read_journal(src["unit"], src["scope"], n, source_id, src["label"])
    return {"id": source_id, "ok": False, "error": "unknown type", "lines": []}


def _read_file(path: str, n: int, source_id: str, label: str) -> dict:
    """Try `tail -n N path` first; fall back to `sudo tail` if perms deny."""
    for cmd in (["tail", "-n", str(n), path], ["sudo", "-n", "tail", "-n", str(n), path]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=4)
            if r.returncode == 0:
                lines = r.stdout.splitlines()
                return {"id": source_id, "label": label, "ok": True, "type": "file", "path": path, "lines": lines}
        except Exception:
            continue
    return {"id": source_id, "label": label, "ok": False, "type": "file", "path": path, "error": "permission denied or file missing", "lines": []}


def _read_journal(unit: str, scope: str, n: int, source_id: str, label: str) -> dict:
    cmd = ["journalctl"]
    if scope == "user":
        cmd.append("--user")
    cmd += ["-u", unit, "-n", str(n), "--no-pager", "-o", "short-iso"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        ok = r.returncode == 0
        lines = (r.stdout if ok else r.stderr).splitlines()
        # journalctl prints "-- No entries --" for empty units; pass through as-is
        return {
            "id": source_id, "label": label, "ok": ok, "type": "journal",
            "unit": unit, "scope": scope, "lines": lines,
            "error": None if ok else r.stderr.strip()[:300],
        }
    except Exception as e:
        return {"id": source_id, "label": label, "ok": False, "type": "journal", "unit": unit, "scope": scope, "lines": [], "error": str(e)[:300]}
