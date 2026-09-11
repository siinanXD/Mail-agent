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
    const detail = await response.json().catch(() => ({}));
    // 422 liefert eine Liste von Feldfehlern statt eines Satzes.
    if (Array.isArray(detail.detail)) throw new Error("Eingabe unvollständig oder ungültig.");
    throw new Error(detail.detail || `Fehler ${response.status}`);
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
}

/** Entfernt alles, was zur bisherigen Sitzung gehoert. Ohne das saehe ein
 *  anderer Nutzer, der sich ohne Neuladen anmeldet, Chatverlauf, Verlauf,
 *  Mitarbeiter und zuletzt geoeffnete Mail des vorigen Mandanten. */
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
  clearStaffPage();
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

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = $("login-error");
  error.hidden = true;
  try {
    await api("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: $("email").value, password: $("password").value }),
    });
    $("password").value = "";
    applySession(await api("/api/session"));
    showApp();
    await showView();
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  }
});

$("logout").addEventListener("click", async () => {
  try {
    await api("/api/logout", { method: "POST" });
  } catch { /* Sitzung ist ohnehin beendet */ }
  showLogin();
});

/* ---------------------------------------------------------------- Bereiche */

const VIEWS = { timeline: "view-timeline", staff: "view-staff" };

function currentView() {
  return location.hash === "#/mitarbeiter" ? "staff" : "timeline";
}

async function showView() {
  const view = currentView();
  for (const [key, id] of Object.entries(VIEWS)) $(id).hidden = key !== view;
  for (const tab of document.querySelectorAll(".tab")) {
    if (tab.dataset.view === view) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  }
  if (view === "staff") await loadStaffPage();
  else await load();
}

window.addEventListener("hashchange", () => {
  if (!$("app").hidden) showView();
});

$("refresh").addEventListener("click", () => showView());

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
  }
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

function formatIsoDate(iso) {
  const [year, month, day] = iso.split("-");
  return `${day}.${month}.${year}`;
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
