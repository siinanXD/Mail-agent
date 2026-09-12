/* Mail Agent - Oberflaeche. Kein Framework, nur fetch + DOM. */

const TYPES = {
  booking:      { label: "Buchung",     css: "buchung" },
  cancellation: { label: "Stornierung", css: "stornierung" },
  change:       { label: "Änderung",    css: "change" },
  request:      { label: "Gastanfrage", css: "request" },
  complaint:    { label: "Beschwerde",  css: "complaint" },
  other:        { label: "Sonstiges",   css: "other" },
};

const $ = (id) => document.getElementById(id);
const state = { types: new Set(), search: "" };

/** Wird bei jedem Abmelden hochgezaehlt. Antworten, die noch fuer eine fruehere
 *  Sitzung unterwegs waren, werden daran erkannt und verworfen. */
let sessionEpoch = 0;

const typeInfo = (key) => TYPES[key] || { label: key, css: "other" };
const color = (key, part) => `var(--${typeInfo(key).css}-${part})`;

function formatDate(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit", year: "numeric" });
}

function formatDateTime(iso) {
  const d = new Date(iso);
  return d.toLocaleString("de-DE", {
    day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options });
  if (response.status === 401) {
    showLogin();
    throw new Error("Nicht angemeldet");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    // detail ist meist ein Text; bei der unbestaetigten Adresse ein Objekt
    // mit reason, damit die Oberflaeche gezielt zum Code-Feld springen kann.
    const detail = body.detail;
    const error = new Error(
      (typeof detail === "string" ? detail : detail?.message) || `Fehler ${response.status}`,
    );
    error.status = response.status;
    error.reason = typeof detail === "object" ? detail?.reason : undefined;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

/* ---------------------------------------------------------------- Anmeldung */

function showLogin() {
  clearSessionData();
  $("app").hidden = true;
  $("chat").hidden = true;
  $("login").hidden = false;
  showAuthView("login");
}

/** Anmelden, Registrieren, Bestaetigen, Passwort vergessen - eine Karte davon. */
function showAuthView(name) {
  for (const card of document.querySelectorAll(".auth-view")) {
    card.hidden = card.dataset.view !== name;
  }
  for (const field of document.querySelectorAll(".auth-view .error, .auth-view .notice")) {
    field.hidden = true;
  }
  const first = document.querySelector(`.auth-view[data-view="${name}"] input`);
  if (first) first.focus();
}

function setMessage(id, text, isError = true) {
  const field = $(id);
  field.textContent = text;
  field.hidden = !text;
  field.classList.toggle("ok", !isError);
}

/** Formular absenden, Fehler an der Karte anzeigen, Doppelklicks abfangen. */
async function submitAuth(form, errorId, action) {
  const button = form.querySelector("button[type=submit]");
  $(errorId).hidden = true;
  button.disabled = true;
  try {
    await action();
  } catch (err) {
    setMessage(errorId, err.message);
  } finally {
    button.disabled = false;
  }
}

/** Entfernt alles, was zur bisherigen Sitzung gehoert. Ohne das saehe ein
 *  anderer Nutzer, der sich ohne Neuladen anmeldet, Chatverlauf, Verlauf und
 *  zuletzt geoeffnete Mail des vorigen Mandanten. */
function clearSessionData() {
  sessionEpoch += 1;
  state.types.clear();
  state.search = "";
  $("search").value = "";
  $("stats").innerHTML = "";
  $("rows").innerHTML = "";
  $("hint").textContent = "Auf eine Zeile klicken, um die Original-Mail und die Belege zu sehen.";
  $("tenant-name").textContent = "";
  $("user-email").textContent = "";
  for (const id of ["d-badge", "d-subject", "d-meta", "d-headers", "d-body", "d-evidence"]) {
    $(id).innerHTML = "";
  }
  closeDetail();
  resetChat();
  toggleChat(false);
  try {
    // Neue Sitzung, neuer Gespraechsfaden.
    localStorage.removeItem("mailagent_thread");
  } catch { /* Speicher gesperrt - dann gibt es auch nichts zu entfernen */ }
}

function showApp() {
  $("login").hidden = true;
  $("app").hidden = false;
  $("chat").hidden = false;
}

/** Zeigt, als wer und fuer welchen Mandanten man angemeldet ist. */
function applySession(session) {
  $("tenant-name").textContent = session.tenant_name || "";
  $("user-email").textContent = session.email || "";
}

const send = (path, body) =>
  api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

/** Adresse, um die es im gerade offenen Schritt geht (Code bestaetigen, Reset). */
let pendingEmail = "";

/** Nach Anmeldung, Bestaetigung oder Reset: rein in die Anwendung. */
async function enterApp() {
  applySession(await api("/api/session"));
  showApp();
  await load();
}

for (const link of document.querySelectorAll("[data-goto]")) {
  link.addEventListener("click", () => showAuthView(link.dataset.goto));
}

$("login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  submitAuth($("login-form"), "login-error", async () => {
    try {
      await send("/api/login", {
        email: $("email").value,
        password: $("password").value,
      });
    } catch (err) {
      if (err.reason === "unverified") {
        // Konto gibt es, nur die Adresse fehlt noch - direkt zum Code-Feld.
        pendingEmail = $("email").value.trim();
        $("verify-email").textContent = pendingEmail;
        showAuthView("verify");
        setMessage("verify-notice", err.message, false);
        return;
      }
      throw err;
    }
    $("password").value = "";
    await enterApp();
  });
});

$("register-form").addEventListener("submit", (event) => {
  event.preventDefault();
  submitAuth($("register-form"), "register-error", async () => {
    pendingEmail = $("reg-email").value.trim();
    const answer = await send("/api/register", {
      email: pendingEmail,
      password: $("reg-password").value,
      company: $("reg-company").value,
    });
    $("reg-password").value = "";
    $("verify-email").textContent = pendingEmail;
    showAuthView("verify");
    setMessage("verify-notice", answer.message, false);
  });
});

$("verify-form").addEventListener("submit", (event) => {
  event.preventDefault();
  submitAuth($("verify-form"), "verify-error", async () => {
    await send("/api/verify", { email: pendingEmail, code: $("verify-code").value });
    $("verify-code").value = "";
    await enterApp();
  });
});

$("verify-resend").addEventListener("click", async () => {
  try {
    const answer = await send("/api/register/resend", { email: pendingEmail });
    setMessage("verify-notice", answer.message, false);
  } catch (err) {
    setMessage("verify-error", err.message);
  }
});

$("reset-form").addEventListener("submit", (event) => {
  event.preventDefault();
  submitAuth($("reset-form"), "reset-error", async () => {
    pendingEmail = $("reset-email").value.trim();
    await send("/api/password-reset", { email: pendingEmail });
    $("reset-confirm-email").textContent = pendingEmail;
    showAuthView("reset-confirm");
  });
});

$("reset-confirm-form").addEventListener("submit", (event) => {
  event.preventDefault();
  submitAuth($("reset-confirm-form"), "reset-confirm-error", async () => {
    await send("/api/password-reset/confirm", {
      email: pendingEmail,
      code: $("reset-code").value,
      password: $("reset-password").value,
    });
    $("reset-code").value = "";
    $("reset-password").value = "";
    $("email").value = pendingEmail;
    showAuthView("login");
    setMessage("login-notice", "Passwort geändert. Bitte neu anmelden.", false);
  });
});

$("logout").addEventListener("click", async () => {
  try {
    await api("/api/logout", { method: "POST" });
  } catch { /* Sitzung ist ohnehin beendet */ }
  showLogin();
});

/* ---------------------------------------------------------------- Verlauf */

function renderStats(counts) {
  const order = ["booking", "cancellation", "change", "request", "complaint"];
  $("stats").innerHTML = order
    .map((key) => `
      <div class="tile">
        <span>${typeInfo(key).label}</span>
        <strong style="color:${color(key, "fg")}">${counts[key] || 0}</strong>
      </div>`)
    .join("");
}

function renderChips() {
  const keys = ["booking", "cancellation", "change", "request", "complaint"];
  const all = `<button class="chip" data-type="" aria-pressed="${state.types.size === 0}"
    style="background:${state.types.size === 0 ? "var(--brand)" : "var(--other-bg)"};
           color:${state.types.size === 0 ? "#fff" : "var(--other-fg)"}">Alle</button>`;

  $("chips").innerHTML = all + keys.map((key) => {
    const active = state.types.has(key);
    return `<button class="chip" data-type="${key}" aria-pressed="${active}"
      style="background:${color(key, "bg")};color:${color(key, "fg")}">${typeInfo(key).label}</button>`;
  }).join("");

  for (const button of $("chips").querySelectorAll(".chip")) {
    button.addEventListener("click", () => {
      const key = button.dataset.type;
      if (!key) state.types.clear();
      else if (state.types.has(key)) state.types.delete(key);
      else state.types.add(key);
      load();
    });
  }
}

function renderRows(entries) {
  const rows = $("rows");
  if (!entries.length) {
    rows.innerHTML = `<p class="empty">Keine Vorgänge gefunden.
      Wurden schon E-Mails importiert (<code>POST /emails/import</code>)?</p>`;
    return;
  }

  rows.innerHTML = entries.map((entry) => {
    const info = typeInfo(entry.email_type);
    const unit = entry.unit
      ? (entry.unit_source === "verknuepft"
          ? escapeHtml(entry.unit)
          : `<em>${escapeHtml(entry.unit)} (aus dem Text)</em>`)
      : "—";
    return `
      <button class="row entry" data-id="${entry.email_id}">
        <span class="stripe" style="background:${color(entry.email_type, "fg")}"></span>
        <span class="c-date">${formatDate(entry.received_at)}</span>
        <span class="c-type"><span class="badge"
          style="background:${color(entry.email_type, "bg")};color:${color(entry.email_type, "fg")}">
          ${info.label}</span></span>
        <span class="c-guest">${escapeHtml(entry.guest_name || "—")}</span>
        <span class="c-summary" title="${escapeHtml(entry.summary)}">${escapeHtml(entry.summary)}</span>
        <span class="c-unit">${unit}</span>
        <span class="c-ref">${escapeHtml(entry.booking_reference || "—")}</span>
      </button>`;
  }).join("");

  for (const row of rows.querySelectorAll(".entry")) {
    row.addEventListener("click", () => openDetail(row.dataset.id));
  }
}

async function load() {
  renderChips();
  const params = new URLSearchParams();
  for (const type of state.types) params.append("types", type);
  if (state.search) params.set("search", state.search);

  const epoch = sessionEpoch;
  try {
    const data = await api(`/api/timeline?${params}`);
    if (epoch !== sessionEpoch) return; // inzwischen abgemeldet
    renderStats(data.counts_by_type);
    renderRows(data.entries);
    $("hint").textContent = `${data.count} Vorgänge · auf eine Zeile klicken für die Original-Mail und die Belege.`;
  } catch (err) {
    if (epoch !== sessionEpoch) return;
    if (err.message !== "Nicht angemeldet") {
      $("rows").innerHTML = `<p class="empty">${escapeHtml(err.message)}</p>`;
    }
  }
}

$("refresh").addEventListener("click", load);

let searchTimer;
$("search").addEventListener("input", (event) => {
  clearTimeout(searchTimer);
  state.search = event.target.value.trim();
  searchTimer = setTimeout(load, 250);
});

/* ---------------------------------------------------------------- Detail */

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

/** Setzt <mark> um die vom Server gemeldeten Fundstellen.
 *  Die Offsets zaehlen Unicode-Zeichen (Python), String.slice aber UTF-16-
 *  Einheiten - ein Emoji davor wuerde jede Markierung verschieben. Deshalb
 *  wird ueber ein Array aus Zeichen geschnitten. */
function highlight(body, ranges) {
  if (!ranges.length) return escapeHtml(body);
  const chars = Array.from(body);
  const part = (from, to) => escapeHtml(chars.slice(from, to).join(""));
  let out = "";
  let cursor = 0;
  for (const { start, end } of ranges) {
    if (start < cursor || start > chars.length) continue;
    out += part(cursor, start);
    out += `<mark>${part(start, end)}</mark>`;
    cursor = end;
  }
  return out + part(cursor);
}

async function openDetail(emailId) {
  const epoch = sessionEpoch;
  const detail = await api(`/api/emails/${emailId}`);
  if (epoch !== sessionEpoch) return; // inzwischen abgemeldet
  const info = typeInfo(detail.email_type);

  const badge = $("d-badge");
  badge.textContent = info.label;
  badge.style.background = color(detail.email_type, "bg");
  badge.style.color = color(detail.email_type, "fg");

  $("d-subject").textContent = detail.subject || "(kein Betreff)";
  $("d-meta").textContent =
    `E-Mail-ID ${detail.email_id} · ${detail.provider_message_id}`;

  $("d-headers").innerHTML = `
    <dt>Von</dt><dd>${escapeHtml(detail.sender)}</dd>
    <dt>An</dt><dd>${escapeHtml(detail.recipient)}</dd>
    <dt>Datum</dt><dd>${formatDateTime(detail.received_at)}</dd>
    <dt>Betreff</dt><dd>${escapeHtml(detail.subject)}</dd>`;

  $("d-body").innerHTML = highlight(detail.body, detail.highlights);

  $("d-evidence").innerHTML = detail.evidence.length
    ? detail.evidence.map((item) => `
        <li>
          <span class="ev-label">${escapeHtml(item.label)}</span>
          <span class="ev-value">${escapeHtml(item.value)}</span>
          <span class="ev-tag ${item.found ? "ev-found" : "ev-derived"}">
            ${item.found ? "wörtlich in der Mail" : "abgeleitet, nicht wörtlich"}
          </span>
        </li>`).join("")
    : `<li class="muted small">Aus dieser E-Mail wurden keine strukturierten
       Daten extrahiert – sie ist als „${escapeHtml(info.label)}" abgelegt.</li>`;

  $("overlay").hidden = false;
}

function closeDetail() { $("overlay").hidden = true; }

$("close").addEventListener("click", closeDetail);
$("overlay").addEventListener("click", (event) => {
  if (event.target === $("overlay")) closeDetail();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeDetail();
});

/* ---------------------------------------------------------------- Assistent */

const SUGGESTIONS = [
  "Wie viele Stornierungen gab es letzte Woche?",
  "Welche Buchungen hat die FeWo Seeblick?",
  "Wer hat wegen eines Flugausfalls storniert?",
  "Gab es Umbuchungen?",
];

/** Eigene Thread-ID je Browser, damit der Verlauf beim Neuladen bleibt. */
function threadId() {
  let id = localStorage.getItem("mailagent_thread");
  if (!id) {
    id = `web-${Math.random().toString(36).slice(2, 10)}`;
    localStorage.setItem("mailagent_thread", id);
  }
  return id;
}

/** Minimales Markdown: **fett**, Aufzählungen, Zeilenumbrüche. */
function formatAnswer(raw) {
  const lines = escapeHtml(raw).split("\n");
  let html = "";
  let inList = false;

  for (const line of lines) {
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    if (bullet) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${bullet[1]}</li>`;
      continue;
    }
    if (inList) { html += "</ul>"; inList = false; }
    if (line.trim()) html += `${line}<br>`;
  }
  if (inList) html += "</ul>";

  return linkReports(html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>"));
}

/** Nur Download-Pfade erzeugter Putzplaene - kein beliebiges URL-Schema. */
const REPORT_PATH = String.raw`/reports/cleaning-plan/[\w.-]+\.xlsx`;
// Nackter Pfad nur, wenn er nicht mitten in einer URL oder einem Wort steht -
// sonst wuerde aus "https://fremd.example/reports/..." ein lokaler Link.
const REPORT_LINK = new RegExp(
  String.raw`\[([^\]\n]+)\]\((${REPORT_PATH})\)|(?<![\w.:/-])(${REPORT_PATH})`, "g");

/** Macht die vom Putzplan-Tool gelieferten Pfade klickbar - als Markdown-Link
 *  "[Text](/reports/...)" oder als nackter Pfad. Der Text ist zu diesem
 *  Zeitpunkt bereits escaped; der Pfad kann nur aus harmlosen Zeichen bestehen. */
function linkReports(html) {
  return html.replace(REPORT_LINK, (match, text, linkedPath, barePath) => {
    const path = linkedPath || barePath;
    return `<a href="${path}" download>${text || path}</a>`;
  });
}

function addMessage(role, html, tools) {
  const log = $("chat-log");
  log.querySelector(".chat-empty")?.remove();

  const node = document.createElement("div");
  node.className = `msg ${role}`;
  node.innerHTML = html;
  if (tools && tools.length) {
    node.innerHTML += `<span class="tools">Werkzeuge: ${escapeHtml(tools.join(", "))}</span>`;
  }
  log.appendChild(node);
  log.scrollTop = log.scrollHeight;
  return node;
}

function renderSuggestions() {
  $("chat-suggestions").innerHTML = SUGGESTIONS
    .map((s) => `<button type="button">${escapeHtml(s)}</button>`).join("");
  for (const button of $("chat-suggestions").querySelectorAll("button")) {
    button.addEventListener("click", () => sendMessage(button.textContent));
  }
}

function resetChat() {
  $("chat-log").innerHTML =
    `<p class="chat-empty">Frag mich etwas zu den Buchungen im Postfach.<br>
     Ich schaue in der Datenbank nach und sage dazu, welche Werkzeuge ich benutzt habe.</p>`;
  $("chat-suggestions").hidden = false;
}

async function sendMessage(message) {
  const trimmed = (message || "").trim();
  if (!trimmed) return;

  $("chat-message").value = "";
  $("chat-suggestions").hidden = true;
  addMessage("user", escapeHtml(trimmed));

  const send = $("chat-send");
  send.disabled = true;
  const pending = addMessage("bot", '<span class="typing">denkt nach …</span>');
  const epoch = sessionEpoch;

  try {
    const data = await api("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ thread_id: threadId(), message: trimmed }),
    });
    // Antwort fuer eine beendete Sitzung nicht mehr anzeigen.
    if (epoch !== sessionEpoch) return;
    pending.innerHTML = formatAnswer(data.answer || "(keine Antwort)");
    if (data.tool_calls && data.tool_calls.length) {
      pending.innerHTML +=
        `<span class="tools">Werkzeuge: ${escapeHtml(data.tool_calls.join(", "))}</span>`;
    }
  } catch (err) {
    if (epoch !== sessionEpoch) return;
    pending.className = "msg error";
    pending.textContent = err.message;
  } finally {
    send.disabled = false;
    if (epoch === sessionEpoch) {
      $("chat-log").scrollTop = $("chat-log").scrollHeight;
      $("chat-message").focus();
    }
  }
}

function toggleChat(open) {
  $("chat-panel").hidden = !open;
  $("chat-toggle").hidden = open;
  $("chat-toggle").setAttribute("aria-expanded", String(open));
  if (open) $("chat-message").focus();
}

$("chat-toggle").addEventListener("click", () => toggleChat(true));
$("chat-close").addEventListener("click", () => toggleChat(false));
$("chat-form").addEventListener("submit", (event) => {
  event.preventDefault();
  sendMessage($("chat-message").value);
});
$("chat-reset").addEventListener("click", async () => {
  const id = threadId();
  localStorage.removeItem("mailagent_thread");
  resetChat();
  try {
    await api(`/api/chat/${encodeURIComponent(id)}`, { method: "DELETE" });
  } catch { /* Verlauf ist ohnehin nur im Speicher */ }
});

/* ---------------------------------------------------------------- Start */

(async function start() {
  renderSuggestions();
  resetChat();
  try {
    const session = await api("/api/session");
    $("logout").hidden = false;
    if (session.authenticated) {
      applySession(session);
      showApp();
      await load();
    } else {
      showLogin();
    }
  } catch {
    showLogin();
  }
})();
