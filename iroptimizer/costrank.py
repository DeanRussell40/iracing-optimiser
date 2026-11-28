"""Cost ranking: which graphics settings cost this PC the most frames, measured one step at a time.

From the user's own settings, each candidate lowers one setting by one step (or one closely tied pair). All of
them run in one session against the user's settings: each visit plays 3 runs in a row after one sim restart, and
the visits go forwards then backwards, so heat and drift land on every candidate alike with the fewest restarts.

Each candidate gets a verdict from the usual A/B rules (abtest.compare: bigger than the noise and no overlap), and
the list is ranked by how much it raised the 1% low. "No measurable difference" is a real answer: that setting can
stay where it is for free.
"""
from __future__ import annotations

PER_LOAD = 3
CYCLES = 2
MAX_CANDIDATES = 8
CARS_UP_TO = 28  # about the field in the test replay; 64 is the sim's maximum


def _int(flat: dict, key: str) -> int | None:
    try:
        return int(float(flat.get(key.lower())))
    except (TypeError, ValueError):
        return None


def _down(flat: dict, key: str, label: str, minimum: int = 0, step: int = 1):
    v = _int(flat, key)
    if v is None or v - step < minimum:
        return None
    return label, {key: str(v - step)}


def candidates(flat: dict) -> list[tuple[str, dict]]:
    """(label, changes) for each setting that can go one step lower, in the order of the user's quality priority
    (anti-aliasing, shadows, then detail). flat: {lowercase key: value} of the race settings."""
    out = []
    aa, samples = _int(flat, "AntiAliasMethod"), _int(flat, "MSAASamples")
    if aa == 1 and samples and samples > 2:
        out.append((f"MSAA {samples}x to {samples // 2}x", {"MSAASamples": str(samples // 2)}))
    elif aa == 1:
        out.append(("MSAA off", {"AntiAliasMethod": "0"}))
    for c in (_down(flat, "ShaderQuality", "Shader quality one step lower"),
              _down(flat, "StaticShadowRes", "Shadow resolution one step lower"),
              _down(flat, "ShadowMapType", "Shadows off"),
              _down(flat, "CarDetail", "Car detail one step lower"),
              _down(flat, "ObjectDetail", "Track objects one step lower"),
              _down(flat, "ParticleDetail", "Particles one step lower")):
        if c:
            out.append(c)
    g, cr = _int(flat, "GrandstandDetail"), _int(flat, "CrowdDetail")
    pair = {}
    if g:
        pair["GrandstandDetail"] = str(g - 1)
    if cr:
        pair["CrowdDetail"] = str(cr - 1)
    if pair:
        out.append(("Grandstands and crowd one step lower", pair))
    # The one step up worth measuring: iRacing draws at most MaxCarsToDraw cars per view, and a race start can
    # have more cars than that in sight. Raising it is a quality gain, so the ranking shows what it would cost.
    mc = _int(flat, "MaxCarsToDraw")
    if mc is not None and mc < CARS_UP_TO:
        out.insert(0, (f"Cars drawn {mc} to {CARS_UP_TO}", {"MaxCarsToDraw": str(CARS_UP_TO)}))
    return out[:MAX_CANDIDATES]


def rank(comparisons: list[dict], names: dict[str, dict]) -> list[dict]:
    """Rows sorted by 1% low gained, real gains first. names: {variant name: changes}."""
    rows = []
    for c in comparisons:
        m = (c.get("metrics") or {}).get("low1_fps") or {}
        p = (c.get("metrics") or {}).get("frametime_p99_ms") or {}
        a, b = m.get("a"), m.get("b")
        gain = round(b - a, 1) if a is not None and b is not None else None
        rows.append({"name": c.get("variant"), "changes": names.get(c.get("variant"), {}), "verdict": c.get("verdict"),
                     "low1_gain": gain, "p99_a": p.get("a"), "p99_b": p.get("b"),
                     "real": c.get("verdict") == "better"})
    rows.sort(key=lambda r: (not r["real"], -(r["low1_gain"] or 0)))
    return rows


def lines(rows: list[dict]) -> list[str]:
    out = []
    for r in rows:
        g = r["low1_gain"]
        if r["real"]:
            out.append(f"{r['name']}: +{g:.0f} FPS on the 1% low (a real gain).")
        elif g is None:
            out.append(f"{r['name']}: not enough valid runs to tell.")
        elif r["verdict"] == "worse":
            out.append(f"{r['name']}: costs {abs(g):.0f} FPS on the 1% low.")
        else:
            out.append(f"{r['name']}: no measurable difference ({g:+.0f} FPS is within the noise), so it can stay.")
    return out
