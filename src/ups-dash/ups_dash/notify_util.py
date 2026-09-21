"""Small formatting/lookup helpers shared by notify_events.py and
notify_events_states.py.

Split out purely to keep both of those under the 300-line cap -- there is no
behavioural reason these couldn't live in either file. No I/O, no state,
never raises (`_g` is the one thing every caller leans on to survive a
malformed or partial snapshot without a try/except at every call site).
"""


def _g(d, *keys):
    """Safe nested get -- returns None on any missing key or non-dict hop."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _n(v):
    return "?" if v is None else ("%g" % v)


def _fmt_secs(sec):
    if not isinstance(sec, (int, float)):
        return "?"
    sec = int(max(0, sec))
    if sec < 90:
        return "%ds" % sec
    m = sec // 60
    return "%dm" % m if m < 90 else "%dh %02dm" % (m // 60, m % 60)


def ep_suffix(curr):
    """`(episode #N)` when one is open, else "".  Links the push notification
    back to the same episode id the dashboard's HISTORY view uses, so a
    human can find the full trace for what the phone just buzzed about."""
    ep = curr.get("episode")
    if isinstance(ep, dict) and ep.get("id") is not None:
        return " (episode #%s)" % ep["id"]
    return ""


def ups_line(curr):
    """`Charge %, runtime.` -- the two numbers a lock-screen glance needs,
    shared by every event that talks about the pack."""
    charge = _g(curr, "ups", "charge")
    runtime = _g(curr, "ups", "runtime")
    return "Charge %s%%, runtime %s." % (_n(charge), _fmt_secs(runtime))
