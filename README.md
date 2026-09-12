# Mail Agent

Ein Mail-Agent für eine Ferienwohnungs-Vermietung. Er sitzt dauerhaft auf dem
IMAP-Postfach, erkennt Buchungen, Stornierungen, Umbuchungen und Gastnachrichten,
ordnet sie automatisch dem richtigen Objekt zu, legt nichts doppelt an - und liefert
auf Nachfrage den Putzplan einer Kalenderwoche als Excel.

**Putzplan für die Reinigungskräfte:** Mitarbeiter mit Telefonnummer anlegen, ihnen die
erkannten Wohnungen zuweisen – jede Woche geht zum eingestellten Termin jedem sein
Putzplan per WhatsApp raus. Kommen danach Stornierungen oder Umbuchungen, bekommen nur
die Betroffenen eine Änderung (Abschnitt 6).

**Wohnungen und Belegung:** Zu jedem erkannten Objekt lässt sich ein Profil pflegen –
Beschreibung, Hausregeln, Größe, Reinigungsfenster, Adresse und Zugang. Auf dem
Dashboard zeigt ein Monatskalender, welcher Tag frei, belegt oder storniert ist;
ein Klick auf den Tag nennt die Wohnungen, ein Klick auf die Buchung ihre ganze
Geschichte (Abschnitt 7).

Dazu gibt es eine **Weboberfläche** unter `/`: ein farbcodierter Verlauf aller
Vorgänge, und pro Eintrag die Original-E-Mail mit dem Beleg, welcher extrahierte
Wert wo im Text steht.

**Mandantenfähig:** Mehrere Vermietungen teilen sich eine Installation, jede mit
eigenen Nutzern, eigenem Postfach und eigenen Daten. Getrennt wird doppelt – im Code
und per PostgreSQL Row-Level-Security (Abschnitt 12).

Danach kann man in natürlicher Sprache Fragen stellen:

> „Wie viele Stornierungen gab es letzte Woche?“
> „Welche davon waren wegen Flugausfällen?“

Stack: **Python · FastAPI · LangChain · OpenAI · PostgreSQL + pgvector · SQLAlchemy · Pydantic · Langfuse · Docker**

---

## 1. Architektur

```
                 POST /chat
                     │
                     ▼
        ┌────────────────────────┐        ┌──────────────────┐
        │      MailAgent         │◄──────►│ Conversation     │
        │  (LangChain create_    │        │ Memory (thread)  │
        │   agent, Tool Calling) │        └──────────────────┘
        └───────────┬────────────┘
                    │  wählt Tools
   ┌────────────────┼─────────────────────────────┐
   │                │                             │
   ▼ SQL            ▼ SQL                         ▼ Vektor
search_emails   search_bookings              knowledge_search
get_email       search_cancellations               │
                count_cancellations                │
   │                │                              │
   └────────────────┴──────────────┬───────────────┘
                                   ▼
                     PostgreSQL (Source of Truth)
                     emails · bookings · cancellations
                     email_embeddings (pgvector)
```

### Dauerbetrieb

```
IMAP-Postfach ──(0/12/18 Uhr)──▶ Watcher ──▶ bekannt? ──ja──▶ überspringen
                                              │ nein
                                              ▼
                          Extraktion (LLM) ──▶ Typ + Objekt + Daten
                                              ▼
                  emails · units · bookings · cancellations · booking_changes
                                              ▼
                                      Chunks ──▶ pgvector
```

Die Dublettenbremse sitzt **vor** der Extraktion: Mails, deren
`provider_message_id` schon in der Datenbank steht, werden übersprungen, ohne
einen einzigen LLM-Call zu kosten.

### Die wichtigste Regel: nicht alles ist RAG

| Fragetyp | Beispiel | Weg |
|---|---|---|
| **strukturiert** | „Wie viele Stornierungen gab es letzte Woche?“ | SQL (`count_cancellations`) |
| **strukturiert** | „Welche Buchungen hatte Max Mustermann?“ | SQL (`search_bookings`) |
| **strukturiert** | „Wer reist Samstag ab, was muss geputzt werden?“ | SQL (`check_occupancy`) |
| **semantisch** | „Wer hat wegen eines Flugausfalls storniert?“ | Embeddings + pgvector (`knowledge_search`) |
| **semantisch** | „Da war eine Beschwerde über das Frühstück.“ | Embeddings + pgvector |
| **kombiniert** | „Welche Stornierungen letzte Woche waren wegen Flugausfall?“ | erst SQL, dann Vektor, Join über `email_id` |

Zahlen kommen **immer** aus SQL, nie aus einer Ähnlichkeitssuche.

### Schichten

| Schicht | Ort | Aufgabe |
|---|---|---|
| LLM | `app/llm/client.py` | Einziger Ort mit OpenAI-SDK: Chat-Modell, Embeddings, `complete()` |
| Agent | `app/agent/` | System-Prompt, Tool-Auswahl, Tool-Implementierungen |
| Ingestion | `app/email/` | IMAP-Abruf, Watcher, Parsen, Extraktion, Import |
| Objekte | `app/units.py` | Normalisierung der Objektnamen (Dublettenschutz) |
| Reports | `app/reports/` | Putzplan als Excel, Belegung je Objekt für den Assistenten |
| Mitarbeiter | `app/staff/`, `app/messaging/` | Wohnungszuordnung, Putzplan per WhatsApp (Twilio) |
| Knowledge | `app/knowledge/` | Chunking, Embedding-Indexierung, Vektor-Retrieval |
| Memory | `app/memory/` | Verlauf pro `thread_id` |
| Daten | `app/database/` | Modelle, Engine, Repositories (reines SQL, keine LLM-Logik) |
| Observability | `app/observability/` | Langfuse-Callback (optional) |
| API | `app/api/` | `/health`, `/chat`, `/emails/*`, `/reports/*`, `/api/*` |
| Belege | `app/evidence.py` | Sucht extrahierte Werte im Originaltext |
| Oberfläche | `app/web/` | Statisches HTML/CSS/JS, kein Build-Schritt |

### Ingestion-Pipeline

```
E-Mail (IMAP oder .txt/.json/.eml) → parsen → schon bekannt? → ja: überspringen
  → Typ erkennen + extrahieren (Beds24 regelbasiert, sonst LLM Structured Output)
  → Objekt zuordnen/anlegen → emails speichern
  → bookings / cancellations / booking_changes speichern
  → chunken → Embeddings → email_embeddings (pgvector)
```

### Datenmodell

Verwaltung (ohne RLS, mandantenübergreifend lesbar für Anmeldung und Watcher):

* `tenants` – `name`, `slug` (unique), `active`
* `users` – `tenant_id`, `email` (unique), `password_hash` (scrypt), `active`, `verified_at` (Adresse per Code bestätigt; `NULL` = keine Anmeldung möglich)
* `login_sessions` – `user_id`, `token_hash` (SHA-256 des Cookie-Tokens), `expires_at`. Sitzungen liegen in der Datenbank, ein Neustart meldet niemanden ab
* `verification_codes` – `user_id`, `purpose` (`signup` / `password_reset`), `code_hash` (scrypt), `expires_at`, `attempts`, `used_at`
* `mailboxes` – `tenant_id`, `host`, `port`, `username`, `password_encrypted` (Fernet), `folder`, `since_date`, `last_polled_at`, `last_error`, `last_uid` / `uid_validity` (IMAP-Cursor), `retry_uid` / `retry_count` (Wiederholung fehlgeschlagener Mails), `status` / `status_message` / `last_check_at` (Verbindungstest, unabhängig vom Mailabruf)
* `agent_runs` – `tenant_id`, `mailbox_id`, `kind`, `status`, `started_at` / `finished_at`, `imported`, `skipped`, `bookings`, `cancellations`, `changes`, `failed`, `message`. Protokoll der Arbeitsgänge für die Aktivitätsanzeige

Mandantendaten (jede Zeile mit `tenant_id`, geschützt per Row-Level-Security):

* `emails` – `provider_message_id` (unique je Mandant), `sender`, `recipient`, `subject`, `body`, `received_at`, `email_type`
* `units` – `name`, `normalized_name` (unique je Mandant) und das von Hand gepflegte Profil:
  `description`, `house_rules`, `rooms`, `beds`, `size_sqm`, `max_guests`, `cleaning_window`,
  `address`, `floor`, `access_encrypted` (Fernet – Schlüssel, Codes, WLAN nie im Klartext)
* `bookings` – `booking_reference` (unique je Mandant), `guest_name`, `arrival_date`, `departure_date`, `status`, `unit_id`, `source_email_id`, `state_as_of` (Eingang der neuesten vollständigen Mail zur Buchung; zusammen mit den Zeitpunkten in `booking_changes` entscheidet er je Angabe, ob eine später importierte ältere Mail sie noch setzen darf – eine Umbuchung wird so nicht zurückgesetzt, eine ältere Umbuchung eines anderen Felds aber noch übernommen)
* `cancellations` – `booking_id`, `cancelled_at`, `reason`, `source_email_id`
* `booking_changes` – `booking_id`, `changed_at`, `field`, `old_value`, `new_value`, `source_email_id`
* `email_embeddings` – `email_id`, `chunk`, `embedding vector(1536)`, `metadata`
* `staff_members` – `name`, `phone` (E.164), `active`
* `staff_units` – `staff_id`, `unit_id`: welche Wohnungen ein Mitarbeiter putzt (mehrere je Seite möglich)
* `cleaning_schedules` – `enabled`, `send_weekday` (0 = Montag), `send_time`, `active_since` – eine Zeile je Mandant
* `cleaning_dispatches` – `staff_id`, `week_start`, `kind` (`plan`/`update`), `success`, `tasks` (die Reinigungen, die der Mitarbeiter damit kannte), `provider_message_id`, `error`

E-Mail-Typen: `booking`, `cancellation`, `change`, `request`, `complaint`, `other`.

### Objekt-Erkennung ohne Dubletten

Objekte werden vollautomatisch aus den Mails angelegt. Damit
„Ferienwohnung Seeblick", „FeWo Seeblick" und „fewo seeblick!" nicht zu drei
Wohnungen werden, wird jeder Name auf einen Schlüssel normalisiert
(Kleinschreibung, Umlaute, Satzzeichen, Gattungswörter wie „Ferienwohnung"/„FeWo"/
„Haus" entfallen) → alle drei ergeben `seeblick`. Nur ein neuer Schlüssel erzeugt
ein neues Objekt. Als Anzeigename gilt die **zuerst gesehene** Schreibweise.

---

## 2. Installation

Empfohlen ist Docker (Abschnitt 4). Lokal ohne Container:

```bash
python -m venv .venv
.venv/Scripts/activate        # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # Werte eintragen
```

Dazu braucht es ein PostgreSQL mit pgvector – am einfachsten nur den DB-Container:

```bash
docker compose up -d postgres
```

---

## 3. Environment-Variablen

`.env.example` kopieren nach `.env`. Keine Secrets ins Repo committen (`.env` ist in `.gitignore`).

| Variable | Bedeutung | Default |
|---|---|---|
| `OPENAI_API_KEY` | Pflicht für Import und Chat | – |
| `OPENAI_MODEL` | Chat-Modell des Agenten | `gpt-4o-mini` |
| `OPENAI_EMBEDDING_MODEL` | Embedding-Modell | `text-embedding-3-small` |
| `EMBEDDING_DIM` | Muss zum Embedding-Modell passen | `1536` |
| `POSTGRES_PASSWORD` | Passwort des Datenbank-Owners (Superuser). Pflicht für Docker Compose | – |
| `DATABASE_URL` | SQLAlchemy-URL (psycopg3). `127.0.0.1` statt `localhost` – die Datenbank lauscht nur auf IPv4 | `postgresql+psycopg://mailagent_app@127.0.0.1:5432/mailagent` |
| `LANGFUSE_PUBLIC_KEY` | optional | leer |
| `LANGFUSE_SECRET_KEY` | optional | leer |
| `LANGFUSE_HOST` | optional | `https://cloud.langfuse.com` |
| `MIGRATION_DATABASE_URL` | Owner-Verbindung für Migrationen und App-Rolle. Leer = `DATABASE_URL` (dann RLS wirkungslos) | leer |
| `APP_DB_PASSWORD` | Passwort der App-Rolle `mailagent_app` (Docker Compose) | `bitte-aendern` |
| `ENCRYPTION_KEY` | Fernet-Schlüssel für Postfach-Passwörter. **Nie ändern**, sobald Postfächer existieren | leer |
| `BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD` | Erster Nutzer im Mandanten `standard` – nur solange es keinen Nutzer gibt | leer |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | Twilio-Zugang für den Putzplan per WhatsApp. Leer = es wird nichts verschickt | leer |
| `TWILIO_WHATSAPP_FROM` | WhatsApp-Absender, z.B. `whatsapp:+14155238886` (Sandbox) | leer |
| `TWILIO_CONTENT_SID` | Freigegebene WhatsApp-Vorlage (`HX…`) für den Wochenplan. Leer = freier Text | leer |
| `TWILIO_UPDATE_CONTENT_SID` | Vorlage für Änderungen nach dem Versand | leer |
| `PHONE_DEFAULT_COUNTRY_CODE` | Vorwahl für Nummern wie `0171 1234567` | `49` |
| `CLEANING_DISPATCH_ENABLED` | Automatischen Putzplan-Versand ein-/ausschalten | `true` |
| `SMTP_HOST` | Mailserver für die Einmalcodes. Leer = kein Versand, der Code steht im Server-Log (nur für die Entwicklung) | leer |
| `SMTP_PORT` | SMTP-Port | `587` |
| `SMTP_USER` / `SMTP_PASSWORD` | Zugangsdaten des Absenderkontos | leer |
| `SMTP_FROM` | Absender, z.B. `Mail Agent <noreply@example.de>`. Leer = `SMTP_USER` | leer |
| `SMTP_STARTTLS` | `true`: Port 587 mit STARTTLS, `false`: Port 465 mit SSL | `true` |
| `SIGNUP_ENABLED` | Selbstregistrierung erlauben | `true` |
| `SESSION_HOURS` | Gültigkeit einer Sitzung | `12` |
| `COOKIE_SECURE` | Cookie nur über HTTPS senden | `false` |
| `IMAP_HOST` | Postfach-Server, z.B. `outlook.office365.com` | leer |
| `IMAP_PORT` | IMAP-Port | `993` |
| `IMAP_USER` / `IMAP_PASSWORD` | Zugangsdaten (Gmail/Outlook: App-Passwort) | leer |
| `IMAP_FOLDER` | Zu überwachender Ordner | `INBOX` |
| `IMAP_SSL` | SSL/TLS verwenden | `true` |
| `IMAP_SINCE` | Nur Mails ab diesem Datum holen | leer |
| `WATCH_ENABLED` | Automatischen Abruf ein-/ausschalten | `true` |
| `POLL_TIMES` | Abrufzeiten (lokale Zeit, kommagetrennt) | `00:00,12:00,18:00` |
| `POLL_BATCH_SIZE` | Max. Mails je Durchlauf | `50` |
| `CONNECTION_CHECK_MINUTES` | Abstand der reinen Verbindungstests (Login, Ordner wählen, keine Mails) | `5` |
| `TZ` | Zeitzone des Containers – bestimmt, wann `POLL_TIMES` feuert | `Europe/Berlin` |
| `POSTGRES_PORT` / `API_PORT` | Host-Ports, falls 5432/8000 belegt sind | `5432` / `8000` |
| `EXPORTS_DIR` | Ablage der erzeugten Excel-Dateien | `data/exports` |
| `IMPORTS_DIR` | Import-Ordner je Mandant: `<IMPORTS_DIR>/tenant-<id>/` | `data/imports` |
| `SAMPLE_EMAILS_DIR` | Verzeichnis der Test-E-Mails | `data/sample_emails` |
| `LOG_LEVEL` | Log-Level | `INFO` |

Ohne Langfuse-Keys startet die Anwendung normal – Tracing ist dann einfach aus.
Ohne IMAP-Zugangsdaten startet sie ebenfalls normal – der Watcher bleibt dann aus,
Mails lassen sich weiterhin per `POST /emails/import` aus dem Import-Ordner des eigenen Mandanten einlesen.

---

## 4. Docker starten

```bash
cp .env.example .env                 # OPENAI_API_KEY eintragen
python -m app.admin generate-key     # Ergebnis als ENCRYPTION_KEY in .env
# in .env: APP_DB_PASSWORD, BOOTSTRAP_ADMIN_EMAIL, BOOTSTRAP_ADMIN_PASSWORD setzen
docker compose up --build
```

Danach:

* API: <http://localhost:8000>
* Swagger: <http://localhost:8000/docs>
* PostgreSQL (pgvector): `127.0.0.1:5432` – nur auf dem eigenen Rechner erreichbar, nicht im Netz

Das Schema legt **Alembic** beim Start an (`CREATE EXTENSION vector` inklusive) –
siehe Abschnitt 13.

Ins Image kommen nur Code und die erfundenen Demo-Mails. `data/imports`,
`data/exports`, `.env` und Postfach-Exporte schließt `.dockerignore` aus – Image-Schichten
behalten Dateien dauerhaft, und dort sollen keine Gastdaten landen. `data/` wird zur
Laufzeit als Volume eingebunden. Die API läuft im Container als Nutzer `mailagent`
(UID 1000); unter Linux muss `./data` für diese UID beschreibbar sein.

```bash
curl http://localhost:8000/health
```

```json
{"status":"ok","database":"ok","rls_effective":true,"llm_configured":true,
 "langfuse_enabled":false,"encryption_configured":true,"watcher_enabled":true,
 "watcher_running":true,"watcher_last_run":"2026-09-11T12:00:03",
 "watcher_next_run":"2026-09-11T18:00:00","whatsapp_configured":true,
 "cleaning_dispatch_running":true}
```

`rls_effective: false` heißt: Die App verbindet als Superuser, die Datenbank trennt
die Mandanten dann **nicht**. `status` steht in dem Fall auf `degraded`.

---

## 5. Daten importieren

Importiert wird immer in den Mandanten des angemeldeten Nutzers – und nur aus dessen
eigenem Import-Ordner `data/imports/tenant-<id>/` (oder einem Unterordner davon). Die
mitgelieferten, erfundenen Demo-E-Mails aus `data/sample_emails/` (`.txt` mit
Headerblock oder `.json`) gibt es ausdrücklich mit `sample_data`. Vorher anmelden,
siehe Abschnitt 7:

```bash
curl -b cookies.txt -X POST http://localhost:8000/emails/import \
  -H "Content-Type: application/json" \
  -d '{"sample_data": true}'
```

```json
{"imported":14,"skipped":0,"bookings":8,"cancellations":3,
 "changes":1,"units":3,"chunks":14,"failed":[]}
```

Ein zweiter Aufruf importiert nichts doppelt – und kostet keinen LLM-Call:

```json
{"imported":0,"skipped":14,"bookings":0,"cancellations":0,"changes":0,"units":0,"chunks":0,"failed":[]}
```

Hat sich die Extraktionslogik geändert und sollen bekannte Mails erneut durch das
LLM laufen, geht das mit `{"reprocess": true}`.

Eigene Dateien legst du in den Import-Ordner deines Mandanten und gibst den Unterordner
an. `..` und absolute Pfade werden abgelehnt, ohne `directory` wird der ganze Ordner
gelesen:

```bash
# Dateien liegen in data/imports/tenant-1/beds24-export/
curl -b cookies.txt -X POST http://localhost:8000/emails/import \
  -H "Content-Type: application/json" \
  -d '{"directory": "beds24-export"}'
```

Früher genügte „irgendwo unter `data/`". Dann hätte jeder Mandant die Exporte aller
anderen in sein eigenes Konto kopieren können – und Row-Level-Security hilft dagegen
nicht, weil die Kopien seine `tenant_id` bekommen. `data/imports/` ist deshalb auch
von der Versionierung ausgeschlossen.

Der Import ist idempotent – dieselbe `provider_message_id` wird aktualisiert, nicht dupliziert.

### Postfach-Exporte (`.eml`) und Beds24

Neben `.txt`/`.json` liest der Import `.eml`-Dateien, auch in Unterordnern. Liegt
im Verzeichnis eine `manifest.csv` (Spalten `file`, `intent`, `received_at`), liefert
sie das Eingangsdatum für Mails ohne `Date`-Header – sonst stimmt die Reihenfolge
von Buchung und Stornierung nicht.

Benachrichtigungen des Channel-Managers **Beds24** (Buchung, Stornierung, Änderung,
unverbindliche Anfrage) werden regelbasiert ausgelesen (`app/email/beds24.py`) –
ohne LLM-Call. Nur alle anderen Mails gehen an die LLM-Extraktion. Unverbindliche
Anfragen werden als `request` gespeichert, nicht als Buchung. Nennt eine
Änderungsmail eine unbekannte Buchungsnummer samt neuem Stand, wird die Buchung
daraus angelegt.

### Extraktion gegen Labels messen

```bash
python -m app.email.evaluate PFAD/ZUM/EXPORT              # nur Beds24-Regeln, kostenlos
python -m app.email.evaluate PFAD/ZUM/EXPORT --llm        # Rest per LLM (kostet)
python -m app.email.evaluate PFAD/ZUM/EXPORT --all-labels # auch unzuverlässige Labels
```

Das Skript vergleicht den erkannten Typ mit dem `intent` aus der `manifest.csv`, listet
Abweichungen und nicht erkannte Mails und zählt Stornos/Änderungen ohne Buchungsmail.
Es schreibt nichts in die Datenbank. `guest_inquiry`, `payment_issue` und `review`
werden standardmäßig nicht gewertet – diese Labels sind in Exporten meist Newsletter.

Echte Exporte enthalten Namen, Telefonnummern und E-Mail-Adressen von Gästen –
sie gehören nicht ins Repository.

### Postfach im Dauerbetrieb

Sind die `IMAP_*`-Variablen gesetzt, startet die API einen Watcher, der zu festen
Uhrzeiten neue Mails holt und automatisch verarbeitet – standardmäßig um
**00:00, 12:00 und 18:00 Uhr**:

```env
POLL_TIMES=00:00,12:00,18:00
TZ=Europe/Berlin
```

Zweimal täglich statt dreimal? Einfach eine Zeit streichen, z.B. `POLL_TIMES=06:00,18:00`.
Die Zeiten gelten in der Zeitzone des Containers, deshalb `TZ` – ohne die Angabe
läuft Docker in UTC und die Abrufe verschieben sich um ein bis zwei Stunden.

Ob der Watcher läuft und wann er zuletzt bzw. als Nächstes abruft, steht in `/health`
(`watcher_running`, `watcher_last_run`, `watcher_next_run`). Fehler je Postfach stehen
bewusst nicht dort – `/health` ist ohne Anmeldung erreichbar –, sondern in
`mailboxes.last_error` (`python -m app.admin list`) und in der Antwort von
`POST /emails/poll`.

Sofort abrufen, statt auf den nächsten Zyklus zu warten (angemeldet, siehe Abschnitt 7):

```bash
curl -b cookies.txt -X POST http://localhost:8000/emails/poll
```

Hinweise:

* Der Watcher **liest nur** – nichts wird als gelesen markiert, verschoben oder gelöscht.
* Ein verpasster Termin (Container war aus) wird nicht nachgeholt – der nächste
  reguläre Lauf holt die Mails ohnehin mit. Sofort geht es per `POST /emails/poll`.
* Ob eine Mail neu ist, entscheidet die Datenbank über die `Message-ID`, nicht das
  Gelesen-Flag. Ein Neustart verarbeitet deshalb nichts doppelt.
* Der Watcher merkt sich je Postfach die zuletzt verarbeitete IMAP-UID und holt die
  **ältesten** noch offenen Mails zuerst, Batch für Batch (`POLL_BATCH_SIZE`, höchstens
  20 Batches je Abruf). Ein Rückstau wird so vollständig abgearbeitet, statt dass immer
  nur die neuesten Mails ankommen.
* Der Cursor läuft nie an einer Mail vorbei, die nicht ankam (Server antwortet auf
  FETCH mit NO/BAD) oder deren Import scheiterte (LLM, Netz, Datenbank). Er bleibt
  davor stehen, spätere Mails warten, und beim nächsten Abruf wird sie erneut
  versucht – höchstens 3-mal, dann geht der Abruf an ihr vorbei und vermerkt die UID
  in `last_error`. So kann eine dauerhaft kaputte Mail das Postfach nicht für immer
  blockieren. Bereits importierte Mails desselben Batches überspringt die
  Dublettenprüfung beim Wiederholen.
* Ein Postfach wird nie zweimal gleichzeitig abgerufen. Läuft schon ein Abruf (Zeitplan
  oder ein anderer Nutzer), wird ein weiterer sofort übersprungen und meldet das –
  statt denselben Batch doppelt durch die bezahlte Extraktion zu schicken.
* Zu lange Kopfzeilen (etwa eine To-Zeile mit vielen Empfängern) werden auf die
  Spaltenlänge gekürzt statt den Import scheitern zu lassen; lange Message-IDs und
  Buchungsnummern bekommen dabei einen Hash, damit sie eindeutig bleiben.
* Für Gmail und Outlook/Microsoft 365 wird ein **App-Passwort** gebraucht, das
  normale Kontopasswort funktioniert dort nicht.
* `IMAP_SINCE=2026-09-01` begrenzt den ersten Lauf, damit nicht ein ganzes
  Archiv durch die Extraktion läuft.

---

## 6. Putzplan erstellen

Der Putzplan wird **nur auf Nachfrage** erzeugt – im Chat:

> „Kannst du mir den Putzplan für KW 37 erstellen?"
> „Erstell mir bitte den Putzplan für die Woche vom 7. bis 13. September 2026."

Der Agent ruft dann `create_cleaning_plan` auf, nennt die fälligen Reinigungen und
den Download-Pfad. Direkt über die API geht es auch:

```bash
curl -o putzplan.xlsx "http://localhost:8000/reports/cleaning-plan?week=37&year=2026"
```

Die Excel-Datei enthält die volle Wochenbelegung, eine Zeile je Objekt:

| Objekt | Mo 07.09. | ... | Sa 12.09. | So 13.09. |
|---|---|---|---|---|
| Ferienwohnung Seeblick | ANREISE / Familie Meier | belegt | **WECHSEL** / Meier, Yilmaz | belegt / Emre Yilmaz |
| FeWo Bergblick | belegt / Piotr Kowalski | belegt | **ABREISE** / Piotr Kowalski | |
| Haus Anna | | | belegt / Marta Novak | **ABREISE** / Marta Novak |

* **WECHSEL** (rot) – Abreise und Anreise am selben Tag, Reinigung zwingend
* **ABREISE** (gelb) – Reinigung fällig
* **ANREISE** (grün), *belegt* (blau), leer = frei

Stornierte Buchungen tauchen nicht auf, Umbuchungen sind bereits eingerechnet.

### Putzplan an die Mitarbeiter per WhatsApp

In der Oberfläche unter **Mitarbeiter & Putzplan** (Abschnitt 7):

1. **Mitarbeiter hinzufügen** – Name und Telefonnummer. `0171 1234567`, `+49 (0)171-1234567`
   und `0049 171 1234567` werden zu `+491711234567`.
2. **Wohnungen zuweisen** – direkt im Dropdown der Mitarbeiterzeile. Zur Auswahl stehen alle
   Wohnungen, die aus den Mails erkannt wurden. Ein Mitarbeiter kann mehrere Wohnungen haben,
   eine Wohnung mehrere Mitarbeiter.
3. **Automatischen Versand einschalten** – Wochentag und Uhrzeit wählen.

Zum Termin bekommt jeder aktive Mitarbeiter die Reinigungen der **kommenden Woche** (Montag
bis Sonntag) für seine Wohnungen. Sonntag 18:00 schickt die Woche ab Montag, ein Termin am
Montag die laufende Woche:

    Hallo Maria,

    dein Putzplan KW 38 (14.09.–20.09.2026) von Ferienwohnungen Müller – 2 Reinigungen:

    Sa 19.09. – FeWo Bergblick
    So 20.09. – Haus Anna (Wechsel: neuer Gast reist am selben Tag an)

Eine Reinigung ist fällig an jedem Abreisetag, wie im Excel-Plan. Gastnamen stehen bewusst
nicht drin – die Nachricht läuft über Twilio und WhatsApp.

**Aktuell zum Versand:** Der Plan wird erst beim Versand aus der Datenbank berechnet. Was bis
dahin an Stornierungen und Umbuchungen eingegangen ist, steckt schon drin.

**Änderungen nach dem Versand:** Nach jedem Mail-Abruf (und nach `POST /emails/import`) wird
der zuletzt zugestellte Plan jedes Mitarbeiters mit dem aktuellen Stand verglichen. Hat sich
ab heute etwas geändert, bekommt **nur dieser Mitarbeiter** eine Nachricht mit „Neu",
„Entfällt" bzw. „Geändert" und dem ganzen aktuellen Plan. Vergangene Tage zählen nicht. Das
gilt auch, wenn eine Wohnung einem anderen Mitarbeiter zugewiesen wird.

Weitere Regeln:

* **Genau einmal je Woche.** Was verschickt ist, steht in `cleaning_dispatches` – ein Neustart
  schickt nichts doppelt.
* **Ein verpasster Termin wird nachgeholt**, solange seine Woche läuft (etwa, weil der Server
  Sonntagabend aus war). Beim Einschalten mitten in der Woche geht dagegen nichts sofort raus,
  der erste Versand ist der nächste Termin. Wer sofort will: „Jetzt an alle senden".
* **Mitten in der Woche** verschickt (von Hand oder nachgeholt) enthält der Plan nur noch die
  Reinigungen ab heute.
* **Fehlgeschlagene Zustellung** (falsche Nummer, Twilio nicht erreichbar) wird bis zu 3-mal
  automatisch versucht, danach nur noch von Hand. Der Fehler steht in der Vorschau.
* **Geprüft wird alle 5 Minuten** – der Plan kommt bis zu 5 Minuten nach dem Termin.
* **Die Vorschau** zeigt je Mitarbeiter, was jetzt verschickt würde, wann zuletzt etwas
  zugestellt wurde und ob sich seitdem etwas geändert hat. Sie warnt vor Reinigungen, für die
  niemand zuständig ist – auch aus Buchungen ohne erkannte Wohnung.

#### Twilio einrichten

```env
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
```

Zum Ausprobieren reicht die **Twilio-Sandbox**: Jeder Mitarbeiter schickt einmal den
Beitrittscode an die Sandbox-Nummer, danach kommen die Nachrichten als freier Text an.

Im Betrieb erlaubt WhatsApp Nachrichten, die das Unternehmen von sich aus schickt, nur als
**von Meta freigegebene Vorlage**. Dafür in Twilio (Content Template Builder) zwei Vorlagen
anlegen und freigeben lassen – eine Vorlage darf nicht mit einer Variablen beginnen oder enden:

```
Wochenplan:  Hallo {{1}}, hier ist dein {{2}}: {{3}}. Bei Fragen melde dich gern.
Änderung:    Hallo {{1}}, es gibt eine {{2}}: {{3}}. Bei Fragen melde dich gern.
```

```env
TWILIO_CONTENT_SID=HX...
TWILIO_UPDATE_CONTENT_SID=HX...
```

`{{1}}` ist der Name, `{{2}}` der Titel („Putzplan KW 38 (14.09.–20.09.2026) von …" bzw.
„Änderung am Putzplan KW 38 …"), `{{3}}` die Reinigungen in einer Zeile
(„Sa 19.09. – FeWo Bergblick; So 20.09. – Haus Anna (Wechsel: …)") – WhatsApp lehnt
Zeilenumbrüche in Variablen ab. Ohne `TWILIO_UPDATE_CONTENT_SID` gehen auch Änderungen über
die Wochenplan-Vorlage. Ohne jede Vorlage wird freier Text geschickt; der kommt außerhalb der
Sandbox nur an, wenn der Mitarbeiter in den letzten 24 Stunden selbst geschrieben hat
(Twilio-Fehler 63016).

Ohne Twilio-Zugang funktionieren Mitarbeiter, Zuordnung und Vorschau trotzdem – die
Oberfläche zeigt dann einen Hinweis, und es wird nichts verschickt.

---

## 7. Weboberfläche

Nach dem Start liegt die Oberfläche unter <http://localhost:8000>.

**Verlauf** – alle Vorgänge chronologisch, neueste zuerst. Jede Zeile ist doppelt
markiert: ein Farbstreifen links und ein Badge mit dem Typ als Wort (nur Farbe
wäre für farbfehlsichtige Nutzer unbrauchbar).

| Typ | Farbe |
|---|---|
| Buchung | grün |
| Stornierung | rot |
| Änderung | amber |
| Gastanfrage | blau |
| Beschwerde | violett |

Darüber Kennzahlen je Typ, Filter-Chips (mehrere kombinierbar) und eine Volltextsuche
über Betreff und Text.

**Beleg-Ansicht** – ein Klick auf eine Zeile öffnet die Original-E-Mail. Links der
unveränderte Text mit gelb markierten Fundstellen, rechts jedes extrahierte Feld
einzeln:

* **wörtlich in der Mail** – der Wert steht genau so im Text, die Stelle ist markiert
* **abgeleitet, nicht wörtlich** – das Modell hat den Wert erschlossen

Beispiel Storno wegen Flugausfall: Buchungsnummer, Gast, Objekt und Grund stehen
wörtlich in der Mail; das Stornodatum ist das Empfangsdatum der Mail und wird
deshalb ehrlich als *abgeleitet* ausgewiesen.

Die Extraktion läuft über ein LLM und liefert keine Textstellen mit. Statt
Fundstellen zu erfinden, sucht `app/evidence.py` die Werte nachträglich im Original –
inklusive gängiger Datumsschreibweisen (`2026-09-12`, `12.09.2026`, `12.9.2026`) und
Objekt-Schreibvarianten. Was sich nicht finden lässt, wird als abgeleitet markiert.

**Belegungskalender** – oben auf dem Verlauf-Dashboard, standardmäßig der laufende Monat,
mit „Zurück", „Heute" und „Vor". Jeder Tag ist eingefärbt:

| Tag | Farbe |
|---|---|
| nichts gebucht | weiß |
| belegt | grün |
| nur stornierte Buchung | rot |
| Buchung mit Umbuchung | amber markiert |

Eine Nacht zählt vom Anreise- bis vor dem Abreisetag – ein reiner Abreisetag ist also
wieder frei, genau wie im Putzplan. Stornierte Buchungen verschwinden nicht, sie werden
durchgestrichen gezeigt: Man soll sehen, dass da einmal etwas war. Ein Klick auf den Tag
listet die Buchungen, ein Klick darauf zeigt Zeitraum, Nächte, Umbuchungen, Stornogrund
und führt zur Original-Mail.

**Wohnungen** – zweiter Reiter oben (`/#/wohnungen`): je Objekt eine Karte mit Beschreibung,
Hausregeln, Zimmer/Betten/Größe/Gästen, Reinigungsfenster, Adresse und den zuständigen
Reinigungskräften. Die Objekte selbst entstehen weiterhin automatisch aus den Mails, der
Name lässt sich hier nicht ändern. Zugangsdaten (Schlüssel, Code, WLAN) werden mit
`ENCRYPTION_KEY` verschlüsselt gespeichert; fehlt der Schlüssel, lehnt die API das Feld ab,
statt es im Klartext abzulegen.

**Mitarbeiter & Putzplan** – dritter Reiter oben (`/#/mitarbeiter`): Mitarbeiter anlegen,
bearbeiten und löschen, Wohnungen je Mitarbeiter im Dropdown anhaken (wird sofort
gespeichert), den automatischen Versand einstellen und die Vorschau für diese oder nächste
Woche – mit „Senden" je Mitarbeiter und „Jetzt an alle senden". Details in Abschnitt 6.

### Assistent (Chat-Bubble)

Unten rechts sitzt der Agent aus Abschnitt 8 als kleine Bubble. Gefragt wird in
normaler Sprache, geantwortet wird aus der Datenbank:

> „Wie viele Stornierungen gab es letzte Woche?" → *„In der letzten Woche gab es
> insgesamt 2 Stornierungen."* · Werkzeuge: `count_cancellations`

Unter jeder Antwort steht, **welche Werkzeuge** der Agent benutzt hat. Damit ist
nachvollziehbar, ob eine Zahl aus SQL kommt oder aus der semantischen Suche – und ob
die Antwort überhaupt in der Datenbank nachgesehen hat.

Folgefragen funktionieren („Welche davon war wegen eines Flugausfalls?"): Die
Thread-ID liegt im `localStorage`, das Gespräch überlebt ein Neuladen der Seite.
„Neu" startet ein frisches Gespräch und löscht das Memory des alten Threads.

### Anmeldung

Mit E-Mail und Passwort. Jeder Nutzer gehört zu genau einem Mandanten und sieht nur
dessen Daten; oben neben dem Logo steht, in welchem Mandanten man gerade arbeitet.

* Passwörter liegen als scrypt-Hash in `users`, nie im Klartext.
* Falsches Passwort und unbekannte Adresse liefern dieselbe Meldung – und dauern
  gleich lang. Sonst ließen sich gültige Adressen erraten.
* Der Mandant kommt immer aus der Sitzung, nie aus einem Parameter des Clients.
* Nach 5 Fehlversuchen je Adresse und Absender-IP innerhalb von 15 Minuten antwortet
  `/api/login` mit `429` – auch beim richtigen Passwort, sonst ließe sich weiter raten.
  **Einschränkung:** Hinter der Docker-Portweiterleitung oder einem Reverse-Proxy sieht
  die API für alle Clients dieselbe IP (im Log z.B. `172.25.0.1`). Dann zählt praktisch
  nur die Adresse – wer eine Login-Adresse kennt, kann sie für 15 Minuten sperren. Das
  ist bewusst so: Ohne verlässliche Client-IP wiegt der Schutz gegen Raten schwerer.
  Hinter einem vertrauenswürdigen Proxy uvicorn mit `--proxy-headers` starten, dann
  wird je echter Client-IP gezählt.
* Wird ein Nutzer oder sein Mandant deaktiviert, endet eine laufende Sitzung sofort.
* Sitzungen stehen in `login_sessions` (nur der SHA-256 des Tokens), nicht im
  Prozessspeicher: ein Neustart oder Deployment meldet niemanden mehr ab.

```env
COOKIE_SECURE=true      # hinter HTTPS
```

### Registrierung, Bestätigung und Passwort vergessen

Wer sich selbst registriert, bekommt **einen eigenen Mandanten** und ist dessen
erster Nutzer – eigene Mails, eigene Buchungen, eigenes Postfach. Weitere Nutzer
desselben Kunden legt die Verwaltung in diesem Mandanten an
(`python -m app.admin create-user <slug> <mail>`).

| Schritt | Endpunkt | Was passiert |
|---|---|---|
| Konto anlegen | `POST /api/register` | Mandant + Nutzer (`verified_at = NULL`), 6-stelliger Code per Mail |
| Code erneut | `POST /api/register/resend` | neuer Code, der alte verfällt |
| Bestätigen | `POST /api/verify` | setzt `verified_at` und meldet gleich an |
| Passwort vergessen | `POST /api/password-reset` | Code per Mail |
| Neues Passwort | `POST /api/password-reset/confirm` | setzt das Passwort und beendet **alle** offenen Sitzungen des Nutzers |

* **Ohne Bestätigung keine Anmeldung.** `/api/login` antwortet dann mit `403` und
  `{"reason": "unverified"}`; die Oberfläche springt direkt zum Code-Feld. Das erfährt
  nur, wer das richtige Passwort kennt – sonst ließe sich damit nach Konten suchen.
* **Registrierung und Reset verraten nie, ob es die Adresse gibt.** Beide antworten
  immer mit `202` und demselben Text. Wurde die Adresse schon benutzt, geht statt eines
  Codes ein Hinweis an ihr Postfach – dorthin, wo er hingehört.
* **Codes gelten 15 Minuten**, sind an ihren Zweck gebunden (ein Reset-Code bestätigt
  keine Adresse) und sind nach 5 Fehlversuchen verbraucht. Sie liegen als scrypt-Hash
  in der Datenbank, nicht im Klartext.
* **Höchstens 3 Code-Anforderungen** je Adresse und IP in 15 Minuten – sonst ließen sich
  über diese Endpunkte fremde Postfächer zumüllen.
* Der Reset-Code beweist Zugriff auf das Postfach: er bestätigt die Adresse gleich mit.
* Bestandsnutzer aus `python -m app.admin create-user` gelten sofort als bestätigt –
  ihre Adresse hat ein Mensch eingetragen (Migration `0008` setzt das rückwirkend).

Ohne `SMTP_HOST` verschickt die Anwendung nichts, sondern schreibt Betreff und Code
als Warnung ins Server-Log. So kommt man in der Entwicklung ohne Mailserver durch den
Ablauf; im Betrieb darf das nicht vorkommen. Selbstregistrierung abschalten:

```env
SIGNUP_ENABLED=false
```

### Postfach verbinden

In der Kopfzeile führt **„Postfach"** in die Einstellungen. Damit entfällt
`python -m app.admin add-mailbox` für den Normalfall – die CLI bleibt für die
Verwaltung.

* **Anbieter-Vorlagen** für Gmail, Outlook/Microsoft 365, GMX, WEB.DE, IONOS und
  Strato füllen Server und Port aus. Bei Gmail und Outlook steht dabei der Hinweis,
  dass IMAP dort ein **eigenes App-Passwort** verlangt – das normale Kontopasswort
  wird abgelehnt, und genau daran scheitert die Einrichtung sonst.
* **„Verbindung testen"** prüft die Zugangsdaten, ohne sie zu speichern, und sagt
  gleich, **wie viele Mails ab dem gewählten Datum** im Ordner liegen.
* Das Passwort geht nur in eine Richtung: verschlüsselt hinein, nie wieder heraus.
  Ein leeres Passwortfeld heißt „unverändert lassen", nicht „löschen".

**Mails lesen ab** bestimmt, wie weit der Agent zurückschaut. Wird das Datum
**zurück**gesetzt, wird zugleich der IMAP-Cursor (`last_uid`) zurückgesetzt – sonst
stünde der Abruf weiter an der alten Stelle und die älteren Mails kämen nie an.
Doppelte Importe verhindert die Dublettenprüfung. Weiter zurück heißt mehr Mails
und mehr LLM-Kosten; deshalb die Zahl vor dem Speichern.

### Ist das Postfach verbunden?

Die Ampel in der Kopfzeile kommt aus einem **eigenen, billigen Verbindungstest**
(Login, Ordner wählen, keine Mails), der alle `CONNECTION_CHECK_MINUTES` läuft –
getrennt vom Mailabruf. Sonst fiele ein abgelehntes Passwort erst beim nächsten
geplanten Abruf auf, also unter Umständen Stunden später.

| Farbe | Status | Bedeutung |
|---|---|---|
| 🟢 | `ok` | verbunden |
| 🟡 | `unreachable` | Server gerade nicht erreichbar – erledigt sich oft von allein |
| 🔴 | `auth_error`, `tls_error`, `folder_missing` | da muss jemand ran: Passwort, Zertifikat oder Ordner |
| ⚪ | `unknown` | noch nie geprüft |

Die Unterscheidung ist der Punkt: Ein weggebrochener Server braucht Geduld, ein
abgelehntes Passwort braucht den Kunden. Beides als „Fehler" anzuzeigen hieße,
dass niemand weiß, ob er etwas tun muss.

### Was der Agent gerade tut

Unter der Kopfzeile steht, woran der Agent arbeitet: „Der Agent liest gerade das
Postfach …" mit Spinner, sonst das Ergebnis des letzten Laufs („Zuletzt 12.09.
18:00: 12 neue Mail(s), 3 Buchung(en) erkannt") und die nächste geplante Abrufzeit.
**„Jetzt abrufen"** stößt einen Lauf sofort an.

Jeder Lauf steht in `agent_runs`; `GET /api/activity` liefert die letzten 20 samt
`busy`-Kennzeichen. Die Oberfläche fragt alle 3 Sekunden nach, solange gearbeitet
wird, sonst alle 20.

Zwei Details, die man beim Zusehen merkt:

* `POST /api/mailbox/poll` antwortet **sofort mit `202`** und arbeitet im
  Hintergrund weiter. Ein Rückstau von tausend Mails braucht Minuten – so lange
  soll kein Browser warten. (Das alte, synchrone `POST /emails/poll` bleibt für
  Skripte.)
* Ein Neustart mitten im Abruf ließe einen Lauf für immer als „läuft" dastehen.
  Solche Läufe werden nach einer Stunde als abgebrochen markiert.

### Endpunkte der Oberfläche

```bash
curl http://localhost:8000/api/timeline
curl "http://localhost:8000/api/timeline?types=cancellation&types=change"
curl "http://localhost:8000/api/timeline?search=Flugausfall"
curl http://localhost:8000/api/emails/7
curl -X POST http://localhost:8000/api/chat   -H "Content-Type: application/json"   -d '{"thread_id":"web-1","message":"Gab es Umbuchungen?"}'
curl http://localhost:8000/api/staff
curl -X POST http://localhost:8000/api/staff -H "Content-Type: application/json" -d '{"name":"Maria","phone":"0171 1234567","unit_ids":[1,3]}'
curl -X PUT http://localhost:8000/api/cleaning-schedule -H "Content-Type: application/json" -d '{"enabled":true,"weekday":6,"send_time":"18:00"}'
curl "http://localhost:8000/api/cleaning-schedule/preview?week_start=2026-09-14"
curl -X POST http://localhost:8000/api/cleaning-schedule/send -H "Content-Type: application/json" -d '{"week_start":"2026-09-14"}'
curl http://localhost:8000/api/units
curl -X PUT http://localhost:8000/api/units/1 -H "Content-Type: application/json" -d '{"description":"Ruhige Lage","rooms":3,"cleaning_window":"Abreisetag ab 11:00"}'
curl "http://localhost:8000/api/calendar?year=2026&month=9"
curl http://localhost:8000/api/bookings/3
```

Mit Passwort vorher anmelden und das Cookie mitschicken:

```bash
curl -c cookies.txt -X POST http://localhost:8000/api/login   -H "Content-Type: application/json" -d '{"email":"anna@beispiel.de","password":"einGutesPasswort"}'
curl -b cookies.txt http://localhost:8000/api/timeline
```

---

## 8. Chat-Endpunkt testen

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"thread_id": "demo-user", "message": "Wie viele Stornierungen gab es letzte Woche?"}'
```

```json
{
  "answer": "Letzte Woche gab es 2 Stornierungen.",
  "thread_id": "demo-user",
  "tool_calls": ["count_cancellations"]
}
```

Folgefrage im selben Thread – der Agent nutzt den Verlauf:

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"thread_id": "demo-user", "message": "Welche davon waren wegen Flugausfällen?"}'
```

Verlauf zurücksetzen:

```bash
curl -X DELETE http://localhost:8000/chat/demo-user
```

### Windows / PowerShell

In PowerShell ist `curl` ein Alias für `Invoke-WebRequest` und versteht die
curl-Flags nicht (es fragt dann nach der `Uri`). Entweder `curl.exe` schreiben:

```powershell
curl.exe -s -X POST http://localhost:8000/chat -H "Content-Type: application/json" -d "{\"thread_id\":\"demo-user\",\"message\":\"Wie viele Stornierungen gab es letzte Woche?\"}"
```

Oder – angenehmer bei Umlauten – PowerShell-nativ:

```powershell
function Ask($msg) {
  $body = @{ thread_id = "demo-user"; message = $msg } | ConvertTo-Json
  Invoke-RestMethod -Uri "http://localhost:8000/chat" -Method Post `
    -ContentType "application/json; charset=utf-8" `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
}
Ask "Wie viele Stornierungen gab es letzte Woche?"
Ask "Welche davon waren wegen Flugausfällen?"
```

Die UTF-8-Kodierung des Bodys ist nötig, weil PowerShell 5.1 sonst Umlaute
verstümmelt. Für die Antwortrichtung schickt die API `charset=utf-8` mit.

---

## 9. Beispiel-Fragen

Strukturiert (SQL):

* Welche Buchungen gab es vom 1. bis 30. September 2026?
* Welche Buchung gehörte zu Thomas Berger?
* Wie viele Stornierungen gab es zwischen dem 31.08.2026 und dem 06.09.2026?
* Zeig mir die Mail zu Buchung BK-2026-0105.
* Welche Buchungen sind noch bestätigt?

Objekte und Umbuchungen:

* Welche Wohnungen gibt es?
* Welche Buchungen hat die FeWo Seeblick?
* Gab es Umbuchungen?

Belegung und Reinigung (`check_occupancy`, ohne Datei):

* Wer wohnt am 08.09.2026 in der FeWo Bergblick? – auch Gäste, die schon vorher angereist sind
* Wer reist am 12.09.2026 ab, und wer kommt am selben Tag an?
* Ist Haus Anna vom 13. bis 16.09.2026 frei?
* Was muss in KW 37 geputzt werden?

Eine Nacht zählt vom Anreisetag bis vor dem Abreisetag – am Abreisetag kann der
nächste Gast anreisen. `search_bookings` filtert dagegen nur nach dem Anreisedatum
und ist für Belegungsfragen ungeeignet. Die Excel-Datei erzeugt der Assistent nur,
wenn ausdrücklich ein Putzplan als Datei gewünscht ist.

Semantisch (pgvector):

* Gab es eine Mail, in der jemand wegen eines Flugausfalls storniert hat?
* Da war eine Beschwerde über das Frühstück – welche Mail war das?
* Ich suche eine Mail, in der Late Check-out erwähnt wurde.

Kombiniert / Folgefragen:

* „Welche Stornierungen gab es letzte Woche?“ → „Welche davon waren wegen Flugausfällen?“
* „Welche Buchungen gab es im September?“ → „Und welche davon gehörten zu Kowalski?“

---

## 10. Projektstruktur

```
mail-agent/
├── app/
│   ├── main.py                    FastAPI-App, Startup (Schema + Langfuse)
│   ├── config.py                  Settings aus .env (pydantic-settings)
│   ├── api/
│   │   ├── health.py              GET  /health
│   │   ├── emails.py              POST /emails/import, POST /emails/poll
│   │   ├── reports.py             GET  /reports/cleaning-plan
│   │   ├── staff.py               /api/staff, /api/cleaning-schedule (Mitarbeiter, Versand)
│   │   ├── units.py               /api/units (Wohnungsprofile)
│   │   ├── calendar.py            /api/calendar, /api/bookings/{id} (Belegung, Buchungsdetail)
│   │   ├── chat.py                POST /chat und /api/chat (beide mit Anmeldung)
│   │   ├── auth.py                Anmeldung, Registrierung, Bestätigung, Reset
│   │   ├── mailbox.py             Postfach einstellen, prüfen, Aktivität
│   │   ├── throttle.py            Bremse gegen Raten und Mailfluten
│   │   └── timeline.py            GET  /api/timeline, GET /api/emails/{id}
│   ├── llm/client.py              OpenAI-Kapselung (Chat, Embeddings, complete)
│   ├── agent/
│   │   ├── agent.py               create_agent + Memory + Tracing
│   │   ├── prompts.py             System-Prompt inkl. Datumsauflösung
│   │   └── tools/                 search_emails, get_email, search_bookings,
│   │                              search_cancellations, count_cancellations,
│   │                              search_booking_changes, list_units,
│   │                              check_occupancy, knowledge_search,
│   │                              create_cleaning_plan
│   ├── email/
│   │   ├── parser.py              .txt/.json → ParsedEmail
│   │   ├── imap_client.py         IMAP-Abruf → ParsedEmail
│   │   ├── watcher.py             zyklisches Polling im Hintergrund
│   │   ├── extractor.py           LLM Structured Output → EmailExtraction
│   │   └── importer.py            komplette Ingestion-Pipeline
│   ├── knowledge/
│   │   ├── chunker.py             Chunking inkl. Betreff-Präfix
│   │   ├── indexer.py             Chunks → Embeddings → pgvector
│   │   └── retriever.py           Cosine-Suche über pgvector
│   ├── web/                       Oberfläche: index.html, app.css, app.js, mailbox.js
│   ├── notify.py                  ausgehende Mails (Einmalcodes) per SMTP
│   ├── evidence.py                Belege: Wert im Originaltext finden
│   ├── units.py                   Normalisierung der Objektnamen
│   ├── staff/
│   │   ├── phone.py               Telefonnummern → E.164
│   │   ├── plan.py                Reinigungen je Mitarbeiter, Nachrichtentext, Versandtermin
│   │   └── dispatcher.py          Wochenversand, Änderungen nach dem Abruf, Versand von Hand
│   ├── messaging/whatsapp.py      WhatsApp über Twilio (einziger Ort mit Twilio)
│   ├── reports/cleaning_plan.py   Putzplan als Excel (openpyxl)
│   ├── reports/occupancy.py       Belegung, An-/Abreisen, Reinigungen je Zeitraum
│   ├── reports/calendar.py        Monatsraster: frei / belegt / storniert / umgebucht
│   ├── memory/memory.py           Conversation Memory pro thread_id
│   ├── database/
│   │   ├── connection.py          Engine, session_scope, init_db
│   │   ├── models.py              SQLAlchemy-Modelle
│   │   └── repositories.py        SQL-Abfragen
│   └── observability/langfuse.py  optionales Tracing
├── migrations/                    Alembic (env.py + versions/)
├── alembic.ini
├── data/sample_emails/            14 Demo-E-Mails
├── data/imports/tenant-<id>/      Import-Ordner je Mandant (nicht versioniert)
├── data/exports/                  erzeugte Putzpläne
├── tests/                         test_agent, test_tools, test_email_import,
│                                  test_units, test_cleaning_plan, test_imap,
│                                  test_schedule, test_web, test_staff,
│                                  test_calendar, test_units_profile,
│                                  test_auth, test_mailbox
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── pyproject.toml
└── README.md
```

---

## 11. Tests

```bash
docker compose up -d postgres
.venv/Scripts/python -m pytest -v
```

Die Tests laufen ohne OpenAI-Key: Extraktion und Embeddings werden über injizierte
Funktionen ersetzt (`tests/fakes.py`), die SQL- und pgvector-Pfade laufen dagegen echt.
Läuft kein PostgreSQL, werden nur die beiden pgvector-Tests übersprungen.

In GitHub Actions (`.github/workflows/ci.yml`) läuft dieselbe Suite bei jedem Push
und jedem Pull Request – mit einem `pgvector/pgvector:pg16`-Dienst, damit die Tests zu
Mandantentrennung und Migrationen wirklich laufen und nicht still übersprungen werden.

Die pgvector-Tests benutzen eine **eigene Datenbank** – den Namen aus `DATABASE_URL`
mit Suffix `_test` (also `mailagent_test`), die beim ersten Lauf automatisch angelegt
wird. Die Anwendungsdatenbank wird nie geleert. Überschreibbar per `TEST_DATABASE_URL`.

---

## 12. Mandanten

### Wie getrennt wird

Jede Datentabelle trägt eine `tenant_id`. Die Trennung ist doppelt abgesichert:

1. **Im Code** – jede Abfrage in `app/database/repositories.py` filtert ausdrücklich
   auf den Mandanten der Session. Ohne gebundenen Mandanten gibt es `NoTenantError`
   statt stiller Daten.
2. **In PostgreSQL** – Row-Level-Security auf allen zehn Datentabellen. Die App setzt
   bei jedem Transaktionsbeginn `app.tenant_id`; die Datenbank liefert nur passende
   Zeilen und lehnt Schreibzugriffe in fremde Mandanten ab. Vergisst eine Abfrage den
   Filter, kommt trotzdem nichts Fremdes zurück.

### Warum es zwei Datenbankrollen gibt

**Superuser umgehen Row-Level-Security immer** – auch mit `FORCE ROW LEVEL SECURITY`.
Das Postgres-Image legt `POSTGRES_USER` als Superuser an. Liefe die App damit, wären
alle Policies wirkungslos, ohne dass irgendetwas auffällt.

| Rolle | Variable | Aufgabe |
|---|---|---|
| `mailagent` (Owner) | `MIGRATION_DATABASE_URL` | Migrationen, pgvector, Verwaltung der App-Rolle |
| `mailagent_app` | `DATABASE_URL` | alle Anfragen der Anwendung – `NOSUPERUSER`, `NOBYPASSRLS` |

Die App-Rolle legt die API beim Start selbst an (Name und Passwort aus `DATABASE_URL`)
und gibt ihr nur Datenrechte. `/health` meldet mit `rls_effective`, ob die Trennung in
der Datenbank tatsächlich greift. Die RLS-Tests laufen bewusst als Nicht-Superuser –
als Superuser wären sie immer grün.

### Verwaltung

```bash
python -m app.admin generate-key
python -m app.admin create-tenant "Ferienwohnungen Müller" mueller
python -m app.admin create-user mueller anna@mueller.de
python -m app.admin add-mailbox mueller imap.example.de anna@mueller.de --since 2026-09-01
python -m app.admin list
```

Passwörter werden abgefragt statt als Argument übergeben (Shell-History). Postfach-
Passwörter werden mit `ENCRYPTION_KEY` verschlüsselt gespeichert – ohne Schlüssel
verweigert das Anlegen, statt sie im Klartext abzulegen.

Der Watcher ruft alle aktiven Postfächer aller aktiven Mandanten ab und importiert jedes
in seinen eigenen Mandanten. Ein ausgefallenes Postfach stoppt die anderen nicht; der
Fehler steht an `mailboxes.last_error` und in `python -m app.admin list`.

### Was sich gegenüber dem Einzelbetrieb geändert hat

* `APP_PASSWORD` entfällt – es gibt echte Nutzerkonten.
* **`/chat` und `/emails/import` brauchen jetzt eine Anmeldung.** Ohne Anmeldung gibt es
  keinen Mandanten, in den geschrieben oder aus dem gelesen werden dürfte. Für curl:
  erst `/api/login`, dann das Cookie mitschicken.
* Das Chat-Gedächtnis ist je Mandant getrennt – gleiche `thread_id` bei zwei Mandanten
  sind zwei verschiedene Gespräche.
* Putzpläne liegen in `data/exports/tenant-<id>/`; herunterladen kann man nur die des
  eigenen Mandanten.
* `/emails/import` liest nur aus dem Import-Ordner des eigenen Mandanten (`data/imports/tenant-<id>/`), die Demo-Mails nur mit `sample_data`.
* Bestehende Daten wandern bei der Migration `0002` in den Mandanten `standard`. Sind
  `IMAP_*` in der `.env` gesetzt, wird das Postfach beim Start dorthin übernommen.

---

## 13. Datenbank-Migrationen (Alembic)

Das Schema wird über Alembic verwaltet. Beim Start der API läuft automatisch
`alembic upgrade head` – frische Datenbanken bekommen alle Tabellen, bestehende
nur die fehlenden Änderungen.

Manuell:

```bash
alembic upgrade head          # auf aktuellen Stand bringen
alembic current               # aktuelle Revision anzeigen
alembic history               # alle Revisionen
alembic downgrade -1          # eine Revision zurück
```

Nach einer Modelländerung eine neue Revision erzeugen:

```bash
alembic revision --autogenerate -m "beschreibung"
```

Die generierte Datei in `migrations/versions/` **immer durchsehen** – Autogenerate
erkennt Typänderungen und Umbenennungen nicht zuverlässig.

Die Datenbank-URL steht nicht in `alembic.ini`, sondern kommt aus
`MIGRATION_DATABASE_URL` und nur ersatzweise aus `DATABASE_URL`
(`migrations/env.py`) – damit landet kein Passwort im Repository. Die App-Rolle
aus `DATABASE_URL` darf absichtlich kein DDL ausführen, manuelle Migrationen
laufen deshalb immer mit dem Owner.

**Bestandsdatenbanken:** Wurden die Tabellen früher mit `create_all` angelegt,
fehlt `alembic_version`. Der Start erkennt dann anhand typischer Tabellen und
Spalten, auf welchem Stand das Schema ist (`SCHEMA_MARKERS` in
`app/database/connection.py`), stempelt genau diesen Stand und migriert den Rest
regulär – eine Datenbank aus der Zeit vor den Mandanten bekommt so Mandanten,
RLS und alle neueren Spalten, bestehende Mails landen im Mandanten „Standard".
Wer eine Migration mit neuem, erkennbarem Merkmal schreibt, ergänzt dort einen
Eintrag.

Die pgvector-Tests bauen ihre Datenbank bei jedem Lauf komplett über die
Migrationen auf. Ein Fehler in einer Migration fällt damit im Test auf.

---

## 14. Langfuse

Sobald `LANGFUSE_PUBLIC_KEY` und `LANGFUSE_SECRET_KEY` gesetzt sind, wird jeder
Agent-Lauf getraced: User-Input, LLM-Calls, Tool-Calls samt Argumenten und Ergebnis,
finale Antwort, Fehler, Latenzen und – soweit von OpenAI geliefert – Tokenverbrauch.
Die `thread_id` wird als Langfuse-Session-ID gesetzt, sodass ein Gespräch
zusammenhängend sichtbar ist.

---

## 15. Bekannte Einschränkungen (MVP)

* **Nur IMAP** – kein Microsoft Graph / Gmail API. Bei Gmail und Microsoft 365
  ist ein App-Passwort nötig; Konten mit erzwungenem OAuth funktionieren nicht.
* **Polling zu festen Zeiten, kein Push** – neue Mails werden erst beim nächsten
  Termin gesehen (Standard 00:00/12:00/18:00), kein IMAP IDLE. Für dringende Fälle
  gibt es `POST /emails/poll`.
* **Ein Watcher pro Prozess** – bei mehreren API-Replicas würden alle dasselbe
  Postfach pollen. Dann `WATCH_ENABLED=false` setzen und nur eine Instanz pollen lassen.
* **Objektnamen sind nur so gut wie die Mails** – erkannt wird, was drinsteht.
  Schreibvarianten werden zusammengeführt, aber zwei wirklich verschiedene Namen
  für dasselbe Objekt („Seeblick" vs. „Wohnung 1") bleiben zwei Objekte. Mails ohne
  Objektangabe landen im Putzplan unter „Nicht zugeordnet".
* **Migrationen laufen beim Start** – bequem für ein MVP, aber bei mehreren
  API-Replicas würden mehrere Prozesse gleichzeitig migrieren. Dann besser
  `alembic upgrade head` als eigenen Schritt vor dem Deploy ausführen.
* **Conversation Memory ist im Prozessspeicher** – nach einem Neustart ist der Verlauf weg,
  und bei mehreren API-Replicas teilen sich die Instanzen den Verlauf nicht. Der Agent
  ist als LangGraph-Graph gebaut, ein Postgres-Checkpointer lässt sich über
  `build_agent(checkpointer=...)` nachrüsten. Gehalten werden höchstens 1000 Gespräche,
  das am längsten unbenutzte fällt heraus.
* **Extraktion hängt am LLM** – bei unklaren Mails kann `email_type` oder ein Datum falsch
  sein; es gibt kein Review-/Korrektur-UI.
* **Stornierungen ohne Buchungsnummer** werden nur zugeordnet, wenn Gastname (exakt) und
  – falls genannt – Anreisedatum genau eine bestätigte Buchung treffen. Sonst wird die
  Stornierung gespeichert, aber bewusst nicht mit einer Buchung verknüpft (`booking_id` ist
  dann `NULL`) und taucht damit nicht in `count_cancellations`-Filtern nach Gast auf.
  Lieber unverknüpft als die falsche Buchung stornieren.
* **Kein Vektor-Index** – ohne IVFFlat/HNSW ist die Suche ein exakter Scan. Für ein paar
  tausend Mails völlig ausreichend, darüber sollte ein Index angelegt werden.
* **Keine Metadaten-Filter im Vektor-Suchpfad** – `knowledge_search` durchsucht alle Chunks;
  Zeitraumfilter macht der Agent, indem er zusätzlich ein SQL-Tool aufruft.
* **Nutzerverwaltung nur per CLI** – `python -m app.admin`, keine Oberfläche. Keine
  Rollen innerhalb eines Mandanten, kein Passwort-Reset. Sitzungen und der Zähler der
  Login-Bremse liegen im Prozessspeicher: nach einem Neustart sind sie weg, und mehrere
  API-Replicas zählen Fehlversuche getrennt.
* **Ein Schlüssel für alle Postfach-Passwörter** – wer `ENCRYPTION_KEY` und einen
  Datenbank-Dump hat, kann alle IMAP-Passwörter entschlüsseln. Wird der Schlüssel
  geändert, sind die gespeicherten Passwörter verloren und müssen neu hinterlegt werden.
* **Tests berühren die Live-Datenbank.** Die Tests mit `TestClient` starten den
  `lifespan`, und der führt Migrationen gegen die konfigurierte Datenbank aus.
  Datenverlust entsteht dabei nicht, aber eine neue Migration landet so schon beim
  Testen in der echten Datenbank.
* **Der Assistent kennt den Zustand der Oberfläche nicht** – gesetzte Filter oder
  eine geöffnete Mail fließen nicht in die Frage ein. Er sieht nur den Gesprächs-
  verlauf und die Datenbank.
* **Belege sind eine nachträgliche Textsuche**, keine Aufzeichnung dessen, was das
  Modell tatsächlich gelesen hat. Ein Wert kann wörtlich vorkommen und trotzdem aus
  einer anderen Stelle stammen. Deshalb heißt die Markierung „steht wörtlich in der
  Mail" und nicht „hier hat das Modell es gelesen".
* **Ein Twilio-Zugang für alle Mandanten** – alle Putzpläne kommen von derselben
  WhatsApp-Nummer, der Mandantenname steht in der Nachricht. Eigene Nummern je Mandant
  bräuchten Zugangsdaten in der Datenbank, wie bei den Postfächern.
* **Ein Putzplan-Versand pro Prozess** – doppelt verschickt wird innerhalb eines Prozesses
  nie, über mehrere API-Replicas hinweg ist das nicht abgesichert. Dann
  `CLEANING_DISPATCH_ENABLED=false` setzen und nur eine Instanz versenden lassen.
* **Keine Zustellbestätigung** – „verschickt" heißt: Twilio hat die Nachricht angenommen.
  Ob WhatsApp sie zugestellt hat (Status-Callback), wird nicht ausgewertet.
* **Ein Schlüssel für alle Zugangsdaten** – Schlüssel, Codes und WLAN der Wohnungen hängen
  am selben `ENCRYPTION_KEY` wie die Postfach-Passwörter. Wird er gewechselt, sind die
  gespeicherten Zugänge verloren; die Oberfläche zeigt dann „nicht lesbar" statt falscher Daten.
* **Der Tagesstatus im Kalender gilt für alle Wohnungen zusammen** – rot wird ein Tag nur,
  wenn dort *keine* Wohnung belegt ist und mindestens eine Stornierung liegt. Welche Wohnung
  betroffen ist, steht im Tagesdetail.
* **Zeitzonen** werden ignoriert – alle Zeitstempel sind naiv (lokale Zeit).
* **Ein Postfach** – keine Mandanten-/Nutzertrennung.
* **Kosten**: Import und Chat erzeugen echte OpenAI-Aufrufe.
