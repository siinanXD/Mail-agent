"""Import und manueller Postfach-Abruf - immer in den Mandanten des Nutzers."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.auth import CurrentUser, require_user
from app.config import get_settings
from app.email import watcher
from app.email.imap_client import ImapNotConfiguredError
from app.email.importer import ImportResult, import_directory
from app.llm.client import LLMNotConfiguredError
from app.staff import dispatcher
from app.tenancy import tenant_session

logger = logging.getLogger(__name__)
router = APIRouter(tags=["emails"])


class ImportRequest(BaseModel):
    #: Unterordner im Import-Ordner des eigenen Mandanten
    #: (``data/imports/tenant-<id>/<directory>``). Leer = der ganze Ordner.
    directory: str | None = None
    #: Statt eigener Dateien die mitgelieferten, erfundenen Demo-Mails importieren.
    sample_data: bool = False
    #: Bereits bekannte Mails erneut durch die Extraktion schicken - noetig,
    #: wenn sich Extraktionslogik oder Prompt geaendert haben. Kostet LLM-Calls.
    reprocess: bool = False


class MailboxPollResult(BaseModel):
    mailbox: str
    imported: int = 0
    skipped: int = 0
    failed: list[str] = []
    error: str | None = None


class PollResponse(BaseModel):
    mailboxes: list[MailboxPollResult]


def imports_dir_for(tenant_id: int) -> Path:
    """Eigener Import-Ordner je Mandant."""
    return Path(get_settings().imports_dir) / f"tenant-{tenant_id}"


def resolve_import_directory(request: ImportRequest | None, tenant_id: int) -> Path:
    """Ordner, aus dem ein Nutzer importieren darf.

    Erlaubt sind nur der Import-Ordner des eigenen Mandanten (samt Unterordnern)
    und ausdruecklich die mitgelieferten Demo-Mails. Frueher reichte "irgendwo
    unter data/" - dann haette jeder Mandant die Exporte aller anderen in sein
    Konto kopieren koennen. RLS schuetzt davor nicht: Die Kopien bekommen die
    tenant_id des Anfragenden.
    """
    settings = get_settings()
    requested = ((request.directory if request else None) or "").strip()

    if request is not None and request.sample_data:
        if requested:
            raise HTTPException(
                status_code=400, detail="directory und sample_data schliessen sich aus."
            )
        return Path(settings.sample_emails_dir).resolve()

    root = imports_dir_for(tenant_id).resolve()
    target = (root / requested).resolve() if requested else root
    if target != root and root not in target.parents:
        raise HTTPException(
            status_code=400, detail="Import ist nur aus dem eigenen Import-Ordner erlaubt."
        )
    if not target.is_dir():
        # Bewusst ohne absoluten Serverpfad in der Antwort.
        shown = f"tenant-{tenant_id}/{requested}".rstrip("/")
        raise HTTPException(status_code=404, detail=f"Import-Ordner nicht gefunden: {shown}")
    return target


@router.post("/emails/import", response_model=ImportResult)
def import_emails(
    request: ImportRequest | None = None, user: CurrentUser = Depends(require_user)
) -> ImportResult:
    directory = resolve_import_directory(request, user.tenant_id)
    try:
        with tenant_session(user.tenant_id) as session:
            result = import_directory(
                session, directory, reprocess=bool(request and request.reprocess)
            )
    except LLMNotConfiguredError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except FileNotFoundError as error:
        # Die Meldung enthielte den absoluten Serverpfad.
        raise HTTPException(status_code=400, detail="Import-Ordner nicht gefunden.") from error
    except Exception as error:
        # Details nur ins Log: Datenbankfehler enthalten SQL und Parameterwerte.
        logger.exception("Import fehlgeschlagen")
        raise HTTPException(
            status_code=500, detail="Import fehlgeschlagen. Details stehen im Server-Log."
        ) from error

    # Importierte Stornos und Umbuchungen koennen verschickte Putzplaene betreffen.
    dispatcher.notify_changes_safely([user.tenant_id])
    return result


@router.post("/emails/poll", response_model=PollResponse)
def poll_mailbox(user: CurrentUser = Depends(require_user)) -> PollResponse:
    """Ruft sofort die Postfaecher des eigenen Mandanten ab."""
    try:
        outcomes = watcher.poll_tenant(user.tenant_id)
    except ImapNotConfiguredError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error

    return PollResponse(
        mailboxes=[
            MailboxPollResult(
                mailbox=outcome.label,
                imported=outcome.result.imported if outcome.result else 0,
                skipped=outcome.result.skipped if outcome.result else 0,
                failed=outcome.result.failed if outcome.result else [],
                error=outcome.error,
            )
            for outcome in outcomes
        ]
    )
