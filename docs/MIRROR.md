# mirror/ — every file this project installed on either machine

**Purpose: recreatability.** If the jetson's SD card dies or the box is rebuilt,
this directory is what lets the system be restored rather than reverse-engineered
from memory. Everything installed on either machine has a copy here.

⚠ **Refresh it after any change**: `./pull-state.sh`. The live machines drift;
this mirror is only as good as its last pull.

🔒 **Secrets are scrubbed.** NUT passwords are replaced with `<secret>` before
anything is written here, so this directory stays safe to read and copy. The real
passwords live only on the machines — read them back with
`sudo grep password /etc/nut/upsd.users` on the jetson.

---

## What lives where

### `box/` → `workstation` (10.0.0.20)

| File here | Installs to | What it does |
|---|---|---|
| `sbin/hibernate-governor` | `/usr/local/sbin/` | decides when to hibernate on battery (**safety-critical**) |
| `sbin/assert-boot-order` | `/usr/local/sbin/` | re-asserts Linux first in UEFI BootOrder (Windows rewrites it) |
| `systemd/box-agent.service` | `/etc/systemd/system/` | telemetry agent for the dashboard |
| `systemd/wol-arm.service` | `/etc/systemd/system/` | arms Wake-on-LAN at boot (**r8169 drops it every reboot**) |
| `systemd/wol-recheck.{service,timer}` | `/etc/systemd/system/` | re-arms WoL every 15 min against silent disarm |
| `systemd/assert-boot-order.service` | `/etc/systemd/system/` | runs the boot-order assertion |
| `systemd/dropins/*.resilience.conf` | `/etc/systemd/system/<unit>.service.d/resilience.conf` | disables systemd's start-rate limit |
| `polkit/49-box-agent-hibernate.rules` | `/etc/polkit-1/rules.d/` | lets box-agent hibernate the box (one action, one user) |
| `nut/upsmon.conf`, `nut/upssched.conf` | `/etc/nut/` | UPS client config |
| `kernel/cmdline` | `/etc/kernel/cmdline` | **resume= and resume_offset= — hibernation depends on these** |
| `kernel/dracut-resume.conf` | `/etc/dracut.conf.d/resume.conf` | pulls the resume module into the initramfs |
| `ups-dash/tunables.json` | `/etc/ups-dash/` | dashboard-editable governor thresholds |

⚠ After changing `kernel/cmdline`, run `sudo reinstall-kernels` — **never hand-edit
`/efi/loader/entries/*.conf`**, they are generated and your edit will vanish on the
next kernel update, silently breaking resume.

### `jetson/` → `jetson-nano` (10.0.0.10)

| File here | Installs to | What it does |
|---|---|---|
| `sbin/ups-sentinel` | `/usr/local/sbin/` | gated recovery controller: wakes the box (**safety-critical**) |
| `systemd/ups-sentinel.service` | `/etc/systemd/system/` | runs it |
| `systemd/ups-dash.service` | `/etc/systemd/system/` | dashboard collector + web server |
| `systemd/dropins/*.resilience.conf` | `/etc/systemd/system/<unit>.service.d/resilience.conf` | **NUT units ship `Restart=no`** — these make them restart forever |
| `nut/*.conf` | `/etc/nut/` | UPS driver + server + users (passwords scrubbed here) |
| `ups-dash/tunables.json` | `/etc/ups-dash/` | dashboard-editable sentinel thresholds |

### Not mirrored (not a file)

**firewalld rich rule on the box**, allowing only the jetson to reach the agent:

```bash
sudo firewall-cmd --permanent --zone=public --add-rich-rule='rule family="ipv4" source address="10.0.0.10" port port="9009" protocol="tcp" accept'
sudo firewall-cmd --reload
```

---

## Application code

Not here — it lives one level up and deploys by `rsync`:

| Source | Target |
|---|---|
| `../ups-dash/` | `jetson:~/projects/ups-dash/` |
| `../box-agent/` | `box:~/projects/box-agent/` |

```bash
rsync -az --delete --exclude __pycache__ ../ups-dash/  jetson@10.0.0.10:~/projects/ups-dash/
rsync -az --delete --exclude __pycache__ ../box-agent/ youruser@10.0.0.20:~/projects/box-agent/
```

Then `sudo systemctl restart ups-dash` / `box-agent` on the respective host.

## `patch-tunables.py`

Adds the fail-safe tunables loader to `hibernate-governor` and `ups-sentinel`.
Idempotent, backs up to `*.pre-tunables-<timestamp>`, and refuses to install a
file that does not compile. Run as `patch-tunables.py governor|sentinel`.

The loader **reassigns the existing module globals**, so every expression that
already read `RESERVE_PCT` is untouched — that is what keeps the diff to these
safety-critical files minimal.
