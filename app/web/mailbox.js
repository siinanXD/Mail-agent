/* Postfach verbinden und dem Agenten bei der Arbeit zusehen.
   Laeuft nach app.js und nutzt dessen Helfer ($, api, send, setMessage,
   submitAuth, formatDateTime, escapeHtml, sessionEpoch). */

/** Vorlagen fuer die gaengigen Anbieter - spart die Suche nach dem richtigen
 *  Servernamen. "app" markiert Anbieter, die fuer IMAP ein eigenes App-Passwort
 *  verlangen; mit dem Kontopasswort scheitert die Anmeldung. */
const PROVIDERS = [
  { id: "gmail", label: "Gmail", host: "imap.gmail.com", port: 993, app: true },
  { id: "outlook", label: "Outlook / Microsoft 365", host: "outlook.office365.com", port: 993, app: true },
  { id: "gmx", label: "GMX", host: "imap.gmx.net", port: 993 },
  { id: "webde", label: "WEB.DE", host: "imap.web.de", port: 993 },
  { id: "ionos", label: "IONOS / 1&1", host: "imap.ionos.de", port: 993 },
  { id: "strato", label: "Strato", host: "imap.strato.de", port: 993 },
  { id: "mailbox", label: "mailbox.org", host: "imap.mailbox.org", port: 993 },
  { id: "posteo", label: "Posteo", host: "posteo.de", port: 993 },
];

const STATUS_TEXT = {
  ok: "verbunden",
  auth_error: "Zugang abgelehnt",
  tls_error: "Verschlüsselung fehlgeschlagen",
  folder_missing: "Ordner fehlt",
  unreachable: "nicht erreichbar",
  unknown: "nicht geprüft",
};

/** Grün = läuft. Gelb = erledigt sich vielleicht von allein (Server gerade weg).
 *  Rot = da muss jemand ran (Passwort, Zertifikat, Ordner). Grau = nie geprüft. */
const STATUS_CLASS = {
  ok: "ok",
  unreachable: "warn",
  auth_error: "bad",
  tls_error: "bad",
  folder_missing: "bad",
  unknown: "",
};

let activityTimer = null;

function fillProviders() {
  const select = $("provider");
  for (const provider of PROVIDERS) {
    const option = document.createElement("option");
    option.value = provider.id;
    option.textContent = provider.label;
    select.append(option);
  }
}

function applyStatus(status, message) {
  const key = STATUS_TEXT[status] ? status : "unknown";
  for (const id of ["status-dot", "mailbox-status-dot"]) {
    $(id).className = `status-dot ${STATUS_CLASS[key]}`.trim();
  }
  $("status-text").textContent = `Postfach ${STATUS_TEXT[key]}`;
  if (message || !$("mailbox-status-text").textContent) {
    $("mailbox-status-text").textContent = message || STATUS_TEXT[key];
  }
}

function applyMailbox(info) {
  if (info.configured) {
    $("mb-host").value = info.host || "";
    $("mb-port").value = info.port || 993;
    $("mb-user").value = info.username || "";
    $("mb-folder").value = info.folder || "INBOX";
    $("mb-ssl").checked = info.use_ssl !== false;
    $("mb-since").value = info.since_date || "";
    $("mb-password-hint").textContent =
      "Gespeichert. Feld leer lassen, um es unverändert zu übernehmen.";
  }
  applyStatus(info.status, info.status_message);
}

async function loadMailbox() {
  try {
    applyMailbox(await api("/api/mailbox"));
  } catch { /* nicht angemeldet - dann zeigt ohnehin die Anmeldung */ }
}

function openSettings() {
  $("mailbox-overlay").hidden = false;
  $("mb-error").hidden = true;
  $("mb-notice").hidden = true;
  loadMailbox();
  refreshActivity();
}

function closeSettings() {
  $("mailbox-overlay").hidden = true;
}

function settingsPayload() {
  return {
    host: $("mb-host").value.trim(),
    username: $("mb-user").value.trim(),
    password: $("mb-password").value || null,
    port: Number($("mb-port").value) || 993,
    folder: $("mb-folder").value.trim() || "INBOX",
    use_ssl: $("mb-ssl").checked,
    since_date: $("mb-since").value || null,
    active: true,
  };
}

$("mailbox-open").addEventListener("click", openSettings);
$("mailbox-close").addEventListener("click", closeSettings);
$("mailbox-overlay").addEventListener("click", (event) => {
  if (event.target === $("mailbox-overlay")) closeSettings();
});

$("provider").addEventListener("change", () => {
  const provider = PROVIDERS.find((entry) => entry.id === $("provider").value);
  $("provider-hint").textContent = provider && provider.app
    ? "Dieser Anbieter verlangt für IMAP ein eigenes App-Passwort – das normale Kontopasswort wird abgelehnt."
    : "";
  if (!provider) return;
  $("mb-host").value = provider.host;
  $("mb-port").value = provider.port;
  $("mb-ssl").checked = true;
});

for (const button of document.querySelectorAll("#since-presets .chip")) {
  button.addEventListener("click", () => {
    const value = button.dataset.since;
    if (!value) {
      $("mb-since").value = "";
      return;
    }
    const day = new Date();
    if (value !== "today") day.setDate(day.getDate() - Number(value));
    $("mb-since").value = day.toISOString().slice(0, 10);
  });
}

$("mb-test").addEventListener("click", async () => {
  const button = $("mb-test");
  button.disabled = true;
  $("mb-error").hidden = true;
  setMessage("mb-notice", "Verbindung wird geprüft …", false);
  try {
    const result = await send("/api/mailbox/test?count_waiting=true", settingsPayload());
    applyStatus(result.status, result.message);
    if (result.ok) {
      const waiting = typeof result.waiting === "number"
        ? ` Ab dem gewählten Datum liegen ${result.waiting} Mails im Ordner.`
        : "";
      setMessage("mb-notice", result.message + waiting, false);
    } else {
      $("mb-notice").hidden = true;
      setMessage("mb-error", result.message);
    }
  } catch (err) {
    $("mb-notice").hidden = true;
    setMessage("mb-error", err.message);
  } finally {
    button.disabled = false;
  }
});

$("mailbox-form").addEventListener("submit", (event) => {
  event.preventDefault();
  submitAuth($("mailbox-form"), "mb-error", async () => {
    const info = await api("/api/mailbox", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settingsPayload()),
    });
    $("mb-password").value = "";
    applyMailbox(info);
    setMessage(
      "mb-notice",
      info.status === "ok"
        ? "Gespeichert, die Verbindung steht."
        : `Gespeichert. ${info.status_message || ""}`,
      info.status === "ok",
    );
    refreshActivity();
  });
});

/* ---------------------------------------------------------------- Aktivität */

function describeRun(run) {
  if (run.status === "running") return "Postfach wird abgerufen …";
  return run.message || (run.imported ? `${run.imported} neue Mail(s)` : "Keine neuen Mails.");
}

function renderRuns(runs) {
  $("run-list").innerHTML = runs.length
    ? runs
        .map(
          (run) => `
      <li>
        <span class="when">${formatDateTime(run.started_at)}</span>
        <span class="${run.status === "error" ? "failed" : ""}">${escapeHtml(describeRun(run))}</span>
      </li>`,
        )
        .join("")
    : '<li class="muted">Noch kein Lauf.</li>';
}

async function refreshActivity() {
  const epoch = sessionEpoch;
  let data;
  try {
    data = await api("/api/activity");
  } catch {
    return; // abgemeldet oder Server weg - der naechste Anlauf klaert es
  }
  if (epoch !== sessionEpoch) return; // inzwischen abgemeldet

  const latest = data.runs[0];
  $("activity-spinner").hidden = !data.busy;
  $("activity-text").textContent = data.busy
    ? "Der Agent liest gerade das Postfach …"
    : latest
      ? `Zuletzt ${formatDateTime(latest.started_at)}: ${describeRun(latest)}`
      : "Noch kein Abruf gelaufen.";
  $("activity-next").textContent = data.next_run
    ? `Nächster Abruf ${formatDateTime(data.next_run)}`
    : "";
  applyStatus(data.mailbox_status, null);
  renderRuns(data.runs);

  // Waehrend der Agent arbeitet oefter nachsehen, sonst sparsam.
  clearTimeout(activityTimer);
  activityTimer = setTimeout(refreshActivity, data.busy ? 3000 : 20000);
}

$("poll-now").addEventListener("click", async () => {
  const button = $("poll-now");
  button.disabled = true;
  try {
    await send("/api/mailbox/poll", {});
    $("activity-spinner").hidden = false;
    $("activity-text").textContent = "Abruf gestartet …";
    setTimeout(refreshActivity, 500);
  } catch (err) {
    $("activity-text").textContent = err.message;
  } finally {
    button.disabled = false;
  }
});

fillProviders();

/** Anmeldung und Abmeldung liegen in app.js, das vor dieser Datei laeuft -
 *  deshalb der Weg ueber window statt ueber einen direkten Aufruf. */
window.mailboxUi = {
  refresh: refreshActivity,
  stop() {
    clearTimeout(activityTimer);
    closeSettings();
  },
};
