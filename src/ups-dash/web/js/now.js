/* NOW -- "will the box survive, and what is everything doing this second?" */

import { renderTimeline } from "./timeline.js";
import { sparkline } from "./spark.js";
import { dur, pct, num, watts, temp, gib, rel, DASH, isNum, esc } from "./format.js";
import { dangerZoneHtml } from "./upsoff.js";

/* The state line is rendered from snap.state, which the server computes in
   states.py. There is deliberately no second copy of this vocabulary here --
   the previous version reimplemented it and drifted, reporting "waiting for
   the 50% gate" while the UPS output was actually dead. */

function stateSentence(snap) {
  const st = snap.state || {};
  return {
    main: st.sentence || "State unknown",
    why: st.detail || "",
    action: st.action || "",
    severity: st.severity || "unknown",
  };
}

function flow(snap) {
  const u = snap.ups || {};
  const v = (snap.box || {}).vitals || {};
  const gpu = v.gpu || {}, cpu = v.cpu || {};
  const onBatt = (snap.derived || {}).mode === "battery";
  return `
    <div class="flow">
      <div class="node">
        <span class="lab">Mains</span>
        <span class="val">${num(u.input_v, 0)}<span class="unit"> V</span></span>
        <span class="sub">${onBatt ? "LOST" : "within window"}</span>
      </div>
      <div class="arrow">→</div>
      <div class="node">
        <span class="lab">UPS draw</span>
        <span class="val">${watts(u.watts)}<span class="unit"> W</span></span>
        <span class="sub">${num(u.load_pct)}% of ${num(u.nominal_w)} W</span>
      </div>
      <div class="arrow">→</div>
      <div class="node">
        <span class="lab">Box GPU</span>
        <span class="val">${num(gpu.power_w, 0)}<span class="unit"> W</span></span>
        <span class="sub">busy ${num(gpu.busy_pct)}% · cpu ${num(cpu.util_pct, 0)}%</span>
      </div>
    </div>`;
}

function tempClass(c, warm, hot) {
  if (!isNum(c)) return "";
  return c >= hot ? "hot" : c >= warm ? "warm" : "";
}

function vitals(snap) {
  const v = (snap.box || {}).vitals || {};
  const gpu = v.gpu || {}, cpu = v.cpu || {}, nvme = v.nvme || {};
  const nano = snap.nano || {};
  const cells = [
    ["GPU jct", gpu.temp_junction_c, 85, 100],
    ["CPU", cpu.temp_c, 75, 90],
    ["NVMe", nvme.temp_c, 60, 75],
    ["Nano", nano.cpu_c, 60, 80],
  ];
  return `<div class="vitals">${cells.map(([lab, c, warm, hot]) => `
    <div class="v ${tempClass(c, warm, hot)}">
      <span class="lab">${lab}</span>
      <span class="val">${temp(c)}</span>
    </div>`).join("")}</div>`;
}

export function renderNow(root, snap, ring) {
  const s = stateSentence(snap);
  const showSpark = !!snap.episode;
  root.innerHTML = `
    <section class="timeline" id="tl"></section>
    <div class="stateline">${s.main}<span class="why">${esc(s.why)}</span>${
      s.action ? `<span class="why" style="color:var(--state)">\u2192 ${esc(s.action)}</span>` : ""
    }</div>
    <section class="panel">
      <h2>Power flow</h2>
      ${flow(snap)}
    </section>
    <section class="panel">
      <h2>Temperatures</h2>
      ${vitals(snap)}
    </section>
    <section class="panel ${showSpark ? "" : "hidden"}">
      <h2>Drain trend · %/min over the last 15 min</h2>
      <canvas class="spark" id="spark"></canvas>
    </section>
    <section class="panel">
      <h2>Box</h2>
      <button class="btn" id="boxbtn">Open box detail &amp; power controls</button>
    </section>`;

  renderTimeline(root.querySelector("#tl"), snap);

  if (showSpark) {
    const css = getComputedStyle(document.body);
    sparkline(root.querySelector("#spark"), ring.map((r) => r.drain), {
      color: css.getPropertyValue("--state").trim() || "#22d3ee",
      line: css.getPropertyValue("--line").trim(),
      dim: css.getPropertyValue("--dim").trim(),
    });
  }
  return root.querySelector("#boxbtn");
}

export function boxSheetHtml(snap) {
  const b = snap.box || {};
  const v = b.last_vitals || {};
  const gpu = v.gpu || {}, cpu = v.cpu || {}, mem = v.mem || {};
  const stale = b.state !== "awake";
  const hibOk = v.can_hibernate;
  const rows = [
    ["State", `${b.state}${stale ? " (readings below are last known)" : ""}`],
    ["Why", b.why],
    ["Last seen", b.seen ? rel(b.seen) : DASH],
    ["Host", v.host],
    ["boot_id", v.boot_id ? String(v.boot_id).slice(0, 8) : DASH],
    ["Uptime", dur(v.uptime_sec)],
    ["RAM in use", gib(mem.used_gb)],
    ["Swap used", gib(mem.swap_used_gb)],
    ["CPU", `${temp(cpu.temp_c)}  ${num(cpu.util_pct, 0)}%  ${num(cpu.cores)} cores`],
    ["GPU power", `${num(gpu.power_w, 0)} W / ${num(gpu.power_cap_w, 0)} W cap${gpu.cap_locked ? " (locked)" : ""}`],
    ["GPU busy", pct(gpu.busy_pct)],
    ["VRAM", `${num(gpu.vram_used_mb, 0)} / ${num(gpu.vram_total_mb, 0)} MB`],
  ];
  return `
    <h3>Box · ${esc(v.host || "unknown")}</h3>
    ${rows.map(([k, val]) => `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${esc(val == null ? DASH : val)}</span></div>`).join("")}
    <div id="ctlresult" class="notebox hidden"></div>
    ${hibOk === false ? `<div class="notebox">Hibernate is unavailable: the
      agent is not authorised to hibernate this machine. Waking needs no
      privileges and works regardless.</div>` : ""}
    <div class="btnrow">
      <button class="btn primary" id="ctl-wake">Wake (WoL)</button>
      <button class="btn danger" id="ctl-hib" ${hibOk ? "" : "disabled"}>Hibernate</button>
    </div>
    ${dangerZoneHtml(snap)}
    <button class="btn close" id="sheetclose">Close</button>`;
}
