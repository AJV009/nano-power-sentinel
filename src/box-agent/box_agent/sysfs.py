"""Wrapped sysfs/procfs reads.  Every function returns None instead of raising."""

import glob
import os


def read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except Exception:
        return None


def as_float(path, scale=1.0, digits=2):
    raw = read(path)
    if raw is None:
        return None
    try:
        return round(float(raw) / scale, digits)
    except Exception:
        return None


def as_int(path):
    raw = read(path)
    if raw is None:
        return None
    try:
        return int(float(raw))
    except Exception:
        return None


def hwmon_by_name():
    """Map chip name -> [dirs].  Never trust a fixed hwmon index; they move."""
    found = {}
    for path in glob.glob("/sys/class/hwmon/hwmon*"):
        name = read(os.path.join(path, "name"))
        if name:
            found.setdefault(name, []).append(path)
    return found


def temp_by_label(hwmon_dir, label):
    """Find tempN_input whose tempN_label matches; indices vary by kernel."""
    if not hwmon_dir:
        return None
    for i in range(1, 16):
        lab = read("%s/temp%d_label" % (hwmon_dir, i))
        if lab and lab.lower() == label.lower():
            return as_float("%s/temp%d_input" % (hwmon_dir, i), 1000.0, 1)
    return None


def first_temp(hwmon_dir):
    if not hwmon_dir:
        return None
    return as_float("%s/temp1_input" % hwmon_dir, 1000.0, 1)
