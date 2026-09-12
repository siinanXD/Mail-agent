"""Bremse gegen Massenanfragen - im Prozessspeicher, ohne Zusatzdienst.

Gezaehlt wird je Schluessel (meist Adresse + Absender-IP) in einem gleitenden
Fenster. Gedacht gegen Passwort-Raten und gegen Mailfluten ueber die
Code-Endpunkte, nicht gegen ein verteiltes Botnetz.

Achtung: Hinter Docker-Portweiterleitung oder einem Reverse-Proxy sieht die API
fuer alle Clients dieselbe IP. Dann zaehlt praktisch nur die Adresse - wer sie
kennt, kann sie fuer das Fenster sperren. Bewusst in Kauf genommen: Ohne
vertrauenswuerdige Client-IP ist die Bremse wichtiger. Mit echtem Proxy uvicorn
mit --proxy-headers betreiben.

Es pollt und bedient ohnehin nur eine Instanz (README); mit mehreren Prozessen
gilt das Limit je Prozess.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from threading import Lock

#: Schutz gegen unbegrenztes Wachsen durch Anfragen mit immer neuen Schluesseln.
MAX_TRACKED_KEYS = 10_000
#: Wie viele der aeltesten Schluessel auf einmal weichen, wenn es voll ist.
EVICT_AT_ONCE = 1_000


class Throttle:
    """Hoechstens ``limit`` Ereignisse je Schluessel innerhalb von ``window``."""

    def __init__(self, limit: int, window: timedelta) -> None:
        self.limit = limit
        self.window = window
        self._events: dict[tuple[str, ...], list[datetime]] = {}
        self._lock = Lock()

    def blocked(self, *key: str, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        return self._recent(key, now) >= self.limit

    def reserve(self, *key: str, now: datetime | None = None) -> bool:
        """Zaehlt den Versuch und meldet, ob er noch erlaubt ist.

        Pruefen und Zaehlen gehoeren unter dasselbe Schloss - und der Versuch
        zaehlt, bevor das Passwort geprueft wird. Wurde erst nach der (langsamen)
        scrypt-Pruefung gezaehlt, kamen viele gleichzeitige Anfragen alle noch an
        der Bremse vorbei, waehrend die erste noch rechnete. Eine gelungene
        Anmeldung loescht den Zaehler mit ``clear`` wieder.
        """
        now = now or datetime.now()
        with self._lock:
            recent = [t for t in self._events.get(key, []) if now - t < self.window]
            if len(recent) >= self.limit:
                self._events[key] = recent
                return False
            if key not in self._events:
                self._make_room(now)
            recent.append(now)
            self._events[key] = recent
            return True

    def record(self, *key: str, now: datetime | None = None) -> None:
        now = now or datetime.now()
        with self._lock:
            if key not in self._events:
                self._make_room(now)
            self._events.setdefault(key, []).append(now)

    def _make_room(self, now: datetime) -> None:
        """Haelt das Verzeichnis klein. Erwartet das gehaltene Schloss."""
        if len(self._events) < MAX_TRACKED_KEYS:
            return
        for k in [k for k, times in self._events.items() if now - times[-1] >= self.window]:
            del self._events[k]
        # Sind alle Eintraege noch frisch, raeumt das nichts weg - wer immer neue
        # Adressen durchprobiert, hielte das Verzeichnis voll und den Speicher
        # wachsend. Dann weichen die aeltesten, in einem Rutsch.
        if len(self._events) >= MAX_TRACKED_KEYS:
            by_age = sorted(self._events, key=lambda k: self._events[k][-1])
            for oldest in by_age[: len(self._events) - MAX_TRACKED_KEYS + EVICT_AT_ONCE]:
                del self._events[oldest]

    def clear(self, *key: str) -> None:
        with self._lock:
            self._events.pop(key, None)

    def _recent(self, key: tuple[str, ...], now: datetime) -> int:
        with self._lock:
            recent = [t for t in self._events.get(key, []) if now - t < self.window]
            if recent:
                self._events[key] = recent
            else:
                self._events.pop(key, None)
            return len(recent)
