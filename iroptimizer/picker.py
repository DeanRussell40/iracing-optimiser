"""The floors-then-quality picker: smoothness is pass/fail, then the best-looking option wins, then FPS.

Inputs are earlier tests on this PC: the floor check (floor_check.json) and the cost ranking (cost_rank.json).

1. Pick which settings to lower. Only settings the cost ranking proved cost frames are candidates, lowered in
   the user's quality order from least to most important (crowd first, MSAA last), until the predicted 1% low
   reaches the preferred FPS with a margin. Gains don't add up exactly, so each is counted at GAIN_SHARE.
   When the floor check found that even the lowest graphics can't reach the preferred FPS, nothing is lowered:
   lowering would cost looks for nothing, and the answer is a lower locked frame rate instead.
2. Measure the options in one session against the user's settings: the picked settings uncapped, the picked
   settings capped at 120, and the user's settings capped at 80 (both caps divide 240 Hz evenly). iRacing's own
   frame limiter is used, so no other app is needed.
3. Judge: smoothness must pass (on the second-worst run); passing options rank by quality, then FPS, with one
   exception: an option that reaches the preferred FPS wins over a better-looking one that doesn't, when what it
   gives up is only low-priority detail. Every option shows its FPS and frame time so the trade is visible.

Not yet: raising lowered settings back one at a time (the back-off step), V-Sync variants, render scale.
A pick under the preferred FPS is a suggestion until the user drives both and chooses.
"""
from __future__ import annotations

from .floors import DEFAULT_FLOOR, floor_low1

GAIN_SHARE = 0.7
MARGIN = 1.05
CAP_HIGH, CAP_LOW = 120, 80

# Least important first: the order things are given up in (Dean's order: render scale, MSAA, shadows,
# reflections, then grass and crowd; detail settings sit between shadows and crowd).
GIVE_UP_ORDER = ("GrandstandDetail", "CrowdDetail", "ParticleDetail", "ObjectDetail", "CarDetail",
                 "ShaderQuality", "StaticShadowRes", "ShadowMapType", "MSAASamples", "AntiAliasMethod")
LOW_PRIORITY = {"GrandstandDetail", "CrowdDetail", "ParticleDetail", "ObjectDetail"}

# Smoothness pass rules (judged on the second-worst run). These are starting values, to be tuned on real runs.
MAX_STUTTERS_PER_MIN = 1.0
MIN_ON_TIME_PCT_CAPPED = 99.0
MAX_P99_OVER_P50_UNCAPPED = 1.6


def _rank_of(changes: dict) -> int:
    return min((GIVE_UP_ORDER.index(k) for k in changes if k in GIVE_UP_ORDER), default=len(GIVE_UP_ORDER))


def choose(floor: dict | None, rank_rows: list[dict], preferred: float = DEFAULT_FLOOR) -> dict:
    """Which settings to lower. Returns {"changes", "lowered": [names], "predicted", "reason"}."""
    floor = floor or {}
    yours = floor.get("yours")
    if floor.get("verdict") == "not_reachable":
        return {"changes": {}, "lowered": [], "predicted": yours,
                "reason": f"Even the lowest graphics can't reach {preferred:.0f}, so nothing is lowered; the "
                          f"options are about a steady lower frame rate instead."}
    if floor.get("verdict") == "met":
        return {"changes": {}, "lowered": [], "predicted": yours,
                "reason": f"Your settings already hold {preferred:.0f}, so nothing needs lowering."}
    gains = [r for r in rank_rows if r.get("real") and (r.get("low1_gain") or 0) > 0 and r.get("changes")
             and "MaxCarsToDraw" not in r["changes"]]
    gains.sort(key=lambda r: (_rank_of(r["changes"]), -(r["low1_gain"] or 0)))
    changes, lowered = {}, []
    predicted = yours
    for r in gains:
        if predicted is not None and predicted >= preferred * MARGIN:
            break
        changes.update(r["changes"])
        lowered.append(r["name"])
        if predicted is not None:
            predicted += r["low1_gain"] * GAIN_SHARE
    if not lowered:
        reason = "The cost ranking found nothing that costs measurable frames, so nothing is lowered."
    else:
        reason = (f"Lowered what costs frames, least important first, until the predicted 1% low reached "
                  f"{preferred:.0f}: " + ", ".join(lowered) + ".")
    return {"changes": changes, "lowered": lowered, "predicted": predicted, "reason": reason}


def cap_changes(fps: int) -> dict:
    return {"LimitFrameRate": "1", "DesiredFPSLimit": str(fps)}


def smooth(runs: list[dict], capped: bool) -> tuple[bool, str]:
    """Pass/fail on the second-worst run of each measure."""
    if len(runs) < 3:
        return False, "not enough valid runs"

    def worst2(key, high_is_bad=True):
        v = sorted((r.get(key) for r in runs if r.get(key) is not None), reverse=high_is_bad)
        return v[1] if len(v) >= 4 else (v[0] if v else None)
    st = worst2("stutters_per_min")
    if st is not None and st > MAX_STUTTERS_PER_MIN:
        return False, f"{st:.1f} stutters a minute"
    if capped:
        ot = worst2("on_time_pct", high_is_bad=False)
        if ot is not None and ot < MIN_ON_TIME_PCT_CAPPED:
            return False, f"only {ot:.1f}% of frames on time"
    else:
        ratios = [r["frametime_p99_ms"] / r["frametime_p50_ms"] for r in runs
                  if r.get("frametime_p99_ms") and r.get("frametime_p50_ms")]
        ratios.sort(reverse=True)
        rt = ratios[1] if len(ratios) >= 4 else (ratios[0] if ratios else None)
        if rt is not None and rt > MAX_P99_OVER_P50_UNCAPPED:
            return False, f"slow frames {rt:.1f}x the typical frame time"
    return True, "smooth"


def _med(runs, key):
    v = sorted(r[key] for r in runs if r.get(key) is not None)
    return v[len(v) // 2] if v else None


def judge(options: list[dict], per_variant: dict[str, list[dict]], preferred: float = DEFAULT_FLOOR) -> dict:
    """options: [{"name", "quality" (higher looks better), "capped", "cap", "low_priority_only"}] in measured order.
    Returns {"rows", "pick", "text"}."""
    rows = []
    for o in options:
        runs = per_variant.get(o["name"]) or []
        ok, why = smooth(runs, o["capped"])
        low1 = floor_low1([r.get("low1_fps") for r in runs])
        rows.append(o | {"smooth": ok, "why": why, "low1": low1, "avg_fps": _med(runs, "avg_fps"),
                         "frame_ms": _med(runs, "frametime_p50_ms"),
                         "latency_ms": _med(runs, "display_latency_p50_ms"),
                         # a held cap can't show a 1% low at the cap, so a smooth capped run reaches its cap
                         "reaches": (ok and o.get("cap", 0) >= preferred) if o["capped"] else
                                    (low1 is not None and low1 >= preferred)})
    passing = [r for r in rows if r["smooth"]]
    if not passing:
        worst = ", ".join(f"{r['name']}: {r['why']}" for r in rows)
        return {"rows": rows, "pick": None,
                "text": f"No option passed the smoothness test ({worst}). Your settings stay as they are."}

    def key(r):
        return (r["quality"], r["avg_fps"] or 0)
    best = max(passing, key=key)
    fast = [r for r in passing if r["reaches"]]
    if not best["reaches"] and fast:
        alt = max(fast, key=key)
        if alt["quality"] >= best["quality"] - 1 and alt.get("low_priority_only"):
            best = alt
    fps = f"{best['avg_fps']:.0f} FPS" if best["avg_fps"] else "?"
    text = f"Pick: {best['name']} ({fps}, smooth)."
    if not best["reaches"]:
        text += (f" It's under {preferred:.0f}. Before keeping it, drive a few laps on it and on the best "
                 f"{preferred:.0f} option, and choose by feel.")
    return {"rows": rows, "pick": best["name"], "text": text}


def row_line(r: dict) -> str:
    bits = [f"{r['avg_fps']:.0f} FPS" if r["avg_fps"] else "? FPS"]
    if r["low1"] is not None:
        bits.append(f"1% low {r['low1']:.0f}")
    if r["frame_ms"]:
        bits.append(f"{r['frame_ms']:.1f} ms a frame")
    if r["latency_ms"]:
        bits.append(f"{r['latency_ms']:.0f} ms to screen")
    return f"{r['name']}: " + ", ".join(bits) + (" — smooth" if r["smooth"] else f" — not smooth ({r['why']})")
