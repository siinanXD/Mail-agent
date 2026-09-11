"""Interne Fehlertexte bleiben im Server-Log und gehen nicht an den Client."""

from __future__ import annotations

import importlib
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.api.auth import CurrentUser, require_user
from app.main import app

#: So koennte ein echter Datenbank- oder Treiberfehler aussehen.
GEHEIM = "password=streng-geheim host=10.0.0.5 SELECT * FROM bookings WHERE guest_name='Ida Nord'"


@pytest.fixture
def client():
    app.dependency_overrides[require_user] = lambda: CurrentUser(
        user_id=1, tenant_id=1, email="anna@standard.de", tenant_name="Standard"
    )
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(require_user, None)


@contextmanager
def _ohne_datenbank(tenant_id=None):
    yield None


def _kaputt(*args, **kwargs):
    raise RuntimeError(GEHEIM)


def test_chatfehler_verraet_keine_interna(client, monkeypatch):
    class KaputterAgent:
        def ask(self, thread_id, message):
            _kaputt()

    monkeypatch.setattr(importlib.import_module("app.api.chat"), "get_agent", KaputterAgent)

    response = client.post("/api/chat", json={"thread_id": "t", "message": "hallo"})

    assert response.status_code == 500
    assert "geheim" not in response.text
    assert "SELECT" not in response.text


def test_importfehler_verraet_keine_interna(client, monkeypatch, tmp_path):
    emails = importlib.import_module("app.api.emails")
    monkeypatch.setattr(emails, "resolve_import_directory", lambda request, tenant_id: tmp_path)
    monkeypatch.setattr(emails, "tenant_session", _ohne_datenbank)
    monkeypatch.setattr(emails, "import_directory", _kaputt)

    response = client.post("/emails/import", json={})

    assert response.status_code == 500
    assert "geheim" not in response.text
    assert "SELECT" not in response.text


def test_putzplanfehler_verraet_keine_interna(client, monkeypatch):
    reports = importlib.import_module("app.api.reports")
    monkeypatch.setattr(reports, "tenant_session", _ohne_datenbank)
    monkeypatch.setattr(reports, "export_cleaning_plan", _kaputt)

    response = client.get("/reports/cleaning-plan", params={"week": 37, "year": 2026})

    assert response.status_code == 500
    assert "geheim" not in response.text
    assert "SELECT" not in response.text
