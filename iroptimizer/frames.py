"""Full PresentMon frame records, including display-side and present-mode columns.

The original `presentmon.load_frames` keeps only time, frame time and CPU/GPU busy. That is enough for
averages but not for the questions this app has to answer:

* Is the game on the Independent Flip path (G-Sync/VRR can work, lowest latency) or composed by DWM?
* What does the *display* see? `MsBetweenDisplayChange` is the on-screen cadence; frame time alone is
  the CPU-side cadence and can look smooth while the screen does not.
* How many frames were never shown?

Column names differ between PresentMon versions, so every field has a list of aliases. Fields that a
given version does not write come back as None and the report says which columns were present.
"""
from __future__ import annotations

import csv
import gzip
from dataclasses import dataclass
from pathlib import Path

from .presentmon import MAX_FRAMES, _fnum, _open_text, _parse_dt, align_clock

ALIASES: dict[str, tuple[str, ...]] = {
    "app": ("Application", "ProcessName"),
    "time_dt": ("CPUStartDateTime",),
    "time_s": ("TimeInSeconds", "CPUStartTimeInSeconds", "CPUStartTime"),
    "time_ms": ("CPUStartTimeInMs",),
    "frametime": ("MsBetweenPresents", "FrameTime", "MsBetweenAppStart", "msBetweenPresents"),
    "cpu_busy": ("MsCPUBusy", "CPUBusy"),
    "gpu_busy": ("MsGPUBusy", "GPUBusy"),
    "until_displayed": ("MsUntilDisplayed", "UntilDisplayed"),
    "display_change": ("MsBetweenDisplayChange", "BetweenDisplayChange", "MsDisplayedTime", "DisplayedTime"),
    "display_latency": ("MsDisplayLatency", "DisplayLatency"),
    "dropped": ("Dropped",),
    "present_mode": ("PresentMode",),
    "allows_tearing": ("AllowsTearing",),
    "sync_interval": ("SyncInterval",),
}

# Columns written to the compact bundle file, in this order (canonical names, not PresentMon's)
CANONICAL = ("t", "frametime", "cpu_busy", "gpu_busy", "until_displayed", "display_change",
             "display_latency", "displayed", "present_mode", "allows_tearing", "sync_interval")


@dataclass(slots=True)
class Frame:
    t: float | None
    frametime: float
    cpu_busy: float | None = None
    gpu_busy: float | None = None
    until_displayed: float | None = None
    display_change: float | None = None
    display_latency: float | None = None
    displayed: bool | None = None
    present_mode: str | None = None
    allows_tearing: bool | None = None
    sync_interval: int | None = None


def _resolve(fields: list[str]) -> dict[str, str | None]:
    return {k: next((c for c in names if c in fields), None) for k, names in ALIASES.items()}


def _bool(x) -> bool | None:
    s = (x or "").strip().lower()
    if s in ("1", "true", "yes"):
        return True
    if s in ("0", "false", "no"):
        return False
    return None


def _displayed(r: dict, cols: dict) -> bool | None:
    if cols["dropped"]:
        b = _bool(r.get(cols["dropped"]))
        return None if b is None else not b
    if cols["until_displayed"]:
        # PresentMon 2.x writes "NA" for frames that never reached the screen
        return _fnum(r.get(cols["until_displayed"])) is not None
    return None


def read_columns(path: str | Path) -> list[str]:
    with _open_text(path) as f:
        return next(csv.reader(f), [])


def load(path: str | Path, t0: float | None = None, process_prefix: str = "iracingsim") -> tuple[list[Frame], dict]:
    """Reads a raw PresentMon CSV (or a compact bundle file). Returns (frames, info).

    info["columns"] maps each field to the PresentMon column it came from (None = not in this file).
    """
    frames: list[Frame] = []
    with _open_text(path) as f:
        rd = csv.DictReader(f)
        fields = rd.fieldnames or []
        if "frametime" in fields and "t" in fields:  # already our compact format
            for r in rd:
                ft = _fnum(r.get("frametime"))
                if not ft:
                    continue
                frames.append(Frame(
                    t=_fnum(r.get("t")), frametime=ft, cpu_busy=_fnum(r.get("cpu_busy")),
                    gpu_busy=_fnum(r.get("gpu_busy")), until_displayed=_fnum(r.get("until_displayed")),
                    display_change=_fnum(r.get("display_change")), display_latency=_fnum(r.get("display_latency")),
                    displayed=_bool(r.get("displayed")), present_mode=r.get("present_mode") or None,
                    allows_tearing=_bool(r.get("allows_tearing")),
                    sync_interval=int(v) if (v := _fnum(r.get("sync_interval"))) is not None else None))
                if len(frames) > MAX_FRAMES:
                    raise ValueError("PresentMon log is too large.")
            cols = {k: (k if k in fields else None) for k in CANONICAL}
            return frames, {"columns": cols, "format": "bundle", "raw_columns": fields}
        cols = _resolve(fields)
        if not cols["frametime"]:
            raise ValueError("No frame-time column in the PresentMon CSV")
        for r in rd:
            if cols["app"] and not (r.get(cols["app"]) or "").lower().startswith(process_prefix):
                continue
            ft = _fnum(r.get(cols["frametime"]))
            if not ft or ft <= 0 or ft > 5000:
                continue
            if cols["time_dt"]:
                t = _parse_dt(r.get(cols["time_dt"]) or "")
            elif cols["time_s"]:
                t = _fnum(r.get(cols["time_s"]))
                t = (t0 + t) if (t is not None and t0) else t
            elif cols["time_ms"]:
                t = _fnum(r.get(cols["time_ms"]))
                t = None if t is None else ((t0 or 0) + t / 1000)
            else:
                t = None
            si = _fnum(r.get(cols["sync_interval"])) if cols["sync_interval"] else None
            frames.append(Frame(
                t=t, frametime=ft,
                cpu_busy=_fnum(r.get(cols["cpu_busy"])) if cols["cpu_busy"] else None,
                gpu_busy=_fnum(r.get(cols["gpu_busy"])) if cols["gpu_busy"] else None,
                until_displayed=_fnum(r.get(cols["until_displayed"])) if cols["until_displayed"] else None,
                display_change=_fnum(r.get(cols["display_change"])) if cols["display_change"] else None,
                display_latency=_fnum(r.get(cols["display_latency"])) if cols["display_latency"] else None,
                displayed=_displayed(r, cols),
                present_mode=(r.get(cols["present_mode"]) or "").strip() or None if cols["present_mode"] else None,
                allows_tearing=_bool(r.get(cols["allows_tearing"])) if cols["allows_tearing"] else None,
                sync_interval=int(si) if si is not None else None))
            if len(frames) > MAX_FRAMES:
                raise ValueError("PresentMon log is too large.")
    # Same whole-hour clock correction as the session recorder (PresentMon wrote +1 h in real use)
    if t0 and frames and frames[0].t is not None:
        shifted = align_clock([(fr.t,) for fr in frames], t0)
        for fr, (t,) in zip(frames, shifted):
            fr.t = t
    return frames, {"columns": cols, "format": "raw", "raw_columns": fields}


def write_bundle_csv(frames: list[Frame], dst: str | Path) -> Path:
    """Writes frames in the compact canonical format, gzip-compressed."""
    dst = Path(dst)

    def fmt(v):
        if v is None:
            return ""
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, float):
            return f"{v:.6f}" if abs(v) > 1e6 else f"{v:.4f}"
        return str(v)

    with gzip.open(dst, "wt", newline="", encoding="utf-8", compresslevel=6) as fo:
        w = csv.writer(fo)
        w.writerow(CANONICAL)
        for fr in frames:
            w.writerow([fmt(getattr(fr, c)) for c in CANONICAL])
    return dst


def slice_window(frames: list[Frame], t_start: float, t_end: float) -> list[Frame]:
    return [fr for fr in frames if fr.t is not None and t_start <= fr.t < t_end]
