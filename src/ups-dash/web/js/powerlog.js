/* The POWER LOG -- HISTORY's "what actually happened", one line per event.

   Everything the collector writes down (logbook.py): the UPS's own status
   flags and draw, the box's state and its governor / systemd-sleep lines,
   the sentinel's and NUT's journal lines, the dashboard's own verdict. Each
   row arrives already worded by the server (logtext.py) -- this file only
   lays it out, groups it by day and filters it by source.

   2026-09-25: working out that the box sat powered-on and stuck for six
   hours took four logs and a LAN sweep. This is those four logs, in order. */

import { get } from "./api.js";
import { clock, dateLabel, esc } from "./format.js";

// Order = chip order. `park` is the dashboard's own park story.
export const SOURCES = [
  ["ups", "UPS"], ["box", "Box"], ["park", "Park"], ["sentinel", "Sentinel"],
  ["nut", "NUT"], ["dash", "Dash"], ["jetson", "Jetson"],
];
const LABEL = Object.fromEntries(SOURCES);
const PAGE = 200;
const STORE = "powerlog.hidden";

function loadHidden() {
  try {
    const v = JSON.parse(localStorage.getItem(STORE) || "[]");
    return new Set(Array.isArray(v) ? v : []);
  } catch (e) {
    return new Set();
  }
}

function saveHidden(set) {
  try { localStorage.setItem(STORE, JSON.stringify([...set])); } catch (e) { /* private window */ }
}

/* One row: time to the second, source, label, the server's wording. */
export function rowHtml(ev) {
  const sev = ["warn", "critical", "ok"].includes(ev.severity) ? ev.severity : "";
  return `
    <li class="lg-row ${sev ? "sev-" + sev : ""}" data-src="${esc(ev.source)}">
      <span class="lg-time">${clock(ev.ts)}<span class="lg-sec">${secs(ev.ts)}</span></span>
      <span class="lg-src src-${esc(ev.source)}">${esc(LABEL[ev.source] || ev.source)}</span>
      <span class="lg-body"><span class="lg-label">${esc(ev.label)}</span>
        ${ev.text ? `<span class="lg-text">${esc(ev.text)}</span>` : ""}</span>
    </li>`;
}

function secs(ts) {
  const s = new Date(ts * 1000).getSeconds();
  return ":" + String(s).padStart(2, "0");
}

/* Rows grouped under day headers, in the order given. */
export function listHtml(events) {
  if (!events.length) return '<div class="empty">nothing logged</div>';
  let day = null;
  const out = [];
  for (const ev of events) {
    const d = dateLabel(ev.ts);
    if (d !== day) {
      if (day !== null) out.push("</ol>");
      out.push(`<div class="lg-day">${esc(d)}</div><ol class="lg-list">`);
      day = d;
    }
    out.push(rowHtml(ev));
  }
  out.push("</ol>");
  return out.join("");
}

function applyFilter(host, hidden) {
  host.querySelectorAll(".lg-row").forEach((r) => {
    r.hidden = hidden.has(r.dataset.src);
  });
  host.querySelectorAll(".lg-chip").forEach((c) => {
    c.setAttribute("aria-pressed", hidden.has(c.dataset.src) ? "false" : "true");
  });
}

/* The HISTORY panel: newest first, source chips, "load older". */
export async function renderPowerLog(section) {
  const hidden = loadHidden();
  const events = [];
  section.innerHTML = `
    <h2>Power log</h2>
    <div class="lg-chips" role="group" aria-label="Filter by source">
      ${SOURCES.map(([k, l]) => `<button type="button" class="lg-chip src-${k}"
          data-src="${k}" aria-pressed="true">${l}</button>`).join("")}
    </div>
    <div class="lg-host">loading…</div>
    <button type="button" class="btn lg-more" hidden>Load older</button>`;
  const host = section.querySelector(".lg-host");
  const more = section.querySelector(".lg-more");

  async function page() {
    const before = events.length ? events[events.length - 1].ts : "";
    more.disabled = true;
    let got;
    try {
      got = (await get(`api/log?limit=${PAGE}${before ? `&before=${before}` : ""}`)).events || [];
    } catch (e) {
      host.innerHTML = `<div class="empty">could not load the log: ${esc(e.message)}</div>`;
      return;
    }
    events.push(...got);
    host.innerHTML = listHtml(events);
    applyFilter(host, hidden);
    more.hidden = got.length < PAGE;
    more.disabled = false;
  }

  section.querySelectorAll(".lg-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const k = chip.dataset.src;
      if (hidden.has(k)) hidden.delete(k); else hidden.add(k);
      saveHidden(hidden);
      applyFilter(section, hidden);
    });
  });
  more.addEventListener("click", page);
  await page();
}
