"""Tools: Objekte auflisten und Umbuchungen finden."""

from __future__ import annotations

from datetime import date

from langchain_core.tools import tool

from app.agent.tools.common import to_json
from app.database import repositories as repo
from app.tenancy import tenant_session


@tool
def list_units() -> str:
    """Listet alle bekannten Objekte (Ferienwohnungen/Haeuser) mit Buchungszahl.

    Nuetzlich, wenn der Nutzer nach "welche Wohnungen gibt es" fragt oder wenn
    du wissen musst, wie ein Objekt in der Datenbank genau heisst.
    """
    with tenant_session() as session:
        units = repo.list_units(session)
        return to_json(
            {
                "count": len(units),
                "units": [
                    {
                        "unit_id": unit.id,
                        "name": unit.name,
                        "bookings": len(unit.bookings),
                    }
                    for unit in units
                ],
            }
        )


@tool
def search_booking_changes(
    booking_reference: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 50,
) -> str:
    """Findet Umbuchungen (geaenderte An-/Abreise oder Objektwechsel).

    Zeitraumfilter beziehen sich auf das Datum der Aenderungsmail. Zeigt je
    Aenderung das betroffene Feld sowie alten und neuen Wert.
    """
    with tenant_session() as session:
        changes = repo.search_booking_changes(
            session,
            booking_reference=booking_reference,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )
        return to_json(
            {
                "count": len(changes),
                "changes": [
                    {
                        "booking_reference": change.booking.booking_reference,
                        "guest_name": change.booking.guest_name,
                        "field": change.field,
                        "old_value": change.old_value,
                        "new_value": change.new_value,
                        "changed_at": change.changed_at,
                        "source_email_id": change.source_email_id,
                    }
                    for change in changes
                ],
            }
        )
