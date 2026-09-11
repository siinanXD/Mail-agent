"""Putzplan erzeugen und herunterladen - je Mandant.

Die Dateien liegen in einem Unterordner pro Mandant. Ein gemeinsamer Ordner
waere ein Leck: Mandant A koennte den Plan von B einfach ueber den Dateinamen
abrufen, der sich aus Kalenderwoche und Jahr ergibt.
"""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from app.api.auth import CurrentUser, require_user
from app.reports.cleaning_plan import export_cleaning_plan, exports_dir_for
from app.tenancy import tenant_session

logger = logging.getLogger(__name__)
router = APIRouter(tags=["reports"])

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/reports/cleaning-plan")
def cleaning_plan(
    week: int = Query(..., ge=1, le=53),
    year: int | None = Query(default=None),
    user: CurrentUser = Depends(require_user),
) -> FileResponse:
    """Erzeugt den Putzplan der Kalenderwoche und liefert die Excel-Datei aus."""
    try:
        with tenant_session(user.tenant_id) as session:
            _, path = export_cleaning_plan(
                session, year=year or date.today().year, week=week
            )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        # Details nur ins Log: Fehlertexte enthalten Pfade, SQL oder Parameterwerte.
        logger.exception("Putzplan konnte nicht erzeugt werden")
        raise HTTPException(
            status_code=500,
            detail="Putzplan konnte nicht erzeugt werden. Details stehen im Server-Log.",
        ) from error

    return FileResponse(path, filename=path.name, media_type=XLSX_MEDIA_TYPE)


@router.get("/reports/cleaning-plan/{filename}")
def download_cleaning_plan(
    filename: str, user: CurrentUser = Depends(require_user)
) -> FileResponse:
    """Laedt einen bereits erzeugten Plan des eigenen Mandanten herunter."""
    directory = exports_dir_for(user.tenant_id).resolve()
    path = (directory / filename).resolve()

    # Kein Ausbruch aus dem eigenen Ordner - weder per "../" in fremde
    # Mandantenordner noch sonst irgendwohin.
    if not path.is_file() or path.parent != directory:
        raise HTTPException(status_code=404, detail="Datei nicht gefunden")

    return FileResponse(path, filename=path.name, media_type=XLSX_MEDIA_TYPE)
