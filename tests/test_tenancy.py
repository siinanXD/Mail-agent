"""Mandantentrennung: im Code (SQLite) und in der Datenbank (Postgres-RLS)."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.crypto import (
    DecryptionError,
    EncryptionNotConfiguredError,
    decrypt_secret,
    encrypt_secret,
    generate_key,
    hash_password,
    verify_password,
)
from app.database import repositories as repo
from app.database.connection import rls_status
from app.email.importer import import_directory
from app.tenancy import NoTenantError, bind_tenant, require_current_tenant, use_tenant
from tests.fakes import rule_based_extractor


def _mail(provider_id: str, subject: str = "Hallo") -> dict:
    return dict(
        provider_message_id=provider_id,
        sender="a@b.de",
        recipient="c@d.de",
        subject=subject,
        body="Text",
        received_at=datetime(2026, 9, 1, 10, 0),
        email_type="other",
    )


# ---------------------------------------------------------------- im Code


def test_mandanten_sehen_nur_ihre_eigenen_mails(session, other_session, sample_dir):
    import_directory(
        session, sample_dir, extractor=rule_based_extractor, embedder=lambda c: []
    )
    session.commit()

    assert len(repo.timeline_emails(session)) == 14
    assert repo.timeline_emails(other_session) == []
    assert repo.search_bookings(other_session) == []
    assert repo.list_units(other_session) == []
    assert repo.count_cancellations(other_session) == 0


def test_fremde_mail_ist_ueber_die_id_nicht_erreichbar(session, other_session):
    eigene = repo.upsert_email(session, **_mail("m-1"))
    session.commit()

    assert repo.get_email(session, eigene.id) is not None
    assert repo.get_email(other_session, eigene.id) is None


def test_gleiche_buchungsnummer_bei_zwei_mandanten(session, other_session):
    """Früher global eindeutig - jetzt je Mandant."""
    repo.upsert_booking(
        session, booking_reference="BK-1", guest_name="A", arrival_date=None, departure_date=None
    )
    session.commit()
    repo.upsert_booking(
        other_session, booking_reference="BK-1", guest_name="B", arrival_date=None, departure_date=None
    )
    other_session.commit()

    assert repo.get_booking_by_reference(session, "BK-1").guest_name == "A"
    assert repo.get_booking_by_reference(other_session, "BK-1").guest_name == "B"


def test_gleiches_objekt_ist_je_mandant_ein_eigenes(session, other_session):
    a = repo.get_or_create_unit(session, "Ferienwohnung Seeblick")
    session.commit()
    b = repo.get_or_create_unit(other_session, "FeWo Seeblick")
    other_session.commit()

    assert a.id != b.id
    assert b.tenant_id == 2


def test_ohne_mandant_wird_nichts_geschrieben(sqlite_engine):
    from sqlalchemy.orm import sessionmaker

    ungebunden = sessionmaker(bind=sqlite_engine)()
    with pytest.raises(NoTenantError):
        repo.upsert_email(ungebunden, **_mail("m-x"))
    ungebunden.close()


def test_kontext_setzt_und_raeumt_den_mandanten_auf():
    with pytest.raises(NoTenantError):
        require_current_tenant()
    with use_tenant(7):
        assert require_current_tenant() == 7
    with pytest.raises(NoTenantError):
        require_current_tenant()


# ---------------------------------------------------------------- in Postgres


def test_app_rolle_ist_kein_superuser(pg_engine):
    """Sonst waeren alle folgenden RLS-Tests wertlos."""
    status = rls_status(pg_engine)

    assert status["role"] == "mailagent_test_app"
    assert status["effective"] is True


def test_rls_ohne_mandant_keine_zeilen(pg_engine):
    with pg_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', '1', true)"))
        conn.execute(
            text(
                "INSERT INTO emails (tenant_id, provider_message_id, sender, recipient, "
                "subject, body, received_at, email_type, created_at) "
                "VALUES (1, 'rls-1', 'a', 'b', 'c', 'd', now(), 'other', now())"
            )
        )

    with pg_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM emails")).scalar() == 0

    with pg_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', '2', true)"))
        assert conn.execute(text("SELECT count(*) FROM emails")).scalar() == 0

    with pg_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', '1', true)"))
        assert conn.execute(text("SELECT count(*) FROM emails")).scalar() == 1


def test_rls_verbietet_schreiben_in_fremden_mandanten(pg_engine):
    with pytest.raises(DBAPIError, match="row-level security"):
        with pg_engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', '2', true)"))
            conn.execute(
                text(
                    "INSERT INTO emails (tenant_id, provider_message_id, sender, recipient, "
                    "subject, body, received_at, email_type, created_at) "
                    "VALUES (1, 'rls-fremd', 'a', 'b', 'c', 'd', now(), 'other', now())"
                )
            )


def test_rls_greift_auch_wenn_der_code_den_filter_vergisst(pg_engine):
    """Genau dafuer ist RLS da: eine rohe Abfrage ohne WHERE tenant_id."""
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=pg_engine)
    with bind_tenant(factory(), 1) as eins:
        repo.upsert_email(eins, **_mail("vergessen-1"))
        eins.commit()

    with bind_tenant(factory(), 2) as zwei:
        roh = zwei.execute(text("SELECT count(*) FROM emails")).scalar()
        assert roh == 0


def test_mandant_bleibt_ueber_commits_hinweg_gesetzt(pg_engine):
    """set_config gilt pro Transaktion - das after_begin-Event setzt ihn neu."""
    from sqlalchemy.orm import sessionmaker

    with bind_tenant(sessionmaker(bind=pg_engine)(), 1) as db:
        repo.upsert_email(db, **_mail("commit-1"))
        db.commit()
        repo.upsert_email(db, **_mail("commit-2"))  # neue Transaktion
        db.commit()
        assert len(repo.timeline_emails(db)) == 2


# ---------------------------------------------------------------- Kryptografie


def test_passwort_hash_verifiziert_und_ist_gesalzen():
    erster = hash_password("korrekt-pferd")
    zweiter = hash_password("korrekt-pferd")

    assert erster != zweiter  # anderes Salz
    assert verify_password("korrekt-pferd", erster)
    assert not verify_password("falsch", erster)
    assert not verify_password("korrekt-pferd", "kaputt$hash")


def test_postfach_passwort_ist_umkehrbar_verschluesselt(monkeypatch):
    from app import crypto

    schluessel = generate_key()
    monkeypatch.setattr(
        crypto, "get_settings", lambda: type("S", (), {"encryption_key": schluessel})()
    )
    token = encrypt_secret("imap-geheim")

    assert "imap-geheim" not in token
    assert decrypt_secret(token) == "imap-geheim"


def test_falscher_schluessel_wird_klar_gemeldet(monkeypatch):
    from app import crypto

    alt, neu = generate_key(), generate_key()
    monkeypatch.setattr(crypto, "get_settings", lambda: type("S", (), {"encryption_key": alt})())
    token = encrypt_secret("imap-geheim")
    monkeypatch.setattr(crypto, "get_settings", lambda: type("S", (), {"encryption_key": neu})())

    with pytest.raises(DecryptionError):
        decrypt_secret(token)


def test_ohne_schluessel_wird_nicht_im_klartext_gespeichert(monkeypatch):
    from app import crypto

    monkeypatch.setattr(crypto, "get_settings", lambda: type("S", (), {"encryption_key": ""})())
    with pytest.raises(EncryptionNotConfiguredError):
        encrypt_secret("imap-geheim")
