"""Import nur aus dem Ordner des eigenen Mandanten (Codex-Review P1)."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import emails
from app.api.emails import ImportRequest, imports_dir_for, resolve_import_directory


@pytest.fixture
def import_root(tmp_path, monkeypatch):
    """Zwei Mandanten mit je einem Export-Unterordner, dazu Demo-Mails."""
    settings = type(
        "S", (), {"imports_dir": str(tmp_path / "imports"), "sample_emails_dir": str(tmp_path / "samples")}
    )()
    monkeypatch.setattr(emails, "get_settings", lambda: settings)
    for tenant in (1, 2):
        (tmp_path / "imports" / f"tenant-{tenant}" / "beds24").mkdir(parents=True)
    (tmp_path / "samples").mkdir()
    return tmp_path


def test_ohne_angabe_der_ganze_eigene_import_ordner(import_root):
    assert resolve_import_directory(None, 1) == (import_root / "imports" / "tenant-1").resolve()


def test_unterordner_im_eigenen_ordner(import_root):
    target = resolve_import_directory(ImportRequest(directory="beds24"), 1)

    assert target == (import_root / "imports" / "tenant-1" / "beds24").resolve()


@pytest.mark.parametrize(
    "directory",
    ["../tenant-2", "../tenant-2/beds24", "..", "../..", "beds24/../../tenant-2", "/etc", "C:\\Windows"],
)
def test_kein_weg_aus_dem_eigenen_ordner_heraus(import_root, directory):
    """Frueher reichte data/ - dann waeren die Exporte aller Mandanten erreichbar."""
    with pytest.raises(HTTPException) as info:
        resolve_import_directory(ImportRequest(directory=directory), 1)

    assert info.value.status_code in (400, 404)


def test_fremder_mandant_kommt_nicht_an_meine_exporte(import_root):
    with pytest.raises(HTTPException) as info:
        resolve_import_directory(ImportRequest(directory="../tenant-1/beds24"), 2)

    assert info.value.status_code == 400


def test_fehlender_ordner_nennt_keinen_serverpfad(import_root):
    with pytest.raises(HTTPException) as info:
        resolve_import_directory(ImportRequest(directory="gibt-es-nicht"), 1)

    assert info.value.status_code == 404
    assert str(import_root) not in info.value.detail
    assert "tenant-1/gibt-es-nicht" in info.value.detail


def test_demo_mails_nur_ausdruecklich(import_root):
    target = resolve_import_directory(ImportRequest(sample_data=True), 2)

    assert target == (import_root / "samples").resolve()


def test_demo_und_ordner_zugleich_sind_ungueltig(import_root):
    with pytest.raises(HTTPException) as info:
        resolve_import_directory(ImportRequest(sample_data=True, directory="beds24"), 1)

    assert info.value.status_code == 400


def test_mandanten_haben_getrennte_ordner():
    assert imports_dir_for(1) != imports_dir_for(2)
    assert imports_dir_for(1).name == "tenant-1"


def test_endpunkt_nutzt_den_ordner_des_angemeldeten_mandanten(import_root, monkeypatch):
    """Der Mandant kommt aus der Sitzung - nicht aus dem Request."""
    from fastapi.testclient import TestClient

    from app.api.auth import CurrentUser, require_user
    from app.email.importer import ImportResult
    from app.main import app

    gesehen: dict[str, Path] = {}

    def fake_import(session, directory, reprocess=False):
        gesehen["directory"] = Path(directory)
        return ImportResult()

    @contextmanager
    def fake_scope(tenant_id):
        yield None

    monkeypatch.setattr(emails, "import_directory", fake_import)
    monkeypatch.setattr(emails, "tenant_session", fake_scope)
    app.dependency_overrides[require_user] = lambda: CurrentUser(
        user_id=9, tenant_id=2, email="bernd@zweiter.de", tenant_name="Zweiter Mandant"
    )
    try:
        with TestClient(app) as client:
            eigener = client.post("/emails/import", json={"directory": "beds24"})
            fremder = client.post("/emails/import", json={"directory": "../tenant-1/beds24"})
    finally:
        app.dependency_overrides.clear()

    assert eigener.status_code == 200
    assert gesehen["directory"] == (import_root / "imports" / "tenant-2" / "beds24").resolve()
    assert fremder.status_code == 400