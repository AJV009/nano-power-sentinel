"""What is running on the box when box-agent is silent? Asked over the LAN.

2026-09-25: the box booted into Windows. From the jetson it then answers
ARP at its usual address (same MAC, DHCP gave the same IP) but drops ping
and every TCP port without a reset -- Windows Firewall's default ("Public"
profile), and no NetBIOS / SSDP / mDNS reply either. Every other state the
dashboard knows looks different from here:

    Linux, normal          ARP  ping ttl 64  ssh open   agent answers
    Linux, agent dead      ARP  ping ttl 64  ssh open   agent silent
    Windows                ARP  -- or ttl 128  dropped  --
    stuck in firmware      --   --           --         --  (park_stuck.py)
    off / hibernated       --   --           --         --

So the verdicts are:
  "linux"       something Linux answers (ssh open or refused, or TTL <= 64)
  "windows"     TTL 65..128, or Windows' RPC / SMB ports answer
  "firewalled"  on the LAN (ARP) and silent to everything: Windows' default.
                Allowing ICMP echo in Windows turns this into "windows".
  None          not on the LAN at all

Runs on its own thread, and only while the collector says the agent is
silent: a ping and four 1 s connects must never sit in the 1 Hz loop.
"""

import re
import socket
import subprocess
import threading
import time

from . import park_io, settings

INTERVAL = 15.0
FRESH_SEC = 60.0
PORTS = (22, 9009, 135, 445)          # ssh, box-agent, Windows RPC, SMB
_TTL = re.compile(r"ttl=(\d+)")


def ping_ttl(ip, timeout=1):
    try:
        out = subprocess.run(["ping", "-c1", "-W%d" % timeout, ip],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=timeout + 2).stdout.decode("ascii", "replace")
    except Exception:
        return None
    m = _TTL.search(out)
    return int(m.group(1)) if m else None


def tcp_state(ip, port, timeout=1.0):
    """open | refused (a live stack sent RST) | filtered (silent drop)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return "open"
    except ConnectionRefusedError:
        return "refused"
    except Exception:
        return "filtered"
    finally:
        s.close()


def verdict(arp, ttl, tcp):
    tcp = tcp or {}
    if any(tcp.get(p) in ("open", "refused") for p in (22, 9009)):
        return "linux"
    if ttl is not None:
        return "linux" if ttl <= 64 else ("windows" if ttl <= 128 else "firewalled")
    if any(tcp.get(p) == "open" for p in (135, 445)):
        return "windows"
    return "firewalled" if arp else None


def probe(ip):
    ttl = ping_ttl(ip)                    # also makes the kernel ask ARP
    arp = park_io.lan_present(ip)
    if not arp and ttl is None:
        return {"os": None, "arp": False, "ttl": None, "tcp": {}}
    tcp = {p: tcp_state(ip, p) for p in PORTS}
    return {"os": verdict(arp, ttl, tcp), "arp": arp, "ttl": ttl, "tcp": tcp}


WHY = {
    "linux": "Linux answers on the LAN, but box-agent is silent",
    "windows": "running Windows (it answers like Windows on the LAN)",
    "firewalled": "on the LAN but silent to ping and every port -- "
                  "another OS, most likely Windows",
}


class LanProbe(object):
    def __init__(self, ip=None, probe_fn=None, clock=time.time):
        self.ip = ip or settings.BOX_IP
        self._probe = probe_fn or probe
        self._clock = clock
        self._want = False
        self.result = None                # {"os", "arp", "ttl", "tcp", "at"}

    def start(self):
        threading.Thread(target=self._run, name="ups-dash-lanprobe",
                         daemon=True).start()

    def want(self, flag):
        """The collector: is the agent silent (so is a probe worth it)?"""
        self._want = bool(flag)
        if not flag:
            self.result = None

    def verdict(self, now):
        r = self.result
        if not self._want or r is None or now - r["at"] > FRESH_SEC:
            return None
        return r.get("os")

    def _run(self):
        while True:
            if self._want:
                try:
                    r = self._probe(self.ip)
                    r["at"] = self._clock()
                    if self._want:
                        self.result = r
                except Exception:
                    pass
            time.sleep(INTERVAL)
