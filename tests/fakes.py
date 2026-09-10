"""Test-Doubles: deterministischer Extractor, Embedder und Chat-Modelle.

Damit laufen die Tests ohne OpenAI-Key. Die Produktivpfade (LLM-Extraktion,
OpenAI-Embeddings) sind identisch, nur die injizierte Funktion ist eine andere.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda

from app.config import get_settings
from app.email.extractor import EmailExtraction
from app.email.parser import ParsedEmail, parse_datetime

BOOKING_REF = re.compile(r"BK-\d{4}-\d{4}")
DATE = r"(\d{2}\.\d{2}\.\d{4})"


# ----------------------------------------------------------- Extraktion


def rule_based_extractor(email: ParsedEmail) -> EmailExtraction:
    """Regelbasierte Extraktion fuer die Demo-Mails (Ersatz fuer den LLM-Call)."""
    text = f"{email.subject}\n{email.body}"
    lowered = text.lower()

    reference = BOOKING_REF.search(text)
    has_stay_dates = "anreise" in lowered
    is_change = "neue anreise" in lowered or "neue abreise" in lowered

    if "storn" in lowered:
        email_type = "cancellation"
    elif is_change:
        email_type = "change"
    elif "beschwerde" in lowered or "enttäuschung" in lowered:
        email_type = "complaint"
    elif reference and has_stay_dates:
        email_type = "booking"
    elif "frage" in lowered or "möglich" in lowered:
        email_type = "request"
    elif reference:
        email_type = "booking"
    else:
        email_type = "other"
    reason = None
    if email_type == "cancellation":
        if "flug" in lowered:
            reason = "Flugausfall"
        elif "krank" in lowered:
            reason = "Krankheit"
        elif "termin" in lowered or "beruflich" in lowered:
            reason = "berufliche Terminverschiebung"

    return EmailExtraction(
        email_type=email_type,
        booking_reference=reference.group(0) if reference else None,
        guest_name=_guest_name(text, email),
        arrival_date=_find_date(text, "Anreise"),
        departure_date=_find_date(text, "Abreise"),
        cancellation_date=(
            email.received_at.date() if email_type == "cancellation" else None
        ),
        cancellation_reason=reason,
        unit_name=_unit_name(text),
        new_arrival_date=_find_date(text, "Neue Anreise"),
        new_departure_date=_find_date(text, "Neue Abreise"),
    )


def _unit_name(text: str) -> str | None:
    match = re.search(r"Objekt:\s*(.+)", text)
    if match:
        return match.group(1).strip()
    match = re.search(
        r"((?:Ferienwohnung|FeWo|Ferienhaus|Haus|Wohnung)\s+[A-ZÄÖÜ][\wäöüß-]+)", text
    )
    return match.group(1).strip() if match else None


def _guest_name(text: str, email: ParsedEmail) -> str | None:
    match = re.search(r"(?:Gast|Name):\s*(.+)", text)
    if match:
        return match.group(1).split("(")[0].strip()
    lines = [line.strip() for line in email.body.splitlines() if line.strip()]
    return lines[-1] if lines else None


def _find_date(text: str, label: str):
    match = re.search(rf"{label}(?:datum)?:?\s*{DATE}", text)
    if not match:
        return None
    return parse_datetime(match.group(1)).date()


# ----------------------------------------------------------- Embeddings


def fake_embedder(texts: list[str]) -> list[list[float]]:
    """Deterministische Bag-of-Words-Hash-Embeddings in der konfigurierten Dimension."""
    return [_hash_vector(text) for text in texts]


def fake_query_embedder(text: str) -> list[float]:
    return _hash_vector(text)


def _hash_vector(text: str) -> list[float]:
    dim = get_settings().embedding_dim
    vector = [0.0] * dim
    for token in re.findall(r"\w+", text.lower()):
        digest = hashlib.md5(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        vector[index] += 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        vector[0] = 1.0
        return vector
    return [value / norm for value in vector]


# ----------------------------------------------------------- Chat-Modelle


class StructuredOutputStub(BaseChatModel):
    """Chat-Modell, das bei with_structured_output ein festes Objekt liefert."""

    result: Any = None
    prompts: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "structured-stub"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=""))])

    def with_structured_output(self, schema, **kwargs):
        def _run(prompt):
            self.prompts.append(str(prompt))
            return self.result

        return RunnableLambda(_run)


class ScriptedChatModel(BaseChatModel):
    """Spielt vorbereitete AIMessages ab - inkl. Tool-Calls fuer den Agenten."""

    responses: list[AIMessage] = []
    index: int = 0
    bound_tools: list[str] = []
    seen_messages: list[list[BaseMessage]] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        self.bound_tools = [getattr(tool, "name", str(tool)) for tool in tools]
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs,
    ) -> ChatResult:
        self.seen_messages.append(messages)
        message = self.responses[min(self.index, len(self.responses) - 1)]
        self.index += 1
        return ChatResult(generations=[ChatGeneration(message=message)])
