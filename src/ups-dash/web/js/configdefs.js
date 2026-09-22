/* What the CONFIG tab exposes, and what it deliberately does not.
   Mirrors DASHBOARD.md §09 -- keep the two in step. */

export const TIER1 = [
  { key: "reserve_pct", name: "Reserve after hibernate", unit: "%",
    machine: "box", min: 10, max: 80, step: 1,
    help: "Charge that must still remain once the hibernate write has finished." },
  { key: "wake_charge_pct", name: "Wake when charge reaches", unit: "%",
    machine: "jetson", min: 20, max: 100, step: 1,
    help: "The box is held powered off until the pack refills to here." },
  { key: "mains_stable_sec", name: "Mains must hold for", unit: "s",
    machine: "jetson", min: 30, max: 1800, step: 10,
    help: "Uninterrupted mains required before a wake fires. This is what stops a flapping supply from power-cycling the box repeatedly." },
];

export const TIER2 = [
  { key: "safety_sec", name: "Safety margin", unit: "s",
    machine: "box", min: 0, max: 300, step: 5,
    help: "Extra slack added on top of the estimated hibernate cost." },
  { key: "write_rate_gbps", name: "Hibernate write rate", unit: "GB/s",
    machine: "box", min: 0.1, max: 5, step: 0.05,
    help: "Used to estimate how long the hibernate image takes to write. Conservative is safe; optimistic is dangerous." },
  { key: "fixed_overhead_sec", name: "Fixed overhead", unit: "s",
    machine: "box", min: 0, max: 120, step: 1,
    help: "Freeze, snapshot and power-off time that does not scale with image size." },
  { key: "comms_loss_limit_sec", name: "Comms-loss limit", unit: "s",
    machine: "box", min: 15, max: 600, step: 5,
    help: "On battery and unable to read the UPS for this long, the box hibernates anyway rather than gambling on a sensor it cannot see." },
  { key: "wake_tries", name: "Wake retries", unit: "",
    machine: "jetson", min: 1, max: 20, step: 1,
    help: "How many magic packets to send before giving up on waking the box." },
  { key: "wake_interval_sec", name: "Wake retry interval", unit: "s",
    machine: "jetson", min: 5, max: 300, step: 5,
    help: "Gap between wake attempts." },
];

/* Shown so the reasoning is not lost, never editable from a web page. */
export const TIER3 = [
  { name: "NUT pollinterval", value: "2 s", machine: "jetson",
    why: "Only refreshes status bits and timers — load, voltage and runtime follow pollfreq instead. The 2026-09-20 failure was a stall with no USB disconnect, which NUT 2.7.4 cannot recover from. /dev/hidraw1 “vanishing” was normal: usbhid-ups detaches the kernel HID driver while it holds the device." },
  { name: "Debounce", value: "3 samples", machine: "box",
    why: "Changes trigger semantics rather than a threshold." },
  { name: "Settle time", value: "30 s", machine: "box",
    why: "Compensates a hardware gauge transient after transfer; not a preference." },
  { name: "Box identity", value: "from site.env", machine: "jetson",
    why: "Set in /etc/ups-dash/site.env (BOX_IP, BOX_MAC), not here. Identity, not tuning." },
];

/* The one genuinely dangerous combination in the whole surface. */
export const WAKE_RESERVE_MARGIN = 10;

export function validate(values) {
  const errs = [];
  const r = values.reserve_pct, w = values.wake_charge_pct;
  if (typeof r === "number" && typeof w === "number"
      && w < r + WAKE_RESERVE_MARGIN) {
    errs.push(`Wake (${w}%) must be at least ${WAKE_RESERVE_MARGIN} points above reserve (${r}%). Otherwise the box wakes into a charge where the governor immediately wants to hibernate again — a wake/hibernate flapping loop.`);
  }
  return errs;
}
