"""File ops for the env/secrets feature.

All writes go through atomic ``mkstemp(dir=parent) -> os.replace`` to keep
the temp file on the same filesystem as the target (so ``os.replace`` is a
single rename syscall on POSIX). Mode preservation rules:

* Overwriting an existing file: copy its mode.
* New ``.env`` / files under ``.secrets/`` / SSH private keys / ``authorized_keys``
  / ``known_hosts``: ``0600``.
* New SSH ``*.pub`` files: ``0644``.

Public surface (used by :mod:`secrets_routes`):

* ``parse_env`` / ``write_env``           — round-trip aware .env round-trip
* ``mask_value``                          — list-endpoint masking primitive
* ``list_secret_files`` / ``read_secret_file`` / ``write_secret_file`` /
  ``delete_secret_file``
* ``parse_authorized_keys`` / ``parse_known_hosts``
* ``write_lines``                         — atomic full-file rewrite for
                                            authorized_keys / known_hosts
* ``generate_ssh_key``                    — wraps ``ssh-keygen``
* ``ssh_key_fingerprint``                 — pipes a single line through
                                            ``ssh-keygen -lf -``

Errors:

* ``ValueError``       — bad input (HTTP 400)
* ``FileNotFoundError`` — missing file/entry (HTTP 404)
* ``FileExistsError``  — clobber refused (HTTP 409)
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRET_FILE_RE = re.compile(r"^[A-Za-z0-9._-][A-Za-z0-9._-]{0,127}$")
SSH_KEY_NAME_RE = re.compile(r"^[A-Za-z0-9._-][A-Za-z0-9._-]{0,63}$")
SSH_KEY_TYPES = ("ed25519", "rsa")

DEFAULT_PRIVATE_MODE = 0o600
DEFAULT_PUBLIC_MODE = 0o644
DEFAULT_DIR_MODE = 0o700

_BINARY_SAMPLE_BYTES = 8192


# ── validators ────────────────────────────────────────────────────────


def validate_env_key(key: str) -> str:
    if not isinstance(key, str):
        raise ValueError("env key must be a string")
    k = key.strip()
    if not k:
        raise ValueError("env key required")
    if not ENV_KEY_RE.match(k):
        raise ValueError(
            "env key must match [A-Za-z_][A-Za-z0-9_]* (no spaces, no separators)"
        )
    return k


def validate_secret_file_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("file name must be a string")
    n = name.strip()
    if not n:
        raise ValueError("file name required")
    if "/" in n or "\\" in n:
        raise ValueError("file name cannot contain path separators")
    if n in (".", ".."):
        raise ValueError("invalid file name")
    if n.startswith("."):
        raise ValueError("file name cannot start with '.'")
    if not SECRET_FILE_RE.match(n):
        raise ValueError("file name has invalid characters")
    return n


def validate_ssh_key_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("ssh key name must be a string")
    n = name.strip()
    if not n:
        raise ValueError("ssh key name required")
    if "/" in n or "\\" in n:
        raise ValueError("ssh key name cannot contain path separators")
    if n.startswith(".") or n.startswith("_"):
        raise ValueError("ssh key name cannot start with '.' or '_'")
    if not SSH_KEY_NAME_RE.match(n):
        raise ValueError("ssh key name has invalid characters")
    if n.endswith(".pub"):
        raise ValueError("ssh key name must not include the .pub suffix")
    return n


def validate_ssh_key_type(t: str) -> str:
    if t not in SSH_KEY_TYPES:
        raise ValueError(f"ssh key type must be one of {list(SSH_KEY_TYPES)}")
    return t


# ── atomic write primitive ────────────────────────────────────────────


def _atomic_write_bytes(target: Path, payload: bytes, *, mode: int) -> None:
    """Write ``payload`` to ``target`` atomically, applying ``mode``.

    Mirrors :func:`orbit.library.write_sidecar`. Cleans up the
    tempfile on any failure so we never leave .tmp turds in the parent dir.
    """
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".secret.", suffix=".tmp", dir=str(parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _atomic_write_text(target: Path, text: str, *, mode: int) -> None:
    _atomic_write_bytes(target, text.encode("utf-8"), mode=mode)


def _mode_for_overwrite(target: Path, default: int) -> int:
    """Preserve target's mode if it exists, else return ``default``."""
    if target.exists() and not target.is_symlink():
        try:
            return target.stat().st_mode & 0o777
        except OSError:
            return default
    return default


# ── .env round-trip parser ────────────────────────────────────────────


@dataclass
class EnvLine:
    """One line from a .env file. ``kind`` is one of:

    * ``"comment"`` — full-line comment / blank line; ``body`` is the
      verbatim source (no trailing newline).
    * ``"kv"`` — key/value pair; ``key`` and ``value`` are the parsed
      strings (unquoted, unescaped). ``raw`` keeps the original line so a
      no-edit round-trip is byte-stable.
    """

    kind: str
    body: str = ""
    key: str = ""
    value: str = ""
    raw: str = ""


_KV_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _parse_value(raw_value: str) -> str:
    """Parse a single-line .env right-hand side. Rejects multi-line values.

    Handles:
    * double quotes with C-style escapes (``\\n``, ``\\t``, ``\\\\``, ``\\"``,
      ``\\'``).
    * single quotes (literal — no escape processing).
    * unquoted values (trailing inline ``# comment`` is stripped).
    """
    s = raw_value
    # Strip leading whitespace; trailing whitespace handled below.
    s = s.lstrip()
    if not s:
        return ""

    if s[0] == '"':
        end = _find_unescaped(s, 1, '"')
        if end == -1:
            raise ValueError("unterminated double-quoted .env value (multi-line not supported in v1)")
        body = s[1:end]
        # Anything after the closing quote that isn't whitespace / comment is bogus.
        tail = s[end + 1 :].strip()
        if tail and not tail.startswith("#"):
            raise ValueError(f"unexpected content after quoted value: {tail!r}")
        return _unescape_double(body)

    if s[0] == "'":
        end = s.find("'", 1)
        if end == -1:
            raise ValueError("unterminated single-quoted .env value (multi-line not supported in v1)")
        body = s[1:end]
        tail = s[end + 1 :].strip()
        if tail and not tail.startswith("#"):
            raise ValueError(f"unexpected content after quoted value: {tail!r}")
        return body

    # Unquoted. Strip an inline ``# ...`` comment (only if preceded by space
    # or at column 0) and trailing whitespace.
    out: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "#" and (i == 0 or s[i - 1] in (" ", "\t")):
            break
        out.append(ch)
        i += 1
    return "".join(out).rstrip()


def _find_unescaped(s: str, start: int, ch: str) -> int:
    i = start
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            i += 2
            continue
        if c == ch:
            return i
        i += 1
    return -1


_DOUBLE_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "\\": "\\",
    '"': '"',
    "'": "'",
}


def _unescape_double(body: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            out.append(_DOUBLE_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _quote_for_env(value: str) -> str:
    """Render a value for the right-hand side of a .env line.

    Strategy: if the value is "simple" (no whitespace, no special chars,
    not empty), emit unquoted. Otherwise wrap in double quotes and escape
    backslash, double-quote, newline, tab, carriage return.
    """
    if value == "":
        return '""'
    if any(c in value for c in (' ', '\t', '\n', '\r', '"', "'", '#', '=', '\\')):
        body = (
            value.replace("\\", "\\\\")
                 .replace('"', '\\"')
                 .replace("\n", "\\n")
                 .replace("\r", "\\r")
                 .replace("\t", "\\t")
        )
        return f'"{body}"'
    return value


def parse_env(path: Path) -> tuple[dict[str, str], list[EnvLine]]:
    """Parse a .env file. Empty / missing file is fine (returns empty).

    Returns ``(values, lines)`` where ``lines`` preserves source ordering of
    comments / blanks / kv pairs for round-trip via :func:`write_env`.
    """
    if not path.exists():
        return {}, []
    if not path.is_file():
        raise ValueError(f"{path} is not a regular file")

    text = path.read_text(encoding="utf-8", errors="replace")
    values: dict[str, str] = {}
    lines: list[EnvLine] = []
    for raw in text.split("\n"):
        # Drop the synthetic trailing-empty token from the final "\n" if any
        # (preserved by the join in write_env).
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(EnvLine(kind="comment", body=raw, raw=raw))
            continue
        m = _KV_RE.match(raw)
        if not m:
            # Malformed line — keep it as a comment so a round-trip doesn't
            # silently delete user content. Surface via a sentinel kind so
            # we can warn later if needed.
            lines.append(EnvLine(kind="comment", body=raw, raw=raw))
            continue
        key = m.group(1)
        value = _parse_value(m.group(2))
        values[key] = value
        lines.append(EnvLine(kind="kv", key=key, value=value, raw=raw))

    # Trim a single trailing blank entry if the source ended with "\n" — the
    # split above generates one. We re-add the trailing newline in write_env.
    if lines and lines[-1].kind == "comment" and lines[-1].body == "":
        lines.pop()
    return values, lines


def serialize_env(values: dict[str, str], original_lines: list[EnvLine] | None) -> str:
    """Build the .env text for ``values``, preserving comments/order.

    Strategy:
    * Walk ``original_lines``: keep comments verbatim. For each kv whose
      key still exists in ``values``, emit an updated ``KEY=VALUE`` line
      (re-quoting). Drop kv lines whose key has been removed.
    * Append any keys in ``values`` that were absent from the source, in
      insertion order, at the end.
    """
    seen: set[str] = set()
    out: list[str] = []

    if original_lines:
        for line in original_lines:
            if line.kind == "comment":
                out.append(line.body)
                continue
            if line.kind == "kv":
                if line.key not in values:
                    continue
                seen.add(line.key)
                out.append(f"{line.key}={_quote_for_env(values[line.key])}")

    for key, value in values.items():
        if key in seen:
            continue
        out.append(f"{key}={_quote_for_env(value)}")

    text = "\n".join(out)
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def write_env(path: Path, values: dict[str, str], original_lines: list[EnvLine] | None = None) -> None:
    """Atomic round-trip-aware write."""
    for key in values:
        validate_env_key(key)
    text = serialize_env(values, original_lines)
    mode = _mode_for_overwrite(path, DEFAULT_PRIVATE_MODE)
    _atomic_write_text(path, text, mode=mode)


def env_last_modified(path: Path) -> float | None:
    try:
        return path.stat().st_mtime if path.is_file() else None
    except OSError:
        return None


def init_env_file(path: Path) -> bool:
    """Touch an empty .env at ``path`` if missing. Idempotent.

    Returns True if a new file was created, False if one already existed.
    Raises ValueError if ``path`` exists but isn't a regular file (e.g. a
    directory or symlink).
    """
    if path.exists():
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"{path} exists but is not a regular file")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, "", mode=DEFAULT_PRIVATE_MODE)
    return True


def init_secrets_dir(secrets_dir: Path) -> bool:
    """Create ``secrets_dir`` (mode 0700) if missing. Idempotent.

    Returns True if newly created, False if it already existed. Raises
    ValueError if the path exists but isn't a regular directory.
    """
    if secrets_dir.exists():
        if not secrets_dir.is_dir() or secrets_dir.is_symlink():
            raise ValueError(f"{secrets_dir} exists but is not a directory")
        return False
    secrets_dir.mkdir(parents=True, exist_ok=False, mode=DEFAULT_DIR_MODE)
    return True


# ── masking ───────────────────────────────────────────────────────────


def _looks_binary(data: bytes) -> bool:
    if not data:
        return False
    sample = data[:_BINARY_SAMPLE_BYTES]
    if b"\x00" in sample:
        return True
    try:
        sample.decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True


def mask_value(value: str | bytes, kind: str = "env") -> str:
    """Uniform masked rendering for the list endpoints.

    * ``kind="env"``     -> short tail (``"sk-…cv2D"`` / ``"••••cv2D"``).
    * ``kind="binary"``  -> ``"<<<NN bytes>>>"``.
    * ``kind="ssh"``     -> SHA-256 fingerprint (``"sha256:abcd…"``) when
      ``value`` is an SSH key/text. Falls back to short tail if the input
      doesn't parse.
    """
    if kind == "binary":
        if isinstance(value, str):
            value = value.encode("utf-8", errors="replace")
        return f"<<<{len(value)} bytes>>>"

    if kind == "ssh":
        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8", errors="replace")
            except Exception:
                value = ""
        digest = hashlib.sha256(value.encode("utf-8", errors="replace")).digest()
        b64 = base64.b64encode(digest).decode("ascii").rstrip("=")
        return f"SHA256:{b64[:16]}…"

    # default: short tail
    if isinstance(value, bytes):
        if _looks_binary(value):
            return f"<<<{len(value)} bytes>>>"
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return f"<<<{len(value)} bytes>>>"
    if not isinstance(value, str):
        value = str(value)
    if not value:
        return "(empty)"
    if len(value) <= 4:
        return "•" * len(value)
    tail = value[-4:]
    return f"••••{tail}"


# ── secrets-dir CRUD ──────────────────────────────────────────────────


@dataclass
class SecretFileInfo:
    name: str
    size: int
    mode: int
    masked: str


def list_secret_files(secrets_dir: Path) -> list[SecretFileInfo]:
    if not secrets_dir.is_dir():
        return []
    out: list[SecretFileInfo] = []
    for entry in sorted(secrets_dir.iterdir()):
        if entry.is_symlink() or not entry.is_file():
            continue
        if entry.name.startswith("."):
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        try:
            sample = entry.read_bytes()[:_BINARY_SAMPLE_BYTES]
        except OSError:
            sample = b""
        kind = "binary" if _looks_binary(sample) else "env"
        masked = mask_value(sample, kind=kind)
        out.append(SecretFileInfo(
            name=entry.name,
            size=st.st_size,
            mode=st.st_mode & 0o777,
            masked=masked,
        ))
    return out


def read_secret_file(secrets_dir: Path, name: str) -> bytes:
    name = validate_secret_file_name(name)
    target = secrets_dir / name
    if not target.is_file() or target.is_symlink():
        raise FileNotFoundError(f"secret not found: {name}")
    return target.read_bytes()


def write_secret_file(secrets_dir: Path, name: str, payload: bytes) -> SecretFileInfo:
    name = validate_secret_file_name(name)
    if not isinstance(payload, (bytes, bytearray)):
        raise ValueError("payload must be bytes")
    secrets_dir.mkdir(parents=True, exist_ok=True, mode=DEFAULT_DIR_MODE)
    target = secrets_dir / name
    mode = _mode_for_overwrite(target, DEFAULT_PRIVATE_MODE)
    _atomic_write_bytes(target, bytes(payload), mode=mode)
    st = target.stat()
    sample = bytes(payload[:_BINARY_SAMPLE_BYTES])
    kind = "binary" if _looks_binary(sample) else "env"
    return SecretFileInfo(
        name=name,
        size=st.st_size,
        mode=st.st_mode & 0o777,
        masked=mask_value(sample, kind=kind),
    )


def delete_secret_file(secrets_dir: Path, name: str) -> None:
    name = validate_secret_file_name(name)
    target = secrets_dir / name
    if not target.is_file() or target.is_symlink():
        raise FileNotFoundError(f"secret not found: {name}")
    target.unlink()


# ── ssh-keygen wrappers ──────────────────────────────────────────────


def _ssh_keygen_available() -> bool:
    return shutil.which("ssh-keygen") is not None


def ssh_key_fingerprint(line: str) -> str | None:
    """Pipe ``line`` through ``ssh-keygen -lf -`` and return the fingerprint
    (e.g. ``"SHA256:abc…"``). Returns ``None`` if the line doesn't look
    like a key or ssh-keygen is unavailable.

    Single-shot — for batched callers (authorized_keys / known_hosts
    listing) prefer :func:`ssh_key_fingerprints` to avoid spawning one
    subprocess per line.
    """
    if not line or not line.strip():
        return None
    if not _ssh_keygen_available():
        return None
    try:
        proc = subprocess.run(
            ["ssh-keygen", "-lf", "-"],
            input=line + "\n",
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    parts = proc.stdout.strip().split()
    if len(parts) < 2:
        return None
    return parts[1]


def ssh_key_fingerprints(lines: list[str]) -> list[str | None]:
    """Batched ssh-keygen for N candidate lines — returns fingerprints in
    input order with ``None`` for any line that ssh-keygen rejected.

    Strategy: write all non-blank/non-comment candidates to one tempfile,
    run a single ``ssh-keygen -lf <tmp>``. If ssh-keygen emits one output
    line per candidate (the common case — all keys valid), use positional
    zip mapping. If the counts diverge — ssh-keygen silently drops lines
    it can't parse (options-prefixed entries like
    ``command="…" ssh-rsa AAAA…``, or malformed key material) — fall back
    to the per-line ``ssh_key_fingerprint`` path so each candidate is
    matched to its own subprocess result. The fallback is N forks instead
    of one, matching pre-batch behaviour, but only when batching can't
    safely map positions.
    """
    if not lines:
        return []
    if not _ssh_keygen_available():
        return [None] * len(lines)
    # Track which inputs are non-blank/non-comment so we can map the
    # condensed stdout back to original positions. ssh-keygen ignores
    # comment and blank lines silently.
    candidates: list[tuple[int, str]] = []
    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        candidates.append((idx, raw))
    out: list[str | None] = [None] * len(lines)
    if not candidates:
        return out
    fd, tmp_path = tempfile.mkstemp(prefix=".ssh-fp.", suffix=".tmp")
    batch_ok = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for _idx, raw in candidates:
                fh.write(raw.rstrip("\n") + "\n")
        try:
            proc = subprocess.run(
                ["ssh-keygen", "-lf", tmp_path],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            proc = None  # type: ignore[assignment]
        if proc is not None and proc.returncode == 0:
            stdout_lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
            # 1:1 correspondence between candidates and ssh-keygen output —
            # safe to map positionally. If any candidate was silently dropped
            # by ssh-keygen, the counts diverge and we fall back below.
            if len(stdout_lines) == len(candidates):
                for (idx, _raw), fp_line in zip(candidates, stdout_lines):
                    parts = fp_line.strip().split()
                    if len(parts) >= 2:
                        out[idx] = parts[1]
                batch_ok = True
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
    if batch_ok:
        return out
    # Fallback: per-line subprocess so each candidate gets its own result
    # (or None if ssh-keygen rejects it). Same correctness as the
    # pre-batch path; pays N forks only when batching can't be trusted.
    for idx, raw in candidates:
        out[idx] = ssh_key_fingerprint(raw)
    return out


@dataclass
class SshLine:
    idx: int
    line: str
    type: str | None
    comment: str | None
    fingerprint: str | None
    key_short: str | None
    masked: str
    host: str | None = None


_AUTHORIZED_OPTIONS_RE = re.compile(
    r"^(?P<options>(?:[a-zA-Z0-9_-]+(?:=\"[^\"]*\")?\s*,?\s*)+)\s+(?P<rest>.+)$"
)


def _parse_authorized_line(idx: int, line: str, fp: str | None = None) -> SshLine:
    """Best-effort parse of one authorized_keys line.

    ``fp`` (optional): pre-computed fingerprint — pass the batched result
    from :func:`ssh_key_fingerprints` to skip the per-line subprocess.
    """
    parts = line.split()
    key_type = comment = key_b64 = None
    if len(parts) >= 2:
        key_type = parts[0]
        key_b64 = parts[1] if len(parts) >= 2 else None
        comment = " ".join(parts[2:]) if len(parts) >= 3 else None
    if fp is None:
        fp = ssh_key_fingerprint(line)
    return SshLine(
        idx=idx,
        line=line,
        type=key_type,
        comment=comment,
        fingerprint=fp,
        key_short=(key_b64[-12:] if key_b64 else None),
        masked=fp or mask_value(line, kind="ssh"),
    )


def _parse_known_hosts_line(idx: int, line: str, fp: str | None = None) -> SshLine:
    """Pre-computed fingerprint via ``fp`` skips the per-line subprocess."""
    parts = line.split()
    host = key_type = comment = key_b64 = None
    if len(parts) >= 3:
        host = parts[0]
        key_type = parts[1]
        key_b64 = parts[2]
        comment = " ".join(parts[3:]) if len(parts) >= 4 else None
    if fp is None:
        fp = ssh_key_fingerprint(line)
    return SshLine(
        idx=idx,
        line=line,
        type=key_type,
        comment=comment,
        fingerprint=fp,
        key_short=(key_b64[-12:] if key_b64 else None),
        masked=fp or mask_value(line, kind="ssh"),
        host=host,
    )


def _read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    raw = text.split("\n")
    if raw and raw[-1] == "":
        raw.pop()
    return raw


def parse_authorized_keys(path: Path) -> list[SshLine]:
    raw_lines = _read_lines(path)
    # Pre-filter to real key lines (drop blanks/comments) so the batched
    # fingerprint call's output order maps 1:1 with our SshLine output.
    candidates: list[tuple[int, str]] = []
    for idx, raw in enumerate(raw_lines):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        candidates.append((idx, raw))
    fps = ssh_key_fingerprints([raw for _, raw in candidates])
    return [
        _parse_authorized_line(idx, raw, fp=fp)
        for (idx, raw), fp in zip(candidates, fps)
    ]


def parse_known_hosts(path: Path) -> list[SshLine]:
    raw_lines = _read_lines(path)
    candidates: list[tuple[int, str]] = []
    for idx, raw in enumerate(raw_lines):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        candidates.append((idx, raw))
    fps = ssh_key_fingerprints([raw for _, raw in candidates])
    return [
        _parse_known_hosts_line(idx, raw, fp=fp)
        for (idx, raw), fp in zip(candidates, fps)
    ]


def write_lines(path: Path, lines: list[str], *, default_mode: int = DEFAULT_PRIVATE_MODE) -> None:
    """Atomic full-file rewrite for authorized_keys / known_hosts."""
    body = "\n".join(line.rstrip("\n") for line in lines)
    if body:
        body += "\n"
    mode = _mode_for_overwrite(path, default_mode)
    _atomic_write_text(path, body, mode=mode)


def append_line(path: Path, line: str) -> int:
    """Append a single line and return its 0-based index in the new file."""
    if not isinstance(line, str) or not line.strip():
        raise ValueError("line must be a non-empty string")
    lines = _read_lines(path)
    lines.append(line.rstrip("\n"))
    write_lines(path, lines)
    return len(lines) - 1


def replace_line(path: Path, idx: int, line: str) -> None:
    if not isinstance(line, str) or not line.strip():
        raise ValueError("line must be a non-empty string")
    lines = _read_lines(path)
    if idx < 0 or idx >= len(lines):
        raise FileNotFoundError(f"line idx out of range: {idx}")
    lines[idx] = line.rstrip("\n")
    write_lines(path, lines)


def delete_line(path: Path, idx: int, *, refuse_last: bool = False) -> None:
    """Drop the line at ``idx``. With ``refuse_last=True`` (authorized_keys
    guard from the plan), refuses to leave the file with zero lines.
    """
    lines = _read_lines(path)
    if idx < 0 or idx >= len(lines):
        raise FileNotFoundError(f"line idx out of range: {idx}")
    if refuse_last and len(lines) == 1:
        raise ValueError("refusing to delete the only line in authorized_keys")
    lines.pop(idx)
    write_lines(path, lines)


# ── ssh key generation ───────────────────────────────────────────────


@dataclass
class GeneratedSshKey:
    name: str
    private_path: Path
    public_path: Path
    public_key: str
    fingerprint: str | None


def generate_ssh_key(
    ssh_dir: Path,
    name: str,
    key_type: str = "ed25519",
    comment: str = "",
) -> GeneratedSshKey:
    """Wrap ``ssh-keygen -t <type> -f <path> -N "" -C <comment>``.

    Refuses to clobber an existing private or public key file.
    """
    name = validate_ssh_key_name(name)
    key_type = validate_ssh_key_type(key_type)
    if not isinstance(comment, str):
        raise ValueError("comment must be a string")

    if not _ssh_keygen_available():
        raise FileNotFoundError("ssh-keygen binary not found on PATH")

    ssh_dir.mkdir(parents=True, exist_ok=True, mode=DEFAULT_DIR_MODE)
    private_path = ssh_dir / name
    public_path = ssh_dir / f"{name}.pub"
    if private_path.exists() or public_path.exists():
        raise FileExistsError(f"ssh key already exists: {name}")

    cmd = [
        "ssh-keygen",
        "-t", key_type,
        "-f", str(private_path),
        "-N", "",
        "-C", comment,
        "-q",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise ValueError(f"ssh-keygen failed: {e}") from e
    if proc.returncode != 0:
        # Best-effort cleanup of partially-written files.
        for p in (private_path, public_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        raise ValueError(f"ssh-keygen failed: {proc.stderr.strip() or proc.stdout.strip()}")

    try:
        os.chmod(private_path, DEFAULT_PRIVATE_MODE)
    except OSError:
        pass
    try:
        os.chmod(public_path, DEFAULT_PUBLIC_MODE)
    except OSError:
        pass

    public_key = public_path.read_text(encoding="utf-8", errors="replace").strip()
    return GeneratedSshKey(
        name=name,
        private_path=private_path,
        public_path=public_path,
        public_key=public_key,
        fingerprint=ssh_key_fingerprint(public_key),
    )


def list_ssh_dir(ssh_dir: Path) -> dict:
    """Walk ``ssh_dir`` and split entries into private / public / authorized_keys
    / known_hosts / other.

    ``authorized_keys`` and ``known_hosts`` are returned parsed (one entry per
    line); other files are returned as ``{name, size, mode}``.
    """
    private: list[dict] = []
    public: list[dict] = []
    other: list[dict] = []

    if not ssh_dir.is_dir():
        return {
            "private_keys": [],
            "public_keys": [],
            "authorized_keys": [],
            "known_hosts": [],
            "other": [],
        }

    for entry in sorted(ssh_dir.iterdir()):
        if entry.is_symlink() or not entry.is_file():
            continue
        name = entry.name
        if name in ("authorized_keys", "known_hosts"):
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        try:
            text = entry.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        info = {"name": name, "size": st.st_size, "mode": st.st_mode & 0o777}
        if name.endswith(".pub"):
            stripped = text.strip()
            parts = stripped.split()
            public.append({
                **info,
                "type": parts[0] if len(parts) >= 2 else None,
                "comment": " ".join(parts[2:]) if len(parts) >= 3 else None,
                "value": stripped,
                "fingerprint": ssh_key_fingerprint(stripped),
            })
            continue
        # Private-key heuristic: PEM-style header. Anything else (config,
        # known_hosts.old, etc.) lands in "other".
        if "PRIVATE KEY" in text.split("\n", 1)[0]:
            private.append({
                **info,
                "type": _detect_private_type(text),
                "fingerprint": _private_fingerprint(entry),
                "comment": _private_comment_from_pub(ssh_dir, name),
            })
            continue
        other.append(info)

    auth_path = ssh_dir / "authorized_keys"
    kh_path = ssh_dir / "known_hosts"
    auth = [_ssh_line_to_dict(x) for x in parse_authorized_keys(auth_path)]
    kh = [_ssh_line_to_dict(x) for x in parse_known_hosts(kh_path)]

    return {
        "private_keys": private,
        "public_keys": public,
        "authorized_keys": auth,
        "known_hosts": kh,
        "other": other,
    }


def _ssh_line_to_dict(line: SshLine) -> dict:
    out = {
        "idx": line.idx,
        "type": line.type,
        "comment": line.comment,
        "fingerprint": line.fingerprint,
        "masked": line.masked,
        "key_short": line.key_short,
    }
    if line.host is not None:
        out["host"] = line.host
    return out


def _detect_private_type(text: str) -> str | None:
    head = text.split("\n", 1)[0]
    if "OPENSSH" in head:
        return "openssh"
    if "RSA" in head:
        return "rsa"
    if "EC" in head:
        return "ec"
    if "DSA" in head:
        return "dsa"
    return None


def _private_fingerprint(private_path: Path) -> str | None:
    if not _ssh_keygen_available():
        return None
    try:
        proc = subprocess.run(
            ["ssh-keygen", "-lf", str(private_path)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    parts = proc.stdout.strip().split()
    if len(parts) < 2:
        return None
    return parts[1]


def _private_comment_from_pub(ssh_dir: Path, private_name: str) -> str | None:
    pub = ssh_dir / f"{private_name}.pub"
    if not pub.is_file():
        return None
    try:
        text = pub.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    parts = text.split()
    return " ".join(parts[2:]) if len(parts) >= 3 else None


def read_private_key(ssh_dir: Path, name: str) -> str:
    name = validate_ssh_key_name(name)
    private_path = ssh_dir / name
    if not private_path.is_file() or private_path.is_symlink():
        raise FileNotFoundError(f"private key not found: {name}")
    return private_path.read_text(encoding="utf-8", errors="replace")


__all__ = [
    "DEFAULT_DIR_MODE",
    "DEFAULT_PRIVATE_MODE",
    "DEFAULT_PUBLIC_MODE",
    "EnvLine",
    "GeneratedSshKey",
    "SecretFileInfo",
    "SshLine",
    "append_line",
    "delete_line",
    "delete_secret_file",
    "env_last_modified",
    "generate_ssh_key",
    "list_secret_files",
    "list_ssh_dir",
    "mask_value",
    "parse_authorized_keys",
    "parse_env",
    "parse_known_hosts",
    "read_private_key",
    "read_secret_file",
    "replace_line",
    "serialize_env",
    "ssh_key_fingerprint",
    "validate_env_key",
    "validate_secret_file_name",
    "validate_ssh_key_name",
    "validate_ssh_key_type",
    "write_env",
    "write_lines",
    "write_secret_file",
]
