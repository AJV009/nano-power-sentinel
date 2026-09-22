# UPS tooling: what NUT can and cannot do with this APC

Research done on 2026-09-21. Nothing was written to the UPS: every live reading
below came through upsd or sysfs. Raw reports, with a citation for every claim,
are in `private/research/` (gitignored).

**Evidence tags:**

| Tag | Meaning |
|---|---|
| **[measured]** | read on this unit |
| **[code]** | proven from the NUT 2.7.4 source that runs on the jetson (Ubuntu does not patch this driver) |
| **[field]** | a report from another user, usually on a sibling model |
| **[inference]** | a conclusion drawn from the evidence above it |

---

## 1. Why the "live" numbers were not live

`pollinterval` was the wrong knob. `usbhid-ups` works on two clocks **[code]**:

| Every `pollinterval` (2 s) | Every `pollfreq` (30 s) |
|---|---|
| interrupt pipe, plus the quick-poll items: status bits, `OFF`, the three countdown timers | **everything else**: `ups.load`, `input.voltage`, `battery.voltage`, `battery.runtime` |

Measured across 7,077 of the dashboard's own 1 Hz samples **[measured]**:

| Field | Typical gap between changes |
|---|---|
| `ups.load` → watts | 31–33 s, or a multiple of it |
| `input.voltage` | 31–32 s |
| `battery.runtime` | 31–33 s |
| `battery.voltage` | 31–32 s |
| `battery.charge` | 2–5 s (arrives on the interrupt pipe) |
| box GPU watts (control) | 5 s |

So the watts line has been a 30-second staircase drawn at 1 Hz.
`pollinterval = 1` could never have fixed that. The setting that would is
`pollfreq = 10` (or 5) in the `[apc]` section. Nobody has reported lowering it
on a BR unit **[field]**. Test it on mains, with the stall watchdog (§2) in
place first.

## 2. The 2026-09-20 "driver died" incident, re-read

The kernel log settles it **[measured]**:

```
16:06:03  upsd: Data for UPS [apc] is stale        <- no USB disconnect anywhere near this
16:15:14  driver restarted by hand -> fine
16:18:40  kernel: usb 1-2.4: USB disconnect        <- the killpower test: the UPS switched off
16:22:06  kernel: re-enumerated (mains back)
16:22:07  upsd: data is no longer stale            <- 2.7.4 reconnected by itself
```

- **It was a stall, not a disconnect.** The UPS stayed on the bus and simply
  stopped answering.
- **2.7.4 never recovers from timeouts** **[code]**. Only a real disconnect
  triggers its reconnect path; a stall means endless 5 s timeouts behind
  "Data stale". NUT 2.8.5 added a reconnect when a full poll gets zero
  answers.
- **`pollinterval = 1` probably contributed.** The same symptom is documented
  on a BR1350MS, fixed by slowing polling back down **[field]**. The real
  failure was the missing recovery.
- **`/dev/hidraw1` vanishing was not a symptom.** `usbhid-ups` detaches the
  kernel HID driver when it claims the UPS **[code]**. A hidraw node for the
  APC only exists when NUT is *not* holding it — as at 16:22:06, before the
  driver reclaimed it.

### ⚠ Dashboard bug this exposes

The readiness panel's **UPS USB link** row lists every `/dev/hidraw*`. The only
node on the jetson is `hidraw0`, which is the **touchscreen** (hid-multitouch).
The row therefore always reads healthy, even with the UPS unplugged.

The honest check is sysfs. Find the device with `idVendor` 051d and read its
interface driver:

| Interface driver | Meaning |
|---|---|
| `usbfs` | NUT holds it — healthy |
| `hid-generic` / `usbhid` | on the bus but NUT is not attached |
| absent | unplugged, or the UPS itself is off |

### Stall watchdog (proposed)

If upsd reports stale for more than about 15 s while sysfs still shows the
device, restart `nut-driver`.

- That recovers the 09-20 stall in seconds instead of 9 minutes.
- It lands well inside the governor's 45 s comms-loss failsafe, which is what
  actually hibernated the box that day.
- The existing `Restart=always` drop-in cannot help, because a stalled driver
  never exits.

## 3. Getting the output back on

This family has **three** shutdown registers **[field, code]**:

| Register | Reached by | What the UPS does |
|---|---|---|
| **0x15** `PowerSummary.DelayBeforeShutdown` | `load.off`, `load.off.delay` — **what both emergency modes use today** | Cuts and **stays off until the front button** is pressed. That is our `OL OFF` latch, matched exactly by a Back-UPS XS 700U bench test. |
| **0x40** `APCGeneralCollection.APCDelayBeforeReboot` | `shutdown.reboot` | Cuts after a grace period (~60 s), then **restores**. On mains that is about 5 s later (BR550GI, 2013). On battery it waits for mains, then restores (APC support statement, XS 700U bench). Behaves as `shutdown.return` would. |
| **0x41** `APCGeneralCollection.APCDelayBeforeShutdown` | not reachable from NUT (a PowerSummary row shadows it) | apcupsd's "hibernate" killpower. APC staff tested it on a BR1500G: cut after 60 s, back on at mains return. |

What this means for us:

- **`load.off.delay` was the wrong register for a recoverable cut.** Our
  killpower test proved the hardware can't come back — but only for 0x15.
- **2.7.4's own killpower already agrees.** `upsdrvctl shutdown` on this unit
  tries `shutdown.return`, fails, and lands on `shutdown.reboot` = 10 **[code]**.
  It never reaches `load.off.delay`.
- **No dedicated power-on exists.** No `DelayBeforeStartup` register exists,
  so there is no `load.on`.
- **A 0x40 write from `OL OFF` does NOT start the output** **[measured]**. The
  HID spec suggested it might. On this unit the UPS ACKs the command, never
  arms (`ups.timer.reboot` stays 0), and the output stays off. After a 0x15
  cut, the front button is the only way back (bench test 2, §7).
- **Which value to write.** Use `1`, not NUT's default `10`. BX and XS units
  acknowledge 10 and never execute it. The BR550GI did execute 10. `1` works
  everywhere it has been tried **[field]**.
- **Nothing cancels an armed 0x40.** `shutdown.stop` only clears 0x15.

### ⚠ The BR re-execution loop

A known BR firmware bug **[field]**, seen on the RS 900G, BR900GI, BR550GI,
RS 550G and RS1000G:

1. Mains returns during or shortly after a kill.
2. The powered host boots and enumerates USB.
3. The UPS jumps back to battery and cuts again, in a loop.

apcupsd works around it by reading a status register right after the kill.
NUT has no fix, even in master.

Our topology may be immune **[inference]**. The jetson is not powered by this
UPS, so its driver keeps polling straight through a kill, and the box never
touches this USB cable. Test it before relying on it.

## 4. Data the UPS gives us that the dashboard ignores

| Variable | What it adds |
|---|---|
| `input.transfer.reason` | **Why** it went to battery — right now "input voltage out of range". A per-episode cause (blackout vs brownout vs self-test) for free. |
| `ups.timer.shutdown` / `ups.timer.reboot` | Quick-polled live countdowns. Proof the UPS **accepted** a cut, and a real "output off in 14 s" for the safe mode. |
| `input.sensitivity`, `input.transfer.low/high` (RW) | Tunable. Mains runs ~243 V here; if brownout transfers get noisy these are the lever. |
| `battery.charge.low` / `battery.runtime.low` (RW) | The **UPS's own** low-battery point (10 % / 120 s). It raises `LB`, which the box's upsmon acts on independently of the governor. Worth showing beside the governor's reserve. |
| `test.battery.start.quick` + `ups.test.result` | A monthly battery-health check, on mains at full charge. It briefly shows `OL OFF`, so anything reacting to `OFF` needs a persistence window. |
| `beeper.enable/disable/mute` | Currently disabled. Could become a "mute" control. |

**Not worth pursuing:**

- `battery.date` (2001/09/25) is a factory placeholder on every APC ever
  dumped. Battery age comes from `battery.mfr.date`, which the dashboard
  already uses.
- `test.battery.start.deep` drains the battery to recalibrate runtime. Not on
  a live system.
- AVR boost/trim status is not exposed by this family at all.

## 5. Upgrading the driver

A newer `usbhid-ups` can run against the jetson's 2.7.4 `upsd`: the socket
protocol is backward compatible **[code]**. Build it into a private prefix and
swap only the driver.

**What it buys:**

- Stall recovery (2.8.5).
- `upsrw -s driver.debug=N` at runtime, with no restart.
- A startup list of unmapped HID items.
- Writable `battery.mfr.date`.
- `shutdown.return` mapped to 0x40 = 1, in master only (PR #3566, merged
  2026-08-17). That is exactly the register §3 wants.

**Caveats:**

- Use master, or 2.8.5 plus the #3550 fix. Stock 2.8.5 segfaults on
  reconnecting to a wedged device (reproduced on a Jetson Orin).
- Keep 2.8-only keys out of the shared `ups.conf`, because 2.7.4 refuses to
  start on unknown keys.
- Keep `master`/`slave` wording on the jetson.

apcupsd reaches 0x41 and has the loop fix, but it has been dead upstream since
2016. Debian's maintainer points users to NUT.

## 6. Seeing everything the UPS exposes

The HID report descriptor is 1,134 bytes. It was captured on 2026-09-22
(`private/research/apc-dump-274.txt`) and confirms the BR-family layout
**[measured]**:

| Report | Path | Idle value |
|---|---|---|
| 0x15 | `PowerSummary.DelayBeforeShutdown` (16-bit) | -1 |
| 0x40 | `APCGeneralCollection.APCDelayBeforeReboot` (8-bit) | 0 |
| 0x41 | `APCGeneralCollection.APCDelayBeforeShutdown` (16-bit) | -1 |
| 0x42 | `APCGeneralCollection.DelayBeforeShutdown` (16-bit) | -1 |

- **No `DelayBeforeStartup` anywhere.**
- **Present status bits** also include `NeedReplacement` (NUT: `RB`),
  `Overload` (`OVER`), `VoltageNotRegulated` and `CommunicationLost`.
- **Input reports** (interrupt, pushed rather than polled): report 0x0c
  (charge + runtime) and the status bits. `PercentLoad` and `Input.Voltage`
  are Feature-only, which is why they follow `pollfreq`.
- **Unknown vendor usages** (`ff860024` under Input/Battery/PowerConverter,
  `ff860029`, `ff86002a`, `ff860090.*`) are unknown to NUT master as well.

It cannot be read while the driver holds the device. To repeat the capture
(about 30 s), **on mains only**:

```bash
sudo systemctl stop nut-driver
sudo /lib/nut/usbhid-ups -a apc -u nut -DDD 2>&1 | tee ~/apc-dump.txt   # a working driver: upsd reconnects
# Ctrl-C after the init walk, then:
sudo systemctl start nut-driver
```

⚠ **Never do this on battery.** A stale UPS that was last seen on battery is
treated as critical by upsmon (the box's, in our setup).

## 7. Bench tests — run 2026-09-22, nothing plugged into the UPS

Script: `scripts/ups-bench-test.sh 1|2|3`. It refuses to run unless the UPS is
in the right state, and logs each run on the jetson.

| # | State | Command | Result **[measured]** |
|---|---|---|---|
| 1 | on mains | `shutdown.reboot 1` | ✅ Armed at once (`timer.reboot` 1). Cut ~62 s later, off ~4 s, **back on by itself**. Stable for 3 min after — no loop. |
| 2 | `OL OFF` after `load.off` | `shutdown.reboot 1` | ❌ ACKed but **never armed**. Output stayed off 4 min. **No remote power-on.** |
| 3 | on battery (wall plug pulled) | `shutdown.reboot 1`, mains back after the cut | ✅ Armed at once. Cut ~60 s later; the UPS stayed alive on USB (`OB OFF`). Output back **~1 s after mains returned**. No re-execution loop over the following ~6 min. |

Test 1 trace:

```
00:43:52  shutdown.reboot 1   -> OK
00:43:55  OB DISCHRG   timer.reboot 1   <- it moves itself to battery for the grace period
00:43:56  OL DISCHRG   timer.reboot 1   <- and reports this odd mix until the cut
00:44:54  OL OFF       timer.reboot 0   <- cut, ~62 s after the command
00:44:58  OL CHRG                       <- restored, ~4 s later
```

What that settles:

- **Two kinds of cut, chosen by intent.**
  - `load.off` / `load.off.delay` (0x15) = cut and **stay off** until the
    button. The emergency modes use this, correctly.
  - `shutdown.reboot 1` (0x40) = cut, then **come back on**: after ~4 s on
    mains, or ~1 s after mains returns on battery.
- **There is no remote "on".** A dashboard "restore output" button is not
  possible on this unit. `upsops.RESTORE_SUPPORTED` stays False.
- **A 0x40 arm cannot be cancelled.** During the ~60 s grace the UPS reports
  `OB`/`OL DISCHRG`, so anything watching for an outage will see one.
- **The jetson being on a different UPS matters.** The re-execution loop other
  BR owners report is triggered by a powered host re-enumerating USB. Here the
  jetson's driver polls straight through the cut, and no loop appeared.
