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

async function run(action, out, label, body) {
  say(out, "notebox", `${esc(label)}…`);
  const res = await post(`api/control/${action}`, body || {});
  if (res.status === 409) {
    say(out, "warnbox", `${esc(res.data.error)}
      <div class="btnrow"><button class="btn danger" id="ovr">Override and ${esc(action)} anyway</button></div>`);
    const btn = out.querySelector("#ovr");
    if (btn) btn.addEventListener("click", () => run(action, out, label, Object.assign({}, body, { override: true })));
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
  const stStart = sheet.querySelector("#ctl-selftest-start");
  const stStop = sheet.querySelector("#ctl-selftest-stop");

  if (wake) wake.addEventListener("click", () => run("wake", out, "Sending magic packet"));
  if (hib) hib.addEventListener("click", () => {
    if (!window.confirm("Hibernate the box now? Anything unsaved on it stays in the image, but it will go offline.")) return;
    run("hibernate", out, "Asking the box to hibernate");
  });
  if (stStart) stStart.addEventListener("click", () => {
    if (!window.confirm("Run a battery self-test? The UPS briefly switches to battery power to test the pack -- a failing battery could momentarily drop the load.")) return;
    run("self-test", out, "Starting self-test", { action: "start" });
  });
  if (stStop) stStop.addEventListener("click", () => run("self-test", out, "Stopping self-test", { action: "stop" }));

  wireDangerZone(sheet, out);
}
