"""Shared fixtures — the project's first FastAPI TestClient harness.

The ``client`` fixture deliberately does NOT enter the app's lifespan
(``with TestClient(...)``): startup wires the cron scheduler, reminder tick and
pool prewarm, none of which the route tests need and all of which would leak
threads / touch the real ``~/.orchestrator`` state. Route handlers work fine
without lifespan; tests that need the event hub use a fresh ``SessionEventHub``
in-process instead of the app singleton.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from orbit import app as app_mod
from orbit import notify_topics as _notify_topics

SID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture(autouse=True)
def _isolate_notify_routes(monkeypatch, tmp_path):
    """No test may read the developer's own ``~/.orchestrator/notify_routes.json``.

    ``notify.notify()`` resolves its destination through :mod:`notify_topics`,
    so once a real routing file exists on the box every notify test starts
    asserting against that machine's live group id — which is exactly how
    ``test_notify_happy_path`` began expecting ``12345`` and receiving a real
    supergroup. Autouse + tmp path keeps the suite hermetic; tests that care
    about the file patch this same attribute again with their own path.
    """
    monkeypatch.setattr(_notify_topics, "ROUTES_PATH", tmp_path / "notify_routes.json")
    _notify_topics.invalidate()
    yield
    _notify_topics.invalidate()


@pytest.fixture(scope="session")
def app():
    return app_mod.create_app()


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=True)
