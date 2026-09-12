/* Mail Agent - Oberflaechenbausteine: Theme, Meldungen, Befehlspalette,
   Fortschritt und die kleinen Momente dazwischen.

   Laeuft vor app.js und meldet sich als window.ui an. Kein Framework, keine
   Abhaengigkeit - alles, was hier passiert, sind ein paar Dutzend Zeilen DOM.  */

(function () {
  const $ = (id) => document.getElementById(id);
  const ruhig = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ------------------------------------------------------------ Mandantenfarbe */

  /** Aus dem Namen der Vermietung wird ihre Farbe.
   *
   *  Wer fuer zwei Mandanten arbeitet, sieht am Farbton sofort, in wessen Daten
   *  er steht - noch bevor er den Namen liest. Immer derselbe Name, immer
   *  derselbe Ton, ohne dass irgendwo eine Farbe gespeichert werden muesste. */
  function setTenantAccent(name) {
    const wurzel = document.documentElement;
    if (!name) {
      wurzel.style.removeProperty("--tenant");
      wurzel.style.removeProperty("--tenant-soft");
      return;
    }
    let hash = 0;
    for (const zeichen of name) hash = (hash * 31 + zeichen.codePointAt(0)) % 360;
    // Der Bereich um Rot bleibt frei - Rot heisst in dieser App "Stornierung".
    const ton = 200 + (hash % 150);
    const dunkel = wurzel.dataset.theme === "dark"
      || (!wurzel.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
    wurzel.style.setProperty("--tenant", `hsl(${ton} ${dunkel ? 78 : 70}% ${dunkel ? 72 : 52}%)`);
    wurzel.style.setProperty("--tenant-soft", `hsl(${ton} ${dunkel ? 40 : 90}% ${dunkel ? 18 : 95}%)`);
  }

  /* ------------------------------------------------------------ Hell und dunkel */

  const THEMEN = ["system", "light", "dark"];
  const THEME_TEXT = { system: "System", light: "Hell", dark: "Dunkel" };
  const THEME_ZEICHEN = { system: "◑", light: "☀", dark: "☾" };

  function themeLesen() {
    try {
      return localStorage.getItem("mailagent_theme") || "system";
    } catch {
      return "system"; // Speicher gesperrt - dann eben jede Sitzung neu
    }
  }

  function themeSetzen(wahl) {
    const wurzel = document.documentElement;
    if (wahl === "system") delete wurzel.dataset.theme;
    else wurzel.dataset.theme = wahl;
    try {
      localStorage.setItem("mailagent_theme", wahl);
    } catch { /* ohne Speicher gilt die Wahl nur fuer diese Sitzung */ }
    const knopf = $("theme-toggle");
    if (knopf) {
      knopf.textContent = THEME_ZEICHEN[wahl];
      knopf.title = `Darstellung: ${THEME_TEXT[wahl]}`;
      knopf.setAttribute("aria-label", `Darstellung: ${THEME_TEXT[wahl]}. Umschalten.`);
    }
    // Die Mandantenfarbe haengt an der Helligkeit - neu mischen.
    setTenantAccent(window.ui?.tenant || "");
  }

  function themeWeiter() {
    const jetzt = themeLesen();
    themeSetzen(THEMEN[(THEMEN.indexOf(jetzt) + 1) % THEMEN.length]);
  }

  /* ------------------------------------------------------------ Meldungen */

  /** Kurze Rueckmeldung unten rechts. Verschwindet von allein. */
  function toast(text, art = "info", dauer = 4000) {
    let kasten = $("toasts");
    if (!kasten) {
      kasten = document.createElement("div");
      kasten.id = "toasts";
      document.body.append(kasten);
    }
    const zeichen = { ok: "✓", bad: "!", info: "i" }[art] || "i";
    const el = document.createElement("div");
    el.className = `toast ${art}`;
    el.setAttribute("role", art === "bad" ? "alert" : "status");
    el.innerHTML = `<span class="zeichen" aria-hidden="true">${zeichen}</span><span></span>`;
    el.lastElementChild.textContent = text;
    kasten.append(el);
    setTimeout(() => {
      el.classList.add("weg");
      el.addEventListener("animationend", () => el.remove(), { once: true });
      // Falls Animationen aus sind, feuert kein animationend.
      if (ruhig()) el.remove();
    }, dauer);
    return el;
  }

  /* ------------------------------------------------------------ Zahlen */

  /** Zaehlt eine Zahl hoch, statt sie hinzustellen - macht Veraenderung sichtbar. */
  function countUp(el, ziel) {
    const start = Number(el.dataset.wert || 0);
    el.dataset.wert = String(ziel);
    if (ruhig() || start === ziel) {
      el.textContent = String(ziel);
      return;
    }
    const dauer = 420;
    const beginn = performance.now();
    (function schritt(jetzt) {
      const t = Math.min(1, (jetzt - beginn) / dauer);
      const weich = 1 - Math.pow(1 - t, 3);
      el.textContent = String(Math.round(start + (ziel - start) * weich));
      if (t < 1) requestAnimationFrame(schritt);
    })(beginn);
  }

  /* ------------------------------------------------------------ Erfolg */

  /** Einmaliger Konfettiregen. Der Schluessel verhindert, dass es sich
   *  bei jedem Neuladen wiederholt - gefeiert wird das erste Mal, nicht jedes. */
  function feiern(schluessel) {
    try {
      if (schluessel && localStorage.getItem(`mailagent_gefeiert_${schluessel}`)) return;
      if (schluessel) localStorage.setItem(`mailagent_gefeiert_${schluessel}`, "1");
    } catch { /* ohne Speicher feiern wir eben oefter */ }
    if (ruhig()) return;

    const buehne = document.createElement("div");
    buehne.className = "konfetti";
    buehne.setAttribute("aria-hidden", "true");
    const farben = ["--tenant", "--accent", "--ok", "--request-fg", "--change-fg"];
    for (let i = 0; i < 42; i++) {
      const teil = document.createElement("i");
      teil.style.left = `${Math.random() * 100}%`;
      teil.style.background = `var(${farben[i % farben.length]})`;
      teil.style.setProperty("--fall", `${1.8 + Math.random() * 1.4}s`);
      teil.style.setProperty("--dreh", `${180 + Math.random() * 540}deg`);
      teil.style.animationDelay = `${Math.random() * 350}ms`;
      teil.style.opacity = String(0.7 + Math.random() * 0.3);
      buehne.append(teil);
    }
    document.body.append(buehne);
    setTimeout(() => buehne.remove(), 4000);
  }

  /* ------------------------------------------------------------ Erste Schritte */

  const SCHRITTE = [
    { id: "konto", text: "Konto angelegt" },
    { id: "postfach", text: "Postfach verbunden" },
    { id: "abruf", text: "Erster Abruf gelaufen" },
    { id: "treffer", text: "Erste Buchung erkannt" },
  ];

  /** Checkliste mit Fortschrittsring. Sie verschwindet, sobald alles erledigt
   *  ist - eine Einrichtungshilfe, die bleibt, wird zur Tapete. */
  function updateOnboarding(stand) {
    const kasten = $("onboard");
    if (!kasten) return;
    const fertig = SCHRITTE.filter((s) => stand[s.id]).length;
    const alles = fertig === SCHRITTE.length;

    if (alles && kasten.dataset.gefeiert === "1") {
      kasten.hidden = true;
      return;
    }
    kasten.hidden = false;

    const anteil = fertig / SCHRITTE.length;
    const umfang = 2 * Math.PI * 19;
    kasten.querySelector(".fortschritt").style.strokeDasharray = String(umfang);
    kasten.querySelector(".fortschritt").style.strokeDashoffset = String(umfang * (1 - anteil));
    kasten.querySelector(".onboard-ring b").textContent = `${fertig}/${SCHRITTE.length}`;

    const offen = SCHRITTE.find((s) => !stand[s.id]);
    kasten.querySelector(".onboard-text strong").textContent = alles
      ? "Alles startklar – der Agent arbeitet für dich. 🎉"
      : `Noch ${SCHRITTE.length - fertig} Schritt${SCHRITTE.length - fertig === 1 ? "" : "e"}: ${offen.text}`;

    kasten.querySelector(".onboard-steps").innerHTML = SCHRITTE.map(
      (s) => `<span class="onboard-step ${stand[s.id] ? "fertig" : ""}">
                <span class="haken" aria-hidden="true">✓</span>${s.text}
              </span>`,
    ).join("");

    if (alles && kasten.dataset.gefeiert !== "1") {
      kasten.dataset.gefeiert = "1";
      feiern("einrichtung");
      toast("Einrichtung abgeschlossen – ab jetzt läuft es von allein.", "ok", 6000);
      setTimeout(() => { kasten.hidden = true; }, 6000);
    }
  }

  /* ------------------------------------------------------------ Befehlspalette */

  let befehle = [];
  let auswahl = 0;

  function cmdkAufbauen() {
    const schicht = document.createElement("div");
    schicht.id = "cmdk";
    schicht.hidden = true;
    schicht.innerHTML = `
      <div class="cmdk-box" role="dialog" aria-modal="true" aria-label="Befehle">
        <input type="text" id="cmdk-suche" placeholder="Was möchtest du tun?"
               autocomplete="off" spellcheck="false" />
        <ul class="cmdk-list" id="cmdk-liste" role="listbox"></ul>
      </div>`;
    document.body.append(schicht);
    schicht.addEventListener("click", (e) => { if (e.target === schicht) cmdkSchliessen(); });
    $("cmdk-suche").addEventListener("input", cmdkZeichnen);
    $("cmdk-suche").addEventListener("keydown", cmdkTaste);
    return schicht;
  }

  function passende() {
    const suche = ($("cmdk-suche")?.value || "").trim().toLowerCase();
    return befehle
      .filter((b) => !b.wenn || b.wenn())
      .filter((b) => !suche || b.text.toLowerCase().includes(suche));
  }

  function cmdkZeichnen() {
    const liste = $("cmdk-liste");
    const treffer = passende();
    auswahl = Math.min(auswahl, Math.max(0, treffer.length - 1));
    liste.innerHTML = treffer.length
      ? treffer
          .map(
            (b, i) => `<li role="option" aria-selected="${i === auswahl}" data-i="${i}">
                 <span aria-hidden="true">${b.zeichen || "›"}</span>
                 <span>${b.text}</span>
                 ${b.taste ? `<span class="kbd">${b.taste}</span>` : ""}
               </li>`,
          )
          .join("")
      : `<li class="muted" aria-disabled="true">Nichts gefunden.</li>`;
    for (const li of liste.querySelectorAll("li[data-i]")) {
      li.addEventListener("click", () => cmdkAusfuehren(treffer[Number(li.dataset.i)]));
      li.addEventListener("mousemove", () => {
        auswahl = Number(li.dataset.i);
        for (const anderes of liste.querySelectorAll("li[data-i]")) {
          anderes.setAttribute("aria-selected", anderes === li);
        }
      });
    }
  }

  function cmdkTaste(event) {
    const treffer = passende();
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const richtung = event.key === "ArrowDown" ? 1 : -1;
      auswahl = (auswahl + richtung + treffer.length) % Math.max(1, treffer.length);
      cmdkZeichnen();
    } else if (event.key === "Enter") {
      event.preventDefault();
      cmdkAusfuehren(treffer[auswahl]);
    } else if (event.key === "Escape") {
      cmdkSchliessen();
    }
  }

  function cmdkAusfuehren(befehl) {
    if (!befehl) return;
    cmdkSchliessen();
    befehl.tun();
  }

  function cmdkOeffnen() {
    const schicht = $("cmdk") || cmdkAufbauen();
    schicht.hidden = false;
    auswahl = 0;
    $("cmdk-suche").value = "";
    cmdkZeichnen();
    $("cmdk-suche").focus();
  }

  function cmdkSchliessen() {
    const schicht = $("cmdk");
    if (schicht) schicht.hidden = true;
  }

  function setBefehle(liste) {
    befehle = liste;
  }

  /* ------------------------------------------------------------ Tastatur */

  document.addEventListener("keydown", (event) => {
    const inEingabe = /^(input|textarea|select)$/i.test(event.target.tagName);
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      $("cmdk")?.hidden === false ? cmdkSchliessen() : cmdkOeffnen();
      return;
    }
    if (event.key === "Escape") cmdkSchliessen();
    // "/" springt in die Suche - wie in den meisten Werkzeugen.
    if (event.key === "/" && !inEingabe && !$("app").hidden) {
      const suche = $("search");
      if (suche && suche.offsetParent) {
        event.preventDefault();
        suche.focus();
      }
    }
  });

  /* ------------------------------------------------------------ Ladezustand */

  /** Platzhalter in der Form dessen, was gleich kommt. */
  function skelett(ziel, zeilen = 5) {
    ziel.innerHTML = Array.from(
      { length: zeilen },
      () => '<div class="skeleton skeleton-row"></div>',
    ).join("");
  }

  window.ui = {
    tenant: "",
    setTenantAccent(name) {
      this.tenant = name || "";
      setTenantAccent(name);
    },
    themeSetzen,
    themeWeiter,
    themeLesen,
    toast,
    countUp,
    feiern,
    updateOnboarding,
    setBefehle,
    cmdkOeffnen,
    skelett,
  };

  // Die Darstellung steht vor dem ersten Bild - sonst blitzt Hell auf, bevor
  // Dunkel greift.
  themeSetzen(themeLesen());
})();
