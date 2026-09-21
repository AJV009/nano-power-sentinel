"""Flatten a live snapshot into a `sample` table row."""


def flatten(snap):
    ups = snap["ups"]
    box_blk = snap.get("box") or {}
    box = box_blk.get("last_vitals") or {}
    gpu = box.get("gpu") or {}
    cpu = box.get("cpu") or {}
    mem = box.get("mem") or {}
    nano = snap.get("nano") or {}
    return {
        "ts": snap["ts"],
        "ups_status": ups.get("status"),
        "charge": ups.get("charge"),
        "runtime": ups.get("runtime"),
        "batt_v": ups.get("batt_v"),
        "load_pct": ups.get("load_pct"),
        "input_v": ups.get("input_v"),
        "watts": ups.get("watts"),
        "box_state": box_blk.get("state"),
        "box_gpu_w": gpu.get("power_w"),
        "box_gpu_c": gpu.get("temp_junction_c"),
        "box_cpu_c": cpu.get("temp_c"),
        "box_nvme_c": (box.get("nvme") or {}).get("temp_c"),
        "box_gpu_busy": gpu.get("busy_pct"),
        "box_vram_mb": gpu.get("vram_used_mb"),
        "box_ram_gb": mem.get("used_gb"),
        "box_cpu_util": cpu.get("util_pct"),
        "nano_cpu_c": nano.get("cpu_c"),
        "nano_ram_mb": nano.get("ram_used_mb"),
    }
