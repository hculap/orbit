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

SID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture(scope="session")
def app():
    return app_mod.create_app()


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=True)
