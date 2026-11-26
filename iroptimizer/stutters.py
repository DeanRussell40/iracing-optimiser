"""What was going on at each stutter: busy apps, driver time (DPC and interrupts) and whole-PC CPU.

The background sampler takes one sample a second, so this shows what was busy in the second around each stutter, not
proof of cause. It is still enough to separate the three usual suspects:
  * an app that wakes up (its CPU during stutters is well above its own average),
  * a driver (DPC or interrupt time jumps: network, audio, USB, storage or GPU drivers),
  * neither, which points inside iRacing (texture or shader loading, cars coming into view) or at the GPU.
"""
from __future__ import annotations

from statistics import mean

MAX_EVENTS = 20
APP_MIN_CORES = 0.10   # at least this busy during stutters to be named
APP_LIFT = 1.5         # and this many times its usual load
DPC_MIN_PCT = 2.0      # driver time worth naming


def _sample_at(samples: list[dict], t: float) -> dict | None:
    """The sample that covers time t: the first one taken at or after it (each covers the second before it)."""
    for s in samples:
        if s["t"] >= t:
            return s if s["t"] - t <= 2.0 else None
    return None


def _driver_pct(s: dict) -> float | None:
    sy = s.get("sys") or {}
    if sy.get("dpc_pct") is None and sy.get("interrupt_pct") is None:
        return None
    return (sy.get("dpc_pct") or 0) + (sy.get("interrupt_pct") or 0)


def attribute(events: list[dict], samples: list[dict], t0: float | None = None, t1: float | None = None) -> dict:
    """events: [{"t": epoch, "ms": frame time, "run": label, "at_s": seconds into the run}].
    samples: BackgroundSampler samples. t0/t1 bound the measured time for the "usual" baseline."""
    base = [s for s in samples if (t0 is None or s["t"] >= t0) and (t1 is None or s["t"] <= t1 + 1)]
    out = {"count": len(events), "events": [], "apps": [], "driver": None, "available": bool(base)}
    if not events:
        out["text"] = "No stutters in the measured runs."
        return out
    if not base:
        out["text"] = "No background samples were recorded, so stutters can't be attributed."
        return out
    hit = []
    for ev in events:
        s = _sample_at(base, ev["t"])
        if s is None:
            continue
        hit.append(s)
        if len(out["events"]) < MAX_EVENTS:
            top = sorted(s["procs"].items(), key=lambda kv: -kv[1])[:3]
            out["events"].append({**ev, "top": [{"name": n, "cores": round(c, 2)} for n, c in top],
                                  "driver_pct": _driver_pct(s), "cpu_pct": (s.get("sys") or {}).get("cpu_pct")})
    if not hit:
        out["text"] = "The stutters fell outside the sampled time, so they can't be attributed."
        return out
    uniq = list({id(s): s for s in hit}.values())
    names = {n for s in base for n in s["procs"]}
    apps = []
    for n in names:
        usual = sum(s["procs"].get(n, 0) for s in base) / len(base)
        during = sum(s["procs"].get(n, 0) for s in uniq) / len(uniq)
        if during >= APP_MIN_CORES and during >= APP_LIFT * max(usual, 0.01):
            apps.append({"name": n, "during": round(during, 2), "usual": round(usual, 2)})
    out["apps"] = sorted(apps, key=lambda a: -(a["during"] - a["usual"]))[:5]
    dv = [d for d in (_driver_pct(s) for s in base) if d is not None]
    dd = [d for d in (_driver_pct(s) for s in uniq) if d is not None]
    if dv and dd:
        out["driver"] = {"during": round(mean(dd), 1), "usual": round(mean(dv), 1)}
    parts = []
    drv = out["driver"]
    if drv and drv["during"] >= DPC_MIN_PCT and drv["during"] >= max(2 * drv["usual"], drv["usual"] + 1.0):
        parts.append(f"Driver time (DPC and interrupts) rose to {drv['during']:.1f}% of the CPU during stutters, "
                     f"against {drv['usual']:.1f}% normally. That points at a driver (network, audio, USB, storage "
                     "or graphics). LatencyMon can name which one.")
    for a in out["apps"][:2]:
        parts.append(f"{a['name']} used {a['during']:.2f} cores during stutters, against {a['usual']:.2f} normally.")
    if not parts:
        parts.append("Nothing outside iRacing stood out: no app and no driver was busier than usual during the "
                     "stutters. The likely cause is inside the sim (loading textures or shaders, cars coming into "
                     "view) or the graphics card.")
    n = len(events)
    out["text"] = f"{n} stutter{'s' if n != 1 else ''} in the measured runs. " + " ".join(parts) + \
        " (Samples are once a second, so this shows what was busy around each stutter, not proof.)"
    return out
