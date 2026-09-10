"""Misst die Extraktion an gelabelten Testdaten (Ordner mit ``manifest.csv``).

    python -m app.email.evaluate PFAD               # nur Beds24-Regeln, kostenlos
    python -m app.email.evaluate PFAD --llm         # Rest per LLM (echte OpenAI-Aufrufe)
    python -m app.email.evaluate PFAD --all-labels  # auch unzuverlaessige Labels werten

Liest nur Dateien und schreibt nichts in die Datenbank.

Das Manifest nennt je Datei einen ``intent``. Die Labels ``guest_inquiry``,
``payment_issue`` und ``review`` sind in den vorliegenden Exporten ueberwiegend
Newsletter und Spam - sie werden deshalb standardmaessig nicht gewertet.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from app.email.beds24 import parse_beds24_records
from app.email.extractor import EmailExtraction
from app.email.importer import default_extractor
from app.email.parser import (
    MANIFEST_NAME,
    ParsedEmail,
    list_email_files,
    parse_file,
    read_manifest,
)
from app.llm.client import LLMNotConfiguredError

INTENT_TO_TYPE = {
    "new_booking": "booking",
    "cancellation": "cancellation",
    "change": "change",
    "guest_inquiry": "request",
    "payment_issue": "other",
    "review": "other",
    "other": "other",
}
UNRELIABLE_INTENTS = frozenset({"guest_inquiry", "payment_issue", "review"})

#: Anzeige fuer Mails, die der Extractor nicht erkannt hat.
UNRECOGNIZED = "-"

MaybeExtractor = Callable[
    [ParsedEmail], EmailExtraction | list[EmailExtraction] | None
]


class Sample(BaseModel):
    file: str
    intent: str
    expected: str
    #: ``None``: der Extractor hat die Mail nicht erkannt.
    predicted: str | None
    subject: str
    booking_reference: str | None = None
    #: Alle Nummern, die die Mail als Buchung anlegt (Gruppen: mehrere).
    booked_references: list[str] = []

    @property
    def correct(self) -> bool:
        return self.predicted == self.expected


class LabelStats(BaseModel):
    total: int = 0
    recognized: int = 0
    correct: int = 0


class EvaluationReport(BaseModel):
    samples: list[Sample] = []
    #: Mails mit unzuverlaessigem Label, nicht gewertet.
    skipped: int = 0
    #: Dateien ohne (bekanntes) Label im Manifest.
    unlabeled: list[str] = []
    failed: list[str] = []

    def per_label(self) -> dict[str, LabelStats]:
        stats: dict[str, LabelStats] = {}
        for sample in self.samples:
            entry = stats.setdefault(sample.intent, LabelStats())
            entry.total += 1
            entry.recognized += sample.predicted is not None
            entry.correct += sample.correct
        return stats

    def confusion(self) -> Counter[tuple[str, str]]:
        return Counter(
            (sample.expected, sample.predicted or UNRECOGNIZED)
            for sample in self.samples
        )

    def orphans(self) -> list[Sample]:
        """Stornos/Aenderungen, zu deren Nummer keine Buchungsmail im Satz liegt."""
        booked = {
            reference
            for sample in self.samples
            for reference in sample.booked_references
        }
        return [
            sample
            for sample in self.samples
            if sample.predicted in ("cancellation", "change")
            and sample.booking_reference
            and sample.booking_reference not in booked
        ]


def evaluate_directory(
    directory: Path,
    *,
    extractor: MaybeExtractor = parse_beds24_records,
    include_unreliable: bool = False,
) -> EvaluationReport:
    manifest = read_manifest(directory)
    if not manifest:
        raise FileNotFoundError(f"Kein {MANIFEST_NAME} in {directory}")

    report = EvaluationReport()
    for path in list_email_files(directory):
        name = path.relative_to(directory).as_posix()
        entry = manifest.get(path.resolve())
        if entry is None or entry.intent not in INTENT_TO_TYPE:
            report.unlabeled.append(name)
            continue
        if entry.intent in UNRELIABLE_INTENTS and not include_unreliable:
            report.skipped += 1
            continue

        try:
            parsed = parse_file(path, received_at=entry.received_at)
            result = extractor(parsed)
            records = (result if isinstance(result, list) else [result]) if result else []
        except LLMNotConfiguredError:
            raise
        except Exception as error:
            report.failed.append(f"{name}: {error}")
            continue

        report.samples.append(
            Sample(
                file=name,
                intent=entry.intent,
                expected=INTENT_TO_TYPE[entry.intent],
                predicted=records[0].email_type if records else None,
                subject=parsed.subject,
                booking_reference=records[0].booking_reference if records else None,
                booked_references=[
                    record.booking_reference
                    for record in records
                    if record.email_type == "booking" and record.booking_reference
                ],
            )
        )
    return report


def render(report: EvaluationReport) -> str:
    samples = report.samples
    recognized = [sample for sample in samples if sample.predicted is not None]
    correct = [sample for sample in recognized if sample.correct]

    lines = [
        f"Gewertet: {len(samples)} Mails (nicht gewertet: {report.skipped} mit "
        f"unzuverlaessigem Label, {len(report.unlabeled)} ohne Label, "
        f"{len(report.failed)} nicht lesbar)",
        f"Erkannt:  {len(recognized)} von {len(samples)} "
        f"({_percent(len(recognized), len(samples))})",
        f"Richtig:  {len(correct)} von {len(recognized)} erkannten "
        f"({_percent(len(correct), len(recognized))})",
        "",
        f"{'Label':<16}{'Mails':>7}{'erkannt':>9}{'richtig':>9}",
    ]
    for intent, stats in sorted(report.per_label().items()):
        lines.append(
            f"{intent:<16}{stats.total:>7}{stats.recognized:>9}{stats.correct:>9}"
        )

    lines += ["", "Erwartet -> Erkannt"]
    for (expected, predicted), count in sorted(report.confusion().items()):
        lines.append(f"  {expected:<13} -> {predicted:<13}{count:>5}")

    wrong = [sample for sample in recognized if not sample.correct]
    if wrong:
        lines += ["", f"Abweichungen ({len(wrong)}):"]
        lines += [
            f"  {s.file}: {s.intent} -> {s.predicted} | {_short(s.subject)}"
            for s in wrong
        ]

    missed = [s for s in samples if s.predicted is None and s.expected != "other"]
    if missed:
        lines += ["", f"Nicht erkannt ({len(missed)}):"]
        lines += [f"  {s.file}: {_short(s.subject)}" for s in missed]

    orphans = report.orphans()
    if orphans:
        lines += [
            "",
            f"Stornos/Aenderungen ohne Buchungsmail im Datensatz: {len(orphans)}",
        ]

    if report.failed:
        lines += ["", "Nicht lesbar:"] + [f"  {entry}" for entry in report.failed]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.email.evaluate",
        description="Extraktion gegen die Labels einer manifest.csv messen.",
    )
    parser.add_argument("directory", type=Path, help="Ordner mit manifest.csv")
    parser.add_argument(
        "--llm",
        action="store_true",
        help="Nicht per Regel erkannte Mails per LLM klassifizieren (kostet).",
    )
    parser.add_argument(
        "--all-labels",
        action="store_true",
        help="Auch guest_inquiry, payment_issue und review werten.",
    )
    args = parser.parse_args(argv)

    # Gastnamen enthalten beliebige Schriftzeichen; die Windows-Konsole nicht.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    try:
        report = evaluate_directory(
            args.directory,
            extractor=default_extractor if args.llm else parse_beds24_records,
            include_unreliable=args.all_labels,
        )
    except (FileNotFoundError, LLMNotConfiguredError) as error:
        print(error, file=sys.stderr)
        return 1

    print(render(report))
    return 0


def _percent(part: int, whole: int) -> str:
    return f"{part / whole:.0%}" if whole else "-"


def _short(text: str, limit: int = 90) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


if __name__ == "__main__":
    raise SystemExit(main())
