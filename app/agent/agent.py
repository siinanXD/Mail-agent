"""Der LangChain-Agent: Tool-Auswahl, Memory und Tracing.

``create_agent`` (LangChain 1.x) liefert einen kompilierten LangGraph-Graphen.
Das Conversation Memory laeuft im MVP bewusst ueber unsere eigene, kleine
``ConversationMemory``-Abstraktion; wer spaeter persistieren will, uebergibt
stattdessen einen LangGraph-Checkpointer (Parameter ``checkpointer``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool

from app.agent.prompts import build_system_prompt
from app.agent.tools import ALL_TOOLS
from app.llm.client import get_chat_model
from app.memory.memory import ConversationMemory, default_memory
from app.observability.langfuse import run_config

logger = logging.getLogger(__name__)


@dataclass
class AgentAnswer:
    answer: str
    thread_id: str
    tool_calls: list[str] = field(default_factory=list)


def build_agent(
    model: BaseChatModel | None = None,
    tools: list[BaseTool] | None = None,
    today: date | None = None,
    checkpointer: Any | None = None,
):
    """Baut den Tool-Calling-Agenten. Model/Tools sind injizierbar (Tests)."""
    return create_agent(
        model=model or get_chat_model(),
        tools=tools if tools is not None else ALL_TOOLS,
        system_prompt=build_system_prompt(today),
        checkpointer=checkpointer,
        name="mail-agent",
    )


class MailAgent:
    """Duenne Fassade um den Graphen inklusive Conversation Memory."""

    def __init__(
        self,
        model: BaseChatModel | None = None,
        tools: list[BaseTool] | None = None,
        memory: ConversationMemory | None = None,
    ) -> None:
        self._model = model
        self._tools = tools
        self._memory = memory or default_memory
        self._built_for: date | None = None
        self._graph = None

    def _graph_for_today(self):
        """Der System-Prompt enthaelt das Datum - nach Tageswechsel neu bauen."""
        today = date.today()
        if self._graph is None or self._built_for != today:
            self._graph = build_agent(
                model=self._model, tools=self._tools, today=today
            )
            self._built_for = today
        return self._graph

    def ask(self, thread_id: str, message: str) -> AgentAnswer:
        graph = self._graph_for_today()
        messages = [*self._memory.get(thread_id), HumanMessage(content=message)]
        try:
            result = graph.invoke(
                {"messages": messages},
                config=run_config(thread_id, user_input=message),
            )
        except Exception:
            logger.exception("Agent-Lauf fehlgeschlagen (thread_id=%s)", thread_id)
            raise

        produced = result["messages"][len(messages) :]
        answer = _final_answer(produced)
        tool_calls = [
            call["name"]
            for message_ in produced
            if isinstance(message_, AIMessage)
            for call in message_.tool_calls
        ]

        self._memory.append(thread_id, message, answer)
        logger.info("thread=%s tools=%s", thread_id, ",".join(tool_calls) or "keine")
        return AgentAnswer(answer=answer, thread_id=thread_id, tool_calls=tool_calls)


def _final_answer(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage) and message.text():
            return message.text().strip()
    return ""


_agent: MailAgent | None = None


def get_agent() -> MailAgent:
    """Lazy Singleton - erst beim ersten Request wird ein LLM-Client gebraucht."""
    global _agent
    if _agent is None:
        _agent = MailAgent()
    return _agent
