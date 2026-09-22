/* HISTORY -- "what happened while I was asleep or away?"

   The ONLY place in the app with a time-series chart of charge, and it is
   always scoped to one past episode.  A live chart elsewhere would just be a
   second rendering of what the NOW timeline already says. */

import { get } from "./api.js";
import { dur, pct, num, clock, dateLabel, DASH, isNum, esc } from "./format.js";

const KIND = { outage: "Outage", box_absent: "Box absent",
               manual_hibernate: "Manual hibernate", test: "Test" };

function summary(ep) {
  const bits = [];
  if (isNum(ep.charge_start) && isNum(ep.charge_min)) {
    bits.push(`${pct(ep.charge_start)} → ${pct(ep.charge_min)}`);
  }
  // The tracker writes the whole story (hibernated / parked / back via ...)
  // as `summary` when the episode closes; before 2026-09-22 nothing did, and
  // every card claimed "rode it out".
  if (ep.summary) {
    bits.push(esc(ep.summary));
  } else if (ep.hibernated) {
    bits.push("hibernated");
    if (ep.wake_cause) bits.push(`back via ${esc(ep.wake_cause)}`);
  } else if (ep.kind === "outage") {
    bits.push(ep.ended ? "box stayed up" : "in progress");
  } else {
    bits.push(ep.ended ? "box was down" : "box is down");
  }
  return bits.join(" · ");
}

function card(ep) {
  const end = ep.ended ? clock(ep.ended) : "ongoing";
  const len = ep.ended ? dur(ep.ended - ep.started) : "…";
  return `
    <div class="episode" data-ep="${ep.id}">
      <div class="row1">
        <span class="kind">${esc(KIND[ep.kind] || ep.kind)}</span>
        <span class="when">${dateLabel(ep.started)} ${clock(ep.started)} → ${end} · ${len}</span>
      </div>
      <div class="detail">${summary(ep)}</div>
      <div class="trace" id="trace-${ep.id}"></div>
    </div>`;
}

function drawTrace(host, trace) {
  const rows = trace.filter((r) => isNum(r.ts));
  if (rows.length < 2) {
    host.innerHTML = '<div class="empty">no trace recorded for this episode</div>';
    return;
  }
  const css = getComputedStyle(document.body);
  const c = (n, f) => css.getPropertyValue(n).trim() || f;
  const data = [
    rows.map((r) => r.ts),
    rows.map((r) => (isNum(r.charge) ? r.charge : null)),
    rows.map((r) => (isNum(r.watts) ? r.watts : null)),
    rows.map((r) => (isNum(r.batt_v) ? r.batt_v : null)),
  ];
  host.innerHTML = "";
  // eslint-disable-next-line no-undef
  new uPlot({
    width: host.clientWidth || 320,
    height: 220,
    padding: [10, 8, 0, 0],
    cursor: { y: false },
    scales: { x: { time: true } },
    series: [
      {},
      { label: "charge %", stroke: c("--state", "#22d3ee"), width: 2, scale: "pct" },
      { label: "draw W", stroke: c("--warn", "#f59e0b"), width: 1.2, scale: "w" },
      { label: "pack V", stroke: c("--dim", "#7a7a85"), width: 1, scale: "v", dash: [4, 3] },
    ],
    axes: [
      { stroke: c("--dim", "#7a7a85"), grid: { stroke: c("--line", "#26262b") },
        ticks: { stroke: c("--line", "#26262b") } },
      { scale: "pct", stroke: c("--dim", "#7a7a85"),
        grid: { stroke: c("--line", "#26262b") }, ticks: { stroke: c("--line", "#26262b") } },
      { scale: "w", side: 1, stroke: c("--warn", "#f59e0b"), grid: { show: false } },
    ],
  }, data, host);
}

export async function renderHistory(root) {
  root.innerHTML = '<section class="panel"><h2>Episodes</h2><div id="eps">loading…</div></section>';
  let payload;
  try {
    payload = await get("api/episodes?limit=40");
  } catch (e) {
    root.querySelector("#eps").innerHTML =
      `<div class="empty">could not load history: ${esc(e.message)}</div>`;
    return;
  }
  const eps = payload.episodes || [];
  const host = root.querySelector("#eps");
  host.innerHTML = eps.length
    ? eps.map(card).join("")
    : `<div class="empty">No episodes recorded yet.<br>
        An episode opens when the UPS goes on battery or the box stops answering.</div>`;

  host.querySelectorAll(".episode").forEach((node) => {
    node.addEventListener("click", async () => {
      const id = node.dataset.ep;
      const t = node.querySelector(`#trace-${id}`);
      if (t.dataset.open === "1") { t.innerHTML = ""; t.dataset.open = "0"; return; }
      t.innerHTML = "loading trace…";
      t.dataset.open = "1";
      try {
        const detail = await get(`api/episode/${id}`);
        drawTrace(t, detail.trace || []);
      } catch (e) {
        t.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
      }
    });
  });
}
