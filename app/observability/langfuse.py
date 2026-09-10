"""Langfuse-Tracing. Optional - die App startet auch ohne Credentials."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache
def get_callback_handler() -> Any | None:
    """Liefert den LangChain-CallbackHandler oder None, wenn nicht konfiguriert.

    Der Handler traced automatisch LLM-Calls, Tool-Calls samt Argumenten,
    die finale Antwort, Fehler, Latenzen und - soweit vom Provider geliefert -
    den Tokenverbrauch.
    """
    settings = get_settings()
    if not settings.langfuse_enabled:
        logger.info("Langfuse nicht konfiguriert - Tracing ist deaktiviert.")
        return None

    try:
        from langfuse import Langfuse
        from langfuse.langchain import CallbackHandler

        Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
        logger.info("Langfuse-Tracing aktiv (%s)", settings.langfuse_host)
        return CallbackHandler()
    except Exception:
        logger.exception("Langfuse konnte nicht initialisiert werden - ohne Tracing weiter.")
        return None


def run_config(thread_id: str, *, user_input: str) -> dict[str, Any]:
    """Baut die LangChain-RunnableConfig inkl. Langfuse-Metadaten."""
    handler = get_callback_handler()
    config: dict[str, Any] = {
        "run_name": "mail-agent",
        "metadata": {
            "thread_id": thread_id,
            "langfuse_session_id": thread_id,
            "user_input": user_input,
        },
        "tags": ["mail-agent"],
    }
    if handler is not None:
        config["callbacks"] = [handler]
    return config


def flush() -> None:
    """Sendet gepufferte Events - beim Shutdown aufrufen."""
    handler = get_callback_handler()
    if handler is None:
        return
    try:
        from langfuse import get_client

        get_client().flush()
    except Exception:
        logger.warning("Langfuse-Flush fehlgeschlagen.", exc_info=True)
