"""Power controls.

Wake needs no privileges whatsoever -- a Wake-on-LAN magic packet is just a
UDP broadcast, and the box's NIC does the rest (armed at boot by wol-arm.service).

Hibernate is the privileged half and is gated separately; see box-agent.

THE INTERLOCK is the important part here. While an episode is open -- on
battery, mid-hibernate, or waiting at the wake gate -- the sentinel is running
a sequence. A stray tap must not be able to fight it. Controls refuse during
an episode unless the caller explicitly overrides, and the override is
recorded as an event either way.
"""

import json
import socket
import urllib.request

from . import settings

BOX_MAC = settings.BOX_MAC
BROADCAST = settings.BROADCAST
WOL_PORTS = (9, 7)


def magic_packet(mac=BOX_MAC, broadcast=BROADCAST):
    """Send the WoL magic packet. Returns (ok, detail)."""
    clean = mac.replace(":", "").replace("-", "").strip()
    if len(clean) != 12:
        return False, "malformed MAC %r" % mac
    try:
        payload = bytes.fromhex("ff" * 6 + clean * 16)
    except Exception as exc:
        return False, "malformed MAC: %s" % exc
    sent = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for port in WOL_PORTS:
            sock.sendto(payload, (broadcast, port))
            sent.append(port)
        sock.close()
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    return True, "magic packet sent to %s on ports %s" % (
        mac, ", ".join(str(p) for p in sent))


def interlock(collector, override=False):
    """Return a refusal string, or None when the action may proceed."""
    if override:
        return None
    ep = collector.episodes.current
    if ep:
        return ("An episode is in progress (%s). The sentinel is running a "
                "sequence and a manual action could fight it. Re-send with "
                "override to proceed anyway." % ep.get("kind"))
    return None


def box_hibernate(box_url, timeout=25.0):
    req = urllib.request.Request(box_url.rstrip("/") + "/hibernate",
                                 data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return False, {"error": "%s: %s" % (type(exc).__name__, exc)}
