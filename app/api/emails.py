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
from app.tenancy import tenant_session

logger = logging.getLogger(__name__)
router = APIRouter(tags=["emails"])


class ImportRequest(BaseModel):
    directory: str | None = None
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


def _allowed_directory(requested: str | None) -> Path:
    """Nur Ordner unterhalb von data/ - kein Nutzer liest beliebige Serverpfade ein."""
    settings = get_settings()
    root = Path(settings.sample_emails_dir).resolve().parent
    target = Path(requested or settings.sample_emails_dir).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(
            status_code=400, detail="Import ist nur aus Ordnern unterhalb von data/ erlaubt."
        )
    return target


@router.post("/emails/import", response_model=ImportResult)
def import_emails(
    request: ImportRequest | None = None, user: CurrentUser = Depends(require_user)
) -> ImportResult:
    directory = _allowed_directory(request.directory if request else None)
    try:
        with tenant_session(user.tenant_id) as session:
            return import_directory(
                session, directory, reprocess=bool(request and request.reprocess)
            )
    except LLMNotConfiguredError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        logger.exception("Import fehlgeschlagen")
        raise HTTPException(status_code=500, detail=f"Import-Fehler: {error}") from error


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
