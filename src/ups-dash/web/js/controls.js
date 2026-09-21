/* Power controls.

   Interlocked with the sentinel: while an episode is open the server refuses
   with 409 and an explanation, and the user must explicitly override. That is
   deliberate -- a stray tap during a real outage must not fight the automation. */

import { post } from "./api.js";
import { esc } from "./format.js";
import { wireDangerZone } from "./upsoff.js";

function say(out, cls, html) {
  out.className = cls;
  out.innerHTML = html;
  out.classList.remove("hidden");
}

async function run(action, out, label, override) {
  say(out, "notebox", `${esc(label)}…`);
  const res = await post(`api/control/${action}`, override ? { override: true } : {});
  if (res.status === 409) {
    say(out, "warnbox", `${esc(res.data.error)}
      <div class="btnrow"><button class="btn danger" id="ovr">Override and ${esc(action)} anyway</button></div>`);
    const btn = out.querySelector("#ovr");
    if (btn) btn.addEventListener("click", () => run(action, out, label, true));
    return;
  }
  if (!res.ok || res.data.ok === false) {
    say(out, "warnbox",
        esc((res.data && (res.data.detail || res.data.error)) || `failed (${res.status})`));
    return;
  }
  const detail = res.data.detail;
  say(out, "notebox", esc(typeof detail === "string" ? detail : JSON.stringify(detail)));
}

export function wireSheet(sheet, snap) {
  const out = sheet.querySelector("#ctlresult");
  const wake = sheet.querySelector("#ctl-wake");
  const hib = sheet.querySelector("#ctl-hib");
  if (wake) wake.addEventListener("click", () => run("wake", out, "Sending magic packet"));
  if (hib) hib.addEventListener("click", () => {
    if (!window.confirm("Hibernate the box now? Anything unsaved on it stays in the image, but it will go offline.")) return;
    run("hibernate", out, "Asking the box to hibernate");
  });
  wireDangerZone(sheet, out);
}
