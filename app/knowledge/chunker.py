"""Zerlegt E-Mail-Text in Chunks fuer die Embedding-Erzeugung."""

from __future__ import annotations

DEFAULT_CHUNK_SIZE = 700
DEFAULT_OVERLAP = 100


def chunk_text(
    text: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[str]:
    """Teilt Text an Absatzgrenzen, faellt auf harte Schnitte zurueck."""
    cleaned = text.strip()
    if not cleaned:
        return []
    if len(cleaned) <= chunk_size:
        return [cleaned]

    paragraphs = [p.strip() for p in cleaned.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        if len(paragraph) > chunk_size:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_hard_split(paragraph, chunk_size, overlap))
            continue
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= chunk_size:
            current = candidate
        else:
            chunks.append(current)
            current = paragraph

    if current:
        chunks.append(current)
    return chunks


def chunk_email(subject: str, body: str, **kwargs) -> list[str]:
    """Der Betreff wird jedem Chunk vorangestellt, damit er mit-embedded wird."""
    parts = chunk_text(body, **kwargs) or [""]
    prefix = f"Betreff: {subject}".strip()
    return [f"{prefix}\n\n{part}".strip() for part in parts]


def _hard_split(text: str, chunk_size: int, overlap: int) -> list[str]:
    step = max(chunk_size - overlap, 1)
    return [text[i : i + chunk_size] for i in range(0, len(text), step)]
