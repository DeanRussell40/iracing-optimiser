"""The floor probe: can graphics settings alone get this PC to the user's minimum FPS?

It runs the lowest graphics against the user's own on the same replay. If even the lowest graphics stay under the
minimum, the limit is the CPU or the sim and no graphics choice will reach it, so the picker shouldn't search. If
they clear it, the gap between the two is the room the picker works in.

Pass/fail uses the second-worst run, not the median: a minimum that holds only on an average run fails on a bad
one. With fewer than 4 runs that is the worst run.
"""
from __future__ import annotations

# Lowest GPU cost, keeping what changes how the sim drives or looks at a basic level: mirrors, HDR, cars drawn,
# field of view and the view toggles are left alone. Each key changes the race and replay copies (iniswitch.expand).
LOWEST = {
    "AntiAliasMethod": "0",
    "ShaderQuality": "0",
    "ShadowMapType": "0",
    "DynamicShadowMaps": "0",
    "StaticShadowRes": "0",
    "DNSMEnable": "0",
    "SSAO": "0",
    "SSRLevel": "0",
    "NumDynamicCubemaps": "0",
    "NumFixedCubemaps": "0",
    "ParticleDetail": "0",
    "ObjectDetail": "0",
    "GrandstandDetail": "0",
    "CrowdDetail": "0",
    "PitObjectDetail": "0",
    "CarDetail": "0",
    "FoliageDetail": "0",
    "WeekendDetail": "0",
    "TwoPassTrees": "0",
    "HeadlightLevel": "0",
    "SkyRefreshRate": "0",
    "Sharpening": "0",
    "HeatHaze": "0",
    "DepthOfField": "0",
}
DEFAULT_FLOOR = 120


def floor_low1(lows: list[float]) -> float | None:
    """The 1% low that has to clear the floor: the second-worst run (the worst with fewer than 4 runs)."""
    v = sorted(x for x in lows if x is not None)
    if not v:
        return None
    return v[1] if len(v) >= 4 else v[0]


def verdict(lows_yours: list[float], lows_lowest: list[float], floor: float = DEFAULT_FLOOR) -> dict:
    yours, lowest = floor_low1(lows_yours), floor_low1(lows_lowest)
    if yours is None or lowest is None:
        return {"verdict": "unknown", "text": "The floor check didn't get enough valid runs to decide."}
    if yours >= floor:
        v, text = "met", (f"Your own settings already hold {floor:.0f} FPS (1% low {yours:.0f} on a bad run), so "
                          f"the picker can spend the room on looks.")
    elif lowest >= floor:
        v, text = "reachable", (f"{floor:.0f} FPS is reachable with graphics: lowest settings hold a 1% low of "
                                f"{lowest:.0f}, yours {yours:.0f}. The picker will find what costs the most.")
    else:
        v, text = "not_reachable", (f"Graphics alone can't reach {floor:.0f} FPS on this replay: even the lowest "
                                    f"settings only hold a 1% low of {lowest:.0f} (yours {yours:.0f}). The limit "
                                    f"here is the CPU or the sim, so the picker should aim for smoothness at a "
                                    f"lower locked frame rate instead.")
    return {"verdict": v, "text": text, "yours": yours, "lowest": lowest, "floor": floor}


def lows_by_variant(summary: dict) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for r in summary.get("runs") or []:
        w, m = r.get("window") or {}, r.get("metrics") or {}
        if w.get("warmup") or not w.get("valid", True) or m.get("low1_fps") is None:
            continue
        out.setdefault(w.get("variant"), []).append(m["low1_fps"])
    return out
