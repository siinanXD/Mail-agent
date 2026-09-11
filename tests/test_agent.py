"""Agent, Prompt und Conversation Memory."""

from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.agent.agent import MailAgent
from app.agent.prompts import build_system_prompt
from app.agent.tools import ALL_TOOLS
from app.email.importer import import_directory
from app.main import app
from app.memory.memory import InMemoryConversationMemory
from tests.fakes import ScriptedChatModel, rule_based_extractor


@pytest.fixture
def seeded(session, sample_dir, use_session):
    import_directory(
        session, sample_dir, extractor=rule_based_extractor, embedder=lambda c: []
    )
    session.commit()
    use_session(session)
    return session


def tool_call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def test_system_prompt_loest_relative_zeitangaben_auf():
    prompt = build_system_prompt(today=date(2026, 9, 10))

    assert "heute ist 2026-09-10" in prompt
    assert "2026-08-31 bis 2026-09-06" in prompt  # letzte Woche
    assert "knowledge_search" in prompt


def test_alle_tools_sind_registriert():
    names = {tool.name for tool in ALL_TOOLS}

    assert names == {
        "search_emails",
        "get_email",
        "search_bookings",
        "search_cancellations",
        "count_cancellations",
        "search_booking_changes",
        "list_units",
        "check_occupancy",
        "knowledge_search",
        "create_cleaning_plan",
    }


def test_agent_ruft_tool_auf_und_antwortet(seeded):
    model = ScriptedChatModel(
        responses=[
            tool_call(
                "count_cancellations",
                {"start_date": "2026-08-31", "end_date": "2026-09-06"},
                "call-1",
            ),
            AIMessage(content="Letzte Woche gab es 2 Stornierungen."),
        ]
    )
    agent = MailAgent(model=model, memory=InMemoryConversationMemory())

    answer = agent.ask("demo-user", "Wie viele Stornierungen gab es letzte Woche?")

    assert answer.tool_calls == ["count_cancellations"]
    assert answer.answer == "Letzte Woche gab es 2 Stornierungen."
    assert "count_cancellations" in model.bound_tools
    # Das Tool hat wirklich SQL ausgefuehrt:
    tool_message = model.seen_messages[1][-1]
    assert json.loads(tool_message.content)["count"] == 2


def test_folgefrage_nutzt_conversation_memory(seeded):
    model = ScriptedChatModel(
        responses=[
            tool_call("count_cancellations", {"start_date": "2026-08-31"}, "c1"),
            AIMessage(content="Es gab 2 Stornierungen."),
            tool_call("search_cancellations", {"guest_name": "Berger"}, "c2"),
            AIMessage(content="Davon war die Stornierung von Thomas Berger."),
        ]
    )
    memory = InMemoryConversationMemory()
    agent = MailAgent(model=model, memory=memory)

    agent.ask("demo-user", "Welche Stornierungen gab es letzte Woche?")
    second = agent.ask("demo-user", "Welche davon waren wegen Flugausfällen?")

    assert second.tool_calls == ["search_cancellations"]
    history_text = " ".join(
        str(message.content) for message in model.seen_messages[2]
    )
    assert "Welche Stornierungen gab es letzte Woche?" in history_text
    assert "Es gab 2 Stornierungen." in history_text
    assert len(memory.get("demo-user")) == 4


def test_system_prompt_wird_pro_lauf_mit_aktuellem_datum_gebaut(seeded):
    """Der Graph wird bei Tageswechsel neu gebaut - kein eingefrorenes Datum."""
    model = ScriptedChatModel(responses=[AIMessage(content="Alles klar.")])
    agent = MailAgent(model=model, memory=InMemoryConversationMemory())

    agent.ask("demo-user", "Hallo")
    system_message = model.seen_messages[0][0]

    assert date.today().isoformat() in str(system_message.content)
    assert agent._built_for == date.today()


def test_memory_ist_pro_thread_getrennt():
    memory = InMemoryConversationMemory()
    memory.append("a", "Frage A", "Antwort A")
    memory.append("b", "Frage B", "Antwort B")

    assert len(memory.get("a")) == 2
    assert memory.get("a")[0].content == "Frage A"

    memory.clear("a")
    assert memory.get("a") == []
    assert len(memory.get("b")) == 2


def test_health_endpoint_antwortet():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"ok", "degraded"}
    assert "langfuse_enabled" in body
