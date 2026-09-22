/* Battery-floor PARK -- CONFIG tab, "Battery floor" group.

   Split out of config.js to keep that file under the 300-line cap
   (DASHBOARD.md file-size rule), the same reason upsconfig.js exists.

   park_enabled/park_floor_pct are ordinary jetson tunables -- the same
   tunables.json and LOCAL_SPEC as wake_charge_pct (config.py) -- so they
   stage into the tab's one pending store and go out in the same single
   PUT api/config as every other tunable, after the review popup. The old
   window.confirm() on the toggle is gone: the popup's matrix is now the
   one, explicit confirmation for every change on the tab. */

import { num, isNum, esc } from "./format.js";
import { floorWarning } from "./catalog.js";
import { setText, pillSync } from "./cfgdom.js";

function floorEffect(value) {
  return `If power is still out when the pack reaches ${num(value, 0)}%, the UPS parks `
    + `— output off, charge held — and comes back seconds after mains returns. The box `
    + `then needs BIOS AC BACK = Always On to power itself back on.`;
}

// Unknown reads as on: that is the compiled-in default (config.BASELINE).
const isOn = (v) => (isNum(v) ? v >= 0.5 : true);

export function parkHtml(store) {
  const enabled = isOn(store.value("park_enabled"));
  const floor = store.value("park_floor_pct");
  return `
    <section class="panel"><h2>Battery floor</h2>
      <div class="cfg" data-key="park_enabled">
        <div class="top">
          <span class="name">Park at the floor</span>
          <span class="val" data-out>${enabled ? "on" : "off"}</span>
        </div>
        <div class="btnrow" style="grid-template-columns:repeat(2,1fr)">
          <button type="button" class="btn${enabled ? " primary" : ""}" data-choice="1">On</button>
          <button type="button" class="btn${enabled ? "" : " primary"}" data-choice="0">Off</button>
        </div>
        <div class="meta"><span>jetson</span><span data-dirty class="pill hidden"></span></div>
        <div class="effect">When on, and the outage is still running once the pack reaches
          the floor below, the UPS is told to park: output off, charge held at the floor,
          back within seconds of mains returning. When off, the pack is left to the UPS's
          own idle drain for the rest of the outage.</div>
      </div>
      <div class="cfg${enabled ? "" : " locked"}" data-key="park_floor_pct">
        <div class="top">
          <span class="name">Battery floor</span>
          <span class="val"><span data-out>${num(floor, 0)}</span> %</span>
        </div>
        <div class="meta"><span>jetson</span><span>range 15–80</span>
          <span data-dirty class="pill hidden"></span></div>
        <input type="range" min="15" max="80" step="5" value="${isNum(floor) ? floor : 35}"
               aria-label="Battery floor" ${enabled ? "" : "disabled"}>
        <div class="effect" data-effect>${esc(floorEffect(floor))}</div>
        <div class="lockwhy hidden" data-warn></div>
      </div>
    </section>`;
}

/* Wires both rows; returns sync() to run after any store change. */
export function wireParkConfig(root, store) {
  const enableNode = root.querySelector('.cfg[data-key="park_enabled"]');
  const floorNode = root.querySelector('.cfg[data-key="park_floor_pct"]');
  if (!enableNode || !floorNode) return () => {};
  const input = floorNode.querySelector("input");

  enableNode.querySelectorAll("[data-choice]").forEach((btn) => {
    btn.addEventListener("click", () => store.stage("park_enabled", parseFloat(btn.dataset.choice)));
  });
  input.addEventListener("input", () => store.stage("park_floor_pct", parseFloat(input.value)));

  return function sync() {
    const enabled = isOn(store.value("park_enabled"));
    const floor = store.value("park_floor_pct");
    setText(enableNode.querySelector("[data-out]"), enabled ? "on" : "off");
    enableNode.querySelectorAll("[data-choice]").forEach((btn) => {
      btn.classList.toggle("primary", (btn.dataset.choice === "1") === enabled);
    });
    pillSync(enableNode, store, "park_enabled");
    floorNode.classList.toggle("locked", !enabled);
    input.disabled = !enabled;
    if (isNum(floor) && parseFloat(input.value) !== floor) input.value = String(floor);
    setText(floorNode.querySelector("[data-out]"), num(floor, 0));
    setText(floorNode.querySelector("[data-effect]"), floorEffect(floor));
    // Checked against the reserve that WOULD result, pending edit included.
    const warn = enabled ? floorWarning(floor, store.value("reserve_pct")) : "";
    const warnNode = floorNode.querySelector("[data-warn]");
    setText(warnNode, warn);
    warnNode.classList.toggle("hidden", !warn);
    pillSync(floorNode, store, "park_floor_pct");
  };
}
