"""Alle Agent-Tools an einer Stelle."""

from langchain_core.tools import BaseTool

from app.agent.tools.count_cancellations import count_cancellations
from app.agent.tools.create_cleaning_plan import create_cleaning_plan
from app.agent.tools.get_email import get_email
from app.agent.tools.knowledge_search import knowledge_search
from app.agent.tools.search_bookings import search_bookings
from app.agent.tools.search_cancellations import search_cancellations
from app.agent.tools.search_emails import search_emails
from app.agent.tools.search_units import list_units, search_booking_changes

ALL_TOOLS: list[BaseTool] = [
    search_emails,
    get_email,
    search_bookings,
    search_cancellations,
    count_cancellations,
    search_booking_changes,
    list_units,
    knowledge_search,
    create_cleaning_plan,
]

__all__ = [
    "ALL_TOOLS",
    "count_cancellations",
    "create_cleaning_plan",
    "get_email",
    "knowledge_search",
    "list_units",
    "search_booking_changes",
    "search_bookings",
    "search_cancellations",
    "search_emails",
]
