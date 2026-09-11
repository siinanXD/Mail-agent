"""Chat-Endpunkte - beide hinter der Anmeldung.

``/chat`` und ``/api/chat`` sind derselbe Agent. Im Mehrmandantenbetrieb kann
keiner davon offen sein: ohne Anmeldung gibt es keinen Mandanten, dessen Daten
der Agent befragen duerfte. Fuer Skripte: erst ``/api/login``, dann das Cookie
mitschicken.

Das Gespraechsgedaechtnis ist nach Mandant getrennt. Ohne das koennte ein
Mandant mit derselben ``thread_id`` den Verlauf eines anderen fortsetzen.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.agent.agent import get_agent
from app.api.auth import CurrentUser, require_user
from app.llm.client import LLMNotConfiguredError
from app.memory.memory import default_memory
from app.tenancy import use_tenant

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])
secure_router = APIRouter(prefix="/api", tags=["chat"])


class ChatRequest(BaseModel):
    thread_id: str = Field(default="default", description="Konversations-ID")
    message: str = Field(min_length=1)


class ChatResponse(BaseModel):
    answer: str
    thread_id: str
    tool_calls: list[str] = []


def memory_key(user: CurrentUser, thread_id: str) -> str:
    return f"tenant-{user.tenant_id}:{thread_id}"


def _ask(request: ChatRequest, user: CurrentUser) -> ChatResponse:
    try:
        # Die Tools oeffnen eigene Sessions und lesen den Mandanten aus dem
        # Kontext - LangGraph reicht die ContextVar an seine Worker-Threads weiter.
        with use_tenant(user.tenant_id):
            result = get_agent().ask(memory_key(user, request.thread_id), request.message)
    except LLMNotConfiguredError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        # Details nur ins Log: Fehlertexte von Datenbank oder LLM-Anbieter koennen
        # SQL, Parameterwerte oder interne Adressen enthalten.
        logger.exception("Chat fehlgeschlagen")
        raise HTTPException(
            status_code=500,
            detail="Der Assistent konnte die Frage nicht beantworten. Details stehen im Server-Log.",
        ) from error

    return ChatResponse(
        answer=result.answer, thread_id=request.thread_id, tool_calls=result.tool_calls
    )


@router.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest, user: CurrentUser = Depends(require_user)) -> ChatResponse:
    return _ask(request, user)


@router.delete("/chat/{thread_id}", status_code=204)
def reset_thread(thread_id: str, user: CurrentUser = Depends(require_user)) -> None:
    default_memory.clear(memory_key(user, thread_id))


@secure_router.post("/chat", response_model=ChatResponse)
def chat_secure(
    request: ChatRequest, user: CurrentUser = Depends(require_user)
) -> ChatResponse:
    return _ask(request, user)


@secure_router.delete("/chat/{thread_id}", status_code=204)
def reset_thread_secure(thread_id: str, user: CurrentUser = Depends(require_user)) -> None:
    default_memory.clear(memory_key(user, thread_id))
