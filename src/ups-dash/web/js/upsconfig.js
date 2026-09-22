/* UPS firmware settings -- CONFIG tab, rendered and wired from config.js.

   These are RW variables written straight to the UPS via `SET VAR`, not
   values in a tunables.json -- but on the page they behave exactly like the
   tunables above them: an edit only stages into the tab's pending store,
   and nothing reaches the UPS until the review popup's Confirm. The popup
   then reports, per row, whether the command was accepted AND whether the
   UPS reads back the new value -- the firmware can silently ignore a value,
   so those stay two separate verdicts (applychanges.js).

   The beeper is split in two on purpose:
     Enabled / Disabled   a SETTING -- staged and reviewed like the rest
     Mute now             an ACTION -- fires immediately, no staging, no
                          review, not interlocked: silencing an alarm is
                          exactly what you need mid-outage. Drawn apart from
                          the setting so the two are never mistaken. */

import { post } from "./api.js";
import { num, isNum, esc, DASH } from "./format.js";
import { BEEPER_KEY } from "./catalog.js";
import { setText, pillSync } from "./cfgdom.js";

function valText(v, unit) {
  if (v == null || v === "") return DASH;
  const n = typeof v === "number" ? v : Number(v);
  const body = isFinite(n) ? num(n) : String(v);
  return unit ? `${body} ${unit}` : body;
}

const pill = '<span data-dirty class="pill hidden"></span>';

function enumRow(s, value) {
  const choices = s.choices || [];
  return `
    <div class="cfg" data-ups="${esc(s.name)}" data-type="enum">
      <div class="top">
        <span class="name">${esc(s.label || s.name)}</span>
        <span class="val" data-out>${esc(valText(value, s.unit))}</span>
      </div>
      <div class="btnrow" style="grid-template-columns:repeat(${choices.length},1fr)">
        ${choices.map((c) => `<button type="button" class="btn" data-choice="${esc(c)}">${esc(c)}</button>`).join("")}
      </div>
      <div class="meta"><span>UPS</span><span>${esc(s.name)}</span>${pill}</div>
      <div class="effect">${esc(s.help || "")}</div>
    </div>`;
}

function intRow(s, value) {
  const n = value == null ? NaN : Number(value);
  const v = isFinite(n) ? n : s.min;   // unknown: thumb parks at min, value reads —
  return `
    <div class="cfg" data-ups="${esc(s.name)}" data-type="int" data-unit="${esc(s.unit || "")}">
      <div class="top">
        <span class="name">${esc(s.label || s.name)}</span>
        <span class="val" data-out>${esc(valText(value, s.unit))}</span>
      </div>
      <div class="meta"><span>UPS</span><span>range ${s.min}–${s.max}</span>${pill}</div>
      <input type="range" min="${s.min}" max="${s.max}" step="${s.step || 1}" value="${v}"
             aria-label="${esc(s.label || s.name)}">
      <div class="effect">${esc(s.help || "")}</div>
    </div>`;
}

function readonlyRow(s) {
  return `
    <div class="cfg locked" data-ro="${esc(s.block_key || "")}" data-unit="${esc(s.unit || "")}">
      <div class="top">
        <span class="name">${esc(s.label || s.name)}</span>
        <span class="val" data-out>${esc(valText(s.value, s.unit))}</span>
      </div>
      <div class="meta"><span>read-only</span></div>
      <div class="lockwhy">${esc(s.help || "follows input.sensitivity")}</div>
    </div>`;
}

function beeperHtml(status) {
  return `
    <div class="cfg" data-ups="${BEEPER_KEY}" data-type="beeper">
      <div class="top">
        <span class="name">Beeper</span>
        <span class="val" data-out>${esc(status || DASH)}</span>
      </div>
      <div class="btnrow" style="grid-template-columns:repeat(2,1fr)">
        <button type="button" class="btn" data-choice="enabled">Enabled</button>
        <button type="button" class="btn" data-choice="disabled">Disabled</button>
      </div>
      <div class="meta"><span>UPS</span><span>ups.beeper.status</span>${pill}</div>
      <div class="effect">Whether the UPS sounds its alarms at all. Staged and reviewed
        like every other setting on this page.</div>
      <div class="cfg-act">
        <div class="act-tag">Action · takes effect immediately</div>
        <button type="button" class="btn act" data-mute>Mute now</button>
        <div class="effect">Silences the alarm sounding right now, without touching the
          setting above. Not staged and not interlocked — mid-outage is exactly when
          you need it.</div>
        <div class="notebox hidden" data-result></div>
      </div>
    </div>`;
}

export function upsFirmwareHtml(settings, store) {
  const editable = (settings && settings.editable) || [];
  const readonly = (settings && settings.readonly) || [];
  const rows = editable.map((s) => (s.type === "enum"
    ? enumRow(s, store.value(s.name)) : intRow(s, store.value(s.name))));
  return `
    <section class="panel"><h2>UPS firmware</h2>
      ${rows.join("")}
      ${readonly.map(readonlyRow).join("")}
      ${beeperHtml(store.value(BEEPER_KEY))}
    </section>`;
}

/* Read-only transfer points follow input.sensitivity; refreshed in place
   from the stream (the snapshot's ups block) or a re-read of api/config. */
export function paintReadonly(root, upsBlock) {
  if (!upsBlock) return;
  root.querySelectorAll(".cfg[data-ro]").forEach((node) => {
    const k = node.dataset.ro;
    if (k) setText(node.querySelector("[data-out]"), valText(upsBlock[k], node.dataset.unit));
  });
}

function wireMute(node) {
  const btn = node.querySelector("[data-mute]");
  const out = node.querySelector("[data-result]");
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    out.className = "notebox";
    out.textContent = "muting…";
    let res;
    try {
      res = await post("api/control/beeper", { mode: "mute" });
    } catch (e) {
      res = { ok: false, status: 0, data: { error: e.message } };
    }
    btn.disabled = false;
    if (!res.ok || !res.data || res.data.ok === false) {
      out.className = "warnbox";
      out.textContent = (res.data && (res.data.detail || res.data.error)) || `failed (${res.status})`;
      return;
    }
    // The status line above follows the stream; it reads "muted" once the
    // UPS reports it on its next poll.
    out.textContent = "Muted.";
  });
}

/* Wires every row; returns sync() to run after any store change. */
export function wireUpsFirmware(root, store) {
  const nodes = Array.from(root.querySelectorAll(".cfg[data-ups]"));
  nodes.forEach((node) => {
    const key = node.dataset.ups;
    node.querySelectorAll("[data-choice]").forEach((btn) => {
      btn.addEventListener("click", () => store.stage(key, btn.dataset.choice));
    });
    const input = node.querySelector("input[type=range]");
    if (input) input.addEventListener("input", () => store.stage(key, parseFloat(input.value)));
    if (node.dataset.type === "beeper") wireMute(node);
  });

  return function sync() {
    nodes.forEach((node) => {
      const key = node.dataset.ups;
      const v = store.value(key);
      setText(node.querySelector("[data-out]"), valText(v, node.dataset.unit || ""));
      node.querySelectorAll("[data-choice]").forEach((btn) => {
        btn.classList.toggle("primary", v != null
          && String(v).toLowerCase() === btn.dataset.choice.toLowerCase());
      });
      const input = node.querySelector("input[type=range]");
      if (input && v != null && isFinite(Number(v)) && parseFloat(input.value) !== Number(v)) {
        input.value = String(Number(v));
      }
      pillSync(node, store, key);
    });
  };
}
