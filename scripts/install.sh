#!/usr/bin/env bash
# Install Power Sentinel onto the two machines described in .env.
#
# DELIBERATELY NOT A ONE-SHOT INSTALLER. It does the mechanical, reversible
# parts -- copying files, substituting your site values into the placeholders,
# enabling units -- and then STOPS and tells you what it did not do.
#
# What it refuses to do for you, and why:
#   * hibernate setup (swapfile, resume=, resume_offset=) -- getting this
#     wrong breaks boot, and the correct commands differ per distro and
#     bootloader. docs/INSTALL.md walks through it.
#   * NUT configuration -- it needs credentials, and inventing them here
#     would put them somewhere a script can read.
#   * anything that grants privileges (polkit, UPS command credentials).
#
# Usage:  ./scripts/install.sh [--dry-run] [jetson|box|all]
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
ENVFILE="$HERE/.env"

[ -f "$ENVFILE" ] || { echo "No .env -- copy .env.example and fill it in."; exit 1; }
# shellcheck disable=SC1090
. "$ENVFILE"

: "${JETSON_USER:?set JETSON_USER}" "${JETSON_IP:?set JETSON_IP}"
: "${BOX_USER:?set BOX_USER}" "${BOX_IP:?set BOX_IP}"
: "${BOX_MAC:?set BOX_MAC}" "${BROADCAST:?set BROADCAST}"

DRY=0
TARGET=all
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    jetson|box|all) TARGET="$a" ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done

if [ "$BOX_MAC" = "aa:bb:cc:dd:ee:ff" ]; then
  echo "BOX_MAC is still the placeholder. Wake-on-LAN would target nothing."
  exit 1
fi

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
run()  { if [ "$DRY" = 1 ]; then echo "  [dry-run] $*"; else eval "$@"; fi; }

# Substitute site values into a placeholder-bearing source file.
render() {
  sed -e "s|10\.0\.0\.20|$BOX_IP|g" \
      -e "s|10\.0\.0\.10|$JETSON_IP|g" \
      -e "s|10\.0\.0\.255|$BROADCAST|g" \
      -e "s|aa:bb:cc:dd:ee:ff|$BOX_MAC|g" \
      -e "s|\bworkstation\b|${BOX_HOST:-workstation}|g" \
      -e "s|\bjetson-nano\b|${JETSON_HOST:-jetson-nano}|g" \
      -e "s|\beth0\b|${BOX_NIC:-eth0}|g" \
      -e "s|\byouruser\b|$BOX_USER|g" "$1"
}

install_jetson() {
  local host="$JETSON_USER@$JETSON_IP"
  say "Jetson ($host)"

  # Site config, so nothing site-specific is compiled into the code.
  run "grep -vE '^\\s*#|^\\s*\$' '$ENVFILE' | ssh '$host' 'cat > /tmp/site.env'"
  run "ssh '$host' 'sudo install -d -m 755 /etc/ups-dash && sudo install -m 640 /tmp/site.env /etc/ups-dash/site.env && rm -f /tmp/site.env'"
  echo "  installed /etc/ups-dash/site.env"

  run "rsync -az --delete --exclude __pycache__ '$HERE/src/ups-dash/' '$host:~/projects/ups-dash/'"
  echo "  deployed the dashboard to ~/projects/ups-dash"

  for f in "$HERE"/src/jetson/*; do
    [ -f "$f" ] || continue
    local base; base="$(basename "$f")"
    render "$f" | run "ssh '$host' 'cat > /tmp/$base'" || true
    run "ssh '$host' 'sudo install -m 750 /tmp/$base /usr/local/sbin/$base && rm -f /tmp/$base'"
    echo "  installed /usr/local/sbin/$base"
  done

  for u in "$HERE"/src/jetson/systemd/*; do
    [ -f "$u" ] || continue
    local base; base="$(basename "$u")"
    render "$u" | run "ssh '$host' 'cat > /tmp/$base'" || true
    run "ssh '$host' 'sudo install -m 644 /tmp/$base /etc/systemd/system/$base && rm -f /tmp/$base'"
    echo "  installed $base"
  done
  run "ssh '$host' 'sudo systemctl daemon-reload'"
}

install_box() {
  local host="$BOX_USER@$BOX_IP"
  say "Workstation ($host)"
  run "rsync -az --delete --exclude __pycache__ '$HERE/src/box-agent/' '$host:~/projects/box-agent/'"
  echo "  deployed the agent to ~/projects/box-agent"

  for f in "$HERE"/src/box/*; do
    [ -f "$f" ] || continue
    local base; base="$(basename "$f")"
    render "$f" | run "ssh '$host' 'cat > /tmp/$base'" || true
    run "ssh '$host' 'sudo install -m 755 /tmp/$base /usr/local/sbin/$base && rm -f /tmp/$base'"
    echo "  installed /usr/local/sbin/$base"
  done

  for u in "$HERE"/src/box/systemd/*; do
    [ -f "$u" ] || continue
    local base; base="$(basename "$u")"
    render "$u" | run "ssh '$host' 'cat > /tmp/$base'" || true
    run "ssh '$host' 'sudo install -m 644 /tmp/$base /etc/systemd/system/$base && rm -f /tmp/$base'"
    echo "  installed $base"
  done
  run "ssh '$host' 'sudo systemctl daemon-reload'"
}

[ "$TARGET" = all ] || [ "$TARGET" = jetson ] && install_jetson
[ "$TARGET" = all ] || [ "$TARGET" = box ] && install_box

cat <<'DONE'

Installed. NOT done for you -- see docs/INSTALL.md:

  1. Hibernate on the workstation: swapfile, resume=/resume_offset= on the
     kernel command line, and the resume module in the initramfs.
     VERIFY IT before anything else: hibernate, power back on by hand, and
     confirm /proc/sys/kernel/random/boot_id is UNCHANGED. If it changed you
     cold-booted, and nothing downstream will save your session.

  2. NUT on the sentinel: ups.conf, upsd.conf, upsd.users, and
     `upscmd -l <ups>` to learn whether your UPS can restore its own output.
     Most cannot.

  3. Enable the units you actually want:
       sentinel:     ups-sentinel  ups-dash
       workstation:  hibernate-governor  box-agent  wol-arm

  4. Optional and privileged, read the docs first:
       setup-upscmd-creds        (lets the dashboard cut the UPS output)
       polkit/*.rules            (lets the agent hibernate the machine)
DONE
