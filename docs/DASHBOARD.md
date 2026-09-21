# Power telemetry dashboard — design spec

**Status:** BUILT 2026-09-20, phases 1-8 (hibernate authorisation outstanding).
Spec written 2026-09-20 and kept in step with what was actually built.
**Companion docs:** [DESIGN.md](DESIGN.md) (the power system itself), [BUILD-LOG.md](BUILD-LOG.md) (build log),
the machine inventory (not published) (not published -- personal machine inventory) (parked jetson optimisation survey).

A web dashboard for the jetson/UPS/W7900 power system built in DESIGN.md. Runs as a
background service on the jetson, reachable from a phone over tailscale.

This spec supersedes the two parked notes in BUILD-LOG.md ("Telemetry dashboard" and
"dashboard power controls"), which are now folded in here.

---

## 00 — What it is for

The power system answers one question in hardware: *will the box survive this outage, and
when does it come back?* The dashboard's job is to make that question answerable at a
glance from a phone, to keep a durable record of every outage so patterns become visible,
and to let the thresholds be tuned without ssh.

It is explicitly **not** a general system monitor that happens to know about a UPS.

---

## 01 — Decisions taken, and what was rejected

Recorded so they are not silently relitigated later.

| # | Decision | Rejected alternatives | Why |
|---|---|---|---|
| D1 | **Jetson hosts everything.** No remote/Oracle leg. | Oracle-hosted UI; dual-host | During an outage the ISP modem is almost certainly not on a UPS, so the uplink dies — but the router *is* on the jetson's UPS. A jetson-hosted dashboard keeps working on LAN exactly when it matters most. Tailscale then supplies remote access without a second host. |
| D2 | **Access via tailscale**, with a hostname and HTTPS via Tailscale Serve. | LAN-only; public + cloudflared + auth | Gives phone access from anywhere with the tailnet as the auth boundary. No login screen to build, no public exposure of a service that can power-cycle a workstation. |
| D3 | **Python 3.8 stdlib only** on both machines. | Node/Express; FastAPI + venv | The jetson has **no node/npm**, 2 cores at 921 MHz (5 W mode) and 5.5 GB free. Stdlib matches the existing sentinel and governor, needs no package manager, and does not fight the power mode that was deliberately set. |
| D4 | **Static frontend, no build step.** Vanilla JS + vendored uPlot. | React/Vite; server-side rendering | Nothing to build on an ARM64 box with no node. Vendored uPlot is one ~45 KB file with no dependencies, designed for weak devices. |
| D5 | **Survival timeline is the hero**, not a gauge. | Battery gauge; two-machine vitals; event log first | One object encodes charge, runtime, drain rate and the reserve threshold together. This is the single largest anti-duplication win — see §05. |
| D6 | **Pull-based box agent**, jetson polls the box. | Box pushes to jetson | The jetson is already the controller. Pulling lets it correlate *box unreachable* with *we just commanded a hibernate* and label the state correctly rather than alarming. |
| D7 | **Tiered persistence** — 1 Hz only during episodes. | 1 Hz to disk always; RAM only | The SD card is the system's known weak link and is 81% full. See §07. |
| D8 | **Config is editable from the UI**, within hard bounds, with fail-safe loading. | Read-only display; ssh only | Requested. Requires a bounded, defensive change to safety-critical code — see §09. |

---

## 02 — Principles

1. **The dashboard changes declared intent, never control flow.** It writes bounded values
   into a config file. It never edits code, never sequences power itself, and every
   consumer falls back to compiled-in defaults if what it reads is bad.
2. **The sentinel and governor stay verified.** They are safety-critical and were proven by
   test. The only change either is permitted is the tunables loader in §09 — no logic
   changes, no restructuring, defaults unchanged.
3. **No redundant data points.** A number earns its place by answering a question nothing
   else on screen answers. A *transformation* that reveals something new — a rate, a trend,
   a distribution, a comparison — is welcome even when it derives from data shown elsewhere.
   The map in §05 is a working guide to keep screens from becoming walls of repeated
   readouts; it is not a prohibition.
4. **Protect the SD card.** It shaped the storage design more than anything else.
5. **Absence of data is a third state.** Never fold "cannot read" into "fine" or "failing".
   This bug was shipped once in the sentinel already (BUILD-LOG.md, 2026-09-20).

---

## 03 — Architecture

```
   JETSON  jetson-nano  10.0.0.10                     BOX  workstation  10.0.0.20
   +--------------------------------------+            +---------------------------+
   |  nut upsd :3493 ---- 1 Hz ---+        |            |  box-agent  :9009         |
   |  journalctl -f -o json ------+        |            |    read-only sysfs+journal|
   |    (ups-sentinel, nut-*)     |        |            |    narrow config PUT      |
   |                              v        |            +------------^--------------+
   |                        ups-dash       |   5 s poll              |
   |                     +----+-----+------+-------------------------+
   |                     |          |      |
   |            RAM ring |          | SQLite  /var/lib/ups-dash/telemetry.db
   |          (60 min    |          |      |
   |           @ 1 Hz)   |          |      |
   |                     v          v      |
   |              ThreadingHTTPServer :8088|
   |               static bundle + JSON + SSE
   +-------------------|------------------+
                       |
                  tailscale0  --->  phone / laptop, anywhere
```

Two new units total. Nothing existing is restructured.

| Unit | Machine | Purpose |
|---|---|---|
| `ups-dash.service` | jetson | collector + storage + web server |
| `box-agent.service` | box | read-only telemetry endpoint + config PUT |
| (existing) `ups-sentinel`, `hibernate-governor` | both | unchanged except §09 loader |

---

## 04 — Screens

Mobile-first. Bottom nav, four tabs. Each answers one question and nothing else.

| Tab | The one question it answers |
|---|---|
| **NOW** (default) | Will the box survive, and what is everything doing this second? |
| **HISTORY** | What happened while I was asleep or away? |
| **HEALTH** | Will this system still work next month? |
| **CONFIG** | What are the thresholds, and what happens if I change them? |

### 4.1 NOW

**Hero — the survival timeline.** A single horizontal axis. It absorbs charge, runtime,
drain rate, time-to-hibernate and the reserve threshold into one object.

```
   on mains:
     [##############################]  100%
     fully charged - ~66 min runway if mains drops now - drawing ~43 W

   on battery:
      now            hibernate              empty
       |                 |                    |
     [#################|...........|..........]
      elapsed 14m       to-hib 22m   reserve 30%
     ON BATTERY - draining 1.9 %/min - hibernate at 21:58

   recovering (mains back, box still asleep):
                            wake gate
                                |
     [############............|...............]
      38%                      50%
     RECHARGING - box wakes at 50% - mains held 40s of 120s
```

**Three modes, one object.** The hero never disappears or swaps for something else; it
re-scales to whichever question is live — *how much runway*, *how long until hibernate*, or
*how long until wake*. This is what lets NOW carry no gauge, no standalone runtime readout
and no live charge chart: all three are views of this one bar.

Rendered as a canvas/SVG bar with markers, not a chart. State drives colour: cyan when
nominal, amber on battery, red below reserve, green while recovering.

**State line.** One sentence of plain English, the only place state words appear:
`On mains - box awake - sentinel idle`. Other examples: `ON BATTERY 14m - box awake -
hibernate in ~22m`, `Mains back 40s ago - box asleep - waiting for the 50% gate`. The charge figure is
deliberately not repeated here — during recovery the timeline itself shows charge climbing
toward the marked gate.

**Power flow.** A small diagram, not a row of numbers:

```
   mains 244 V  ->  [ UPS ]  ->  ~43 W total
                                    |
                                    +-- box GPU 11 W, rest ~32 W
```

Deliberately carries no temperature and no battery figure: temperatures live in the vitals
strip, charge lives in the timeline, battery voltage lives in HEALTH. This panel is about
energy moving, and nothing else.

The UPS draw and the box's own GPU PPT come from two genuinely independent measurement
paths, so showing both is a cross-check, not duplication — and the diagram makes that
relationship visible instead of listing two watt figures.

WARNING: `ups.load` is an integer percent, so derived watts quantise to 8.65 W steps.
Always render as `~43 W`, never `43.2 W`, with the reason in a tooltip.

**Vitals strip.** Four temperatures as small coloured numerals: GPU junction, CPU Tctl,
NVMe composite, nano CPU. Four distinct sensors, one compact row, no sparklines.

**Box sheet** (tap the box in the state line): uptime, boot_id, RAM/swap, `gpu_busy_percent`,
VRAM, last hibernate/resume durations, and the power controls from §10.

**Drain trend** (episode only). A small sparkline of %/min over the last 15 minutes. This
derives from charge, which the timeline already shows — but it answers a different question:
*is the drain accelerating?* The timeline gives a point projection; the trend shows whether
that projection is getting worse. Given how fast this pack moves, that is worth its space.

Not carried on NOW: a battery percentage gauge and a standalone runtime readout. Both are
literal restatements of the timeline rather than transformations of it, which is the
distinction that matters.

### 4.2 HISTORY

A reverse-chronological feed of **episode cards**. One card per episode — an episode is a
contiguous period of "not normal": an outage, a manual hibernate, or a test.

```
   +--------------------------------------------------+
   | OUTAGE   Sat 20 Sep  16:02 -> 16:47    45m        |
   | 100% -> 62%   hibernated 16:31 (2.5 GiB, 20 s)    |
   | woke 16:49 via WoL after 2m gate wait             |
   +--------------------------------------------------+
```

Tapping a card expands the **recorded 1 Hz trace** for that episode as a uPlot chart:
charge, load/watts, battery voltage, box state bands.

**This is the only place in the app where a time-series chart of charge exists**, and it is
always scoped to one past episode. That single rule eliminates the largest duplication risk
in a dashboard like this — a live chart that says the same thing as the historical chart.

Config changes and service faults also appear in this feed as thin one-line entries.

### 4.3 HEALTH

Slow-moving things no other screen shows.

- **Pack health** — `battery.voltage` under load (the only high-resolution analog reading
  the UPS gives, and the honest degradation signal), pack age from the 2025-11-13 mfr date,
  cycle count derived from episodes, `ups.test.result`, and *measured* runtime vs *claimed*
  runtime drifting apart over months.
- **Mains quality** — distribution of `input.voltage` over time against the 170 V / 294 V
  transfer thresholds. Answers "how close does my mains actually get to tripping the UPS."
- **Infrastructure** — SD card free space (the reliability risk), health of every unit in
  the chain (eight existing, plus the two new), memory on both machines, collector write volume.

### 4.4 CONFIG

See §09.

---

## 05 — The anti-duplication map

Primary home for each datum, so no screen turns into a wall of repeated readouts. Read this
as a guide, not a law: where a transformation answers a genuinely different question, it
belongs, and is marked *derived* below.

| Datum | Home | Elsewhere? |
|---|---|---|
| `battery.charge` | timeline fill | no |
| `battery.runtime` | timeline segment | no |
| drain rate, time-to-hibernate | timeline markers | no |
| `ups.status` | state line | no |
| box power state | state line | no |
| `input.voltage` | power flow (live) + HEALTH (distribution) | **yes, justified** — a live value and a long-run shape are different facts |
| `ups.load` -> watts | power flow | no |
| GPU PPT watts | power flow | no |
| 4x temperatures | vitals strip | no |
| `gpu_busy_percent`, VRAM, uptime, boot_id | box sheet | no |
| charge over time | HISTORY episode trace | *derived:* drain-rate trend on NOW — the second derivative, a different question |
| `battery.voltage` | HEALTH pack panel | no |
| lifecycle timestamps | HISTORY cards | no |
| SD free, unit health | HEALTH infra | no |
| tunable values | CONFIG (edit) + timeline reserve marker (effect) | **yes, justified** — a setting and its visible consequence |

---

## 06 — Components

### 6.1 `box-agent` (box, `/usr/local/sbin/box-agent`)

Python 3 stdlib. `ThreadingHTTPServer` on **:9009**, bound to the LAN interface.

`GET /vitals` returns JSON: GPU PPT watts, GPU edge/junction/mem temps, `gpu_busy_percent`,
VRAM used, CPU Tctl, NVMe composite temp, RAM/swap, uptime, boot_id, load average, and the
last N lines of `hibernate-governor` journal as structured events.

`PUT /config` accepts only the known governor keys within bounds (§09). No paths, no shell,
no arbitrary keys. Rejects with a reason string.

Reads sysfs only — needs no root for `/vitals`. The config write needs write access to
`/etc/ups-dash/tunables.json`, which the dedicated user owns.

The unit runs as an unprivileged user, **not** root. The one privileged thing it must do is
hibernate the box (§10), granted by a narrow sudoers entry permitting exactly
`/usr/bin/systemctl hibernate` — no wildcards, no shell, nothing else. Everything else the
agent does is unprivileged reads.

WARNING: this process must never block. Every sysfs read is wrapped and a failed read
returns `null` for that field, not an error for the whole response.

### 6.2 `ups-dash` (jetson, `/usr/local/sbin/ups-dash`)

Python 3 stdlib. Three threads:

1. **UPS poller** — 1 Hz against `upsd` on 127.0.0.1:3493, reusing the governor's proven
   persistent-socket client. `None` on failure, never a stale value.
2. **Box poller** — 5 s against `box-agent`, hard 3 s timeout. Distinguishes *refused*,
   *timed out* and *unreachable*, and correlates with sentinel state to label the box
   `awake` / `hibernated` / `unreachable` correctly.
3. **Journal tail** — `journalctl -f -o json -u ups-sentinel -u nut-server -u nut-driver`
   as a subprocess, parsed into events. This is how the dashboard learns what the sentinel
   decided **without modifying it**.

Plus the HTTP server thread on **:8088**.

### 6.3 Storage

SQLite at `/var/lib/ups-dash/telemetry.db`, WAL mode.

```sql
CREATE TABLE event (
  id         INTEGER PRIMARY KEY,
  ts         REAL NOT NULL,
  kind       TEXT NOT NULL,   -- outage_start|outage_end|hibernate_start|hibernate_done|
                              -- resume|wake_sent|wake_ok|gate_wait|config_change|
                              -- service_fault|comms_lost|comms_back
  episode_id INTEGER,
  detail     TEXT             -- JSON
);
CREATE INDEX event_ts ON event(ts);

CREATE TABLE episode (
  id           INTEGER PRIMARY KEY,
  started      REAL NOT NULL,
  ended        REAL,
  kind         TEXT NOT NULL,   -- outage|manual_hibernate|test
  charge_start REAL,
  charge_min   REAL,
  charge_end   REAL,
  hibernated   INTEGER DEFAULT 0,
  hib_bytes    INTEGER,
  hib_secs     REAL,
  resumed_at   REAL,
  resume_secs  REAL,
  wake_cause   TEXT,            -- wol|manual|ac_back|unknown
  summary      TEXT             -- JSON
);

CREATE TABLE sample (
  ts           REAL NOT NULL,
  res          INTEGER NOT NULL,  -- 1 = episode 1 Hz, 30 = mains idle, 3600 = rollup
  episode_id   INTEGER,
  ups_status   TEXT,
  charge       REAL,
  runtime      INTEGER,
  batt_v       REAL,
  load_pct     INTEGER,
  input_v      REAL,
  box_state    TEXT,
  box_gpu_w    REAL,
  box_gpu_c    REAL,
  box_cpu_c    REAL,
  box_nvme_c   REAL,
  box_gpu_busy INTEGER,
  box_vram_mb  INTEGER,
  box_ram_gb   REAL,
  nano_cpu_c   REAL,
  nano_ram_mb  INTEGER,
  PRIMARY KEY (ts, res)
);
```

---

## 07 — The SD-card constraint

Writing 1 Hz samples to disk forever would wear out the system's weakest component. The
card is 29 GB, 81% full, and is the single point of failure for a machine whose entire job
is to still be alive when everything else is not.

| Condition | Persisted |
|---|---|
| On mains, nothing happening | one row per **30 s** (`res=30`) |
| **Episode active** — on battery, hibernating, resuming, or gate-waiting | full **1 Hz** (`res=1`) |
| Discrete events | always, forever |

The last 60 minutes at 1 Hz always live in a **RAM ring buffer**, so NOW can draw a smooth
recent trace with zero disk writes. When an episode opens, the relevant slice of the ring
buffer is flushed to disk so the trace includes the moments *before* the trigger.

**Retention:** events forever (tiny). `res=1` episode traces kept whole — an outage is
finite and 12 h at 1 Hz is only ~43k rows. `res=30` rolled up to `res=3600` after 7 days;
hourly rows kept forever at ~8,800 rows/year.

Estimated steady-state write volume: ~2,880 rows/day at rest. Negligible.

Planned but not built: a periodic copy of the DB to the box's 2.2 TB disk over LAN. Same
network, no internet, no third host. Schema and file layout are designed so this is a
drop-in addition. **This is the only mitigation for total SD-card loss** — see §12.

---

## 08 — HTTP API

All paths relative so Tailscale Serve can front the app at any prefix.

| Method | Path | Returns |
|---|---|---|
| GET | `/api/now` | current snapshot |
| GET | `/api/stream` | **SSE**, snapshot pushed ~1 Hz |
| GET | `/api/episodes?limit=&before=` | episode cards |
| GET | `/api/episode/<id>` | detail + full 1 Hz trace |
| GET | `/api/health` | pack, mains distribution, infra |
| GET | `/api/config` | tunables: value, default, bounds, machine, locked flag, help text |
| PUT | `/api/config` | `{key: value}`; returns applied/rejected **with reasons** |
| GET | `/api/calibration` | measured write rates vs configured |
| POST | `/api/control/wake` | send WoL |
| POST | `/api/control/hibernate` | graceful; `?force=1` for forced |

SSE rather than polling: one connection, clean reconnect on a phone changing networks,
trivial in stdlib. With 2 cores at 921 MHz this is sized for a handful of clients, not many.

---

## 09 — Config system

The one part of this design that touches safety-critical code. Treated accordingly.

### 9.1 Where the tunables live today

Hardcoded module constants in two scripts on two machines:

```
/usr/local/sbin/hibernate-governor  (box)      /usr/local/sbin/ups-sentinel  (jetson)
  RESERVE_PCT       = 30.0                       WAKE_CHARGE_PCT   = 50.0
  SAFETY_SEC        = 30.0                       MAINS_STABLE_SEC  = 120.0
  WRITE_RATE_GBPS   = 0.5                        WAKE_TRIES        = 5
  FIXED_OVERHEAD    = 15.0                       WAKE_INTERVAL     = 30.0
  SETTLE_SEC        = 30.0                       POLL              = 5.0
  DEBOUNCE          = 3                          REPORT_EVERY      = 30.0
  POLL              = 1.0
  COMMS_LOSS_LIMIT  = 45.0
  REPORT_EVERY      = 15.0
```

### 9.2 Tier 1 — Thresholds

Primary controls, prominent in the UI.

| Setting | Key | Now | Machine | Bounds |
|---|---|---|---|---|
| Reserve after hibernate | `reserve_pct` | 30 % | box | 10 – 80 |
| Wake when charge reaches | `wake_charge_pct` | 50 % | jetson | `reserve_pct + 10` – 100 |
| Mains must hold for | `mains_stable_sec` | 120 s | jetson | 30 – 1800 |

### 9.3 Tier 2 — Margins

Behind a disclosure toggle.

| Setting | Key | Now | Machine | Bounds |
|---|---|---|---|---|
| Safety margin | `safety_sec` | 30 s | box | 0 – 300 |
| Hibernate write rate | `write_rate_gbps` | 0.50 | box | 0.1 – 5.0 |
| Fixed overhead | `fixed_overhead_sec` | 15 s | box | 0 – 120 |
| Comms-loss limit | `comms_loss_limit_sec` | 45 s | box | 15 – 600 |
| Wake retries | `wake_tries` | 5 | jetson | 1 – 20 |
| Wake retry interval | `wake_interval_sec` | 30 s | jetson | 5 – 300 |

### 9.4 Tier 3 — Shown but locked

Displayed read-only with the reason attached. Not editable from the UI at any tier.

| Setting | Value | Why locked |
|---|---|---|
| NUT `pollinterval` | 2 | **Setting this to 1 killed the APC HID interface mid-outage on 2026-09-20** (`/dev/hidraw1` vanished, driver alive but `Data stale`). Shown specifically so the incident is not forgotten and not repeated. |
| `DEBOUNCE` | 3 | Changing it alters trigger semantics, not a threshold |
| `SETTLE_SEC` | 30 | Compensates a hardware gauge transient; not a preference |
| script `POLL`, `REPORT_EVERY` | — | Loop timing, not policy |
| `BOX_IP`, `BOX_MAC`, `BROADCAST` | — | Identity, not tuning |

### 9.5 Safety mechanics

1. **Fail-safe load.** Config files are `/etc/ups-dash/tunables.json` on each machine.
   Missing, malformed, unparseable, unknown key, wrong type, or out of range -> the
   compiled-in default is used and the rejection is logged loudly. **A bad config file can
   never prevent the governor or the sentinel from starting.** Every other property here
   rests on this one.

2. **Validated twice.** The API rejects with a human-readable reason; the consuming script
   independently clamps at load. The file can also be hand-edited, so the script must not
   trust it.

3. **Coupled-constraint guard.** `wake_charge_pct` must exceed `reserve_pct` by at least 10
   points. Below that, the box wakes into a charge where the governor immediately wants to
   hibernate again — a **wake/hibernate flapping loop** that would chew both the pack and
   the box. The UI refuses the combination and explains why; both scripts also enforce it
   at load. This is the only genuinely dangerous setting in the surface.

4. **Re-read on change.** Each script checks the config file's mtime at the top of its poll
   loop and reloads if it moved. No restart, so there is no window where the governor is
   not running.

5. **Cross-machine writes.** The governor's tunables live on the box. `box-agent` exposes
   one narrow `PUT /config` accepting only the five known governor keys within bounds. If
   the box is hibernated the change is queued on the jetson and applied on next contact,
   with the UI showing `pending - box is asleep`.

6. **Live effect preview.** Moving a control shows the consequence computed with the
   governor's own formula, e.g. moving reserve 30 -> 40: *"at the current ~43 W draw,
   hibernate would fire about 18 min sooner."*

7. **Every change is an event.** `reserve 30 -> 35 - from dashboard - 21:40` appears in the
   HISTORY feed. Six months from now, when behaviour looks odd, this is the record that
   explains it.

8. WARNING: **changes during an active outage take effect immediately.** The UI shows a red
   banner saying so rather than silently deferring. Deferring was considered and rejected —
   if you are watching an outage and decide the reserve is too low, immediate is what you
   want, and a hidden delay would be worse than an honest warning.

### 9.6 The permitted change to safety-critical code

Enumerated exactly. Anything beyond this list is out of scope for this spec.

- Add a shared `load_tunables(path, defaults, bounds)` helper, roughly 30 lines, which
  **never raises**. Returns defaults for anything missing or invalid, logs each rejection.
- In `hibernate-governor`: replace five module constants with lookups into the loaded dict;
  re-read on mtime change at the top of the poll loop. The compiled-in defaults stay
  **byte-identical to today's values**.
- In `ups-sentinel`: the same for four constants.
- No logic changes. No restructuring. No change to the state machines, the `None`-handling,
  or the killpower-free recovery path.

Both files are backed up before the edit, and the existing verification steps in BUILD-LOG.md
§06 are re-run afterwards.

### 9.7 Self-calibration

The dashboard records image size and write duration for every hibernate, so CONFIG can show:

```
  write_rate_gbps   configured 0.50
                    measured   0.72  0.69  0.71   (3 hibernates)
                    [ apply measured 0.70 ]
```

This closes an open item in BUILD-LOG.md: the 0.5 figure rests on a single idle measurement and
was never validated under load, which is exactly the case that matters most. The dashboard
calibrates its own constant from observed behaviour instead of guessing.

WARNING: `apply measured` must never be automatic. A single anomalous hibernate could push
the estimate optimistic in the one situation where being optimistic is dangerous.

---

## 10 — Power controls

In a sheet opened from the box's state line, not a fourth tab.

| Action | Mechanism | Built |
|---|---|---|
| **Wake** | jetson broadcasts a WoL magic packet to `aa:bb:cc:dd:ee:ff` on UDP 9 and 7. **Needs no privileges at all.** | yes |
| **Hibernate now** | `systemctl hibernate` on the box via `box-agent` | blocked, see below |

⚠ **Correction to this spec: the sudoers approach cannot work.** `box-agent`
runs with `NoNewPrivileges=yes`, which disables setuid outright, so sudo is
inert in that unit. `systemctl hibernate` talks to logind over D-Bus, which
polkit governs, so the authorisation must be a **polkit rule** scoped to one
action (`org.freedesktop.login1.hibernate`) and one user. That keeps the
hardening rather than trading it away.

**Installed 2026-09-20 with the owner's explicit authorisation** as
`/etc/polkit-1/rules.d/49-box-agent-hibernate.rules`. It grants exactly one
action to exactly one user and nothing else; `box-agent` now reports
`can_hibernate: true`. **Revoke by deleting that one file** and restarting
`polkit`. It was deliberately left out of the build until asked for, because
granting a service account the right to hibernate a workstation is an
operator's decision rather than a build step.

WARNING: **interlocked with the sentinel.** While the sentinel is mid-sequence (gate-waiting,
or a wake attempt in flight) the controls are disabled with an explanation, and an explicit
override is required. A stray tap during a real outage must not be able to fight the
automation.

The tailnet is the auth boundary — no login screen. This is only acceptable because D2 keeps
the service off the public internet; if that ever changes, auth becomes mandatory first.

---

## 11 — Theme

"Edgy": sharp, high-contrast, instrument-like. Mobile-first, desktop as a widened layout of
the same components.

```
                        dark (default)     light
  --bg                  #0a0a0b            #f7f7f5
  --surface             #131316            #ffffff
  --line                #26262b            #e2e2de
  --text                #e8e8ea            #16161a
  --dim                 #7a7a85            #6b6b73
  --accent  nominal     #22d3ee  cyan      #0891b2
  --warn    on battery  #f59e0b  amber     #b45309
  --danger  below res.  #ef4444            #dc2626
```

- **Zero border radius** everywhere. Hairline 1 px rules, no shadows, no gradients.
- **Monospace numerals** for every measured value; proportional sans for prose.
- Generous negative space; the timeline gets the full width and real vertical room.
- Follows `prefers-color-scheme`; manual toggle persisted to `localStorage`.
- State colour propagates: when on battery the accent shifts amber across the whole UI, so
  the state is legible from across the room without reading anything.

**Mobile specifics** — bottom nav fixed with safe-area insets; minimum 44 px touch targets;
the timeline is the only element allowed to be full-bleed; charts scroll horizontally within
their card rather than forcing page-level horizontal scroll; no hover-dependent affordances.

---

## 12 — Risks and open items

1. **SD card loss destroys all history.** D1 accepted this. The mitigation — periodic DB
   copy to the box's 2.2 TB disk — is designed for but not built. Until it exists, the
   durability goal from the original parked note is **not met**.
2. **CPU contention.** The jetson has 2 cores at 921 MHz and the sentinel must never be
   starved. `ups-dash.service` gets `Nice=10` and a low `CPUWeight`; the sentinel stays at
   default priority. Verify under an SSE client plus an active episode.
3. **Clock.** Episode timestamps depend on the jetson's clock. If NTP is not running, the
   entire history is unreliable. Verify and record.
4. **`box-agent` is another thing that can hang.** The collector uses a hard 3 s timeout and
   treats a hang as `unreachable`, never as stale-but-fine.
5. **Journal tail as an interface.** Reading the sentinel's log means its message wording is
   now load-bearing. Parsing is tolerant, and unmatched lines are surfaced rather than
   silently dropped.
6. **GPU power cap reads 241 W, not 295 W** (`amdgpu-pci-0300`, `PPT cap = 241.00 W`). This
   contradicts the build sheet. Not a dashboard problem, but the dashboard is where it
   became visible — reconcile separately.
7. **Untested path inherited from the power system:** the full power-cycle test through the
   50% gate has still never run end to end. The dashboard will make the next one legible,
   but it does not substitute for running it.

---

## 13 — Build order

Each phase independently testable. Phase 7 is deliberately last.

| # | Phase | Done when |
|---|---|---|
| 1 | `box-agent` read-only | `curl` from the jetson returns full vitals JSON |
| 2 | collector + SQLite + tiering | runs 24 h; write volume measured and matches §07 |
| 3 | server + `/api/now` + SSE | a browser holds a stable stream |
| 4 | **NOW** screen + timeline | reads correctly on mains and during a simulated drain |
| 5 | **HISTORY** + episode traces | a real episode renders end to end |
| 6 | **HEALTH** | pack/mains/infra panels populated |
| 7 | **config plumbing** + CONFIG tab | DONE - both units verified to still start with a deliberately corrupted config file |
| 8 | power controls + interlock | wake DONE; hibernate pending the polkit decision above |
| 9 | tailscale + systemd hardening | TODO - reachable by hostname over HTTPS on the phone |

Phases 1-6 touch nothing that exists. Phase 7 is the only one that modifies proven code, and
by then everything else is working, so a regression there is unambiguous.

---

## 14 — Appendix: what the hardware actually exposes

Surveyed 2026-09-20. Recorded so it is not re-derived.

### UPS, via NUT (`upsc apc`)

```
  ups.status            OL | OB | LB | CHRG | DISCHRG | OFF | RB
  battery.charge        integer %            battery.runtime      seconds
  battery.voltage       27.0  (analog)       battery.voltage.nominal  24.0
  ups.load              integer %            ups.realpower.nominal    865
  input.voltage         244.0                input.transfer.low/high  170 / 294
  input.sensitivity     medium               battery.charge.low/warning  10 / 50
  battery.mfr.date      2025/11/13           ups.test.result
  device.model          Back-UPS RS 1500G-IN
```

NOT AVAILABLE, do not design around these:
- **`ups.realpower`** — no live wattage. Watts must be derived as `ups.load * 8.65`,
  quantised to 8.65 W steps.
- **`input.frequency`** — no mains frequency, so no frequency-based supply-quality signal.
- **output voltage** — not reported.

Note `battery.date` reads `2001/09/25` (firmware default, meaningless);
`battery.mfr.date` `2025/11/13` is the real one.

### Box (`workstation`)

```
  amdgpu-pci-0300  (W7900)   PPT watts, cap 241 W; edge / junction / mem temps
  /sys/class/drm/card*/device/gpu_busy_percent, mem_info_vram_used
  k10temp Tctl               CPU package temp
  nvme-pci-0400 Composite    NVMe temp
  /proc/sys/kernel/random/boot_id   resume-vs-cold-boot discriminator
  free: 91 GiB RAM, 99 GiB swap
```

`amdgpu-pci-1200` is the iGPU (PPT ~8 mW) — ignore it, it is not the W7900.
No `rocm-smi`, no `amdgpu_top`; `sensors` is present.

### Jetson (`jetson-nano`)

```
  thermal zones   AO-therm, CPU-therm, GPU-therm, PLL-therm, PMIC-Die, thermal-fan-est
  tegrastats and jtop both present
  RAM 3962 MB total        SD card 29 GB, 81% full, 5.5 GB free
  Python 3.8.10            NO node, NO npm
```

No INA3221 power rails on a Nano dev kit, so **the jetson cannot measure its own power
draw**. Any "nano watts" figure would be fabricated — do not show one.


---

## 15 — As built

### File size rule

Operator standing requirement: **no source file may exceed 300 lines.** Longer
files get decomposed, not trimmed. Largest in the system is `store.py` at 254.
`box-agent` was split from one 399-line file into seven modules; the collector
from 464 lines into six.

### Deployed locations

Both sides iterate by `rsync` + `systemctl restart` - no install step.

| What | Where |
|---|---|
| collector + server + web bundle | `jetson:~/projects/ups-dash/`, unit `ups-dash.service` |
| box agent | `box:~/projects/box-agent/`, unit `box-agent.service` |
| tunables (governor) | `box:/etc/ups-dash/tunables.json` |
| tunables (sentinel) | `jetson:/etc/ups-dash/tunables.json` |
| telemetry database | `jetson:/var/lib/ups-dash/telemetry.db` |
| patcher + units | `dashboard/deploy/` |

### Corrections learned during the build

- ⚠ **The wake/reserve constraint must be validated against the config FILES,
  not the log-learned values.** The learner lags by up to one poll, and a
  safety constraint checked against a lagging cache is not a safety constraint.
  A dangerous pair got through exactly once this way before the source of truth
  was corrected.
- **The learner must follow `tunables reloaded:` lines too**, not only
  `started:`. A unit can change configuration without restarting, so the
  startup line alone goes stale.
- **`ProtectSystem=full` makes `/etc` read-only**, silently blocking `box-agent`
  from writing its own tunables file. Fixed with `ReadWritePaths=/etc/ups-dash`
  - one hole, no wider. ⚠ It only takes effect on a restart that *follows*
  `daemon-reload`; a restart racing the reload leaves the process without the
  bind mount, and the failure is indistinguishable from a permissions problem.
- **The box runs firewalld**, which silently blocked port 9009 with "No route to
  host". Opened with a rich rule scoped to the jetson's address alone, not the
  LAN, because this agent accepts config writes.
- **GPU power cap is hardware-locked**: `power1_cap_min == power1_cap_max ==
  241 W`. The build sheet's 230 W cap and the on-battery 150 W cap are not
  merely unimplemented - they are impossible on this card. Open item closed.

### Undo

```
# dashboard only - leaves the power system exactly as it was
sudo systemctl disable --now ups-dash    # jetson
sudo systemctl disable --now box-agent   # box
sudo rm /etc/systemd/system/{ups-dash,box-agent}.service
rm -rf ~/projects/ups-dash ~/projects/box-agent
sudo rm -rf /var/lib/ups-dash /etc/ups-dash

# revert the tunables patch (restores the pre-patch scripts verbatim)
sudo cp /usr/local/sbin/hibernate-governor.pre-tunables-* /usr/local/sbin/hibernate-governor
sudo cp /usr/local/sbin/ups-sentinel.pre-tunables-*       /usr/local/sbin/ups-sentinel

# firewall
sudo firewall-cmd --permanent --zone=public --remove-rich-rule='rule family="ipv4" source address="10.0.0.10" port port="9009" protocol="tcp" accept'
sudo firewall-cmd --reload
```
