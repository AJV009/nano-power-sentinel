/* Emergency shutdown controls.

   Built for the actual use case: something is happening in the room and
   everything needs to stop NOW. So no typing, no password -- three taps.
   A confirmation you cannot complete under stress is worse than no button.

   The escalation is the safety mechanism. Each tap restates the consequence
   in stronger terms, and the arming resets after 6 seconds of hesitation, so
   a pocket tap or a stray touch can never reach the third stage. */

import { post } from "./api.js";
import { esc, dur, isNum } from "./format.js";

const RESET_MS = 6000;

const STEPS = {
  safe: [
    "Safe shutdown",
    "Tap again — hibernate the box, then cut power (1/3)",
    "This cannot be undone remotely (2/3)",
    "TAP TO SHUT EVERYTHING DOWN (3/3)",
  ],
  instant: [
    "Instant cut",
    "Tap again — cuts power immediately, no hibernate (1/3)",
    "Anything unsaved on the box will be LOST (2/3)",
    "TAP TO CUT POWER NOW (3/3)",
  ],
};

function phaseLine(cut, timerShutdown) {
  if (!cut || !cut.phase || cut.phase === "idle") return "";
  const bits = {
    hibernating: "Asking the box to hibernate…",
    waiting_for_draw: "Waiting for its power draw to fall…",
    cutting: "Cutting the output…",
    aborted: "Aborted — output still on.",
    failed: "FAILED.",
    done: "Output cut.",
  };
  const load = isNum(cut.current_load)
    ? ` · draw now ${cut.current_load}% (was ${cut.baseline_load}%)` : "";
  const left = isNum(cut.remaining) && cut.remaining > 0
    ? ` · ${dur(cut.remaining)} left` : "";
  // The countdown above is this dashboard's own ESTIMATE of when the cut
  // will land. ups.timer.shutdown is the UPS's own quick-polled register --
  // proof the command actually reached the hardware, not a guess -- so it
  // is shown as a separate confirmation line rather than folded into the
  // same number, unless the two already agree, in which case adding it
  // would just repeat what is already on screen.
  const upsSecs = isNum(timerShutdown) && timerShutdown >= 0 ? Math.round(timerShutdown) : null;
  const dupe = upsSecs !== null && isNum(cut.remaining) && Math.round(cut.remaining) === upsSecs;
  // Always shown once the register is counting -- the point is the proof the
  // UPS armed, and hiding it when the numbers agree would hide that proof.
  const confirm = upsSecs === null ? ""
    : `<div class="notebox">UPS confirms: ${dupe ? "its own countdown matches" : `output off in ${upsSecs}s`}</div>`;
  return `<div class="${cut.phase === "aborted" ? "notebox" : "warnbox"}">
      <b>${esc(bits[cut.phase] || cut.phase)}</b>${esc(load)}${esc(left)}
      ${cut.detail ? `<br><span style="font-size:12px">${esc(cut.detail)}</span>` : ""}
    </div>${confirm}`;
}

export function dangerZoneHtml(snap) {
  const cut = (snap && snap.ups_cut) || {};
  const running = cut.active;
  const timerShutdown = snap && snap.ups && snap.ups.timer_shutdown;
  return `
    <section class="panel" style="margin-top:18px">
      <h2 style="color:var(--danger)">Emergency shutdown</h2>
      <div class="notebox">
        Both options end with the UPS output <b>off</b>. This UPS has no
        <span class="mono">load.on</span>, so it stays off until someone
        presses its front-panel button — Wake-on-LAN cannot bring the box
        back once standby power is gone.
      </div>
      <div id="cutphase">${phaseLine(cut, timerShutdown)}</div>
      ${running ? `<div class="btnrow">
          <button class="btn primary" id="cut-abort">Abort</button>
        </div>` : `
        <div class="btnrow">
          <button class="btn danger" id="cut-safe" data-mode="safe" data-step="0">
            ${STEPS.safe[0]}</button>
          <button class="btn danger" id="cut-instant" data-mode="instant" data-step="0">
            ${STEPS.instant[0]}</button>
        </div>
        <div class="effect" style="margin-top:8px">
          <b>Safe</b> hibernates the box and waits for its draw to actually fall
          before cutting, so it comes back resumable.
          <b>Instant</b> cuts immediately regardless of what is running.
        </div>`}
    </section>`;
}

function arm(btn, out, onFire) {
  let timer = null;
  const reset = () => {
    btn.dataset.step = "0";
    btn.textContent = STEPS[btn.dataset.mode][0];
    btn.classList.remove("primary");
  };
  btn.addEventListener("click", () => {
    const step = parseInt(btn.dataset.step || "0", 10) + 1;
    clearTimeout(timer);
    if (step >= STEPS[btn.dataset.mode].length) {
      reset();
      onFire(btn.dataset.mode);
      return;
    }
    btn.dataset.step = String(step);
    btn.textContent = STEPS[btn.dataset.mode][step];
    // Hesitation disarms it: a stray tap must never survive long enough to
    // become the third one.
    timer = setTimeout(reset, RESET_MS);
  });
}

export function wireDangerZone(sheet, out) {
  const fire = async (mode) => {
    out.classList.remove("hidden");
    out.className = "notebox";
    out.innerHTML = mode === "safe"
      ? "Hibernating the box, then cutting once its draw falls…"
      : "Cutting the output now…";
    const res = await post("api/control/ups-off", { mode, confirmed: true });
    if (!res.ok && res.status !== 202) {
      out.className = "warnbox";
      out.innerHTML = esc((res.data && res.data.error) || `failed (${res.status})`);
      return;
    }
    out.className = "warnbox";
    out.innerHTML = esc(res.data.warning || res.data.detail || "Sequence started.");
  };

  ["cut-safe", "cut-instant"].forEach((id) => {
    const btn = sheet.querySelector("#" + id);
    if (btn) arm(btn, out, fire);
  });

  const abort = sheet.querySelector("#cut-abort");
  if (abort) abort.addEventListener("click", async () => {
    abort.disabled = true;
    const res = await post("api/control/ups-abort", {});
    out.classList.remove("hidden");
    if (res.ok && res.data.ok) {
      out.className = "notebox";
      out.innerHTML = "Aborted. The UPS output stays on.";
    } else {
      out.className = "warnbox";
      out.innerHTML = `Abort FAILED: ${esc((res.data && (res.data.detail || res.data.error)) || res.status)}
        <br><b>Press the UPS front-panel button if the output drops.</b>`;
      abort.disabled = false;
    }
  });
}
