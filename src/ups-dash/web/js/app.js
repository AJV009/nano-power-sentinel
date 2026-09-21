/* App shell: tabs, theme, and the live stream. */

import { stream, get } from "./api.js";
import { renderNow, boxSheetHtml } from "./now.js";
import { renderHistory } from "./history.js";
import { renderHealth } from "./health.js";
import { renderConfig, noteSnapshot } from "./config.js";
import { stateOf } from "./timeline.js";
import { rel, esc } from "./format.js";
import { wireSheet } from "./controls.js";
import { registerServiceWorker, watchInstallability } from "./pwa.js";

const TABS = ["now", "history", "health", "config"];
const SPARK_EVERY = 5000;   // downsample the 1 Hz stream for the trend line
const SPARK_POINTS = 180;   // ~15 minutes
const OFFLINE_TICK = 15000; // how often the "Xm ago" banner text refreshes

const view = document.getElementById("view");
const linkDot = document.getElementById("link");
const offlineBar = document.getElementById("offline");
let snap = null;
let drainRing = [];
let lastSparkAt = 0;
let current = "now";
let offlineTimer = null;

/* ---- theme ---- */
function applyTheme(t) {
  if (t) document.documentElement.setAttribute("data-theme", t);
  else document.documentElement.removeAttribute("data-theme");
  const btn = document.getElementById("theme");
  const eff = t || (window.matchMedia("(prefers-color-scheme: light)").matches
    ? "light" : "dark");
  btn.textContent = eff === "light" ? "LIGHT" : "DARK";
}
function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem("ups-dash-theme"); } catch (e) { /* private mode */ }
  applyTheme(saved);
  document.getElementById("theme").addEventListener("click", () => {
    const now = document.documentElement.getAttribute("data-theme");
    const next = now === "light" ? "dark" : "light";
    applyTheme(next);
    try { localStorage.setItem("ups-dash-theme", next); } catch (e) { /* ignore */ }
    if (current !== "now") show(current);
    else if (snap) paintNow();
  });
}

/* ---- sheet ---- */
function openSheet(html) {
  const back = document.createElement("div");
  back.className = "sheet-backdrop";
  back.innerHTML = `<div class="sheet">${html}</div>`;
  back.addEventListener("click", (e) => {
    if (e.target === back || e.target.id === "sheetclose") back.remove();
  });
  document.body.appendChild(back);
  return back;
}

/* ---- rendering ---- */
function paintNow() {
  if (!snap) return;
  const btn = renderNow(view, snap, drainRing.map((d) => ({ drain: d })));
  if (btn) btn.addEventListener("click", () => {
    const back = openSheet(boxSheetHtml(snap));
    wireSheet(back.querySelector(".sheet"), snap);
  });
}

function show(tab) {
  current = tab;
  TABS.forEach((t) => {
    document.getElementById("tab-" + t)
      .setAttribute("aria-selected", String(t === tab));
  });
  if (tab === "now") { view.innerHTML = ""; paintNow(); }
  else if (tab === "history") renderHistory(view);
  else if (tab === "health") renderHealth(view);
  else if (tab === "config") renderConfig(view);
}

function onSnapshot(s) {
  snap = s;
  noteSnapshot(s);
  document.body.setAttribute("data-state", stateOf(s));
  const now = Date.now();
  if (now - lastSparkAt >= SPARK_EVERY) {
    lastSparkAt = now;
    const d = (s.derived || {}).drain_pct_min;
    drainRing.push(typeof d === "number" ? d : null);
    if (drainRing.length > SPARK_POINTS) drainRing.shift();
  }
  if (current === "now") paintNow();
}

/* ---- offline banner: the link dot dims either way, but a dim dot is easy
   to miss, and this app must never let stale numbers pass as live. Shown
   regardless of the active tab, since History/Health/Config can be open
   while the stream is down too. */
function offlineText() {
  const age = snap && snap.ts ? rel(snap.ts) : "no data received yet";
  return `<b>Offline</b> — showing last-known data <span class="age">· ${esc(age)}</span>`;
}
function showOffline() {
  offlineBar.innerHTML = offlineText();
  offlineBar.classList.remove("hidden");
  if (!offlineTimer) offlineTimer = setInterval(() => {
    offlineBar.innerHTML = offlineText();
  }, OFFLINE_TICK);
}
function hideOffline() {
  offlineBar.classList.add("hidden");
  offlineBar.innerHTML = "";
  if (offlineTimer) { clearInterval(offlineTimer); offlineTimer = null; }
}

function onLink(up) {
  linkDot.style.opacity = up ? "1" : "0.25";
  linkDot.title = up ? "live" : "reconnecting…";
  if (!up && current === "now" && snap) {
    document.body.setAttribute("data-state", "blind");
  }
  if (up) hideOffline(); else showOffline();
}

TABS.forEach((t) => {
  document.getElementById("tab-" + t).addEventListener("click", () => show(t));
});
initTheme();
show("now");
get("api/now").then(onSnapshot).catch(() => {
  view.innerHTML = '<div class="empty">waiting for the collector…</div>';
});
stream(onSnapshot, onLink);
registerServiceWorker();

/* The browser decides installability; we just surface it. A visible button
   beats a menu entry nobody finds -- and if this never appears, the page is
   not in a secure context (plain http:// will not install). */
watchInstallability((promptInstall) => {
  const btn = document.getElementById("install");
  if (!btn) return;
  btn.classList.remove("hidden");
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    const outcome = await promptInstall();
    if (outcome === "accepted") btn.classList.add("hidden");
    else btn.disabled = false;
  });
});
