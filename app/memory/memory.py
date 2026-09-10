"""Conversation Memory pro thread_id.

Bewusst einfach gehalten (Prozessspeicher). Der Agent spricht nur ueber das
``ConversationMemory``-Protokoll mit dem Speicher, damit spaeter ein
LangGraph-/Postgres-Checkpointer eingesetzt werden kann, ohne den Agenten zu
aendern.
"""

from __future__ import annotations

from collections import defaultdict
from threading import Lock
from typing import Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage


class ConversationMemory(Protocol):
    def get(self, thread_id: str) -> list[BaseMessage]: ...

    def append(self, thread_id: str, user: str, assistant: str) -> None: ...

    def clear(self, thread_id: str) -> None: ...


class InMemoryConversationMemory:
    """Haelt die letzten ``max_messages`` Nachrichten je Thread im RAM."""

    def __init__(self, max_messages: int = 20) -> None:
        self._threads: dict[str, list[BaseMessage]] = defaultdict(list)
        self._max_messages = max_messages
        self._lock = Lock()

    def get(self, thread_id: str) -> list[BaseMessage]:
        with self._lock:
            return list(self._threads[thread_id])

    def append(self, thread_id: str, user: str, assistant: str) -> None:
        with self._lock:
            history = self._threads[thread_id]
            history.append(HumanMessage(content=user))
            history.append(AIMessage(content=assistant))
            del history[: max(0, len(history) - self._max_messages)]

    def clear(self, thread_id: str) -> None:
        with self._lock:
            self._threads.pop(thread_id, None)


#: Prozessweiter Standard-Speicher.
default_memory = InMemoryConversationMemory()
