"""Gespraechsgedaechtnis: begrenzt je Thread und in der Zahl der Threads."""

from __future__ import annotations

from app.memory.memory import InMemoryConversationMemory


def test_hoechstens_max_threads_der_am_laengsten_unbenutzte_faellt_heraus():
    """Die thread_id waehlt der Client - ohne Grenze wuchs der Speicher unbegrenzt."""
    memory = InMemoryConversationMemory(max_messages=4, max_threads=2)
    memory.append("a", "Frage a", "Antwort a")
    memory.append("b", "Frage b", "Antwort b")
    memory.get("a")  # a zuletzt benutzt, b ist jetzt der aelteste

    memory.append("c", "Frage c", "Antwort c")

    assert memory.get("b") == []
    assert [message.content for message in memory.get("a")] == ["Frage a", "Antwort a"]
    assert len(memory.get("c")) == 2


def test_lesen_legt_keinen_thread_an():
    memory = InMemoryConversationMemory(max_threads=1)
    memory.append("echt", "Frage", "Antwort")

    for index in range(50):
        assert memory.get(f"unbekannt-{index}") == []

    assert len(memory.get("echt")) == 2


def test_verlauf_je_thread_bleibt_begrenzt():
    memory = InMemoryConversationMemory(max_messages=4)
    for index in range(5):
        memory.append("t", f"Frage {index}", f"Antwort {index}")

    assert [message.content for message in memory.get("t")] == [
        "Frage 3",
        "Antwort 3",
        "Frage 4",
        "Antwort 4",
    ]
