"""Frame-pacing metrics for one measured run, and repeatability across runs.

Everything here is a pure function of frame records so it can be tested on recorded data.

Definitions (stated because "1% low" means different things in different tools):
* frame time p99 = 99th percentile of the time between presents (ms). Lower is better.
* 1% low FPS = 1000 / frame time p99.
* display p99 = 99th percentile of the time between on-screen changes, for displayed frames only.
* pacing error = mean absolute difference between consecutive frame times (ms). Zero means every frame
  took the same time; a high value with a good average is what "not smooth" feels like.
* stutter = a frame more than 2x the median of the previous 60 frames and at least 8 ms longer.
"""
from __future__ import annotations

import math
from collections import Counter, deque
from statistics import median

from .frames import Frame

# Gate for milestone 1: the same replay segment must give p99 frame times within this spread
REPEATABILITY_GATE_PCT = 5.0

PRESENT_MODE_NOTES = {
    "independent": ("good", "Independent Flip: the game's frames go straight to the display. G-Sync/VRR can "
                            "work and there is no extra compositor latency."),
    "composed_flip": ("warn", "Composed Flip: Windows' compositor (DWM) assembles each frame. This usually adds "
                              "about one refresh of latency and G-Sync may not engage. With a window spanning "
                              "three separate monitors this is expected, because Independent Flip only works on "
                              "one display output."),
    "copy": ("bad", "Copy mode: the slowest, highest-latency path. Usually caused by an overlay, an old "
                    "Windows setting or a non-flip swap chain."),
    "unknown": ("warn", "PresentMon could not identify the present path."),
}


def _pct(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return sorted_vals[lo] if lo == hi else sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def classify_present_mode(mode: str | None) -> str:
    m = (mode or "").lower()
    if "independent flip" in m:
        return "independent"
    if "composed" in m and "flip" in m:
        return "composed_flip"
    if "copy" in m:
        return "copy"
    return "unknown"


def present_mode_summary(frames: list[Frame]) -> dict:
    modes = Counter(fr.present_mode for fr in frames if fr.present_mode)
    n = sum(modes.values())
    if not n:
        return {"available": False}
    dominant, count = modes.most_common(1)[0]
    kind = classify_present_mode(dominant)
    tearing = Counter(fr.allows_tearing for fr in frames if fr.allows_tearing is not None)
    sync = Counter(fr.sync_interval for fr in frames if fr.sync_interval is not None)
    level, note = PRESENT_MODE_NOTES[kind]
    return {
        "available": True,
        "dominant": dominant,
        "dominant_pct": 100 * count / n,
        "kind": kind,
        "level": level,
        "note": note,
        "modes": {k: 100 * v / n for k, v in modes.most_common()},
        "allows_tearing_pct": (100 * tearing.get(True, 0) / sum(tearing.values())) if tearing else None,
        "sync_interval": sync.most_common(1)[0][0] if sync else None,
    }


def count_stutters(fts: list[float]) -> int:
    win: deque = deque(maxlen=60)
    n = 0
    for ft in fts:
        if len(win) >= 30:
            med = median(win)
            if ft > 2 * med and ft - med > 8:
                n += 1
        win.append(ft)
    return n


def run_metrics(frames: list[Frame]) -> dict:
    """Metrics for one run. Returns {} when there are too few frames to mean anything."""
    fts = [fr.frametime for fr in frames]
    if len(fts) < 100:
        return {}
    s = sorted(fts)
    mean = sum(fts) / len(fts)
    p50, p99, p999 = _pct(s, 50), _pct(s, 99), _pct(s, 99.9)
    pacing = sum(abs(b - a) for a, b in zip(fts, fts[1:])) / (len(fts) - 1)
    out = {
        "frames": len(fts),
        "seconds": sum(fts) / 1000,
        "avg_fps": 1000 / mean,
        "frametime_p50_ms": p50,
        "frametime_p99_ms": p99,
        "frametime_p999_ms": p999,
        "frametime_max_ms": s[-1],
        "low1_fps": 1000 / p99,
        "low01_fps": 1000 / p999,
        "pacing_error_ms": pacing,
        "stutters": count_stutters(fts),
    }
    disp_known = [fr for fr in frames if fr.displayed is not None]
    if disp_known:
        out["not_displayed_pct"] = 100 * sum(1 for fr in disp_known if not fr.displayed) / len(disp_known)
    dc = sorted(fr.display_change for fr in frames if fr.display_change and fr.displayed is not False)
    if len(dc) >= 100:
        out["display_p50_ms"] = _pct(dc, 50)
        out["display_p99_ms"] = _pct(dc, 99)
    lat = sorted(fr.display_latency for fr in frames if fr.display_latency is not None)
    if len(lat) >= 100:
        out["display_latency_p50_ms"] = _pct(lat, 50)
        out["display_latency_p99_ms"] = _pct(lat, 99)
    bound = [fr for fr in frames if fr.gpu_busy is not None]
    if bound:
        out["gpu_bound_pct"] = 100 * sum(1 for fr in bound if fr.gpu_busy >= 0.9 * fr.frametime) / len(bound)
        out["gpu_busy_p50_ms"] = _pct(sorted(fr.gpu_busy for fr in bound), 50)
    return out


# Metrics compared across repeats: (key, label, lower_is_better)
REPEAT_KEYS = (
    ("frametime_p99_ms", "Frame time p99 (ms)", True),
    ("frametime_p50_ms", "Frame time median (ms)", True),
    ("avg_fps", "Average FPS", False),
    ("low1_fps", "1% low FPS", False),
    ("pacing_error_ms", "Pacing error (ms)", True),
    ("display_p99_ms", "Display interval p99 (ms)", True),
    ("stutters", "Stutters", True),
)


def _monotonic(vals: list[float]) -> bool:
    if len(vals) < 4:
        return False
    inc = all(b > a for a, b in zip(vals, vals[1:]))
    dec = all(b < a for a, b in zip(vals, vals[1:]))
    return inc or dec


def repeatability(runs: list[dict], gate_pct: float = REPEATABILITY_GATE_PCT) -> dict:
    """Spread of each metric across repeated runs of the same segment.

    spread % = (max - min) / median x 100. The gate is on frame time p99: if the same workload moves more than
    gate_pct between runs, any setting whose effect is smaller than that cannot be measured.
    """
    good = [r for r in runs if r]
    out: dict = {"runs": len(good), "gate_pct": gate_pct, "metrics": {}}
    for key, label, _lower in REPEAT_KEYS:
        vals = [r[key] for r in good if r.get(key) is not None]
        if len(vals) < 2:
            continue
        med = median(vals)
        spread = (max(vals) - min(vals)) / med * 100 if med else None
        mean = sum(vals) / len(vals)
        sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1))
        out["metrics"][key] = {"label": label, "values": vals, "median": med, "min": min(vals), "max": max(vals),
                               "spread_pct": spread, "cv_pct": (sd / mean * 100) if mean else None,
                               "trend": _monotonic(vals)}
    p99 = out["metrics"].get("frametime_p99_ms")
    if len(good) < 3 or not p99 or p99["spread_pct"] is None:
        out["verdict"] = "insufficient"
        out["verdict_text"] = "Not enough valid runs to judge repeatability (need at least 3)."
    elif p99["spread_pct"] <= gate_pct:
        out["verdict"] = "pass"
        out["verdict_text"] = (f"PASS: frame time p99 varied {p99['spread_pct']:.1f}% across {len(good)} runs "
                               f"(gate {gate_pct:.0f}%). Differences between settings larger than this can be measured.")
    else:
        out["verdict"] = "fail"
        out["verdict_text"] = (f"FAIL: frame time p99 varied {p99['spread_pct']:.1f}% across {len(good)} runs "
                               f"(gate {gate_pct:.0f}%). The noise has to be found before settings can be compared.")
        if p99["trend"]:
            out["verdict_text"] += (" The values move steadily in one direction, which points at heat soak or "
                                    "shader/texture caching rather than random noise.")
    return out
