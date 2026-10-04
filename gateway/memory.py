"""System memory signals (macOS) for pressure-based eviction. Cheap (about 3 ms) and injectable for tests.

Metal memory is invisible to per-process RSS, so the budget uses each backend's declared ``est_mem_gb``; this probe is the
reality check: how much memory macOS says is free, and whether swap is growing.
"""
from __future__ import annotations

import re
import subprocess

_UNIT = {"M": 1 / 1024, "G": 1.0}


def parse_swapusage(text: str) -> tuple[float, float] | None:
    """'total = 7168.00M  used = 6376.75M  free = 791.25M  (encrypted)' -> (used_gb, total_gb)."""
    m = re.search(r"total = ([\d.]+)([MG]).*?used = ([\d.]+)([MG])", text)
    if not m:
        return None
    return float(m.group(3)) * _UNIT[m.group(4)], float(m.group(1)) * _UNIT[m.group(2)]


def macos_probe() -> dict:
    """{'free_pct': int | None, 'swap_used_gb': float | None, 'swap_total_gb': float | None}"""
    out = {"free_pct": None, "swap_used_gb": None, "swap_total_gb": None}
    try:
        r = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_level", "vm.swapusage"], capture_output=True, text=True, timeout=2)
        lines = r.stdout.strip().splitlines()
        if lines and lines[0].strip().isdigit():
            out["free_pct"] = int(lines[0])
        if len(lines) > 1:
            sw = parse_swapusage(lines[1])
            if sw:
                out["swap_used_gb"], out["swap_total_gb"] = round(sw[0], 2), round(sw[1], 2)
    except (OSError, subprocess.SubprocessError):
        pass
    return out
