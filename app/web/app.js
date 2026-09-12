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

/** "2026-09-12" -> "12.09.2026" - ohne new Date(), das Datumsangaben als UTC liest. */
function formatIsoDate(iso) {
  const [year, month, day] = iso.split("-");
  return `${day}.${month}.${year}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options });
  if (response.status === 401) {
    showLogin();
    throw new Error("Nicht angemeldet");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = body.detail;
    // 422 liefert eine Liste von Feldfehlern statt eines Satzes.
    if (Array.isArray(detail)) throw new Error("Eingabe unvollständig oder ungültig.");
    // Sonst meist ein Text; bei der unbestaetigten Adresse ein Objekt mit reason,
    // damit die Oberflaeche gezielt zum Code-Feld springen kann.
    const error = new Error(
      (typeof detail === "string" ? detail : detail?.message) || `Fehler ${response.status}`,
    );
    error.status = response.status;
    error.reason = typeof detail === "object" ? detail?.reason : undefined;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

const jsonRequest = (method, body) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

/** Kurze Rueckmeldung in einer Statuszeile - immer als Text, nie als HTML. */
function flash(id, text, isError = false) {
  const node = $(id);
  node.textContent = text;
  node.classList.toggle("status-error", isError);
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
 *  anderer Nutzer, der sich ohne Neuladen anmeldet, Chatverlauf, Verlauf,
 *  Kalender, Wohnungen und zuletzt geoeffnete Mail des vorigen Mandanten. */
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
  clearCalendar();
  clearUnitsPage();
  clearStaffPage();
  // mailbox.js wird nach dieser Datei geladen und meldet sich hier an.
  window.mailboxUi?.stop();
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
  // Postfachstatus und Aktivitaetsanzeige - siehe mailbox.js.
  window.mailboxUi?.refresh();
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
  // Zeigt den Bereich aus der Adresse (Verlauf, Wohnungen, Mitarbeiter).
  await showView();
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

/* ---------------------------------------------------------------- Bereiche */

const VIEWS = { timeline: "view-timeline", units: "view-units", staff: "view-staff" };
const ROUTES = { "#/wohnungen": "units", "#/mitarbeiter": "staff" };

function currentView() {
  return ROUTES[location.hash] || "timeline";
}

async function showView() {
  const view = currentView();
  for (const [key, id] of Object.entries(VIEWS)) $(id).hidden = key !== view;
  for (const tab of document.querySelectorAll(".tab")) {
    if (tab.dataset.view === view) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  }
  if (view === "staff") await loadStaffPage();
  else if (view === "units") await loadUnits();
  else await Promise.all([load(), loadCalendar()]);
}

window.addEventListener("hashchange", () => {
  if (!$("app").hidden) showView();
});

$("refresh").addEventListener("click", () => showView());

/* ---------------------------------------------------------------- Belegungskalender */

const WEEKDAYS_SHORT = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
const calState = { year: null, month: null, data: null };

function clearCalendar() {
  calState.year = null;
  calState.month = null;
  calState.data = null;
  $("calendar").innerHTML = "";
  $("cal-label").textContent = "Belegung";
  flash("cal-status", "");
  closeDay();
  closeBooking();
}

async function loadCalendar(year, month) {
  const epoch = sessionEpoch;
  const params = new URLSearchParams();
  if (year && month) {
    params.set("year", year);
    params.set("month", month);
  }
  try {
    const data = await api(`/api/calendar?${params}`);
    if (epoch !== sessionEpoch) return;
    calState.year = data.year;
    calState.month = data.month;
    calState.data = data;
    renderCalendar(data);
  } catch (err) {
    if (epoch !== sessionEpoch || err.message === "Nicht angemeldet") return;
    $("calendar").innerHTML = `<p class="empty">${escapeHtml(err.message)}</p>`;
  }
}

function renderCalendar(data) {
  $("cal-label").textContent = `Belegung · ${data.label}`;
  const cells = WEEKDAYS_SHORT.map((tag) => `<div class="cal-weekday">${tag}</div>`);

  for (const week of data.weeks) {
    for (const day of week) {
      const klassen = ["cal-day", day.status];
      if (!day.in_month) klassen.push("outside");
      if (day.changed) klassen.push("geaendert");
      if (day.date === data.today) klassen.push("today");

      const eintraege = day.bookings.slice(0, 2).map((b) => `
        <span class="cal-entry${b.cancelled ? " cancelled" : ""}" title="${escapeHtml(`${b.unit} · ${b.guest_name}`)}">
          ${escapeHtml(b.unit)}
        </span>`).join("");
      const mehr = day.bookings.length > 2
        ? `<span class="cal-more">+${day.bookings.length - 2} weitere</span>` : "";

      cells.push(`
        <button type="button" class="${klassen.join(" ")}" data-day="${day.date}"
                aria-label="${escapeHtml(`${formatIsoDate(day.date)}, ${day.status}`)}">
          <span class="cal-daynum">${Number(day.date.slice(8, 10))}</span>
          ${eintraege}${mehr}
        </button>`);
    }
  }
  $("calendar").innerHTML = cells.join("");
  for (const button of $("calendar").querySelectorAll("[data-day]")) {
    button.addEventListener("click", () => openDay(button.dataset.day));
  }

  const belegt = data.occupied_days;
  flash("cal-status", belegt === 1
    ? "1 Tag im Monat ist belegt."
    : `${belegt} Tage im Monat sind belegt.`);
}

$("cal-prev").addEventListener("click", () => {
  if (calState.data) loadCalendar(calState.data.previous.year, calState.data.previous.month);
});
$("cal-next").addEventListener("click", () => {
  if (calState.data) loadCalendar(calState.data.next.year, calState.data.next.month);
});
$("cal-today").addEventListener("click", () => loadCalendar());

/* --- Tag: welche Wohnungen sind belegt? --- */

function dayOf(iso) {
  if (!calState.data) return null;
  for (const week of calState.data.weeks) {
    for (const day of week) if (day.date === iso) return day;
  }
  return null;
}

function openDay(iso) {
  const day = dayOf(iso);
  if (!day) return;
  const wochentag = WEEKDAYS_SHORT[(new Date(`${iso}T00:00:00`).getDay() + 6) % 7];
  $("day-title").textContent = `${wochentag}, ${formatIsoDate(iso)}`;
  $("day-sub").textContent = day.bookings.length
    ? `${day.active_label || ""}${day.bookings.length} Buchung(en) an diesem Tag`
    : "An diesem Tag ist nichts gebucht.";

  $("day-list").innerHTML = day.bookings.map((b) => {
    const marken = [];
    if (b.arrival) marken.push("Anreise");
    if (b.departure) marken.push("Abreise");
    if (b.changed) marken.push("umgebucht");
    if (b.cancelled) marken.push("storniert");
    return `
      <button type="button" class="day-entry${b.cancelled ? " cancelled" : ""}" data-booking="${b.booking_id}">
        <span>
          <span class="day-unit">${escapeHtml(b.unit)}</span>
          <span class="day-meta"> · ${escapeHtml(b.guest_name || "—")}</span>
          <br><span class="day-meta">${formatIsoDate(b.arrival_date)} – ${formatIsoDate(b.departure_date)} · ${escapeHtml(b.booking_reference)}</span>
        </span>
        <span class="day-meta">${marken.join(" · ")}</span>
      </button>`;
  }).join("") || `<p class="empty">Keine Buchung an diesem Tag.</p>`;

  for (const button of $("day-list").querySelectorAll("[data-booking]")) {
    button.addEventListener("click", () => openBooking(button.dataset.booking));
  }
  $("day-overlay").hidden = false;
}

function closeDay() { $("day-overlay").hidden = true; }

$("day-close").addEventListener("click", closeDay);
$("day-overlay").addEventListener("click", (event) => {
  if (event.target === $("day-overlay")) closeDay();
});

/* --- Buchungsdetail --- */

async function openBooking(id) {
  const epoch = sessionEpoch;
  let detail;
  try {
    detail = await api(`/api/bookings/${id}`);
  } catch (err) {
    if (epoch === sessionEpoch && err.message !== "Nicht angemeldet") flash("cal-status", err.message, true);
    return;
  }
  if (epoch !== sessionEpoch) return;

  const storniert = detail.status === "cancelled";
  const badge = $("booking-badge");
  badge.textContent = storniert ? "Storniert" : "Buchung";
  badge.style.background = color(storniert ? "cancellation" : "booking", "bg");
  badge.style.color = color(storniert ? "cancellation" : "booking", "fg");
  $("booking-title").textContent = detail.guest_name || "Buchung";

  const zeilen = [
    ["Buchungsnummer", detail.booking_reference],
    ["Objekt", detail.unit || "Nicht zugeordnet"],
    ["Anreise", detail.arrival_date ? formatIsoDate(detail.arrival_date) : "—"],
    ["Abreise", detail.departure_date ? formatIsoDate(detail.departure_date) : "—"],
    ["Nächte", detail.nights ?? "—"],
    ["Status", storniert ? "storniert" : "bestätigt"],
  ];

  const LABELS = { arrival_date: "Anreise", departure_date: "Abreise", unit: "Objekt", guest_name: "Gast" };
  const verlauf = detail.changes.map((c) => `
    <li><strong>${escapeHtml(LABELS[c.field] || c.field)}</strong> am ${formatIsoDate(c.changed_at)}:
      ${escapeHtml(c.old_value || "—")} → ${escapeHtml(c.new_value || "—")}</li>`).join("");

  const storno = detail.cancellation
    ? `<li><strong>Storniert</strong> am ${formatIsoDate(detail.cancellation.cancelled_at)}${
        detail.cancellation.reason ? ` · Grund: ${escapeHtml(detail.cancellation.reason)}` : ""}</li>`
    : "";

  $("booking-body").innerHTML = `
    <dl class="booking-grid">
      ${zeilen.map(([k, v]) => `<dt>${k}</dt><dd>${escapeHtml(String(v))}</dd>`).join("")}
    </dl>
    ${verlauf || storno ? `<ul class="booking-history">${verlauf}${storno}</ul>` : ""}
    ${detail.source_email_id ? `<div class="dialog-actions"><button type="button" class="btn-ghost small" id="booking-mail">Original-Mail öffnen</button></div>` : ""}`;

  const mailButton = $("booking-mail");
  if (mailButton) {
    mailButton.addEventListener("click", () => {
      closeBooking();
      closeDay();
      openDetail(detail.source_email_id);
    });
  }
  $("booking-overlay").hidden = false;
}

function closeBooking() { $("booking-overlay").hidden = true; }

$("booking-close").addEventListener("click", closeBooking);
$("booking-overlay").addEventListener("click", (event) => {
  if (event.target === $("booking-overlay")) closeBooking();
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
  if (event.key === "Escape") {
    closeDetail();
    closeStaffForm();
    closeUnitForm();
    closeBooking();
    closeDay();
  }
});

/* ---------------------------------------------------------------- Wohnungen */

const unitState = { units: [], editing: null, encryption: true };

function clearUnitsPage() {
  unitState.units = [];
  unitState.editing = null;
  $("unit-list").innerHTML = "";
  flash("unit-status", "");
  $("unit-crypto-notice").hidden = true;
  closeUnitForm();
}

async function loadUnits() {
  const epoch = sessionEpoch;
  try {
    const data = await api("/api/units");
    if (epoch !== sessionEpoch) return;
    unitState.units = data.units;
    unitState.encryption = data.encryption_configured;
    $("unit-crypto-notice").hidden = data.encryption_configured;
    renderUnits();
  } catch (err) {
    if (epoch !== sessionEpoch || err.message === "Nicht angemeldet") return;
    $("unit-list").innerHTML = `<p class="empty">${escapeHtml(err.message)}</p>`;
  }
}

function fact(label, value, suffix = "") {
  return value === null || value === undefined || value === ""
    ? "" : `<span class="unit-fact">${escapeHtml(label)}: ${escapeHtml(String(value))}${suffix}</span>`;
}

function block(label, value) {
  return value
    ? `<div class="unit-block"><strong>${escapeHtml(label.toUpperCase())}</strong>${escapeHtml(value)}</div>`
    : "";
}

function renderUnits() {
  const list = $("unit-list");
  if (!unitState.units.length) {
    list.innerHTML = `<p class="empty">Noch keine Wohnungen. Sie entstehen automatisch,
      sobald Buchungsmails importiert wurden.</p>`;
    return;
  }

  list.innerHTML = unitState.units.map((unit) => {
    const fakten = [
      fact("Zimmer", unit.rooms), fact("Betten", unit.beds),
      fact("Größe", unit.size_sqm, " m²"), fact("max. Gäste", unit.max_guests),
    ].join("");
    const zugang = unit.access
      ? block("Zugang (verschlüsselt gespeichert)", unit.access)
      : (unit.access_readable ? "" : `<div class="unit-block"><strong>ZUGANG</strong>
          <span class="status-error">hinterlegt, aber mit dem aktuellen ENCRYPTION_KEY nicht lesbar</span></div>`);
    const leer = !unit.description && !unit.house_rules && !unit.cleaning_window
      && !unit.address && !fakten && !unit.access;

    return `
      <article class="unit-card">
        <header>
          <h3>${escapeHtml(unit.name)}</h3>
          <button type="button" class="btn-ghost small" data-unit="${unit.id}">Bearbeiten</button>
        </header>
        ${fakten ? `<div class="unit-facts">${fakten}</div>` : ""}
        ${leer ? `<p class="unit-empty">Noch kein Profil hinterlegt.</p>` : ""}
        ${block("Beschreibung", unit.description)}
        ${block("Hausregeln", unit.house_rules)}
        ${block("Reinigung", unit.cleaning_window)}
        ${block("Adresse", [unit.address, unit.floor].filter(Boolean).join(" · "))}
        ${zugang}
        ${unit.staff.length ? `<div class="unit-block"><strong>REINIGUNG ÜBERNIMMT</strong>
          <span class="unit-staff">${unit.staff.map((s) => `<span class="unit-fact">${escapeHtml(s.name)}</span>`).join("")}</span></div>` : ""}
      </article>`;
  }).join("");

  for (const button of list.querySelectorAll("[data-unit]")) {
    const unit = unitState.units.find((u) => String(u.id) === button.dataset.unit);
    button.addEventListener("click", () => openUnitForm(unit));
  }
}

function openUnitForm(unit) {
  unitState.editing = unit.id;
  $("unit-form-title").textContent = unit.name;
  $("unit-description").value = unit.description || "";
  $("unit-rules").value = unit.house_rules || "";
  $("unit-rooms").value = unit.rooms ?? "";
  $("unit-beds").value = unit.beds ?? "";
  $("unit-size").value = unit.size_sqm ?? "";
  $("unit-guests").value = unit.max_guests ?? "";
  $("unit-cleaning").value = unit.cleaning_window || "";
  $("unit-address").value = unit.address || "";
  $("unit-floor").value = unit.floor || "";
  $("unit-access").value = unit.access || "";
  $("unit-access").disabled = !unitState.encryption;
  $("unit-access-hint").textContent = unitState.encryption
    ? "Verschlüsselt gespeichert, wie die Postfach-Passwörter. Leeres Feld löscht den Eintrag."
    : "Ohne ENCRYPTION_KEY lassen sich Zugangsdaten nicht speichern.";
  $("unit-error").hidden = true;
  $("unit-overlay").hidden = false;
  $("unit-description").focus();
}

function closeUnitForm() {
  $("unit-overlay").hidden = true;
  unitState.editing = null;
}

const zahl = (id) => ($(id).value === "" ? null : Number($(id).value));

$("unit-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const id = unitState.editing;
  if (!id) return;
  const body = {
    description: $("unit-description").value.trim() || null,
    house_rules: $("unit-rules").value.trim() || null,
    rooms: zahl("unit-rooms"),
    beds: zahl("unit-beds"),
    size_sqm: zahl("unit-size"),
    max_guests: zahl("unit-guests"),
    cleaning_window: $("unit-cleaning").value.trim() || null,
    address: $("unit-address").value.trim() || null,
    floor: $("unit-floor").value.trim() || null,
    access: $("unit-access").value.trim() || null,
  };
  const epoch = sessionEpoch;
  try {
    const gespeichert = await api(`/api/units/${id}`, jsonRequest("PUT", body));
    if (epoch !== sessionEpoch) return;
    closeUnitForm();
    flash("unit-status", `${gespeichert.name} gespeichert.`);
    await loadUnits();
  } catch (err) {
    if (epoch !== sessionEpoch) return;
    $("unit-error").textContent = err.message;
    $("unit-error").hidden = false;
  }
});

$("unit-cancel").addEventListener("click", closeUnitForm);
$("unit-abort").addEventListener("click", closeUnitForm);
$("unit-overlay").addEventListener("click", (event) => {
  if (event.target === $("unit-overlay")) closeUnitForm();
});

/* ---------------------------------------------------------------- Mitarbeiter & Putzplan */

const WEEKDAY_NAMES = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"];
const SHORT_DAYS = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"]; // Reihenfolge von Date.getDay()

const staffState = {
  staff: [],
  units: [],
  editingId: null,
  formUnits: [],
  weekStart: null,
  preview: null,
};

/** Speichern der Wohnungen je Mitarbeiter, leicht verzoegert - wer mehrere
 *  Haekchen hintereinander setzt, loest nur eine Anfrage aus. */
const assignmentTimers = new Map();

function clearStaffPage() {
  for (const timer of assignmentTimers.values()) clearTimeout(timer);
  assignmentTimers.clear();
  Object.assign(staffState, { staff: [], units: [], editingId: null, formUnits: [], weekStart: null, preview: null });
  for (const id of ["staff-list", "preview", "unassigned-warning", "schedule-weekday", "preview-week"]) {
    $(id).innerHTML = "";
  }
  for (const id of ["staff-status", "schedule-status", "schedule-next", "send-status"]) flash(id, "");
  $("unassigned-warning").hidden = true;
  $("whatsapp-notice").hidden = true;
  closeStaffForm();
}

/** "2026-09-12" -> "Sa 12.09." - ohne new Date(iso), das Datumsangaben als UTC liest. */
function formatDay(iso) {
  const [year, month, day] = iso.split("-").map(Number);
  const weekday = SHORT_DAYS[new Date(year, month - 1, day).getDay()];
  return `${weekday} ${String(day).padStart(2, "0")}.${String(month).padStart(2, "0")}.`;
}

async function loadStaffPage() {
  const epoch = sessionEpoch;
  try {
    const [list, schedule] = await Promise.all([api("/api/staff"), api("/api/cleaning-schedule")]);
    if (epoch !== sessionEpoch) return;
    staffState.staff = list.staff;
    staffState.units = list.units;
    renderStaffList();
    renderSchedule(schedule);
    await loadPreview();
  } catch (err) {
    if (epoch !== sessionEpoch || err.message === "Nicht angemeldet") return;
    $("staff-list").innerHTML = `<p class="empty">${escapeHtml(err.message)}</p>`;
  }
}

/** Dropdown mit einem Haekchen je erkannter Wohnung. */
function unitPicker(selectedIds, onChange) {
  const selected = new Set(selectedIds);
  const picker = document.createElement("details");
  picker.className = "multi";
  const summary = document.createElement("summary");
  const options = document.createElement("div");
  options.className = "multi-options";
  picker.append(summary, options);

  const updateSummary = () => {
    const names = staffState.units.filter((unit) => selected.has(unit.id)).map((unit) => unit.name);
    summary.textContent = names.length ? names.join(", ") : "Keine Wohnung ausgewählt";
    summary.title = summary.textContent;
  };

  if (!staffState.units.length) {
    const hint = document.createElement("p");
    hint.className = "muted small";
    hint.textContent = "Noch keine Wohnungen erkannt – sie entstehen automatisch aus den Buchungsmails.";
    options.append(hint);
  }
  for (const unit of staffState.units) {
    const row = document.createElement("label");
    row.className = "multi-option";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = selected.has(unit.id);
    box.addEventListener("change", () => {
      if (box.checked) selected.add(unit.id);
      else selected.delete(unit.id);
      updateSummary();
      onChange([...selected]);
    });
    row.append(box, document.createTextNode(unit.name));
    options.append(row);
  }
  updateSummary();
  return picker;
}

// Offene Dropdowns schliessen, sobald woanders hingeklickt wird.
document.addEventListener("click", (event) => {
  for (const open of document.querySelectorAll("details.multi[open]")) {
    if (!open.contains(event.target)) open.open = false;
  }
});

function renderStaffList() {
  const list = $("staff-list");
  if (!staffState.staff.length) {
    list.innerHTML = `<p class="empty">Noch keine Mitarbeiter. Leg über „Mitarbeiter hinzufügen"
      die erste Reinigungskraft an und wähle ihre Wohnungen aus.</p>`;
    return;
  }

  list.innerHTML = `<div class="staff-row head">
    <span>NAME</span><span>TELEFON (WHATSAPP)</span><span>WOHNUNGEN</span><span></span></div>`;
  for (const member of staffState.staff) {
    const row = document.createElement("div");
    row.className = member.active ? "staff-row" : "staff-row inactive";
    row.innerHTML = `
      <span class="staff-name">${escapeHtml(member.name)}
        ${member.active ? "" : '<span class="badge quiet">inaktiv</span>'}</span>
      <span class="staff-phone">${escapeHtml(member.phone)}</span>
      <span class="staff-units"></span>
      <span class="staff-actions">
        <button type="button" class="btn-ghost small" data-action="edit">Bearbeiten</button>
        <button type="button" class="btn-ghost small danger" data-action="delete">Löschen</button>
      </span>`;
    row.querySelector(".staff-units").append(
      unitPicker(member.units.map((unit) => unit.id), (ids) => saveAssignments(member, ids)));
    row.querySelector('[data-action="edit"]').addEventListener("click", () => openStaffForm(member));
    row.querySelector('[data-action="delete"]').addEventListener("click", () => deleteStaff(member));
    list.append(row);
  }
}

function saveAssignments(member, unitIds) {
  clearTimeout(assignmentTimers.get(member.id));
  assignmentTimers.set(member.id, setTimeout(async () => {
    assignmentTimers.delete(member.id);
    const epoch = sessionEpoch;
    try {
      const updated = await api(`/api/staff/${member.id}`, jsonRequest("PUT", {
        name: member.name, phone: member.phone, active: member.active, unit_ids: unitIds,
      }));
      if (epoch !== sessionEpoch) return;
      Object.assign(member, updated);
      flash("staff-status", `Wohnungen von ${updated.name} gespeichert.`);
      await loadPreview();
    } catch (err) {
      if (epoch !== sessionEpoch) return;
      flash("staff-status", err.message, true);
      await loadStaffPage();
    }
  }, 400));
}

function openStaffForm(member = null) {
  staffState.editingId = member ? member.id : null;
  staffState.formUnits = member ? member.units.map((unit) => unit.id) : [];
  $("staff-form-title").textContent = member ? "Mitarbeiter bearbeiten" : "Mitarbeiter hinzufügen";
  $("staff-name").value = member ? member.name : "";
  $("staff-phone").value = member ? member.phone : "";
  $("staff-active").checked = member ? member.active : true;
  $("staff-error").hidden = true;
  $("staff-units-slot").replaceChildren(
    unitPicker(staffState.formUnits, (ids) => { staffState.formUnits = ids; }));
  $("staff-overlay").hidden = false;
  $("staff-name").focus();
}

function closeStaffForm() {
  $("staff-overlay").hidden = true;
  staffState.editingId = null;
}

$("staff-add").addEventListener("click", () => openStaffForm());
$("staff-cancel").addEventListener("click", closeStaffForm);
$("staff-overlay").addEventListener("click", (event) => {
  if (event.target === $("staff-overlay")) closeStaffForm();
});

$("staff-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const id = staffState.editingId;
  const body = {
    name: $("staff-name").value,
    phone: $("staff-phone").value,
    active: $("staff-active").checked,
    unit_ids: staffState.formUnits,
  };
  const epoch = sessionEpoch;
  try {
    const saved = await api(id ? `/api/staff/${id}` : "/api/staff", jsonRequest(id ? "PUT" : "POST", body));
    if (epoch !== sessionEpoch) return;
    closeStaffForm();
    flash("staff-status", `${saved.name} gespeichert.`);
    await loadStaffPage();
  } catch (err) {
    if (epoch !== sessionEpoch) return;
    $("staff-error").textContent = err.message;
    $("staff-error").hidden = false;
  }
});

async function deleteStaff(member) {
  if (!confirm(`${member.name} wirklich löschen? Die Liste der verschickten Pläne geht dabei verloren.`)) return;
  const epoch = sessionEpoch;
  try {
    await api(`/api/staff/${member.id}`, { method: "DELETE" });
    if (epoch !== sessionEpoch) return;
    flash("staff-status", `${member.name} gelöscht.`);
    await loadStaffPage();
  } catch (err) {
    if (epoch === sessionEpoch) flash("staff-status", err.message, true);
  }
}

function renderSchedule(schedule) {
  $("whatsapp-notice").hidden = schedule.whatsapp_configured;
  $("schedule-enabled").checked = schedule.enabled;
  $("schedule-weekday").innerHTML = WEEKDAY_NAMES
    .map((name, index) => `<option value="${index}" ${index === schedule.weekday ? "selected" : ""}>${name}</option>`)
    .join("");
  $("schedule-time").value = schedule.send_time;
  flash("schedule-next", schedule.enabled && schedule.next_send_at
    ? `Nächster Versand: ${formatDateTime(schedule.next_send_at)} Uhr – Plan für die Woche ab ${formatIsoDate(schedule.next_week_start)}`
    : "Der automatische Versand ist aus.");

  const chosen = staffState.weekStart || schedule.default_week_start;
  $("preview-week").innerHTML = schedule.weeks
    .map((week) => `<option value="${week.week_start}" ${week.week_start === chosen ? "selected" : ""}>${escapeHtml(week.label)}</option>`)
    .join("");
  staffState.weekStart = $("preview-week").value;
}

$("schedule-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const epoch = sessionEpoch;
  try {
    const schedule = await api("/api/cleaning-schedule", jsonRequest("PUT", {
      enabled: $("schedule-enabled").checked,
      weekday: Number($("schedule-weekday").value),
      send_time: $("schedule-time").value,
    }));
    if (epoch !== sessionEpoch) return;
    renderSchedule(schedule);
    flash("schedule-status", "Gespeichert.");
  } catch (err) {
    if (epoch === sessionEpoch) flash("schedule-status", err.message, true);
  }
});

$("preview-week").addEventListener("change", (event) => {
  staffState.weekStart = event.target.value;
  flash("send-status", "");
  loadPreview();
});

async function loadPreview() {
  const epoch = sessionEpoch;
  const params = staffState.weekStart ? `?week_start=${encodeURIComponent(staffState.weekStart)}` : "";
  try {
    const data = await api(`/api/cleaning-schedule/preview${params}`);
    if (epoch !== sessionEpoch) return;
    renderPreview(data);
  } catch (err) {
    if (epoch !== sessionEpoch || err.message === "Nicht angemeldet") return;
    $("preview").innerHTML = `<p class="empty">${escapeHtml(err.message)}</p>`;
  }
}

function taskItem(task) {
  return `<li class="task">
    <span class="task-day">${formatDay(task.day)}</span>
    <span class="task-unit">${escapeHtml(task.unit)}</span>
    ${task.turnover ? '<span class="badge turnover" title="Neuer Gast reist am selben Tag an">Wechsel</span>' : ""}
  </li>`;
}

function dispatchInfo(plan) {
  const last = plan.last_dispatch;
  if (!last) return '<span class="muted small">Noch nicht verschickt</span>';
  const what = last.kind === "update" ? "Änderung" : "Plan";
  const when = formatDateTime(last.created_at);
  if (!last.success) {
    return `<span class="small send-failed">${what} am ${when} nicht zugestellt: ${escapeHtml(last.error || "unbekannter Fehler")}</span>`;
  }
  const changed = plan.changed_since_dispatch
    ? ' <span class="badge changed" title="Geht nach dem nächsten Mail-Abruf als Änderung raus">geändert seit Versand</span>'
    : "";
  return `<span class="muted small">${what} verschickt am ${when}</span>${changed}`;
}

function renderPreview(data) {
  staffState.preview = data;

  const warning = $("unassigned-warning");
  warning.hidden = !data.unassigned.length;
  warning.innerHTML = data.unassigned.length
    ? `<strong>${data.unassigned.length === 1 ? "1 Reinigung hat" : `${data.unassigned.length} Reinigungen haben`}
       keinen zuständigen Mitarbeiter:</strong>
       ${data.unassigned.map((task) => `${formatDay(task.day)} ${escapeHtml(task.unit)}`).join(" · ")}`
    : "";

  $("send-all").disabled = !data.staff.length || !data.whatsapp_configured;

  const preview = $("preview");
  if (!data.staff.length) {
    preview.innerHTML = '<p class="empty">Keine aktiven Mitarbeiter – niemand bekäme einen Plan.</p>';
    return;
  }
  preview.innerHTML = data.staff.map((plan) => `
    <article class="plan-card">
      <header>
        <div><strong>${escapeHtml(plan.name)}</strong>
          <span class="muted small">${escapeHtml(plan.phone)}</span></div>
        <button type="button" class="btn-ghost small" data-send="${plan.staff_id}"
          ${data.whatsapp_configured ? "" : "disabled"}>Senden</button>
      </header>
      ${plan.tasks.length
        ? `<ul class="tasks">${plan.tasks.map(taskItem).join("")}</ul>`
        : '<p class="muted small">Keine Reinigungen in dieser Woche.</p>'}
      <footer>${dispatchInfo(plan)}</footer>
    </article>`).join("");

  for (const button of preview.querySelectorAll("[data-send]")) {
    const plan = data.staff.find((entry) => String(entry.staff_id) === button.dataset.send);
    button.addEventListener("click", () => sendPlan(plan));
  }
}

async function sendPlan(plan = null) {
  const data = staffState.preview;
  if (!data) return;
  const recipients = plan ? plan.name : `${data.staff.length} Mitarbeiter`;
  if (!confirm(`Putzplan ${data.label} jetzt per WhatsApp an ${recipients} senden?`)) return;

  const epoch = sessionEpoch;
  $("send-all").disabled = true;
  flash("send-status", "Wird gesendet …");
  try {
    const result = await api("/api/cleaning-schedule/send", jsonRequest("POST", {
      week_start: data.week_start,
      staff_id: plan ? plan.staff_id : null,
    }));
    if (epoch !== sessionEpoch) return;
    const failed = result.outcomes.filter((outcome) => !outcome.success);
    flash("send-status", failed.length
      ? `${result.sent} verschickt, ${failed.length} fehlgeschlagen: ${failed.map((o) => `${o.name} (${o.error})`).join(", ")}`
      : `${result.sent} verschickt.`, failed.length > 0);
    await loadPreview();
  } catch (err) {
    if (epoch !== sessionEpoch) return;
    flash("send-status", err.message, true);
    $("send-all").disabled = !data.staff.length || !data.whatsapp_configured;
  }
}

$("send-all").addEventListener("click", () => sendPlan());

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
      await showView();
    } else {
      showLogin();
    }
  } catch {
    showLogin();
  }
})();
