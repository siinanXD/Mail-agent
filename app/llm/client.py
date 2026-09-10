"""Duenne LLM-Schicht.

Konzeptioneller Ausgangspunkt war ein einzelner OpenAI-Call:

    from openai import OpenAI
    client = OpenAI()
    response = client.responses.create(model=..., input="Hallo")
    print(response.output_text)

Genau das steckt jetzt hinter ``complete()``. Zusaetzlich stellt dieses Modul
das Chat-Modell fuer den LangChain-Agenten und die Embedding-Funktion bereit.
Der Rest der Anwendung importiert kein OpenAI-SDK direkt.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from langchain_openai import ChatOpenAI
from openai import OpenAI

from app.config import get_settings

logger = logging.getLogger(__name__)


class LLMNotConfiguredError(RuntimeError):
    """Wird geworfen, wenn kein OPENAI_API_KEY gesetzt ist."""


def _require_api_key() -> str:
    key = get_settings().openai_api_key
    if not key:
        raise LLMNotConfiguredError(
            "OPENAI_API_KEY ist nicht gesetzt. Bitte in .env hinterlegen."
        )
    return key


@lru_cache
def _openai_client() -> OpenAI:
    return OpenAI(api_key=_require_api_key())


@lru_cache
def get_chat_model(temperature: float = 0.0) -> ChatOpenAI:
    """Chat-Modell fuer den Agenten (LangChain)."""
    settings = get_settings()
    return ChatOpenAI(
        model=settings.openai_model,
        api_key=_require_api_key(),
        temperature=temperature,
        timeout=60,
    )


def complete(prompt: str) -> str:
    """Einfachster moeglicher LLM-Call - nuetzlich fuer Smoke-Tests."""
    settings = get_settings()
    response = _openai_client().responses.create(
        model=settings.openai_model, input=prompt
    )
    return response.output_text


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Erzeugt Embeddings fuer eine Liste von Texten."""
    if not texts:
        return []
    settings = get_settings()
    response = _openai_client().embeddings.create(
        model=settings.openai_embedding_model, input=texts
    )
    return [item.embedding for item in response.data]


def embed_query(text: str) -> list[float]:
    return embed_texts([text])[0]
