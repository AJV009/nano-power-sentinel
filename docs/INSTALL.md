# Installing

Two machines. Do the workstation first — the sentinel is useless until the
machine it protects can actually hibernate and wake.

Throughout: **sentinel** = the small always-on box holding the UPS USB cable,
**workstation** = the machine being protected.

---

## 0. Before anything

```bash
cp .env.example .env
$EDITOR .env
```

Find the workstation's wired MAC — this is what Wake-on-LAN targets, so it
must be the interface that keeps power in standby:

```bash
ip -br link            # on the workstation
```

⚠ **WoL is layer 2.** The sentinel must be on the same subnet as the
workstation; a magic packet does not route. This has nothing to do with PXE or
network boot, which are a different subsystem entirely.

---

## 1. Workstation: make hibernate work

### Swap large enough for RAM

Hibernate writes all of RAM. A swapfile must be at least as large as the RAM
you ever expect to be in use.

```bash
sudo fallocate -l 100G /swapfile     # size to your RAM
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap defaults 0 0' | sudo tee -a /etc/fstab
```

### Tell the kernel where to resume from

Two values are needed: the UUID of the filesystem holding the swapfile, and
the **physical offset of the file within it**.

```bash
findmnt -no UUID -T /swapfile                        # the UUID
sudo filefrag -v /swapfile | awk 'NR==4 {print $4}'  # the offset
```

Add to the kernel command line:

```
resume=UUID=<ROOT-UUID> resume_offset=<RESUME-OFFSET>
```

⚠ **Apply this the way your distro expects.** On a systemd-boot system using
`kernel-install`/dracut, edit `/etc/kernel/cmdline` and run
`sudo reinstall-kernels`. **Do not hand-edit `/efi/loader/entries/*.conf`** —
those are generated, and your edit will vanish on the next kernel update,
silently breaking resume months later.

Make sure the initramfs includes the resume module:

```bash
echo 'add_dracutmodules+=" resume "' | sudo tee /etc/dracut.conf.d/resume.conf
sudo reinstall-kernels
```

### Verify before continuing

```bash
sudo systemctl hibernate
# power it back on by hand, then:
cat /proc/sys/kernel/random/boot_id
```

`boot_id` is **preserved across hibernate** and changes on a real reboot. If
it changed, you cold-booted — resume is not working, and nothing downstream
will save you. Fix this before going further.

### Arm Wake-on-LAN at every boot

Many drivers (notably `r8169`) drop the WoL flag on every reboot, so a one-off
`ethtool` is not enough.

```bash
sudo cp src/box/systemd/wol-arm.service /etc/systemd/system/
sudo $EDITOR /etc/systemd/system/wol-arm.service    # set your NIC name
sudo systemctl enable --now wol-arm
sudo ethtool <nic> | grep Wake-on                   # must show: g
```

⚠ `/proc/acpi/wakeup` **toggles** when written. Writing the same value twice
turns the wake source back off. The shipped unit greps before writing.

⚠ Also set **ErP / power-on-by-PCIe** appropriately in firmware. Many boards
have no explicit "Wake on LAN" setting — WoL is governed by ErP being
*disabled* plus the OS arming the NIC.

⚠ Set **Restore on AC power loss ("AC BACK") = Always On** if the battery-floor
park is enabled (it is by default). A park cuts the box's standby power, and
the NIC forgets its WoL arming, so the box can only come back by powering
itself on when the output returns. ups-dash then puts it back to sleep, and
the sentinel's gate wakes it properly. With "Always Off", every long outage
ends with a box that needs its power button pressed.

---

## 2. Sentinel: NUT with the UPS on USB

```bash
sudo apt install nut                 # or your distro's package
```

`/etc/nut/ups.conf`:

```ini
[apc]
    pollfreq = 10
    driver = usbhid-ups
    port = auto
    pollinterval = 2
```

- **`pollfreq`** is the setting that decides how fresh load, voltage and
  runtime are. `pollinterval` only refreshes the status bits and timers. The
  default `pollfreq` of 30 s makes every "live" watts reading a 30-second
  staircase ([UPS-TOOLING.md](UPS-TOOLING.md) §1).
- ⚠ **Leave `pollinterval` at 2.** At 1 s, this unit stalled mid-outage: no
  USB disconnect, the driver alive, and `Data stale` until it was restarted.
  The distro 2.7.4 driver never recovers from a stall on its own; see the
  driver upgrade and the stall watchdog below.

Set `upsd.conf` to listen on loopback plus the sentinel's LAN address, create
users in `upsd.users`, then:

```bash
sudo systemctl enable --now nut-server nut-driver nut.target
upsc apc                             # should print the full variable list
```

⚠ On some distros `nut-monitor` is `WantedBy=nut.target` and `nut.target`
itself ships disabled — "enabled" is not the same as "will actually start".

⚠ The packaged NUT units may ship `Restart=no`. If the driver dies, nothing
brings it back. Apply the drop-in:

```bash
sudo mkdir -p /etc/systemd/system/nut-driver.service.d
sudo cp src/jetson/systemd/resilience.conf \
        /etc/systemd/system/nut-driver.service.d/
```

### Check what your UPS can actually do

```bash
upscmd -l apc
```

The command list alone does not tell you what each command *does*. On APC
Back-UPS units, two commands that look similar behave oppositely:

| Command | Register | What this unit does |
|---|---|---|
| `load.off`, `load.off.delay` | 0x15 | cuts and **stays off** until the front button |
| `shutdown.reboot` (value `1`), `shutdown.return` (NUT master) | 0x40 | cuts after ~60 s, then **comes back on**: ~4 s later on mains, or ~1 s after mains returns on battery |

Nothing turns the output back on after a 0x15 cut. So whatever your list
says, **bench-test with nothing plugged in** before relying on any of it:

```bash
scripts/ups-bench-test.sh 1   # on mains
scripts/ups-bench-test.sh 2   # remote power-on from OFF?
scripts/ups-bench-test.sh 3   # on battery (pull the wall plug first)
```

The results for this unit are in [UPS-TOOLING.md](UPS-TOOLING.md) §7.

### Optional: newer driver, stall watchdog, extra permissions

**NUT master driver** (only the driver; upsd and upsmon stay distro):

```bash
# on the jetson, as the normal user
mkdir -p ~/build && cd ~/build && git clone --depth 50 https://github.com/networkupstools/nut.git
./build-nut-master.sh                       # from scripts/, ~10 min
sudo nut-driver-upgrade install             # refuses to run on battery
sudo nut-driver-upgrade rollback            # back to the distro driver
```

**Stall watchdog.** Restarts `nut-driver` after 20 s of `Data stale` while the
UPS is still on the USB bus:

```bash
sudo systemctl enable --now nut-stall-watchdog
```

**Permissions for the dashboard's UPS controls.** Self-test, beeper, settings
and `shutdown.reboot` all need rights the emergency-cut user lacks. Grant
them, with a backup and a self-check:

```bash
sudo grant-ups-ops
```

---

## 3. Deploy

```bash
./scripts/install.sh            # reads .env, installs to both machines
```

Or by hand: copy `src/jetson/*` and `src/box/*` to their machines, write
`.env`'s contents to `/etc/ups-dash/site.env` on the sentinel, and enable the
units.

Verify the sentinel picked up your configuration — the first log line prints
it, so a half-configured install is obvious immediately:

```bash
journalctl -u ups-dash -n 20 | grep config:
```

If it shows `10.0.0.x` or `aa:bb:cc:dd:ee:ff`, your site config was not read.

---

## 4. Test it for real

In order. Do not skip to the last one.

1. **Hibernate and resume by hand.** Confirm `boot_id` is unchanged.
2. **Wake with a magic packet** from the sentinel while the workstation is
   hibernated.
3. **Pull the plug** with the workstation loaded (a CPU burner is fine —
   a loaded machine hibernates earlier, which is the interesting case) and
   watch it hibernate.
4. **Restore power** and confirm the gated wake fires only after the pack has
   recovered and mains has held.

⚠ Processes started with `timeout N` **survive hibernation** — `timeout` does
not count time powered off. A load generator will still be running when the
machine resumes.

---

## Optional

- **Dashboard over HTTPS** — `tailscale serve --bg <port>`. Required for the
  installable PWA: service workers need a secure context, so a plain `http://`
  LAN address will show an install option and then refuse.
- **Notifications** — set `NTFY_URL`/`NTFY_TOPIC` and create
  `/etc/ups-dash/notify.json`. Self-hosting ntfy on the sentinel is
  recommended: during an outage the sentinel survives but your internet
  usually does not, and a local server still reaches a phone on the house
  wifi.
- **Emergency shutdown** — run `setup-upscmd-creds` on the sentinel. Read
  the warning at the top of `upscmd.py` first.
