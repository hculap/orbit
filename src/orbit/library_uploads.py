"""Library uploads — safe extraction of starter zips into Areas/Projects.

Hardened against:
- absolute paths inside the archive
- ``..`` traversal segments
- symlink entries (Unix mode bits in ``zinfo.external_attr``)
- zip-bomb (entry count, total declared size)

Strategy:
1. Walk ``ZipFile.infolist()`` validating every entry first.
2. Sum declared ``file_size`` and assert under ``MAX_EXTRACTED_BYTES``.
3. Only after all entries pass, extract each via ``ZipFile.extract()``.
"""
from __future__ import annotations
import io
import zipfile
from pathlib import Path

MAX_TOTAL_BYTES = 100 * 1024 * 1024     # 100 MB max compressed (input)
MAX_ENTRIES = 1000
MAX_EXTRACTED_BYTES = 500 * 1024 * 1024  # 500 MB max uncompressed total

_SYMLINK_MODE = 0o120000
_TYPE_MASK = 0o170000


def _is_symlink_entry(zinfo: zipfile.ZipInfo) -> bool:
    """Detect symlink via Unix mode bits in external_attr."""
    mode = (zinfo.external_attr >> 16) & _TYPE_MASK
    return mode == _SYMLINK_MODE


def _normalize_name(name: str) -> str:
    """Reject absolute, return forward-slash form."""
    n = name.replace("\\", "/")
    if n.startswith("/"):
        raise ValueError(f"absolute path in archive: {name!r}")
    return n


def _validate_entry(zinfo: zipfile.ZipInfo, target_resolved: Path) -> None:
    """Raise ValueError on any unsafe property."""
    name = _normalize_name(zinfo.filename)
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError(f"path traversal in archive entry: {zinfo.filename!r}")
    if _is_symlink_entry(zinfo):
        raise ValueError(f"symlink not allowed in archive: {zinfo.filename!r}")
    # Resolve the would-be destination and assert it stays inside target
    candidate = (target_resolved / Path(*parts)).resolve()
    if candidate != target_resolved and target_resolved not in candidate.parents:
        raise ValueError(f"entry escapes target: {zinfo.filename!r}")


def extract_starter_zip(zip_bytes: bytes, target_path: Path) -> dict:
    """Extract a zip into ``target_path``. Returns counts on success.

    Raises ``ValueError`` on any safety violation.
    """
    if not isinstance(zip_bytes, (bytes, bytearray)):
        raise ValueError("zip_bytes must be bytes")
    if len(zip_bytes) > MAX_TOTAL_BYTES:
        raise ValueError(f"zip exceeds {MAX_TOTAL_BYTES} bytes")
    if not target_path.is_dir():
        raise ValueError(f"target is not a directory: {target_path}")

    target_resolved = target_path.resolve()

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise ValueError(f"invalid zip: {e}") from e

    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ValueError(f"zip has {len(infos)} entries (max {MAX_ENTRIES})")

        # Validation pass — fail before any write
        total = 0
        for zinfo in infos:
            _validate_entry(zinfo, target_resolved)
            total += int(zinfo.file_size)
            if total > MAX_EXTRACTED_BYTES:
                raise ValueError(
                    f"declared size {total} exceeds cap {MAX_EXTRACTED_BYTES}"
                )

        # Extraction pass — per-entry, never extractall
        for zinfo in infos:
            # Skip pure directory entries (zipfile creates them automatically
            # on file extract anyway, but keep them as no-ops to avoid races).
            zf.extract(zinfo, path=str(target_resolved))

    return {"ok": True, "entries": len(infos), "total_bytes": total}
