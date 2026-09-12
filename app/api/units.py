"""Wohnungsprofile - je Mandant.

Die Objekte selbst entstehen automatisch aus den Mails (``app.units``). Hier
pflegt der Kunde das Profil: Beschreibung, Hausregeln, Groesse, Reinigungsfenster,
Adresse und Zugang.

Zugangsdaten (Schluessel, Codes, WLAN) werden wie die Postfach-Passwoerter mit
``ENCRYPTION_KEY`` verschluesselt gespeichert. Ohne Schluessel wird das Feld
abgelehnt, statt es im Klartext abzulegen - der Rest des Profils laesst sich
trotzdem speichern.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.auth import CurrentUser, require_user
from app.crypto import (
    DecryptionError,
    EncryptionNotConfiguredError,
    decrypt_secret,
    encrypt_secret,
)
from app.database import repositories as repo
from app.tenancy import tenant_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["wohnungen"])


class StaffBrief(BaseModel):
    id: int
    name: str


class UnitOut(BaseModel):
    id: int
    name: str
    description: str | None = None
    house_rules: str | None = None
    rooms: int | None = None
    beds: int | None = None
    size_sqm: int | None = None
    max_guests: int | None = None
    cleaning_window: str | None = None
    address: str | None = None
    floor: str | None = None
    #: Entschluesselt; None, wenn nichts hinterlegt ist oder der Schluessel nicht passt.
    access: str | None = None
    #: False, wenn etwas hinterlegt ist, sich aber nicht entschluesseln laesst.
    access_readable: bool = True
    #: Aktive Mitarbeiter, die fuer diese Wohnung zustaendig sind.
    staff: list[StaffBrief] = []


class UnitListResponse(BaseModel):
    units: list[UnitOut]
    #: False, wenn ENCRYPTION_KEY fehlt - dann laesst sich kein Zugang speichern.
    encryption_configured: bool


class UnitProfileRequest(BaseModel):
    description: str | None = Field(default=None, max_length=4000)
    house_rules: str | None = Field(default=None, max_length=4000)
    rooms: int | None = Field(default=None, ge=0, le=99)
    beds: int | None = Field(default=None, ge=0, le=99)
    size_sqm: int | None = Field(default=None, ge=0, le=9999)
    max_guests: int | None = Field(default=None, ge=0, le=99)
    cleaning_window: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=255)
    floor: str | None = Field(default=None, max_length=64)
    #: Schluessel, Codes, WLAN. Leer loescht den hinterlegten Zugang.
    access: str | None = Field(default=None, max_length=2000)


def _to_out(unit, staff: list) -> UnitOut:
    access, readable = None, True
    if unit.access_encrypted:
        try:
            access = decrypt_secret(unit.access_encrypted)
        except (DecryptionError, EncryptionNotConfiguredError):
            # Falscher oder fehlender Schluessel: nichts anzeigen, aber sagen, dass etwas da ist.
            readable = False
            logger.warning("Zugangsdaten von Objekt %s nicht entschluesselbar", unit.id)
    return UnitOut(
        id=unit.id,
        name=unit.name,
        description=unit.description,
        house_rules=unit.house_rules,
        rooms=unit.rooms,
        beds=unit.beds,
        size_sqm=unit.size_sqm,
        max_guests=unit.max_guests,
        cleaning_window=unit.cleaning_window,
        address=unit.address,
        floor=unit.floor,
        access=access,
        access_readable=readable,
        staff=[StaffBrief(id=member.id, name=member.name) for member in staff],
    )


def _encryption_available() -> bool:
    from app.config import get_settings

    return bool(get_settings().encryption_key)


@router.get("/units", response_model=UnitListResponse)
def list_units(user: CurrentUser = Depends(require_user)) -> UnitListResponse:
    with tenant_session(user.tenant_id) as session:
        staff = repo.staff_by_unit(session)
        return UnitListResponse(
            units=[
                _to_out(unit, staff.get(unit.id, [])) for unit in repo.list_units(session)
            ],
            encryption_configured=_encryption_available(),
        )


@router.put("/units/{unit_id}", response_model=UnitOut)
def update_unit(
    unit_id: int, request: UnitProfileRequest, user: CurrentUser = Depends(require_user)
) -> UnitOut:
    """Speichert das Profil. Der Objektname bleibt unberuehrt - er kommt aus den Mails."""
    fields = request.model_dump(exclude={"access"})
    # Drei Faelle, die auseinandergehalten werden muessen: Feld nicht
    # mitgeschickt (unveraendert lassen), leer (loeschen), Text (ersetzen).
    # Vorher galt "nicht mitgeschickt" als "loeschen" - und weil die Oberflaeche
    # bei nicht entschluesselbaren Zugangsdaten ein leeres Feld zeigt, loeschte
    # jedes Speichern anderer Angaben stillschweigend den verschluesselten Wert.
    access = None if request.access is None else request.access.strip()

    with tenant_session(user.tenant_id) as session:
        unit = repo.get_unit(session, unit_id)
        if unit is None:
            # 404 statt 403: verraet nicht, dass es die ID bei einem anderen Mandanten gibt.
            raise HTTPException(status_code=404, detail="Wohnung nicht gefunden")

        if access:
            try:
                verschluesselt = encrypt_secret(access)
            except EncryptionNotConfiguredError as error:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "Zugangsdaten brauchen einen ENCRYPTION_KEY. Ohne Schlüssel werden "
                        "sie nicht gespeichert – erzeugen mit: python -m app.admin generate-key"
                    ),
                ) from error
        elif access is None:
            verschluesselt = unit.access_encrypted  # unveraendert
        else:
            verschluesselt = None  # ausdruecklich geleert

        repo.update_unit_profile(session, unit, access_encrypted=verschluesselt, **fields)
        staff = repo.staff_by_unit(session)
        return _to_out(unit, staff.get(unit.id, []))
