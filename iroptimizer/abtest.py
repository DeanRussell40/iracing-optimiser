"""A/B comparison, FPS-cap prediction and the Smoothness-to-Graphics focus choice.

All pure functions over run metrics (see metrics.run_metrics), so they are tested on recorded data.

How a verdict is reached. Variants are run interleaved (A B B A A B …) on the same replay segment, so heat
soak and caching hit both sides equally. A change counts as real only when:
  * the median difference is larger than the run-to-run noise of either side (and at least MIN_EFFECT_PCT), and
  * the runs don't overlap: every run of the better side beats the median of the other.
Anything else is "no measurable difference", which is a valid answer, not a failure.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

MIN_EFFECT_PCT = 3.0

# What decides a verdict: what you feel. Pacing and average FPS are reported but never decide on their own: a
# 0.2 ms pacing change with the same 1% lows and stutters is not something anyone can feel.
PRIMARY_KEYS = ("frametime_p99_ms", "low1_fps", "stutters_per_min")

# (key, label, lower_is_better) in order of importance for smoothness
COMPARE_KEYS = (
    ("frametime_p99_ms", "Frame time p99", True),
    ("low1_fps", "1% low", False),
    ("stutters_per_min", "Stutters per minute", True),
    ("pacing_error_ms", "Pacing error", True),
    ("avg_fps", "Average FPS", False),
)


def _spread_pct(vals: list[float]) -> float:
    m = median(vals)
    return (max(vals) - min(vals)) / m * 100 if m else 0.0


def compare(baseline: list[dict], other: list[dict], base_name: str = "A", other_name: str = "B") -> dict:
    """Compares two lists of per-run metrics. Returns per-metric deltas and an overall verdict for other vs baseline."""
    out = {"baseline": base_name, "variant": other_name, "runs": [len(baseline), len(other)], "metrics": {}}
    for key, label, lower in COMPARE_KEYS:
        a = [r[key] for r in baseline if r.get(key) is not None]
        b = [r[key] for r in other if r.get(key) is not None]
        if len(a) < 2 or len(b) < 2:
            continue
        ma, mb = median(a), median(b)
        if key == "stutters_per_min":
            # counts near zero make % meaningless: compare in absolute terms (≥1 per minute and no overlap)
            delta_pct = None
            better = mb < ma and (ma - mb) >= 1 and max(b) < min(a)
            worse = mb > ma and (mb - ma) >= 1 and min(b) > max(a)
        else:
            delta_pct = (mb - ma) / ma * 100 if ma else 0.0
            noise = max(MIN_EFFECT_PCT, _spread_pct(a), _spread_pct(b))
            improved = -delta_pct if lower else delta_pct
            sep_better = (max(b) < ma) if lower else (min(b) > ma)
            sep_worse = (min(b) > ma) if lower else (max(b) < ma)
            better = improved > noise and sep_better
            worse = -improved > noise and sep_worse
        out["metrics"][key] = {"label": label, "a": ma, "b": mb, "delta_pct": delta_pct,
                               "result": "better" if better else "worse" if worse else "same"}
    res = [m["result"] for k, m in out["metrics"].items() if k in PRIMARY_KEYS]
    if "worse" in res and "better" in res:
        verdict = "mixed"
    elif "better" in res:
        verdict = "better"
    elif "worse" in res:
        verdict = "worse"
    else:
        verdict = "same"
    out["verdict"] = verdict
    out["verdict_text"] = _verdict_text(out, other_name, base_name)
    return out


def _verdict_text(c: dict, b: str, a: str) -> str:
    m = c["metrics"]
    parts = []
    if "low1_fps" in m:
        parts.append(f"1% low {m['low1_fps']['a']:.0f} to {m['low1_fps']['b']:.0f} fps")
    if "frametime_p99_ms" in m:
        parts.append(f"p99 {m['frametime_p99_ms']['a']:.1f} to {m['frametime_p99_ms']['b']:.1f} ms")
    if "stutters_per_min" in m:
        parts.append(f"stutters {m['stutters_per_min']['a']:.1f} to {m['stutters_per_min']['b']:.1f}/min")
    nums = "; ".join(parts)
    v = c["verdict"]
    if v == "better":
        return f"{b} is better than {a}: {nums}."
    if v == "worse":
        return f"{b} is worse than {a}: {nums}."
    if v == "mixed":
        return f"{b} is better on some measures and worse on others: {nums}."
    return f"No meaningful difference between {b} and {a} (differences are within run-to-run noise): {nums}."


# ---------------------------------------------------------------------------- FPS cap prediction

CAP_CANDIDATES = (60, 72, 80, 90, 100, 110, 120, 138, 141, 144, 157, 160, 175, 180, 200, 220, 237)
HOLD_PCT = 99.0      # a cap is "held" when at least this share of frames finish inside the cap interval
HOLD_SLACK = 1.02    # 2% tolerance on the interval


def predict_caps(frametimes_ms: list[float], refresh_hz: float | None = None) -> dict:
    """Which frame caps this PC would hold, from an UNCAPPED run's frame times.

    Conservative: a cap also frees GPU time, which usually trims the slow tail further, so a cap predicted to
    hold will hold, and one just above it might too. Verify the chosen cap with a capped run.
    """
    if len(frametimes_ms) < 100:
        return {"available": False}
    n = len(frametimes_ms)
    top = (refresh_hz - 3) if refresh_hz else max(CAP_CANDIDATES)
    rows = []
    for cap in CAP_CANDIDATES:
        if cap > top:
            continue
        limit = 1000 / cap * HOLD_SLACK
        held = 100 * sum(1 for f in frametimes_ms if f <= limit) / n
        rows.append({"cap": cap, "held_pct": held, "holds": held >= HOLD_PCT})
    holding = [r["cap"] for r in rows if r["holds"]]
    best = max(holding) if holding else None
    return {"available": True, "rows": rows, "best": best, "refresh": refresh_hz,
            "text": (f"Your PC holds a cap of {best} FPS on this replay ({HOLD_PCT:.0f}% of frames inside "
                     f"{1000 / best:.1f} ms). Higher caps would be missed in busy moments." if best else
                     "No cap from 60 FPS up is held on this replay: the PC can't keep a steady frame rate here.")}


# ---------------------------------------------------------------------------- focus slider

FOCUS_LABELS = (
    "Highest frame rate your PC holds, at the graphics level it needs",
    "Smooth first, small graphics gains where free",
    "Balanced",
    "Graphics first, frame rate held steady at a lower cap",
    "Most graphics your PC can run without stutter",
)


@dataclass
class Candidate:
    """A measured combination: the cap it holds and how many graphics steps it runs."""
    name: str
    cap_fps: float
    graphics_level: float      # sum of settings steps above minimum, per the user's ladder
    stable: bool               # holds its cap and no more stutters than baseline
    low1_fps: float | None = None
    detail: dict = field(default_factory=dict)


def choose(cands: list[Candidate], focus: int) -> Candidate | None:
    """Picks among measured candidates. focus 0 = Smoothness … 4 = Graphics. Stability is a floor, never traded."""
    ok = [c for c in cands if c.stable]
    if not ok:
        return None
    w = max(0, min(4, int(focus))) / 4
    caps = [c.cap_fps for c in ok]
    gfx = [c.graphics_level for c in ok]
    norm = lambda v, lo, hi: 0.5 if hi == lo else (v - lo) / (hi - lo)
    def score(c: Candidate):
        s = (1 - w) * norm(c.cap_fps, min(caps), max(caps)) + w * norm(c.graphics_level, min(gfx), max(gfx))
        return (round(s, 9), c.low1_fps or 0)
    return max(ok, key=score)
