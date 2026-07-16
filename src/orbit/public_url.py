"""Public base URL for the dashboard, used to build notification deep-links.

Open-source deployments set ``DASHBOARD_PUBLIC_URL`` (e.g.
``https://orbit.example.com`` or a Tailscale MagicDNS name like
``http://my-box.tailnet.ts.net``). When it is unset, deep-links are omitted so
a notification still fires without a broken button pointing at someone else's
host — never hardcode a specific domain in call sites.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse


def public_base_url() -> str:
    """Return the configured public base URL without a trailing slash, or ``""``."""
    return os.environ.get("DASHBOARD_PUBLIC_URL", "").rstrip("/")


def public_link(path: str) -> str | None:
    """Build an absolute deep-link for ``path``, or ``None`` when no public URL is set.

    Callers pass the result straight to ``notify(click=...)`` /
    inline-keyboard ``url`` fields, which treat ``None`` as "omit the button".
    """
    base = public_base_url()
    if not base:
        return None
    return f"{base}/{path.lstrip('/')}"


def default_vapid_subject() -> str:
    """Neutral Web-Push VAPID ``sub`` claim derived from the public URL, or a placeholder.

    Overridable via ``ORBIT_VAPID_SUBJECT``. Web-Push only requires a valid
    ``mailto:``/``https:`` URI, so a host-derived or placeholder value is fine.
    """
    override = os.environ.get("ORBIT_VAPID_SUBJECT")
    if override:
        return override
    base = public_base_url()
    if base:
        host = urlparse(base).hostname
        if host:
            return f"mailto:admin@{host}"
    return "mailto:admin@localhost"
