# Jetson UPS sentinel → W7900 hibernate / wake

Design doc. **Supersedes §03 — POWER of the external build sheet (external build sheet, not part of this repo)**,
which assumed `apcupsd` running on the workstation itself with the UPS plugged into it.
The UPS USB now lives on a Jetson Nano, which changes the architecture.

Status: **designed, not built.** Nothing on either machine has been modified yet.
Recon verified 2026-09-20.

---

## 00 — Why this replaced the old design

The old design's own warning:

> ⚠ Test this one deliberately — it is NOT verified. `systemctl hibernate` blocks until
> the machine powers off, so `apcupsd` is frozen inside the hibernate image rather than
> left running to issue killpower.

That risk exists **because the sequencer is the thing being powered off**. Moving the UPS
USB to an always-on Jetson on a separate UPS removes it structurally: the Jetson watches
the box go dark, *then* issues killpower. It was never a workaround — it's the fix.

The Jetson also closes the old design's recovery hole. Old fallbacks if killpower didn't
fire were "a BIOS RTC wake alarm, or just press the button." Now it's a magic packet.

---

## 01 — The machines

### Power topology

```
APC Back-UPS Pro BR1500G-IN  ──► W7900 workstation only
                             ──► USB data ──► Jetson Nano

Separate UPS                 ──► Jetson Nano
                             ──► TP-Link Archer AX10 (router)
```

⚠ **This corrects §01 of the build sheet**, which budgeted "Router + ONT → plug into the
UPS battery outlets" on the BR1500G-IN. The router now sits on the Jetson's UPS instead —
which is the right call: it keeps the sentinel, its network path and its target-of-control
on independent power from the machine being shut down.

### W7900 workstation — `workstation` / 10.0.0.20

| | |
|---|---|
| OS | EndeavourOS, kernel `6.18.49-1-lts` (also installed: `7.2.2-arch1-1`) |
| Board | Gigabyte B850M AORUS Elite WIFI6E |
| RAM | 91 GiB |
| Root | `/dev/nvme0n1p5`, ext4, 2.2 T (2.0 T free) |
| ESP | `/dev/nvme0n1p1` at `/efi` |
| Wired NIC | `<BOX-NIC>`, Realtek `r8169`, `0000:0a:00.0`, 1000/full |
| WiFi | `wlan0`, `rtw89_8852ce`, `0000:09:00.0` — down under Linux |
| Bootloader | systemd-boot 261.2, Secure Boot **disabled**, TPM2 present |
| Firmware | AMI UEFI 2.110 (5.41) |
| Swap | **none** — 0 B, `resume=` unset |
| Sleep states | `mem_sleep: s2idle [deep]` · `disk: [platform] …` |

**Dual-boot note.** Windows lives on the same ESP and connects over **WiFi**, not ethernet
— it appears on the LAN as `WINDOWS-PC` / 10.0.0.30 with the WiFi module's MAC.
Under Linux the wireless interface is down and everything runs on `<BOX-NIC>`.

### Jetson Nano — `jetson-nano` / 10.0.0.10

| | |
|---|---|
| OS | L4T R32.7.2 (Ubuntu 18.04 base), kernel 4.9.253, aarch64 |
| Resources | 3.9 GiB RAM · 29 GB SD card, **81% full** |
| Runtime | Python 3.8.10, systemd 245 |
| Network | `eth0`, wired, **1000/full** |
| Packages | `nut 2.7.4-11ubuntu4` and `apcupsd 3.14.14-3build1` available in apt |

**UPS is visible and enumerating:**

```
Bus 001 Device 004: ID 051d:0002 American Power Conversion Uninterruptible Power Supply
/dev/hidraw1 -> American Power Conversion Back-UPS RS 1500G-IN FW:901.L11.I USB FW:L11
```

⚠ The device self-reports as **Back-UPS RS 1500G-IN**, not "Pro BR1500G-IN" as on the box.
Same unit, different firmware string. Expect NUT docs and examples to say RS.

⚠ **The SD card is the weak link.** This machine's entire job is to still be alive when
everything else isn't, and it runs from an 81%-full SD card with a full GNOME desktop on
it. Worth addressing before trusting it.

### Network

TP-Link Archer AX10 v2.0, fw 1.3.10 (1.3.12 available, not applied).
Gateway 10.0.0.1 · DHCP pool `.2–.253` · lease 1440 min · DNS 1.1.1.1 / 1.0.0.1.

DHCP reservations **already applied** 2026-09-20:

| Device | MAC | IP |
|---|---|---|
| `workstation` | `aa:bb:cc:dd:ee:ff` | 10.0.0.20 |
| laptop (`laptop`, WiFi) | `aa:bb:cc:00:11:22` | 10.0.0.40 |
| `jetson-nano` | `aa:bb:cc:33:44:55` | 10.0.0.10 |

Jetson ↔ workstation measured at **0.75 ms RTT, 0.07 ms jitter** — same L2 segment, both
wired. This is what makes the magic packet viable; WoL over WiFi would not be.

---

## 01b — FINAL ARCHITECTURE (as built, 2026-09-20)

This supersedes the killpower-based sequence described further down. Read this first.

```
OUTAGE
  mains lost
    -> jetson's usbhid-ups reports OB; box's upsmon sees it over the network
    -> box: GPU cap + inference stop            (upssched ONBATT)
    -> box: hibernate-governor measures, and hibernates when
             runtime_to_reserve <= hibernate_cost + 30s,  reserving 30% charge
    -> box writes its image and powers off
    -> NO killpower. The box sits hibernated on live UPS power,
       so +5VSB keeps its NIC armed and WoL-capable.

RECOVERY
  mains returns
    -> jetson holds the box OFF until BOTH:
         battery.charge >= 50%
         mains continuously present for 120s
    -> any dip back to OB restarts the stability window
    -> gate passes -> jetson sends the WoL magic packet (up to 5 tries, 30s apart)
    -> box wakes from S5 and resumes from the swapfile
```

**Why the gate exists.** With flapping mains, waking the box immediately burns a
hibernate/resume cycle on a pack that never recharges. Eventually there is not enough
battery to *finish* writing an image, which is a hard power loss mid-hibernate — worse
than having no automation. Waiting for 50% proves mains is genuinely back *and* guarantees
the next outage is survivable.

**Why there is no killpower.** Verified 2026-09-20: this UPS latches its output off after
`load.off.delay` and reports `OL OFF` on mains return, waiting for a human to press its
front-panel button. There is no `load.on` command. Cutting the load strands the box with
no +5VSB, so WoL cannot rescue it either. The cost of not cutting is that a very long
outage drains the pack — unavoidable on this hardware.

### Tunables as built

| Where | Constant | Value |
|---|---|---|
| box: `hibernate-governor` | `RESERVE_PCT` | **30%** |
| box: `hibernate-governor` | `SAFETY_SEC` | 30 s |
| box: `hibernate-governor` | `WRITE_RATE_GBPS` / `FIXED_OVERHEAD` | 0.5 / 15 s |
| box: `hibernate-governor` | `COMMS_LOSS_LIMIT` | 45 s |
| jetson: `ups-sentinel` | `WAKE_CHARGE_PCT` | **50%** |
| jetson: `ups-sentinel` | `MAINS_STABLE_SEC` | 120 s |
| jetson: `ups-sentinel` | `WAKE_TRIES` / `WAKE_INTERVAL` | 5 / 30 s |
| jetson: `ups.conf` | `pollinterval` / `pollfreq` | **2 s** (1 s stalled the UPS) / **10 s** (load, voltage, runtime freshness) |

Hibernate at 30%, wake at 50% — the pack always recharges by at least 20 points before the
box is allowed back.

---

## 02 — Architecture

**Jetson** — always-on sentinel, own UPS, holds the USB
- `nut-server`: `usbhid-ups` on `/dev/hidraw1` + `upsd`, publishes UPS state on the LAN
- killpower sequencer: waits for the box to go dark, then cuts UPS output
- fallback waker: after mains returns, verifies the box came back; if not, sends WoL

**Workstation** — on the APC
- `nut-client`: `upsmon` + `upssched` against the Jetson's `upsd`
- 100 GB swapfile + `resume=` / `resume_offset=`
- WoL armed persistently on `<BOX-NIC>`
- BIOS: Restore on AC Power Loss = **Power On**, Wake on LAN/PCIe on, ErP **disabled**

### Why `upssched`, and why it runs on the workstation

The old design's `TIMEOUT 60` — hibernate 60 s after going on battery, so brief flickers
ride through and cancel. Plain `upsmon` can't express that; it acts on LOWBATT/FSD.
`upssched` is NUT's timer daemon built for exactly this shape.

Running it **on the workstation** means the box hibernates *itself* — no SSH push, no
remote sudo, no credential plumbing between machines. `upsmon` already runs as root there.

### Sequence

```
T+0      mains lost. Jetson's usbhid-ups reports OB.
T+2      box: GPU capped 295→150 W, inference stopped     (upssched ONBATT)
T+30     box: governor starts measuring (30 s settle)
T+n      box: systemctl hibernate, when runtime_to_reserve <= cost + safety
         (n is adaptive: ~59 min idle, ~3 min under GPU load)
T+~120   box powered off; stops answering ping
T+~180   Jetson confirms box dark, waits margin, issues killpower
         UPS cuts its own output

mains returns
         UPS re-energises → PSU sees transition → BIOS powers board on   [PRIMARY]
         resume from swapfile
         Jetson pings box; still down after ~3 min → magic packet        [FALLBACK]
```

**The fallback covers both failure paths**, which is the property that makes this worth
building:

| Failure | Why WoL still recovers it |
|---|---|
| killpower never fires | box sits hibernated but still UPS-powered → NIC is live → WoL wakes it |
| killpower fires, BIOS auto-power-on misconfigured | UPS re-energises, +5VSB returns → WoL wakes it |

---

## 03 — Workstation host prep

### 3.1 Swapfile + resume

⚠ **This machine uses `dracut` + `kernel-install`, NOT `mkinitcpio`.** Verified 2026-09-20:
`dracut` and `kernel-install-for-dracut` are installed and `/etc/mkinitcpio.conf` does not
exist. Any Arch guide telling you to edit `HOOKS=(...)` does not apply here.

Swapfile chosen over a dedicated partition: there is **no unallocated space** on the drive
(p1 ESP 1 G · p2 MSR 16 M · p3 Windows 1.5 T NTFS · p4 recovery 861 M · p5 root 2.1 T ext4),
so a partition would mean shrinking a live root. The swapfile needs no repartitioning and
is reversible. 100 GB > 91 GiB RAM, so the image always fits.

```bash
dd if=/dev/zero of=/swapfile bs=1M count=102400 status=progress
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
```

⚠ **Use `dd`, not `fallocate`** — fallocate leaves unwritten extents that `swapon` can
refuse. This writes 100 GB; it is not instant.

`/etc/fstab`:
```
/swapfile none swap defaults 0 0
```

Resume needs the root filesystem UUID and the swapfile's physical offset:

```
root UUID : <ROOT-UUID>        (already known)
offset    : filefrag -v /swapfile | awk '$1=="0:"{gsub(/\./,"",$4); print $4}'
```

⚠ **Do not hand-edit `/efi/loader/entries/*.conf`.** They are generated by
`/usr/lib/kernel/install.d/90-loaderentry-kifd.install` and get overwritten on the next
kernel update. The source of truth is `/etc/kernel/cmdline`, currently:

```
nvme_load=YES nowatchdog rw root=UUID=<ROOT-UUID>
```

Append `resume=UUID=<ROOT-UUID> resume_offset=<N>` to that file,
then regenerate initramfs **and** loader entries for every installed kernel:

```bash
reinstall-kernels
```

dracut ships the resume module at `/usr/lib/dracut/modules.d/74resume` (confirmed present);
it is pulled in once `resume=` appears on the cmdline. If it isn't, force it:

```
# /etc/dracut.conf.d/resume.conf
add_dracutmodules+=" resume "
```

ESP headroom is fine: 1022 M total, 359 M used, 664 M free.

⚠ Verify `systemctl hibernate` by hand and confirm the session returns **before** wiring
any of this to the UPS. Swapfile resume is fiddlier than swap-partition resume — prove it
manually first.


### 3.2 Arming Wake-on-LAN

Three separate switches, all currently off:

| Layer | Current state |
|---|---|
| NIC WoL flag | `Wake-on: d` (supports `pumbg`) |
| PCI device wakeup | `/sys/class/net/<BOX-NIC>/device/power/wakeup` = `disabled` |
| ACPI wake source | `/proc/acpi/wakeup` → `LN00  S4  *disabled  pci:0000:0a:00.0` |

`LN00` maps to `0000:0a:00.0`, which is exactly `<BOX-NIC>`'s bus address — confirmed, not
assumed.

⚠ **`r8169` drops the WoL flag across reboots.** A one-time `ethtool` call is not enough.
A boot-time unit is required:

```ini
[Unit]
Description=Arm Wake-on-LAN on <BOX-NIC>
After=network-pre.target

[Service]
Type=oneshot
ExecStart=/usr/bin/ethtool -s <BOX-NIC> wol g
ExecStart=/bin/sh -c 'echo enabled > /sys/class/net/<BOX-NIC>/device/power/wakeup'
ExecStart=/bin/sh -c 'grep -q "^LN00.*disabled" /proc/acpi/wakeup && echo LN00 > /proc/acpi/wakeup; true'

[Install]
WantedBy=multi-user.target
```

⚠ Writing to `/proc/acpi/wakeup` **toggles** — the `grep` guard stops it flipping the
setting back off on a re-run.

Plus a pre-sleep re-arm at `/usr/lib/systemd/system-sleep/wol-rearm`:

```bash
#!/bin/sh
case "$1" in
  pre) /usr/bin/ethtool -s <BOX-NIC> wol g ;;
esac
```

### 3.3 Boot order — load-bearing, not cosmetic

Current state:

```
BootCurrent: 0002
BootOrder:   0002,0000
Boot0000* Windows Boot Manager   \EFI\Microsoft\Boot\bootmgfw.efi
Boot0002* Linux Boot Manager     \EFI\systemd\systemd-bootx64.efi
```

Linux is first *right now*. The reported flip-flopping is Windows rewriting the UEFI
`BootOrder` variable to put itself first on every boot and after every update. Linux never
fights back, so the order depends on which OS ran last. Nothing is wrong with the firmware.

**Why this matters here:** if the board auto-powers-on into Windows after an outage, the
hibernated Linux image is never resumed and goes stale. The whole recovery path depends on
Linux being first.

A boot-time oneshot re-asserts it, healing after every Windows trip:

```bash
efibootmgr -o 0002,0000
```

⚠ Boot numbers can change if entries are recreated. The unit should resolve the entry by
**label** (`Linux Boot Manager`) rather than hardcoding `0002`.

⚠ `loader.conf` carries `reboot-for-bitlocker 1`. If Windows has BitLocker enabled, that
interaction needs checking separately.

---

## 04 — NUT wiring

Credentials are written as `<…>` placeholders throughout. They are deliberately **not**
recorded in this file.

### 4.1 Jetson — NUT server

`/etc/nut/nut.conf`
```
MODE=netserver
```

`/etc/nut/ups.conf`
```
[apc]
    driver = usbhid-ups
    port   = auto
    desc   = "APC Back-UPS Pro BR1500G-IN"
```

`/etc/nut/upsd.conf` — bind to loopback and the LAN address only:
```
LISTEN 127.0.0.1 3493
LISTEN 10.0.0.10 3493
```

`/etc/nut/upsd.users`
```
[boxmon]
    password = <secret>
    upsmon slave
```

NUT 2.7.4 still uses `master`/`slave` rather than the newer `primary`/`secondary`.

Sanity check once running: `upsc apc` should report `ups.status: OL`, battery charge and
load.

### 4.2 Workstation — NUT client

`/etc/nut/nut.conf`
```
MODE=netclient
```

`/etc/nut/upsmon.conf`
```
MONITOR apc@10.0.0.10 1 boxmon <secret> slave
SHUTDOWNCMD "/usr/bin/systemctl hibernate"
NOTIFYCMD   /usr/bin/upssched
NOTIFYFLAG ONBATT  SYSLOG+EXEC
NOTIFYFLAG ONLINE  SYSLOG+EXEC
NOTIFYFLAG LOWBATT SYSLOG+EXEC
DEADTIME   60
FINALDELAY 5
```

`/etc/nut/upssched.conf`
```
CMDSCRIPT /etc/nut/upssched-cmd
PIPEFN    /run/nut/upssched.pipe
LOCKFN    /run/nut/upssched.lock

AT ONBATT * EXECUTE on-battery
AT ONLINE * EXECUTE on-line
```

`/etc/nut/upssched-cmd`
```bash
#!/bin/bash
case $1 in
  on-battery)
      rocm-smi --setpoweroverdrive 150 >/dev/null 2>&1
      systemctl stop inference.service 2>/dev/null
      logger -t upssched "mains lost - GPU capped 150W, inference stopped; hibernate in 60s"
      ;;
  on-line)
      rocm-smi --setpoweroverdrive 230 >/dev/null 2>&1
      systemctl start inference.service 2>/dev/null
      logger -t upssched "mains restored - GPU back to 230W"
      ;;
  hibernate)
      logger -t upssched "60s on battery - hibernating"
      systemctl hibernate
      ;;
esac
```

⚠ `inference.service` **does not exist yet** — verified absent on the box. The
`systemctl stop/start` lines are harmless no-ops until it does, but don't mistake them for
working integration.

The 150 W / 230 W figures come from the build sheet's standing GPU cap decision.

> ## ↻ REVISED 2026-09-22 — the UPS output IS cut again, at a battery floor
> Bench tests found a second register. `shutdown.reboot 1` (0x40) cuts the
> output, then **restores it when mains returns**; `load.off.delay` (0x15)
> latches OFF. ups-dash now *parks* the UPS once the box is down and the pack
> reaches the floor (default 35 %), so the pack stops draining. With BIOS AC
> BACK = Always On the box powers on with the mains, is put back to sleep by
> ups-dash, and is then woken by the sentinel's normal gate. See
> [UPS-TOOLING.md](UPS-TOOLING.md) §7 and BUILD-LOG.md 2026-09-22.
>
#> ## ⛔ SUPERSEDED 2026-09-20 — killpower is ABANDONED
> Testing proved the UPS **does not re-energise its outlets** after `load.off.delay`.
> It reports `OL OFF` on mains return and waits for a human to press its front-panel
> button. With no `load.on` command, killpower is a one-way door.
>
> **The design is now: never cut UPS output.** The box hibernates and sits on UPS power;
> the jetson wakes it with WoL once the battery is back above 50%. WoL is the sole wake
> path, and `AC BACK` should be `Always Off`. See BUILD-LOG.md for the full result.
>
> Everything below is retained for the record but is **not** what is built.

## 4.3 Jetson — killpower sequencer and fallback waker

A single daemon on the Jetson, watching `upsc apc ups.status`:

```
on OB (on battery):
    wait for 10.0.0.20 to stop answering ping   (cap the wait)
    wait KILL_MARGIN                                 (~60 s)
    upscmd apc load.off.delay 20     <- killpower

on OL (mains restored, after an OB):
    wait ~180 s for BIOS auto-power-on to do its job
    ping 10.0.0.20
    still down  ->  send magic packet, retry ~5x at 30 s
```

Ping-loss is a good hibernate-complete signal: the network interface goes down during
device suspend, which happens *after* the image is written. The margin is insurance, not
the mechanism.

⚠ **Send the magic packet to the broadcast address `10.0.0.255:9`, not to
`10.0.0.20`.** Once the box is off its ARP entry expires and a unicast packet has
nowhere to go.

The Jetson has no `wakeonlan`/`etherwake` installed. Either `apt install wakeonlan` or a
dependency-free sender, since Python 3.8 is present:

```python
import socket
mac = bytes.fromhex("aabbccddeeff")
pkt = b"\xff" * 6 + mac * 16
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
s.sendto(pkt, ("10.0.0.255", 9))
```

### Killpower — resolved 2026-09-20

`upscmd -l apc` against the live driver. Supported here:

```
load.off         - Turn off the load immediately
load.off.delay   - Turn off the load with a delay (seconds)
shutdown.reboot  - Shut down the load briefly while rebooting the UPS
shutdown.stop    - Stop a shutdown in progress
```

⚠ **`shutdown.return` was not offered by the 2.7.4 driver.** The NUT master driver
(2026-09-22) offers it, mapped to the same 0x40 register as `shutdown.reboot 1`. That one
cuts and then *restores*; `load.off.delay` below cuts and *stays off* — bench-tested,
[UPS-TOOLING.md](UPS-TOOLING.md) §7. The killpower used here was **`load.off.delay`**:

```bash
upscmd -u <admin> -p <secret> apc load.off.delay 20
```

`ups.delay.shutdown` already reads `20`, and `shutdown.stop` exists to abort a pending
shutdown — useful if mains returns mid-sequence.

⚠ Running an instant command requires a user with `instcmds` rights in
`/etc/nut/upsd.users`. The existing `monmaster` / `boxmon` entries are `upsmon` roles only
and **cannot** issue `load.off.delay`. A dedicated admin user is needed:

```
[killer]
    password = <secret>
    instcmds = load.off.delay
    instcmds = shutdown.stop
```

✅ **Confirmed working 2026-09-20.** `load.off.delay 20` was issued for real; the UPS
reported `ups.status: OB OFF` within 30 s and then powered itself down entirely. Battery
preserved at ~75%.

⚠ **The UPS drops off USB after killpower** — it powers down completely, so the jetson
goes blind. The sentinel must never read "cannot reach UPS" as "mains returned"; see
BUILD-LOG.md FINDING 3 for the bug this caused.

⚠ **`pollinterval` must stay at 2 s.** At 1 s the UPS stalled mid-outage: driver alive,
`Data stale`, but **no USB disconnect** — the "vanished" `/dev/hidraw1` was normal, since
NUT detaches the kernel HID driver when it claims the device. Re-diagnosed 2026-09-22
([UPS-TOOLING.md](UPS-TOOLING.md) §2). The freshness knob is `pollfreq` (now 10 s), not
`pollinterval`.

⚠ **`ups.delay.start` is empty** — this unit exposes no "restore after N seconds"
variable. Whether the UPS re-energises its outlets by itself when mains returns is
*firmware behaviour we do not command*. It cannot be confirmed from the variable set.
**This is now the riskiest untested assumption in the design** — see §06. If it turns out
the UPS stays off until manually powered on, killpower must be abandoned and WoL promoted
to primary (the box would then simply stay hibernated on UPS power through the outage).

---

## 4.4 Adaptive hibernate governor (built 2026-09-20)

Replaces the fixed 60 s `upssched` timer. Runs **on the box**, not the jetson, because the
decision needs two inputs and the box already has both: UPS state (via NUT over the
network) and its own RAM in use. Putting it on the jetson would have required a new channel
to poll the box's memory plus sudo plumbing to push the trigger.

`/usr/local/sbin/hibernate-governor` + `hibernate-governor.service`.

### The rule

```
runtime_to_reserve = battery.runtime * (charge - RESERVE) / charge
hibernate_cost     = ram_in_use_GiB / WRITE_RATE + FIXED_OVERHEAD

hibernate when   runtime_to_reserve <= hibernate_cost + SAFETY
hard floor       charge <= RESERVE
```

| Constant | Value | Basis |
|---|---|---|
| `RESERVE_PCT` | 10 % | charge remaining **after** hibernation completes |
| `SAFETY_SEC` | 30 s | |
| `WRITE_RATE_GBPS` | 0.5 | measured **0.72 GB/s** on 2026-09-20 (4.3 GiB in ~6 s); derated |
| `FIXED_OVERHEAD` | 15 s | freeze / snapshot / power-off |
| `SETTLE_SEC` | 30 s | ignores the post-transfer gauge transient |
| `DEBOUNCE` | 3 samples | |
| `POLL` | 1 s | requires `pollinterval = 1` on the jetson |
| `COMMS_LOSS_LIMIT` | 45 s | on battery + blind this long -> hibernate anyway |

### Why a loaded box hibernates *earlier*

The cost term scales with RAM in use, so the reserve it demands grows with the image:

| State | RAM | cost | behaviour on battery |
|---|---|---|---|
| idle | 2.5 GiB | 20 s | runs **~59 min**, hibernates near 10 % |
| half loaded | 40 GiB | 95 s | hibernates well before the floor |
| heavy | 80 GiB | 175 s | hibernates **early** - needs 3 min to write |

Verified live 2026-09-20 with the box idle: `charge=97% runtime=4036s ram=2.48GiB`
-> `cost=20s budget=50s time_to_reserve=3620s` -> **keep running, ~59 min of headroom**.

### Safeguards

- **30 s settle** after `OB`. The 97->83 % drop seen in testing implies 6.8 kW from an
  865 W unit - physically impossible, so it is the gauge re-baselining under sag, not
  discharge. Acting on it would panic-hibernate every time.
- **Debounce** of 3 consecutive triggering samples.
- **Stands down on `OL`** - flickers ride through.
- **Comms-loss failsafe**: on battery and unable to read upsd for 45 s -> hibernate. Losing
  visibility mid-outage is exactly when to be conservative.
- **`upsmon` LOWBATT -> `hibernate-now` is retained as a backstop** if the governor dies.

### Transport

One persistent TCP connection to `upsd:3493` issuing `LIST VAR apc`. Polling `upsc` as a
subprocess each second would spawn ~86,000 processes a day.

---

## 05 — Failure modes

| Failure | Behaviour | Mitigation |
|---|---|---|
| Brief flicker | 60 s timer starts, then cancels on ONLINE | by design |
| Jetson dies while on battery | box loses `upsd`; `DEADTIME` fires | tune `DEADTIME` so a Jetson *reboot* on mains doesn't hibernate the box |
| Network dies | same as above | same |
| killpower doesn't fire | box hibernated, still UPS-fed, no transition on restore | Jetson WoL fallback |
| BIOS auto-power-on wrong | box stays off after UPS re-energises | Jetson WoL fallback |
| Box fails to hibernate | stays up, drains battery | Jetson sees it still pinging past deadline → escalate |
| Board boots into Windows | Linux image stale, never resumed | boot-order oneshot (§3.3) |
| SD card failure on Jetson | sentinel is gone entirely | unaddressed — see §01 |

---

## 06 — Verification

In order. Mirrors the build sheet's approach; the first gates everything else.

- [ ] **Waveform test, day one.** GPU under real load, pull the wall plug. A clean transfer
      is silent. Buzzing, clicking, heat, or a drop means the stepped-sine output isn't
      compatible with the RM850e's PFC stage — return within the window. *Deterministic: if
      it passes once it passes every time.*
- [x] `upsc apc` on the Jetson reports status, load % and battery charge. **Done
      2026-09-20** — `OL`, 5 % load, 100 % charge, 66 min runtime.
- [x] `upscmd -l apc` — killpower command set. **Done 2026-09-20 — `load.off.delay`.**
- [ ] `systemctl hibernate` by hand; power on; confirm the session returns. Before any UPS
      wiring.
- [ ] WoL in isolation: hibernate the box, send a magic packet from the Jetson, confirm it
      wakes. Repeat **after a reboot** to prove the arming unit survives (`r8169`).
- [ ] Boot into Windows, back to Linux, confirm `efibootmgr` still shows Linux first.
- [ ] Full sequence: pull the plug, watch `journalctl -f`. GPU capped ~2 s, hibernate at
      60 s, powered off by ~120 s.
- [ ] Killpower, part 1: after the box is off, `upscmd apc load.off.delay 20` and confirm
      the UPS actually cuts its own output.
- [ ] **Killpower, part 2 — the risky one.** Restore mains and confirm the UPS re-energises
      its outlets *by itself*. `ups.delay.start` is empty on this unit, so this is
      undocumented firmware behaviour. If it does not come back on its own, killpower is
      abandoned: the box instead stays hibernated on UPS power and WoL becomes primary.
- [ ] Fallback path: defeat killpower deliberately, confirm the Jetson's WoL recovers it.

---

## 07 — Open items

**Open, and only answerable by testing:**
- Does the UPS re-energise its outlets by itself after `load.off.delay` once mains
  returns? `ups.delay.start` is empty, so this is firmware behaviour, not a command.
  **If no, killpower is abandoned and WoL becomes primary.** See §4.3.

**Resolved 2026-09-20 (box powered back up):**
- No stray swap partition, no swap in `/etc/fstab`, no `resume=` on the cmdline — the
  hibernate side was never built, as expected.
- Neither `nut` nor `apcupsd` installed on the workstation. Clean slate.
- Kernel lockdown: not active (only the LSM init line; Secure Boot is off). Hibernate
  will not be blocked.
- WoL still `Wake-on: d`, ACPI `LN00` disabled, PCI wakeup disabled after a fresh boot —
  consistent with nothing arming it yet.
- **Initramfs is dracut, not mkinitcpio** — §3.1 corrected accordingly.

**Manual, at the machine:**
- BIOS: Restore on AC Power Loss = **Power On**
- BIOS: Wake on LAN / Power On by PCIe = **Enabled**
- BIOS: ErP / EuP Ready = **Disabled** (ErP cuts +5VSB in S5 and kills the NIC)

**Deferred:**
- Jetson SD-card resilience
- `inference.service` does not exist
- Router firmware 1.3.10 → 1.3.12
- On-demand park/wake (trivial once the above works)
