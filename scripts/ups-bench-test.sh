#!/bin/bash
# ups-bench-test.sh 1|2|3|4 -- the shutdown.reboot bench tests (docs/UPS-TOOLING.md s7).
# Run ONLY with nothing plugged into the UPS outlets. Each test refuses to run
# unless the UPS is in the state it needs.
#   1  on mains:              shutdown.reboot 1 -> does it cut, then come back?
#   2  from OL OFF:           load.off, then shutdown.reboot 1 -> remote power-on?
#   3  on battery (unplugged): shutdown.reboot 1 -> cut, then back on at mains return? any loop?
#   4  on battery, PARKED:     as 3, but mains stays out 5 min after the cut -> does the UPS
#                             hold its charge while parked, and still restore afterwards?
set -u
T=${1:-}; case "$T" in 1|2|3|4) ;; *) echo "usage: $0 1|2|3|4"; exit 2;; esac
# Keep a copy of every run next to the script, whoever started it.
exec > >(tee -a "$(dirname "$0")/ups-test-$T.log") 2>&1
CRED=/etc/ups-dash/upscmd.json
U=$(python3 -c "import json;print(json.load(open(\"$CRED\"))[\"user\"])")
P=$(python3 -c "import json;print(json.load(open(\"$CRED\"))[\"password\"])")
st()  { upsc apc ups.status 2>/dev/null; }
cmd() { # send one instant command; abort the test if upsd refuses it
  echo "$(date +%T) >>> $*"; local r
  r=$(upscmd -u "$U" -p "$P" apc "$@" 2>&1 | grep -v "Init SSL"); echo "$r"
  echo "$r" | grep -q "^OK" || { echo "ABORT: command refused -- nothing was sent to the UPS"; exit 1; }
}
trace() { # trace SECONDS [HINT] -- 1 Hz, prints changes only (+15 s heartbeat)
  local end=$(( $(date +%s) + $1 )) prev="" n=0 hinted=0 cur usb cut_at=0
  while [ $(date +%s) -lt $end ]; do
    cur=$(upsc apc 2>&1 | grep -E "^(ups.status|ups.timer.reboot|ups.timer.shutdown|battery.charge|battery.voltage):|stale|not connected" | sed "s/battery.charge/chg/;s/battery.voltage/bv/;s/ups.timer./t./;s/ups.status/st/" | tr "\n" " ")
    usb=$([ -e /sys/bus/usb/devices/1-2.4/idVendor ] && echo usb:up || echo usb:GONE)
    cur="$cur $usb"
    if [ "$cur" != "$prev" ] || [ $((n % 15)) -eq 0 ]; then echo "$(date +%T) $cur"; fi
    if [ "${2:-}" = plug ] && [ $hinted = 0 ] && { [ $usb = usb:GONE ] || echo "$cur" | grep -q "OB.*OFF\|OFF.*OB"; }; then
      echo "=========== CUT SEEN -- plug the UPS back into the wall NOW ==========="; hinted=1; fi
    if [ "${2:-}" = park ] && [ $cut_at = 0 ] && { [ $usb = usb:GONE ] || echo "$cur" | grep -q "OB.*OFF\|OFF.*OB"; }; then
      cut_at=$(date +%s); echo "=========== CUT SEEN -- keep it UNPLUGGED, I will say when (5 min) ==========="; fi
    if [ "${2:-}" = park ] && [ $cut_at != 0 ] && [ $hinted = 0 ] && [ $(( $(date +%s) - cut_at )) -ge 300 ]; then
      echo "=========== 5 MIN PARKED -- plug the UPS back into the wall NOW ==========="; hinted=1; fi
    prev="$cur"; n=$((n+1)); sleep 1
  done
}
S=$(st); echo "$(date +%T) start: status=[$S]"
case $T in
1) echo "$S" | grep -qw OL && ! echo "$S" | grep -qw OFF || { echo "needs OL without OFF"; exit 1; }
   cmd shutdown.reboot 1; trace 240 ;;
2) echo "$S" | grep -qw OL || { echo "needs OL (mains present)"; exit 1; }
   echo "$S" | grep -qw OFF || { cmd load.off; trace 15; }
   st | grep -qw OFF || { echo "output did not go OFF -- stopping"; exit 1; }
   cmd shutdown.reboot 1; trace 240
   st | grep -qw OFF && echo "RESULT: still OFF -- no remote power-on. Press the front button to restore." \
                     || echo "RESULT: output is back ON" ;;
3) echo "$S" | grep -qw OB || { echo "needs OB -- pull the UPS wall plug first, then rerun"; exit 1; }
   cmd shutdown.reboot 1; trace 420 plug
   echo "RESULT: final status [$(st)]  (a loop shows as repeated OB / usb:GONE after mains returned)" ;;
4) echo "$S" | grep -qw OB || { echo "needs OB -- pull the UPS wall plug first, then rerun"; exit 1; }
   cmd shutdown.reboot 1; trace 660 park
   echo "RESULT: final status [$(st)]  (compare chg/bv at the cut vs 5 min later; usb:GONE = fully asleep)" ;;
esac
