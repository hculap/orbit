"""Orchestrator push notifications — VAPID key store + subscription registry + sender.

Lifecycle:
- Frontend prompts the user for Notification permission. On grant, it calls
  PushManager.subscribe (using the public VAPID key fetched from
  ``GET /api/notifications/vapid-key``) and POSTs the resulting subscription
  to ``/api/notifications/subscribe`` keyed by a stable per-device UUID.
- Backend persists the subscription to ``~/.orchestrator/push-subscriptions.json``
  (atomic JSON write, asyncio-locked). VAPID keys live alongside in
  ``vapid_keys.json`` (0600), generated on first call to ``ensure_vapid_keys``.
- When the orchestrator's ``claude -p`` subprocess finishes a turn, the runner
  calls ``send_to_all`` with a title/body/data payload. We push to every
  subscription via pywebpush; 404/410 endpoints are pruned. The frontend
  service worker decides whether to actually display the notification (e.g. it
  suppresses if the user is currently focused on the orchestrator section).

All filesystem mutation goes through tempfile + os.replace; concurrent
mutations are gated by a module-level asyncio.Lock. Failures are logged to
stderr with a ``[push]`` prefix and never raised — push is best-effort.
"""
from __future__ import annotations
import asyncio
import base64
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization

from .public_url import default_vapid_subject

# ── paths / constants ──────────────────────────────────────────────

NOTIF_ROOT: Path = Path.home() / ".orchestrator"
SUBS_PATH: Path = NOTIF_ROOT / "push-subscriptions.json"
VAPID_PATH: Path = NOTIF_ROOT / "vapid_keys.json"
VAPID_SUBJECT: str = default_vapid_subject()

DEVICE_ID_RE: re.Pattern[str] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

_lock = asyncio.Lock()
_vapid_cache: dict[str, str] | None = None


# ── helpers ────────────────────────────────────────────────────────


def _warn(msg: str) -> None:
    print(f"[push] {msg}", file=sys.stderr)


def _ensure_dir() -> None:
    NOTIF_ROOT.mkdir(parents=True, exist_ok=True)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def _atomic_write_json(path: Path, payload: object, *, mode: int | None = None) -> None:
    _ensure_dir()
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        if mode is not None:
            try:
                os.chmod(tmp_path, mode)
            except OSError as exc:
                _warn(f"chmod {tmp_path}: {exc}")
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp.close()
        except Exception:
            pass
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


# ── VAPID keys ─────────────────────────────────────────────────────


def _generate_vapid_keys() -> dict[str, str]:
    """Generate a fresh ECDSA P-256 keypair and serialize to base64url."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_int = private_key.private_numbers().private_value
    private_bytes = private_int.to_bytes(32, "big")
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return {
        "public_key": _b64url(public_bytes),
        "private_key": _b64url(private_bytes),
    }


def _load_vapid_from_disk() -> dict[str, str] | None:
    if not VAPID_PATH.exists():
        return None
    try:
        with VAPID_PATH.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _warn(f"corrupt {VAPID_PATH.name}: {exc}; will regenerate")
        return None
    if not isinstance(payload, dict):
        _warn(f"{VAPID_PATH.name} is not an object; will regenerate")
        return None
    pub = payload.get("public_key")
    priv = payload.get("private_key")
    if not isinstance(pub, str) or not isinstance(priv, str):
        return None
    return {"public_key": pub, "private_key": priv}


def ensure_vapid_keys() -> dict[str, str]:
    """Load existing VAPID keys, or generate + persist a fresh pair on first use.

    Returns ``{"public_key", "private_key"}`` (both base64url, no padding).
    The on-disk file is created with 0600 perms. Idempotent — subsequent calls
    return the cached pair without touching disk.
    """
    global _vapid_cache
    if _vapid_cache is not None:
        return dict(_vapid_cache)
    _ensure_dir()
    keys = _load_vapid_from_disk()
    if keys is None:
        keys = _generate_vapid_keys()
        try:
            _atomic_write_json(VAPID_PATH, keys, mode=0o600)
        except OSError as exc:
            _warn(f"failed to persist VAPID keys: {exc}")
    else:
        # Best-effort tighten perms on existing file.
        try:
            os.chmod(VAPID_PATH, 0o600)
        except OSError:
            pass
    _vapid_cache = dict(keys)
    return dict(keys)


def get_public_key() -> str:
    """Return the VAPID public key (base64url) for the frontend."""
    return ensure_vapid_keys()["public_key"]


def _vapid_private_pem() -> str:
    """Serialize the cached private key as PEM for pywebpush."""
    keys = ensure_vapid_keys()
    private_int = int.from_bytes(_b64url_decode(keys["private_key"]), "big")
    private_key = ec.derive_private_key(private_int, ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("ascii")


# ── subscription store ─────────────────────────────────────────────


def _load_subs_from_disk() -> dict[str, dict]:
    if not SUBS_PATH.exists():
        return {}
    try:
        with SUBS_PATH.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _warn(f"corrupt {SUBS_PATH.name}: {exc}; treating as empty")
        return {}
    if not isinstance(payload, dict):
        _warn(f"{SUBS_PATH.name} is not an object; treating as empty")
        return {}
    cleaned: dict[str, dict] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not DEVICE_ID_RE.match(key):
            continue
        if not isinstance(value, dict):
            continue
        endpoint = value.get("endpoint")
        keys = value.get("keys")
        if not isinstance(endpoint, str) or not isinstance(keys, dict):
            continue
        p256dh = keys.get("p256dh")
        auth = keys.get("auth")
        if not isinstance(p256dh, str) or not isinstance(auth, str):
            continue
        cleaned[key] = {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}
    return cleaned


async def add_subscription(device_id: str, subscription: dict) -> None:
    """Persist or update one push subscription. Atomic write."""
    if not isinstance(device_id, str) or not DEVICE_ID_RE.match(device_id):
        raise ValueError("invalid device_id")
    if not isinstance(subscription, dict):
        raise ValueError("subscription must be an object")
    endpoint = subscription.get("endpoint")
    keys = subscription.get("keys")
    if not isinstance(endpoint, str):
        raise ValueError("subscription.endpoint required")
    if not isinstance(keys, dict):
        raise ValueError("subscription.keys required")
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not isinstance(p256dh, str) or not isinstance(auth, str):
        raise ValueError("subscription.keys.{p256dh,auth} required")

    async with _lock:
        data = await asyncio.to_thread(_load_subs_from_disk)
        new_data = {
            **data,
            device_id: {
                "endpoint": endpoint,
                "keys": {"p256dh": p256dh, "auth": auth},
            },
        }
        try:
            await asyncio.to_thread(_atomic_write_json, SUBS_PATH, new_data)
        except OSError as exc:
            _warn(f"failed to persist subscription for {device_id}: {exc}")


async def remove_subscription(device_id: str) -> None:
    """Remove one device's subscription. Idempotent."""
    if not isinstance(device_id, str) or not DEVICE_ID_RE.match(device_id):
        return
    async with _lock:
        data = await asyncio.to_thread(_load_subs_from_disk)
        if device_id not in data:
            return
        new_data = {k: v for k, v in data.items() if k != device_id}
        try:
            await asyncio.to_thread(_atomic_write_json, SUBS_PATH, new_data)
        except OSError as exc:
            _warn(f"failed to remove subscription for {device_id}: {exc}")


async def list_subscriptions() -> list[dict]:
    """Return all stored subscriptions, each merged with its device_id."""
    async with _lock:
        data = await asyncio.to_thread(_load_subs_from_disk)
    return [
        {"device_id": device_id, **payload}
        for device_id, payload in data.items()
    ]


# ── push sender ────────────────────────────────────────────────────


async def send_to_all(title: str, body: str, data: dict | None = None) -> None:
    """Push to every registered subscription. Best-effort — never raises.

    Dead endpoints (HTTP 404 / 410) are pruned via ``remove_subscription``.
    All other failures are logged to stderr but otherwise ignored. Each
    push is dispatched in a thread so we don't block the event loop on
    the synchronous pywebpush HTTP call.
    """
    try:
        from pywebpush import WebPushException, webpush
    except Exception as exc:  # noqa: BLE001 — missing dep should never crash a turn
        _warn(f"pywebpush import failed: {exc}")
        return

    try:
        ensure_vapid_keys()
        private_pem = _vapid_private_pem()
    except Exception as exc:  # noqa: BLE001
        _warn(f"VAPID key load failed: {exc}")
        return

    subs = await list_subscriptions()
    if not subs:
        return

    payload = json.dumps(
        {"title": title, "body": body, "data": data or {}},
        ensure_ascii=False,
    )

    dead: list[str] = []
    for sub in subs:
        device_id = sub["device_id"]
        sub_info = {"endpoint": sub["endpoint"], "keys": sub["keys"]}
        vapid_claims: dict[str, Any] = {"sub": VAPID_SUBJECT}
        try:
            await asyncio.to_thread(
                webpush,
                subscription_info=sub_info,
                data=payload,
                vapid_private_key=private_pem,
                vapid_claims=vapid_claims,
            )
        except WebPushException as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            if status in (404, 410):
                dead.append(device_id)
                _warn(f"endpoint gone (status={status}); pruning {device_id}")
            else:
                _warn(f"webpush failed for {device_id}: status={status} err={exc}")
        except Exception as exc:  # noqa: BLE001 — never let push raise
            _warn(f"webpush unexpected error for {device_id}: {exc}")

    for device_id in dead:
        try:
            await remove_subscription(device_id)
        except Exception as exc:  # noqa: BLE001
            _warn(f"prune failed for {device_id}: {exc}")
