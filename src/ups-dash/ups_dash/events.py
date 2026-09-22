"""The complete event vocabulary: every discrete thing this system can report.

ONE CATALOG, THREE CONSUMERS. The stored history, the phone notifications and
the HISTORY feed all name events from here, so a given occurrence has exactly
one key, one label and one severity everywhere. Before this existed, event
names were string literals scattered across collector.py, upsoff.py and
server.py, and the notification rules knew about a different subset than the
store did -- which is how a manual shutdown ended up recorded but never
notified.

`states.py` re-exports everything here, so callers import one module.

FIELDS
  label      short human text, used in the HISTORY feed and as the phone title
  severity   nominal | ok | warn | critical | unknown  (drives colour)
  priority   ntfy priority: min | low | default | high | max
  tags       ntfy tags (emoji shortcodes on the phone)
  notify     whether this event is worth waking somebody for
"""

# ---- power / UPS ----------------------------------------------------------
MAINS_LOST = "mains_lost"
MAINS_RESTORED = "mains_restored"
LOW_BATTERY = "low_battery"
REPLACE_BATTERY = "replace_battery"
UPS_OUTPUT_OFF = "ups_output_off"
UPS_OUTPUT_RESTORED = "ups_output_restored"
UPS_UNREADABLE = "ups_unreadable"
UPS_READABLE_AGAIN = "ups_readable_again"
UPS_OVERLOAD = "ups_overload"
TRANSFER_CAUSE = "transfer_cause"

# ---- UPS self-test ----------------------------------------------------------
SELF_TEST_STARTED = "self_test_started"
SELF_TEST_PASSED = "self_test_passed"
SELF_TEST_WARNING = "self_test_warning"
SELF_TEST_FAILED = "self_test_failed"

# ---- box lifecycle --------------------------------------------------------
HIBERNATE_IMMINENT = "hibernate_imminent"
BOX_HIBERNATED_OUTAGE = "box_hibernated_outage"
BOX_HIBERNATED_MANUAL = "box_hibernated_manual"
BOX_HIBERNATING = "box_hibernating"
BOX_POWERED_OFF = "box_powered_off"
BOX_WOKE = "box_woke"
BOX_REBOOTED = "box_rebooted"
BOX_LOST = "box_lost"

# ---- battery-floor park + power-on guard (park.py) -------------------------
UPS_PARK_ARMED = "ups_park_armed"
UPS_PARKED = "ups_parked"
UPS_PARK_SKIPPED = "ups_park_skipped"
BOX_REHIBERNATED = "box_rehibernated"
PARK_NO_POWER_ON = "park_no_power_on"
PARK_FAILED = "park_failed"

# ---- recovery -------------------------------------------------------------
WAKE_GATE_WAIT = "wake_gate_wait"
WAKE_GATE_PASSED = "wake_gate_passed"
WAKE_SENT = "wake_sent"

# ---- operator actions -----------------------------------------------------
MANUAL_WAKE = "manual_wake"
MANUAL_HIBERNATE = "manual_hibernate_requested"
EMERGENCY_SAFE_STARTED = "emergency_safe_started"
EMERGENCY_SAFE_ABORTED = "emergency_safe_aborted"
EMERGENCY_SAFE_CUT = "emergency_safe_cut"
EMERGENCY_INSTANT_CUT = "emergency_instant_cut"
EMERGENCY_ABORT = "emergency_abort"
CONFIG_CHANGE = "config_change"
UPS_SETTING_CHANGED = "ups_setting_changed"
BEEPER_CHANGED = "beeper_changed"

# ---- system health --------------------------------------------------------
SERVICE_DIED = "service_died"
SERVICE_RECOVERED = "service_recovered"
COLLECTOR_STARTED = "collector_started"
QUEUE_OVERFLOW = "queue_overflow"

# ---- episodes -------------------------------------------------------------
EPISODE_START = "episode_start"
EPISODE_END = "episode_end"


def _e(label, severity, priority, tags, notify=True):
    return {"label": label, "severity": severity, "priority": priority,
            "tags": tags, "notify": notify}


CATALOG = {
    MAINS_LOST: _e("Mains lost", "warn", "high", ["warning", "electric_plug"]),
    MAINS_RESTORED: _e("Mains restored", "ok", "default", ["white_check_mark"]),
    LOW_BATTERY: _e("LOW BATTERY", "critical", "max", ["rotating_light", "battery"]),
    REPLACE_BATTERY: _e("Replace battery", "warn", "high", ["warning", "battery"]),
    # The one everyone forgets: after load.off the UPS sits at OL OFF with the
    # output dead. No software path restores it -- bench-tested 2026-09-22,
    # docs/UPS-TOOLING.md §7 -- the alert says what does: the front-panel
    # button.
    UPS_OUTPUT_OFF: _e("UPS OUTPUT OFF", "critical", "max", ["rotating_light", "electric_plug"]),
    UPS_OUTPUT_RESTORED: _e("UPS output restored", "ok", "default", ["white_check_mark"]),
    UPS_UNREADABLE: _e("UPS unreadable", "unknown", "high", ["warning", "question"]),
    UPS_READABLE_AGAIN: _e("UPS readable again", "ok", "default", ["white_check_mark"]),
    UPS_OVERLOAD: _e("UPS OVERLOAD", "critical", "max", ["rotating_light", "zap"]),
    # Stored once per outage, not pushed: input.transfer.reason is only
    # trustworthy pollfreq+2 s after the switch (it may still describe the
    # PREVIOUS transfer before that), and it rides on mains_restored anyway.
    TRANSFER_CAUSE: _e("Transfer cause", "nominal", "min", ["mag"], notify=False),

    # A self-test briefly shows OL OFF / OL DISCHRG (NUT #2104). The START is
    # routine and silent; only the verdict is worth a push.
    SELF_TEST_STARTED: _e("Self-test started", "nominal", "low", ["test_tube"], notify=False),
    SELF_TEST_PASSED: _e("Self-test passed", "ok", "low", ["white_check_mark", "battery"]),
    SELF_TEST_WARNING: _e("Self-test: warning", "warn", "high", ["warning", "battery"]),
    SELF_TEST_FAILED: _e("SELF-TEST FAILED", "critical", "high", ["rotating_light", "battery"]),

    HIBERNATE_IMMINENT: _e("Hibernating now", "warn", "max", ["rotating_light", "computer"]),
    BOX_HIBERNATED_OUTAGE: _e("Box hibernated (outage)", "warn", "high", ["computer", "battery"]),
    # Deliberately a different key and a calmer priority than the outage case:
    # you already know you pressed the button.
    BOX_HIBERNATED_MANUAL: _e("Box off (by you)", "ok", "default", ["computer"]),
    # The moment it actually goes down: the governor at its threshold, or an
    # emergency sequence in flight (still abortable for a few seconds).
    BOX_HIBERNATING: _e("Shutting down", "warn", "high", ["rotating_light", "computer"]),
    BOX_POWERED_OFF: _e("Box powered off", "critical", "max", ["rotating_light", "electric_plug"]),
    BOX_WOKE: _e("Box is back", "ok", "default", ["white_check_mark", "computer"]),
    BOX_REBOOTED: _e("Box cold-booted", "warn", "default", ["arrows_counterclockwise"]),
    BOX_LOST: _e("Box unreachable", "warn", "high", ["warning", "computer"]),

    # The park (park.py): shutdown.return on battery with the box asleep, so
    # the pack holds at the floor instead of feeding the idle inverter
    # (23-55 %/h, bench test 4). Parking itself is announced; its silent
    # middle (the UPS switching itself off) is not. Skipped = the box was
    # still up below the governor's reserve, so the governor is not working.
    UPS_PARK_ARMED: _e("Parking the UPS", "warn", "high", ["parking", "battery"]),
    UPS_PARKED: _e("UPS parked", "nominal", "low", ["parking"], notify=False),
    UPS_PARK_SKIPPED: _e("Park skipped: box awake at the floor", "warn", "high", ["warning", "battery"]),
    BOX_REHIBERNATED: _e("Back to sleep after power-on", "ok", "default", ["zzz", "computer"]),
    PARK_NO_POWER_ON: _e("Box needs its power button", "warn", "high", ["warning", "computer"]),
    PARK_FAILED: _e("UPS park failed", "critical", "high", ["rotating_light", "battery"]),

    WAKE_GATE_WAIT: _e("Waiting to wake the box", "ok", "low", ["hourglass"]),
    WAKE_GATE_PASSED: _e("Wake gate reached", "ok", "low", ["hourglass_flowing_sand"]),
    WAKE_SENT: _e("Wake sent", "ok", "low", ["zap"]),

    MANUAL_WAKE: _e("Wake sent (by you)", "ok", "low", ["zap"]),
    MANUAL_HIBERNATE: _e("Hibernate requested", "ok", "low", ["computer"]),
    EMERGENCY_SAFE_STARTED: _e("Emergency shutdown started", "warn", "high", ["warning"]),
    EMERGENCY_SAFE_ABORTED: _e("Emergency shutdown ABORTED", "warn", "high", ["warning"]),
    EMERGENCY_SAFE_CUT: _e("Emergency shutdown: cutting power", "critical", "max", ["rotating_light"]),
    EMERGENCY_INSTANT_CUT: _e("INSTANT CUT", "critical", "max", ["rotating_light", "electric_plug"]),
    EMERGENCY_ABORT: _e("Cut aborted", "ok", "default", ["white_check_mark"]),
    CONFIG_CHANGE: _e("Settings changed", "nominal", "min", ["gear"], notify=False),
    UPS_SETTING_CHANGED: _e("UPS setting changed", "nominal", "min", ["gear"], notify=False),
    BEEPER_CHANGED: _e("Beeper changed", "nominal", "min", ["bell"], notify=False),

    SERVICE_DIED: _e("Power-chain service down", "critical", "max", ["rotating_light"]),
    SERVICE_RECOVERED: _e("Service recovered", "ok", "default", ["white_check_mark"]),
    COLLECTOR_STARTED: _e("Monitoring started", "nominal", "min", ["zap"], notify=False),
    # Emitted by notify.py itself when its offline queue overflowed.
    QUEUE_OVERFLOW: _e("Notifications dropped while offline", "warn", "default", ["warning"]),

    EPISODE_START: _e("Episode opened", "warn", "low", ["hourglass"], notify=False),
    EPISODE_END: _e("Episode closed", "ok", "low", ["white_check_mark"], notify=False),
}


def meta(kind):
    """Never raises: an unknown key degrades to a plain, visible default
    rather than being silently dropped."""
    return CATALOG.get(kind, _e(kind.replace("_", " ").capitalize(),
                                "warn", "default", ["bell"]))


def label(kind):
    return meta(kind)["label"]


def should_notify(kind):
    return meta(kind)["notify"]


def self_test_verdict(prev_result, curr_result):
    """The event a finished self-test deserves, or None.

    Only a `... progress ...` -> final-value edge counts: ups.test.result
    keeps its last verdict for days, so reading the value alone would
    re-announce an old test on every restart. "Aborted" is a warning, not a
    pass -- somebody (or a mains drop) stopped it before it proved anything.
    ups.test.result is re-read only every pollfreq, so a test short enough to
    start and finish between two full polls is never seen "In progress";
    a dashboard start forces a full walk, which is why those are seen.
    """
    if not isinstance(prev_result, str) or not isinstance(curr_result, str):
        return None
    p, c = prev_result.lower(), curr_result.lower()
    if "progress" not in p or "progress" in c:
        return None
    if "passed" in c:
        return SELF_TEST_PASSED
    if "error" in c:
        return SELF_TEST_FAILED
    if "warning" in c or "abort" in c:
        return SELF_TEST_WARNING
    return None      # e.g. "No test initiated": nothing was concluded
