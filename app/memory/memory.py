"""Conversation Memory pro thread_id.

Bewusst einfach gehalten (Prozessspeicher). Der Agent spricht nur ueber das
``ConversationMemory``-Protokoll mit dem Speicher, damit spaeter ein
LangGraph-/Postgres-Checkpointer eingesetzt werden kann, ohne den Agenten zu
aendern.
"""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock
from typing import Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage


class ConversationMemory(Protocol):
    def get(self, thread_id: str) -> list[BaseMessage]: ...

    def append(self, thread_id: str, user: str, assistant: str) -> None: ...

    def clear(self, thread_id: str) -> None: ...


class InMemoryConversationMemory:
    """Haelt die letzten ``max_messages`` Nachrichten je Thread im RAM.

    Hoechstens ``max_threads`` Threads; der am laengsten unbenutzte faellt
    heraus. Die thread_id waehlt der Client frei - ohne Grenze liesse sich der
    Prozessspeicher mit immer neuen IDs fuellen.
    """

    def __init__(self, max_messages: int = 20, max_threads: int = 1000) -> None:
        self._threads: OrderedDict[str, list[BaseMessage]] = OrderedDict()
        self._max_messages = max_messages
        self._max_threads = max_threads
        self._lock = Lock()

    def get(self, thread_id: str) -> list[BaseMessage]:
        with self._lock:
            # Lesen legt keinen Thread an - sonst fuellte schon eine Anfrage je ID den Speicher.
            history = self._threads.get(thread_id)
            if history is None:
                return []
            self._threads.move_to_end(thread_id)
            return list(history)

    def append(self, thread_id: str, user: str, assistant: str) -> None:
        with self._lock:
            history = self._threads.setdefault(thread_id, [])
            self._threads.move_to_end(thread_id)
            history.append(HumanMessage(content=user))
            history.append(AIMessage(content=assistant))
            del history[: max(0, len(history) - self._max_messages)]
            while len(self._threads) > self._max_threads:
                self._threads.popitem(last=False)

    def clear(self, thread_id: str) -> None:
        with self._lock:
            self._threads.pop(thread_id, None)


#: Prozessweiter Standard-Speicher.
default_memory = InMemoryConversationMemory()
