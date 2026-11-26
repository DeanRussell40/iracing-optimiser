"""What the main window shows, built from the bench bundles on disk.

Pure functions over summary.json files (see bench.analyze_bundle), so every number on screen can be traced to a
bundle and the whole view is tested without Windows or iRacing.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from statistics import median

from . import abtest

KEYS = ("avg_fps", "low1_fps", "frametime_p99_ms", "stutters_per_min", "gpu_bound_pct", "gpu_util_pct",
        "pacing_error_ms")
BG_PAUSED = "Background apps paused"
BG_RUNNING = "Background apps running"


# ---------------------------------------------------------------------------- loading

def _medians(runs: list[dict], variant: str) -> dict:
    ms = [r["metrics"] for r in runs if r["window"].get("variant") == variant and not r["window"].get("warmup")
          and r["window"].get("valid") and r.get("metrics")]
    out = {"runs": len(ms)}
    for k in KEYS:
        vals = [m[k] for m in ms if m.get(k) is not None]
        out[k] = median(vals) if vals else None
    return out


def load_bundle(bdir: Path) -> dict | None:
    p = Path(bdir) / "summary.json"
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    runs = s.get("runs") or []
    names = [v["name"] for v in s.get("variants") or []] or \
        list(dict.fromkeys(r["window"].get("variant", "Current settings") for r in runs)) or ["Current settings"]
    return {
        "id": Path(bdir).name,
        "path": str(Path(bdir)),
        "report": str(Path(bdir) / "report.html"),
        "kind": s.get("kind", "bench"),
        "created": s.get("created") or "",
        "variants": names,
        "descriptions": {v["name"]: v.get("description") for v in s.get("variants") or [] if v.get("description")},
        "per_variant": {n: _medians(runs, n) for n in names},
        "comparisons": s.get("comparisons") or [],
        "repeatability": s.get("repeatability") or {},
        "caps": s.get("caps") or {},
        "suspects": s.get("suspects") or [],
        "stutter_attribution": s.get("stutter_attribution") or {},
        "settings_key": s.get("settings_key"),
        "replay_mismatch": s.get("replay_mismatch") or [],
        "background": s.get("background") or [],
        "present_mode": s.get("present_mode") or {},
        "surface": s.get("surface") or {},
        "gpus": s.get("gpus") or [],
    }


def load_history(bench_root: Path) -> list[dict]:
    """Every analysed bundle under bench_root, oldest first. Bundles without a usable baseline are skipped."""
    root = Path(bench_root)
    if not root.is_dir():
        return []
    out = []
    for d in root.iterdir():
        if d.is_dir() and (b := load_bundle(d)) and b["per_variant"][b["variants"][0]]["runs"]:
            out.append(b)
    return sorted(out, key=lambda b: (b["created"], b["id"]))


# ---------------------------------------------------------------------------- helpers

def _when(iso: str) -> str:
    try:
        d = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or ""
    return f"{d.day} {d:%b}, {d:%I:%M %p}".replace(" 0", " ")


def _fmt(v, nd=0, unit=""):
    return "–" if v is None else f"{v:.{nd}f}{unit}"


def _better(b: dict) -> dict | None:
    """The comparison in this bundle that proved an improvement, if any."""
    return next((c for c in b["comparisons"] if c.get("verdict") == "better"), None)


def current_state(b: dict) -> tuple[str, dict]:
    """The best proven setup in a bundle: the improved variant when an A/B proved one, else the baseline."""
    c = _better(b)
    name = c["variant"] if c else b["variants"][0]
    return name, b["per_variant"].get(name) or {}


def _limit(m: dict) -> tuple[str, str]:
    g = m.get("gpu_bound_pct")
    if g is None:
        return "?", "GPU load not measured"
    if g >= 80:
        return "GPU", f"busy {g:.0f}% of frames"
    if g <= 40:
        return "CPU", f"GPU busy only {g:.0f}% of frames"
    return "Mixed", f"GPU busy {g:.0f}% of frames"


def _find_ab(history: list[dict], variant: str) -> tuple[dict, dict] | None:
    for b in reversed(history):
        for c in b["comparisons"]:
            if c.get("variant") == variant:
                return b, c
    return None


def _tearing(b: dict) -> bool:
    return "tearing" in (b["present_mode"].get("sync_note") or "").lower()


def _gsync_tested(history: list[dict]) -> bool:
    return any("g-sync" in n.lower() or "gsync" in n.lower() for b in history for n in b["variants"])


# ---------------------------------------------------------------------------- next step and findings

def next_step(history: list[dict]) -> dict:
    if not history:
        return {"test": "auto", "label": "Measure my PC",
                "why": "Looks for busy background apps first. If it finds any, it measures with them running and "
                       "paused, which also gives your baseline. If not, it measures your PC as it is."}
    last = history[-1]
    rep = last["repeatability"].get("verdict")
    if rep == "capped":
        return {"test": "repeat", "label": "Measure again uncapped",
                "why": "Turn off every frame rate cap first, then measure again."}
    if last["suspects"] and not _find_ab(history, BG_PAUSED):
        return {"test": "background", "label": "Test background apps",
                "why": "Busy apps were seen during the last test. Find out if they cost you smoothness."}
    if _tearing(last) and not _gsync_tested(history):
        return {"test": "guided", "label": "Test G-Sync on vs off", "a": "G-Sync off", "b": "G-Sync on",
                "why": "Frames are shown with tearing. The app will tell you when to switch G-Sync."}
    return {"test": "repeat", "label": "Measure again",
            "why": "Check where your PC stands now. Compare it with the earlier runs on Results."}


def findings(history: list[dict], elevation_problem: str | None = None) -> list[dict]:
    out = []
    def add(level, title, detail="", action=None):
        out.append({"level": level, "title": title, "detail": detail, "action": action})

    if elevation_problem:
        add("bad", "The app can't control iRacing", elevation_problem,
            {"id": "relaunch_admin", "label": "Restart as administrator"})
    if not history:
        add("info", "Nothing measured yet", "Open a race replay in iRacing and press the orange button.")
        return out
    last = history[-1]
    rep = last["repeatability"]
    if rep.get("verdict") == "capped":
        add("warn", "A frame rate cap is hiding your PC's headroom", rep.get("verdict_text", ""),
            {"id": "test:repeat", "label": "Measure again"})
    elif rep.get("verdict") == "fail":
        add("warn", "Runs vary too much to trust small differences", rep.get("verdict_text", ""))
    elif rep.get("verdict") == "pass":
        add("good", "Measurements are repeatable", rep.get("verdict_text", ""))

    bg = _find_ab(history, BG_PAUSED)
    if bg:
        b, c = bg
        apps = b["descriptions"].get(BG_PAUSED)
        detail = c["verdict_text"] + (f" Apps paused: {apps}." if apps else "")
        if c["verdict"] == "better":
            add("good", "Background apps were costing you smoothness", detail)
        elif c["verdict"] == "worse":
            add("warn", "Pausing background apps made things worse", detail)
        elif c["verdict"] == "mixed":
            add("warn", "Background apps: mixed result", detail)
        else:
            add("good", "Background apps make no measurable difference", detail)
    elif last["suspects"]:
        add("warn", "Busy background apps: " + ", ".join(last["suspects"][:4]),
            "They used CPU time while the replay ran. Test whether they cost you smoothness.",
            {"id": "test:background", "label": "Test them"})

    for b in history:
        for c in b["comparisons"]:
            if c.get("variant") != BG_PAUSED:
                level = {"better": "good", "worse": "warn", "mixed": "warn"}.get(c["verdict"], "info")
                add(level, f"{c['variant']} vs {c['baseline']}", c["verdict_text"])

    if _tearing(last):
        act = None if _gsync_tested(history) else {"id": "test:guided:G-Sync off:G-Sync on", "label": "Test G-Sync"}
        add("warn", "Frames are shown with tearing", last["present_mode"].get("sync_note", ""), act)

    sa = last.get("stutter_attribution") or {}
    if sa.get("count") and sa.get("text"):
        add("warn" if sa["count"] >= 3 else "info", "What was busy during stutters", sa["text"])

    name, m = current_state(last)
    lim, why = _limit(m)
    if lim == "GPU":
        add("info", "The graphics card is the limit",
            f"It is {why}. More frames have to come from lighter graphics settings, and a frame cap below what it "
            "can hold keeps pacing even.")
    elif lim == "CPU":
        add("info", "The processor is the limit", f"The {why}. Lighter graphics settings won't add many frames here.")
    caps = last["caps"]
    if caps.get("available") and caps.get("text"):
        add("info", f"Your PC holds a {caps['best']} FPS cap" if caps.get("best") else "No steady frame cap found",
            caps["text"])
    return out


# ---------------------------------------------------------------------------- summary, chart, tests

def summary(history: list[dict]) -> tuple[str, list[str]]:
    if not history:
        return "Nothing measured yet", ["Run the first measurement on the Tune screen. Every number here comes from "
                                        "your PC on a replay you choose."]
    last = history[-1]
    name, m = current_state(last)
    lim, why = _limit(m)
    surf = last["surface"]
    where = f" across {surf['width']} × {surf['height']}" if surf.get("width") else ""
    paras = []
    load = {"GPU": f"Your graphics card is the limit ({why}) at about {_fmt(m.get('avg_fps'))} fps{where}. "
                   "The processor has headroom.",
            "CPU": f"Your processor is the limit ({why}) at about {_fmt(m.get('avg_fps'))} fps{where}. "
                   "The graphics card is waiting on it.",
            "Mixed": f"Neither part is clearly the limit ({why}) at about {_fmt(m.get('avg_fps'))} fps{where}."
            }.get(lim, f"About {_fmt(m.get('avg_fps'))} fps{where}. GPU load wasn't measured.")
    paras.append(load)
    headline = f"Your PC runs this replay at {_fmt(m.get('avg_fps'))} fps"
    bg = _find_ab(history, BG_PAUSED)
    if bg and bg[1]["verdict"] == "better":
        c = bg[1]
        lo = c["metrics"].get("low1_fps", {})
        paras.append(f"Busy background apps caused stutter. Pausing them changed 1% lows from {_fmt(lo.get('a'))} "
                     f"to {_fmt(lo.get('b'))} fps.")
        if lo.get("a") and lo.get("b") and lo["b"] >= lo["a"] * 1.5:
            headline = "Background apps were costing you smoothness"
    elif bg:
        paras.append(bg[1]["verdict_text"])
    elif last["suspects"]:
        paras.append("Busy background apps were seen (" + ", ".join(last["suspects"][:4]) + "). Not tested yet.")
    if _tearing(last):
        paras.append("Frames are shown with tearing. The app can't read whether G-Sync is on, so "
                     + ("the G-Sync test has the answer." if _gsync_tested(history) else "test G-Sync on vs off next."))
    if last["repeatability"].get("verdict") == "capped":
        paras.append("The last baseline was capped, so it shows the cap rather than what the PC can do.")
    paras.append("No graphics settings have been changed by the app, so there are no compromises to report yet.")
    return headline, paras


def chart(history: list[dict], limit: int = 12) -> list[dict]:
    """1% low per measured setup, in the order they were measured. "stale" marks other graphics settings."""
    pts = []
    for b in history:
        for n in b["variants"]:
            v = b["per_variant"][n].get("low1_fps")
            if v is not None:
                pts.append({"when": _when(b["created"]), "label": n, "value": round(v, 1), "id": b["id"],
                            "stale": bool(b.get("stale"))})
    return pts[-limit:]


def tests_table(history: list[dict]) -> list[dict]:
    rows = []
    for b in reversed(history):
        base = b["variants"][0]
        if not b["comparisons"]:
            m = b["per_variant"][base]
            v = b["repeatability"].get("verdict", "")
            rows.append({"when": _when(b["created"]), "test": "Repeatability check", "low1": _fmt(m.get("low1_fps")),
                         "p99": _fmt(m.get("frametime_p99_ms"), 1, " ms"),
                         "verdict": {"pass": "Repeatable", "fail": "Too noisy", "capped": "Capped"}.get(v, v.title()),
                         "level": {"pass": "good", "fail": "warn", "capped": "warn"}.get(v, "info"), "id": b["id"],
                         "stale": bool(b.get("stale")), "kind": "test", "t": b["created"]})
            continue
        for c in b["comparisons"]:
            mb = b["per_variant"].get(c["variant"], {})
            ma = b["per_variant"][base]
            rows.append({"when": _when(b["created"]), "test": f"{c['variant']} vs {base}",
                         "low1": f"{_fmt(ma.get('low1_fps'))} → {_fmt(mb.get('low1_fps'))}",
                         "p99": f"{_fmt(ma.get('frametime_p99_ms'), 1)} → {_fmt(mb.get('frametime_p99_ms'), 1)} ms",
                         "verdict": c["verdict"].title() if c["verdict"] != "same" else "No difference",
                         "level": {"better": "good", "worse": "bad", "mixed": "warn"}.get(c["verdict"], "info"),
                         "id": b["id"], "stale": bool(b.get("stale")), "kind": "test", "t": b["created"]})
    return rows


def readouts(history: list[dict]) -> list[dict]:
    if not history:
        return [{"label": l, "value": "–", "note": ""} for l in ("Average", "1% low", "Stutters per minute", "Limit")]
    first = history[0]["per_variant"][history[0]["variants"][0]]
    name, m = current_state(history[-1])
    def delta(k, unit=""):
        a, b = first.get(k), m.get(k)
        if len(history) < 2 and name == history[-1]["variants"][0] or a is None or b is None:
            return ""
        d = b - a
        return f"{'+' if d >= 0 else ''}{d:.0f}{unit} since first run"
    st_first = first.get("stutters_per_min")
    lim, why = _limit(m)
    return [
        {"label": "Average", "value": _fmt(m.get("avg_fps"), 0, " fps"), "note": delta("avg_fps")},
        {"label": "1% low", "value": _fmt(m.get("low1_fps"), 0, " fps"), "note": delta("low1_fps")},
        {"label": "Stutters per minute", "value": _fmt(m.get("stutters_per_min"), 1),
         "note": f"was {st_first:.0f}" if st_first is not None and st_first >= 10 else f"was {st_first:.1f}" if st_first is not None and m is not first and
         abs(st_first - (m.get("stutters_per_min") or 0)) >= max(1.0, 0.1 * st_first) else ""},
        {"label": "Limit", "value": lim, "note": why},
    ]


# ---------------------------------------------------------------------------- the whole view

def focus_text(focus: int, history: list[dict]) -> dict:
    """The slider picks among measured graphics candidates. None exist until graphics tests run, and the app
    says so rather than pretending."""
    focus = max(0, min(4, int(focus)))
    return {"value": focus, "label": abtest.FOCUS_LABELS[focus],
            "pick": "No graphics trade-offs have been measured yet, so every position keeps your current settings."}


def _settings_findings(history: list[dict], current: list[dict], settings_problem: str | None,
                       capture: dict | None) -> list[dict]:
    out = []
    def add(level, title, detail="", action=None):
        out.append({"level": level, "title": title, "detail": detail, "action": action})
    if settings_problem:
        add("warn", "Replay and race graphics differ", settings_problem)
    if history and not current:
        add("warn", "Your graphics settings changed since the last test",
            "Earlier results were measured on other settings, so they don't describe your PC as it is now. They "
            "stay on the Results screen, marked as other settings. Measure again for a new baseline.",
            {"id": "test:auto", "label": "Measure my PC"})
    elif current and current[-1].get("replay_mismatch") and not settings_problem:
        add("info", "The last test ran with replay graphics that differed from race graphics",
            "Those numbers describe the replay settings at the time, not what you race with.")
    capture = capture or {}
    if capture.get("simhub") is True:
        from .capture import SIMHUB_ON_TEXT
        add("warn", "SimHub is capturing the screen", SIMHUB_ON_TEXT)
    if capture.get("apps"):
        add("warn", "Screen capture was running: " + ", ".join(capture["apps"]),
            "Recording, streaming and ambient-lighting tools copy the screen many times a second and compete with "
            "the sim for the graphics card. Close them, or test them with the background test.")
    return out


def timeline(tests: list[dict], changes: list[dict]) -> list[dict]:
    """Tests, setting changes and notes, newest first, so each change sits between the tests before and after it."""
    from .changelog import describe
    ev = [{"kind": e["kind"], "t": e["t"], "when": _when(e["t"]),
           "title": "Note" if e["kind"] == "note" else "Settings changed", "text": describe(e)} for e in changes]
    # newest first; events logged in the same second keep their logged order (latest on top)
    order = {id(r): n for n, r in enumerate(ev)}
    return sorted(tests + ev, key=lambda r: (r.get("t") or "", order.get(id(r), -1)), reverse=True)


def _change_finding(current: list[dict], changes: list[dict]) -> dict | None:
    last_t = current[-1]["created"] if current else ""
    after = [e for e in changes if e["kind"] == "change" and e["t"] > last_t]
    if not current or not after:
        return None
    from .changelog import describe
    return {"level": "info", "title": "Settings changed since your last test",
            "detail": " ".join(describe(e) + "." for e in after[-3:]) + " Measure again to see what it did.",
            "action": {"id": "test:repeat", "label": "Measure again"}}


def view(bench_root: Path, focus: int = 1, elevation_problem: str | None = None, machine: str = "",
         current_key: str | None = None, settings_problem: str | None = None, capture: dict | None = None,
         changes: list[dict] | None = None) -> dict:
    """current_key: fingerprint of the graphics settings on disk now. Only tests measured on those settings feed the
    readouts, findings, summary and next step; the rest stay in the chart and table, marked stale."""
    from .capture import capture_apps
    history = load_history(bench_root)
    for b in history:
        b["stale"] = current_key is not None and b.get("settings_key") != current_key
    current = [b for b in history if not b["stale"]]
    capture = dict(capture or {})
    if current:
        last_b = current[-1]
        names = [r.get("name", "") for r in last_b.get("background") or []] + list(last_b.get("suspects") or [])
        capture["apps"] = capture_apps(names)
    head, paras = summary(current)
    if history and not current:
        head = "Your graphics settings changed"
        paras = ["Every earlier test ran on different graphics settings, so none of them describe your PC now. "
                 "Measure again for a new baseline."]
    last = current[-1] if current else None
    sub = ""
    if last:
        name, m = current_state(last)
        rep = last["repeatability"]
        sub = f"Last test {_when(last['created'])} · {name} · {m.get('runs', 0)} runs"
        spread = ((rep.get("metrics") or {}).get("frametime_p99_ms") or {}).get("spread_pct")
        if spread is not None and rep.get("verdict") == "pass":
            sub += f" · repeatable to {spread:.1f}%"
    elif history:
        sub = "No tests on your current graphics settings yet"
    fs = findings(current, elevation_problem)
    if history and not current:
        fs = [f for f in fs if f["title"] != "Nothing measured yet"]
    at = 1 if elevation_problem else 0  # right after the "can't control iRacing" problem, which blocks everything
    fs[at:at] = _settings_findings(history, current, settings_problem, capture)
    cf = _change_finding(current, changes or [])
    if cf:
        fs.insert(at, cf)
    return {
        "machine": machine,
        "subtitle": sub or "No measurements yet",
        "readouts": readouts(current),
        "findings": fs,
        "next": next_step(current),
        "headline": head,
        "summary": paras,
        "chart": chart(history),
        "tests": timeline(tests_table(history), changes or []),
        "focus": focus_text(focus, current),
        "bundles": len(current),
        "stale_bundles": len(history) - len(current),
    }
