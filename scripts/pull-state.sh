#!/usr/bin/env bash
# Mirror every config, unit and script this project installed on either machine
# back into the project directory.
#
# WHY: the live machines drift. If the jetson's SD card dies or the box is
# rebuilt, this directory is what lets the system be recreated rather than
# reverse-engineered. Run it after any change on either host.
#
# SECRETS ARE STRIPPED. NUT passwords are replaced with <secret> placeholders
# before anything is written here -- this directory must stay safe to read,
# copy and share. The real passwords live only on the machines.
set -u

# Site config and (optionally) sudo passwords come from .env, which is
# gitignored. If the *PW vars are unset the script falls back to interactive
# sudo over `ssh -t`, so nothing here ever needs a password on disk.
ENVFILE="$(cd "$(dirname "$0")/.." && pwd)/.env"
# shellcheck disable=SC1090
[ -f "$ENVFILE" ] && . "$ENVFILE"

BOX="${BOX_USER:?set BOX_USER in .env}@${BOX_IP:?set BOX_IP in .env}"
NANO="${JETSON_USER:?set JETSON_USER in .env}@${JETSON_IP:?set JETSON_IP in .env}"
BOXPW="${BOX_SUDO_PW:-}"
NANOPW="${JETSON_SUDO_PW:-}"
HERE="$(cd "$(dirname "$0")/../mirror" && pwd)"
SILENCE='WARNING|post-quantum|store now|upgraded'

scrub() {   # strip anything that looks like a credential
  sed -E 's/^([[:space:]]*password[[:space:]]*=[[:space:]]*).*/\1<secret>/I;
          s/^([[:space:]]*MONITOR[[:space:]]+\S+[[:space:]]+\S+[[:space:]]+\S+[[:space:]]+)\S+/\1<secret>/I;
          s/("token"[[:space:]]*:[[:space:]]*)"[^"]*"/\1"<secret>"/I;
          s/("password"[[:space:]]*:[[:space:]]*)"[^"]*"/\1"<secret>"/I'
}

grab() {    # grab <host> <sudopw> <remote-path> <local-relative-path>
  local host="$1" pw="$2" remote="$3" local="$4"
  mkdir -p "$HERE/$(dirname "$local")"
  if [ -n "$pw" ]; then
    fetch() { ssh -o ConnectTimeout=10 "$host" "echo '$pw' | sudo -S -p '' cat '$remote' 2>/dev/null"; }
  else
    # No password on disk: prompt once per file via a tty.
    fetch() { ssh -t -o ConnectTimeout=10 "$host" "sudo cat '$remote' 2>/dev/null"; }
  fi
  if fetch 2>/dev/null | grep -vE "$SILENCE" | scrub > "$HERE/$local.tmp"; then
    if [ -s "$HERE/$local.tmp" ]; then
      mv "$HERE/$local.tmp" "$HERE/$local"; echo "  ok   $local"
    else
      rm -f "$HERE/$local.tmp"; echo "  MISS $local (empty or absent)"
    fi
  else
    rm -f "$HERE/$local.tmp"; echo "  FAIL $local"
  fi
}

echo "=== BOX (the workstation) ==="
grab $BOX $BOXPW /usr/local/sbin/hibernate-governor        box/sbin/hibernate-governor
grab $BOX $BOXPW /usr/local/sbin/assert-boot-order         box/sbin/assert-boot-order
grab $BOX $BOXPW /etc/systemd/system/box-agent.service     box/systemd/box-agent.service
grab $BOX $BOXPW /etc/systemd/system/wol-arm.service       box/systemd/wol-arm.service
grab $BOX $BOXPW /etc/systemd/system/assert-boot-order.service box/systemd/assert-boot-order.service
grab $BOX $BOXPW /etc/systemd/system/wol-recheck.service   box/systemd/wol-recheck.service
grab $BOX $BOXPW /etc/systemd/system/wol-recheck.timer     box/systemd/wol-recheck.timer
grab $BOX $BOXPW /etc/systemd/system/hibernate-governor.service.d/resilience.conf box/systemd/dropins/hibernate-governor.resilience.conf
grab $BOX $BOXPW /etc/systemd/system/box-agent.service.d/resilience.conf          box/systemd/dropins/box-agent.resilience.conf
grab $BOX $BOXPW /etc/systemd/system/nut-monitor.service.d/resilience.conf        box/systemd/dropins/nut-monitor.resilience.conf
grab $BOX $BOXPW /etc/polkit-1/rules.d/49-box-agent-hibernate.rules box/polkit/49-box-agent-hibernate.rules
grab $BOX $BOXPW /etc/nut/upsmon.conf                      box/nut/upsmon.conf
grab $BOX $BOXPW /etc/nut/upssched.conf                    box/nut/upssched.conf
grab $BOX $BOXPW /etc/kernel/cmdline                       box/kernel/cmdline
grab $BOX $BOXPW /etc/dracut.conf.d/resume.conf            box/kernel/dracut-resume.conf
grab $BOX $BOXPW /etc/ups-dash/tunables.json               box/ups-dash/tunables.json
grab $BOX $BOXPW /usr/local/sbin/box-health                box/sbin/box-health
grab $BOX $BOXPW /etc/systemd/system/box-health.service    box/systemd/box-health.service
grab $BOX $BOXPW /etc/systemd/system/box-health.timer      box/systemd/box-health.timer

echo "=== JETSON (the sentinel) ==="
grab $NANO $NANOPW /usr/local/sbin/ups-sentinel             jetson/sbin/ups-sentinel
grab $NANO $NANOPW /etc/systemd/system/ups-sentinel.service jetson/systemd/ups-sentinel.service
grab $NANO $NANOPW /etc/systemd/system/ups-dash.service     jetson/systemd/ups-dash.service
grab $NANO $NANOPW /etc/systemd/system/ups-sentinel.service.d/resilience.conf jetson/systemd/dropins/ups-sentinel.resilience.conf
grab $NANO $NANOPW /etc/systemd/system/ups-dash.service.d/resilience.conf     jetson/systemd/dropins/ups-dash.resilience.conf
grab $NANO $NANOPW /etc/systemd/system/nut-driver.service.d/resilience.conf   jetson/systemd/dropins/nut-driver.resilience.conf
grab $NANO $NANOPW /etc/systemd/system/nut-server.service.d/resilience.conf   jetson/systemd/dropins/nut-server.resilience.conf
grab $NANO $NANOPW /etc/systemd/system/nut-monitor.service.d/resilience.conf  jetson/systemd/dropins/nut-monitor.resilience.conf
grab $NANO $NANOPW /etc/nut/ups.conf                        jetson/nut/ups.conf
grab $NANO $NANOPW /etc/nut/upsd.conf                       jetson/nut/upsd.conf
grab $NANO $NANOPW /etc/nut/upsd.users                      jetson/nut/upsd.users
grab $NANO $NANOPW /etc/nut/upsmon.conf                     jetson/nut/upsmon.conf
grab $NANO $NANOPW /etc/ups-dash/tunables.json              jetson/ups-dash/tunables.json
grab $NANO $NANOPW /etc/ups-dash/notify.json               jetson/ups-dash/notify.json
grab $NANO $NANOPW /etc/ups-dash/upscmd.json               jetson/ups-dash/upscmd.json
grab $NANO $NANOPW /etc/ntfy/server.yml                    jetson/ntfy/server.yml
grab $NANO $NANOPW /etc/systemd/system/ntfy-tunnel.service jetson/systemd/ntfy-tunnel.service
grab $NANO $NANOPW /home/jetson/.cloudflared/ntfy.yml      jetson/ntfy/tunnel.yml
grab $NANO $NANOPW /usr/local/sbin/setup-upscmd-creds      jetson/sbin/setup-upscmd-creds
grab $NANO $NANOPW /usr/local/sbin/eth-leds                jetson/sbin/eth-leds
grab $NANO $NANOPW /etc/systemd/system/eth-leds-off.service jetson/systemd/eth-leds-off.service
grab $NANO $NANOPW /etc/systemd/system/eth-leds-off.timer  jetson/systemd/eth-leds-off.timer

echo
echo "Secrets scrubbed to <secret>. Verify with: grep -ri password $HERE/box $HERE/jetson"
