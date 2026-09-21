"""Site configuration, read from the environment.

Everything that differs between installations lives here and nowhere else:
addresses, the MAC that Wake-on-LAN targets, ports, the UPS name. Values come
from the environment, which systemd supplies via an EnvironmentFile:

    EnvironmentFile=-/etc/ups-dash/site.env

⚠ THE DEFAULTS ARE DELIBERATELY NOT USABLE. They are documentation-range
addresses, so an installation that forgot to provide a real config fails
visibly against 10.0.0.x rather than silently sending magic packets at
whatever real machine happens to hold that address on someone's LAN. A
default that "works" is worse than one that obviously does not.

Nothing here is a credential. NUT and ntfy secrets live in root-owned files
on the machines themselves and never pass through this module.
"""

import os


def _str(name, default):
    val = os.environ.get(name)
    return val.strip() if val and val.strip() else default


def _int(name, default):
    try:
        return int(_str(name, str(default)))
    except (TypeError, ValueError):
        return default


# ---- the protected workstation --------------------------------------------
BOX_HOST = _str("BOX_HOST", "workstation")
BOX_IP = _str("BOX_IP", "10.0.0.20")
BOX_AGENT_PORT = _int("BOX_AGENT_PORT", 9009)
BOX_URL = _str("BOX_URL", "http://%s:%d" % (BOX_IP, BOX_AGENT_PORT))

# The NIC that stays powered in standby -- this is what WoL wakes.
BOX_MAC = _str("BOX_MAC", "aa:bb:cc:dd:ee:ff")
BOX_NIC = _str("BOX_NIC", "eth0")

# WoL is layer 2, so this must be the broadcast address of the same subnet
# the sentinel sits on. A packet routed off-subnet will not wake anything.
BROADCAST = _str("BROADCAST", "10.0.0.255")

# ---- the sentinel itself ---------------------------------------------------
JETSON_HOST = _str("JETSON_HOST", "jetson-nano")
JETSON_IP = _str("JETSON_IP", "10.0.0.10")

# ---- services --------------------------------------------------------------
UPS_NAME = _str("UPS_NAME", "apc")
NUT_HOST = _str("NUT_HOST", "127.0.0.1")
NUT_PORT = _int("NUT_PORT", 3493)

DASH_BIND = _str("DASH_BIND", "0.0.0.0")
DASH_PORT = _int("DASH_PORT", 8088)

DB_PATH = _str("UPS_DASH_DB", "/var/lib/ups-dash/telemetry.db")
NOTIFY_CONF = _str("UPS_DASH_NOTIFY", "/etc/ups-dash/notify.json")
UPSCMD_CONF = _str("UPS_DASH_UPSCMD", "/etc/ups-dash/upscmd.json")
TUNABLES_CONF = _str("UPS_DASH_TUNABLES", "/etc/ups-dash/tunables.json")
# Persistent, not /run: a hold must survive the jetson rebooting mid-outage.
WAKE_HOLD = _str("UPS_DASH_WAKE_HOLD", "/var/lib/ups-dash/wake-hold")


def summary():
    """One line for the startup log, so a misconfigured deploy is obvious
    immediately rather than at 3am during an outage."""
    return ("box=%s (%s) mac=%s bcast=%s ups=%s listen=%s:%d"
            % (BOX_HOST, BOX_URL, BOX_MAC, BROADCAST, UPS_NAME,
               DASH_BIND, DASH_PORT))


def looks_unconfigured():
    """True when the placeholder defaults are still in play."""
    return BOX_MAC == "aa:bb:cc:dd:ee:ff" or BOX_IP.startswith("10.0.0.")
