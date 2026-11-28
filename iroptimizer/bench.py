"""Replay bench: run the same replay segment several times and measure how repeatable it is.

Milestone 1 of the optimiser. It changes no settings. It answers two questions:

1. Is the measurement trustworthy? The same segment is played N times; if frame time p99 moves more than
   the repeatability gate between runs, no A/B comparison of settings can be trusted yet.
2. Which present path is iRacing on (Independent Flip vs composed)? That decides whether G-Sync can work.

How it drives the sim: only through the official iRacing SDK broadcast messages (replay seek, play speed,
camera). PresentMon runs out-of-process via Windows ETW. Nothing touches the game process, so it stays
clear of anti-cheat.

The user opens a replay in iRacing, picks the car and camera, scrubs to the start of the segment, then
starts the bench. Every run seeks back to that frame, re-applies the camera, waits, plays at 1x, and pauses.
"""
from __future__ import annotations

import html
import json
import shutil
import threading
import time
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from . import frames as framelib
from . import metrics

BUNDLE_VERSION = 1
REPLAY_TICKS_PER_S = 60  # iRacing replays advance 60 frames per second at 1x


@dataclass
class BenchConfig:
    seconds: float = 60.0
    runs: int = 5
    warmup_runs: int = 1
    settle_s: float = 4.0        # paused at the start frame before each run (texture streaming, camera cut)
    lead_trim_s: float = 0.5     # ignore the first moments after pressing play
    start_frame: int | None = None  # None = wherever the replay is when the bench starts
    seek_timeout_s: float = 20.0


@dataclass
class Camera:
    car_idx: int
    car_number: str
    group: int
    camera: int


@dataclass
class RunWindow:
    index: int
    warmup: bool
    t_start: float
    t_end: float
    start_frame: int | None
    end_frame: int | None
    valid: bool = True
    note: str = ""


class ReplayDriver(Protocol):
    def connect(self) -> None: ...
    def in_replay(self) -> bool: ...
    def current_frame(self) -> int | None: ...
    def current_camera(self) -> Camera: ...
    def set_camera(self, cam: Camera) -> None: ...
    def seek(self, frame: int) -> None: ...
    def play(self) -> None: ...
    def pause(self) -> None: ...
    def disconnect(self) -> None: ...


class IRSDKDriver:
    """Replay control through pyirsdk broadcast messages."""

    def __init__(self):
        self.ir = None

    def connect(self) -> None:
        import irsdk
        self.irsdk = irsdk
        self.ir = irsdk.IRSDK()
        if not self.ir.startup() or not self.ir.is_connected:
            raise RuntimeError("iRacing is not running (the telemetry SDK is not available).")

    def _get(self, key):
        try:
            return self.ir[key]
        except Exception:
            return None

    def in_replay(self) -> bool:
        # IsReplayPlaying is true when the sim is showing a replay (paused or not), false when driving live
        return bool(self._get("IsReplayPlaying"))

    def current_frame(self) -> int | None:
        v = self._get("ReplayFrameNum")
        return int(v) if v is not None else None

    def current_camera(self) -> Camera:
        car_idx = int(self._get("CamCarIdx") or 0)
        number = str(car_idx)
        drivers = (self._get("DriverInfo") or {}).get("Drivers") or []
        for d in drivers:
            if d.get("CarIdx") == car_idx:
                number = str(d.get("CarNumber") or d.get("CarNumberRaw") or car_idx)
                break
        return Camera(car_idx, number, int(self._get("CamGroupNumber") or 0), int(self._get("CamCameraNumber") or 0))

    def set_camera(self, cam: Camera) -> None:
        self.ir.cam_switch_num(cam.car_number, cam.group, cam.camera)

    def seek(self, frame: int) -> None:
        self.ir.replay_set_play_position(self.irsdk.RpyPosMode.begin, int(frame))

    def play(self) -> None:
        self.ir.replay_set_play_speed(1, False)

    def pause(self) -> None:
        self.ir.replay_set_play_speed(0, False)

    def disconnect(self) -> None:
        if self.ir:
            try:
                self.ir.shutdown()
            except Exception:
                pass


class GpuSampler:
    """Samples GPU temperature, clock, power and load (NVML) in a thread, to explain drift between runs."""

    def __init__(self, hz: float = 4.0):
        self.hz = hz
        self.samples: list[dict] = []
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self.nvml = None
        try:
            import pynvml
            pynvml.nvmlInit()
            self.nvml, self.h = pynvml, pynvml.nvmlDeviceGetHandleByIndex(0)
        except Exception:
            self.nvml = None

    def _loop(self):
        n, h = self.nvml, self.h
        while not self._stop.wait(1 / self.hz):
            try:
                self.samples.append({
                    "t": time.time(),
                    "temp_c": n.nvmlDeviceGetTemperature(h, n.NVML_TEMPERATURE_GPU),
                    "clock_mhz": n.nvmlDeviceGetClockInfo(h, n.NVML_CLOCK_GRAPHICS),
                    "power_w": round(n.nvmlDeviceGetPowerUsage(h) / 1000, 1),
                    "util_pct": n.nvmlDeviceGetUtilizationRates(h).gpu,
                })
            except Exception:
                pass

    def start(self):
        if self.nvml:
            self._t = threading.Thread(target=self._loop, daemon=True)
            self._t.start()

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join(timeout=2)


class Aborted(Exception):
    pass


def _fingerprint() -> dict:
    """Hardware, driver and iRacing graphics settings at bench time, so runs can be compared over time."""
    fp: dict = {}
    try:
        from . import hardware
        fp["hardware"] = hardware.get_hardware()
    except Exception as e:
        fp["hardware_error"] = str(e)
    try:
        from . import config_reader
        fp["config"] = config_reader.snapshot(config_reader.load())
    except Exception as e:
        fp["config_error"] = str(e)
    return fp


def _sleep(seconds: float, stop: threading.Event | None):
    if stop is None:
        time.sleep(seconds)
    elif stop.wait(seconds):
        raise Aborted()


@dataclass
class Variant:
    """One side of an A/B test. apply() runs before this variant's runs (it may block, e.g. waiting for the
    user to change a setting), revert() after them. A plain bench has one variant with neither."""
    name: str
    apply: Callable[[], None] | None = None
    revert: Callable[[], None] | None = None
    manual: bool = False
    description: str = ""
    settle_runs: int = 0  # unmeasured runs after switching to this variant (a fresh sim start streams textures)


def schedule(variants: list[Variant], runs: int, warmup_runs: int) -> list[tuple[int, bool]]:
    """(variant index, is_warmup) for every run. Warm-ups use the first variant. With two or more variants the
    measured runs go in balanced blocks (A B, B A, A B …), so drift over the session hits every side equally
    and the number of switches stays low."""
    out = [(0, True)] * warmup_runs
    if len(variants) == 1:
        return out + [(0, False)] * runs
    order = list(range(len(variants)))
    for r in range(runs):
        block = order if r % 2 == 0 else order[::-1]
        out += [(v, False) for v in block]
    return out


MIN_SECONDS = 10.0


def validate(cfg: BenchConfig) -> None:
    if not cfg.seconds or cfg.seconds < MIN_SECONDS:
        raise ValueError(f"Seconds per run must be at least {MIN_SECONDS:.0f}.")
    if not cfg.runs or cfg.runs < 3:
        raise ValueError("Use at least 3 runs: fewer can't show whether the result repeats.")
    if cfg.warmup_runs is None or cfg.warmup_runs < 0:
        raise ValueError("Warm-up runs can't be negative.")


def _default_preflight() -> str | None:
    from . import winsys
    return winsys.elevation_mismatch()


def _default_surface() -> dict:
    from . import winsys
    try:
        return winsys.render_surface()
    except Exception as e:
        return {"available": False, "error": str(e)}


def _default_background():
    from . import winsys
    return winsys.BackgroundSampler()


def run_bench(cfg: BenchConfig, out_root: Path, driver: ReplayDriver, capture_factory: Callable[[Path], object],
              log: Callable[[str], None] = print, stop: threading.Event | None = None,
              sampler: GpuSampler | None = None, fingerprint: Callable[[], dict] = _fingerprint,
              clock: Callable[[], float] = time.time, variants: list[Variant] | None = None,
              preflight: Callable[[], str | None] = _default_preflight,
              surface: Callable[[], dict] = _default_surface,
              background=None, kind: str = "bench") -> Path:
    """Runs the bench (or an A/B test when several variants are given) and returns the bundle directory.
    capture_factory(csv_path) -> object with start() -> bool, stop(), .error and .started_at."""
    validate(cfg)
    variants = variants or [Variant("Current settings")]
    problem = preflight() if preflight else None
    if problem:
        raise RuntimeError(problem)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bdir = Path(out_root) / "bench" / stamp
    bdir.mkdir(parents=True, exist_ok=True)
    raw_csv = bdir / "presentmon_raw.csv"

    driver.connect()
    if not driver.in_replay():
        raise RuntimeError("iRacing is not showing a replay. Open a replay, pick the car and camera, "
                           "move to the start of the segment, then start the bench.")
    driver.pause()
    start_frame = cfg.start_frame if cfg.start_frame is not None else driver.current_frame()
    if start_frame is None:
        raise RuntimeError("Could not read the replay position from iRacing.")
    cam = driver.current_camera()
    log(f"Replay frame {start_frame}, car #{cam.car_number}, camera group {cam.group}.")

    fp = fingerprint()
    fp["render_surface"] = surface() if surface else {"available": False}
    cap = capture_factory(raw_csv)
    if not cap.start():
        raise RuntimeError(f"PresentMon could not start: {getattr(cap, 'error', '') or 'unknown error'}. "
                           "If this is a permission problem, use 'Grant permission…' on the Session monitor tab.")
    sampler = sampler or GpuSampler()
    sampler.start()
    bg = background if background is not None else _default_background()
    bg.start()
    windows: list[RunWindow] = []
    plan = schedule(variants, cfg.runs, cfg.warmup_runs)
    active: int | None = None
    counts = [0] * len(variants)

    def switch(to: int) -> bool:
        nonlocal active
        if active == to:
            return False
        if active is not None and variants[active].revert:
            variants[active].revert()
        active = None
        if variants[to].apply:
            log(f"Switching to: {variants[to].name}")
            variants[to].apply()
        active = to
        return True

    def one_run(i: int, v: Variant, warm: bool, label: str) -> None:
        driver.seek(start_frame)
        t_seek = clock()
        got = driver.current_frame()
        first = got
        while got is None or abs(got - start_frame) > 5:
            if clock() - t_seek > cfg.seek_timeout_s:
                hint = ""
                if got is not None and first is not None and got > first:
                    hint = (" The replay kept playing and ignored the jump. This happens when iRacing runs "
                            "as administrator and this app does not: run this app as administrator too.")
                raise RuntimeError(f"The replay did not move to frame {start_frame} (it is at {got}).{hint}")
            _sleep(0.2, stop)
            got = driver.current_frame()
        driver.set_camera(cam)
        _sleep(cfg.settle_s, stop)
        f0 = driver.current_frame()
        log(f"{label}: playing {cfg.seconds:.0f} s…")
        driver.play()
        t0 = clock()
        _sleep(cfg.seconds, stop)
        t1 = clock()
        driver.pause()
        f1 = driver.current_frame()
        w = RunWindow(i, warm, t0 + cfg.lead_trim_s, t1, f0, f1)
        w_dict = asdict(w) | {"variant": v.name}
        if f0 is not None and f1 is not None:
            expected = cfg.seconds * REPLAY_TICKS_PER_S
            adv = f1 - f0
            if adv < expected * 0.85 or adv > expected * 1.15:
                w_dict["valid"] = False
                w_dict["note"] = (f"Replay advanced {adv} frames, expected about {expected:.0f}. "
                                  "It was probably not playing at 1x the whole time.")
                log(f"{label}: {w_dict['note']}")
        windows.append(w_dict)

    try:
        for i, (vi, warm) in enumerate(plan):
            v = variants[vi]
            driver.pause()
            if switch(vi):
                for k in range(v.settle_runs):
                    driver.pause()
                    one_run(i, v, True, f"{v.name}: settling run {k + 1}/{v.settle_runs}")
            if not warm:
                counts[vi] += 1
            label = (f"warm-up {i + 1}" if warm else
                     f"{v.name} run {counts[vi]}/{cfg.runs}" if len(variants) > 1 else f"run {counts[vi]}/{cfg.runs}")
            one_run(i, v, warm, label)
    finally:
        try:
            if active is not None and variants[active].revert:
                variants[active].revert()
        except Exception as e:
            log(f"Could not undo the last change: {e}")
        sampler.stop()
        bg.stop()
        try:
            driver.pause()
        except Exception:
            pass
        cap.stop()
        driver.disconnect()

    meta = {
        "bundle_version": BUNDLE_VERSION,
        "kind": kind if len(variants) == 1 else "ab",
        "created": datetime.now().isoformat(timespec="seconds"),
        "config": asdict(cfg) | {"start_frame": start_frame},
        "camera": asdict(cam),
        "variants": [{"name": v.name, "manual": v.manual, "description": v.description} for v in variants],
        "capture_t0": getattr(cap, "started_at", None),
        "windows": windows,
        "gpu_samples": sampler.samples,
        "background_samples": getattr(bg, "samples", []),
        "fingerprint": fp,
    }
    (bdir / "bench.json").write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")

    if raw_csv.is_file():
        frames, _info = framelib.load(raw_csv, t0=meta["capture_t0"])
        framelib.write_bundle_csv(frames, bdir / "frames.csv.gz")
        # keep the raw log too (compressed): it has every PresentMon column, useful when something looks off
        import gzip
        with open(raw_csv, "rb") as fi, gzip.open(str(raw_csv) + ".gz", "wb", compresslevel=6) as fo:
            shutil.copyfileobj(fi, fo)
        raw_csv.unlink()
    log("Analysing…")
    analyze_bundle(bdir)
    zip_bundle(bdir)
    return bdir


def _gpu_in(samples: list[dict], t0: float, t1: float) -> dict:
    s = [x for x in samples if t0 <= x.get("t", 0) < t1]
    if not s:
        return {}
    avg = lambda k: sum(x[k] for x in s if x.get(k) is not None) / max(1, sum(1 for x in s if x.get(k) is not None))
    return {"gpu_temp_c": avg("temp_c"), "gpu_clock_mhz": avg("clock_mhz"), "gpu_power_w": avg("power_w"),
            "gpu_util_pct": avg("util_pct")}


def _settings_key(fp: dict) -> str | None:
    from . import config_reader
    try:
        return config_reader.settings_key(fp.get("config") or {})
    except Exception:
        return None


def _replay_mismatch(fp: dict, low1: float | None = None) -> list[dict]:
    from . import config_reader
    try:
        return config_reader.replay_mismatch(fp.get("config") or {}, low1)
    except Exception:
        return []


def _median_low1(ms: list[dict]) -> float | None:
    from statistics import median
    vals = [m["low1_fps"] for m in ms if m.get("low1_fps") is not None]
    return median(vals) if vals else None


def analyze_bundle(bdir: Path) -> dict:
    """(Re)analyses a bench bundle. Writes summary.json and report.html and returns the summary."""
    from . import abtest, stutters, winsys
    bdir = Path(bdir)
    meta = json.loads((bdir / "bench.json").read_text(encoding="utf-8"))
    src = bdir / "frames.csv.gz"
    if not src.is_file():
        src = bdir / "presentmon_raw.csv.gz"
    all_frames, info = framelib.load(src, t0=meta.get("capture_t0")) if src.is_file() else ([], {"columns": {}})
    vnames = [v["name"] for v in meta.get("variants") or [{"name": "Current settings"}]]
    runs = []
    events = []
    measured: dict[str, list] = {n: [] for n in vnames}
    bgs = meta.get("background_samples") or []
    for w in meta["windows"]:
        w = {"variant": vnames[0], **w}
        fr = framelib.slice_window(all_frames, w["t_start"], w["t_end"])
        m = metrics.run_metrics(fr)
        m.update(_gpu_in(meta.get("gpu_samples") or [], w["t_start"], w["t_end"]))
        if not fr:
            w = {**w, "valid": False, "note": (w.get("note") or "") + " No PresentMon frames in this window."}
        bg_rows = winsys.summarize_background(bgs, w["t_start"], w["t_end"], top=5)
        runs.append({"window": w, "metrics": m, "background": bg_rows})
        if not w["warmup"] and w["valid"] and m:
            measured.setdefault(w["variant"], []).extend(fr)
            for i in metrics.stutter_indices([f.frametime for f in fr]):
                events.append({"t": fr[i].t, "ms": round(fr[i].frametime, 1), "variant": w["variant"],
                               "run": w["index"] + 1, "at_s": round(fr[i].t - w["t_start"], 1)})
    per_variant = {n: [r["metrics"] for r in runs if r["window"]["variant"] == n and not r["window"]["warmup"]
                       and r["window"]["valid"] and r["metrics"]] for n in vnames}
    base = vnames[0]
    rep = metrics.repeatability(per_variant[base])
    comparisons = [abtest.compare(per_variant[base], per_variant[n], base, n) for n in vnames[1:]]
    base_frames = measured.get(base) or []
    fp = meta.get("fingerprint") or {}
    surf = fp.get("render_surface") or {}
    refresh = (surf.get("refresh") or [None])[0] if surf.get("available") else \
        (fp.get("hardware", {}).get("display") or {}).get("refresh")
    caps = abtest.predict_caps([f.frametime for f in base_frames], refresh) if not rep.get("cap_fps") else \
        {"available": False, "reason": "The baseline runs were capped, so the PC's headroom is hidden."}
    bg_all = winsys.summarize_background(
        bgs, *(lambda ws: (min(ws), max(ws)) if ws else (None, None))(
            [w["t_start"] for w in meta["windows"]] + [w["t_end"] for w in meta["windows"]]), top=8)
    summary = {
        "kind": meta.get("kind", "bench"),
        "created": meta.get("created"),
        "config": meta.get("config"),
        "camera": meta.get("camera"),
        "variants": meta.get("variants"),
        "runs": runs,
        "repeatability": rep,
        "comparisons": comparisons,
        "caps": caps,
        "background": bg_all,
        "suspects": winsys.suspects(bg_all),
        "stutter_attribution": stutters.attribute(
            events, bgs, *(lambda ws: (min(ws), max(ws)) if ws else (None, None))(
                [w["t_start"] for w in meta["windows"]] + [w["t_end"] for w in meta["windows"]])),
        "present_mode": metrics.present_mode_summary(base_frames),
        "columns": info.get("columns"),
        "surface": surf,
        "settings_key": _settings_key(fp),
        "replay_mismatch": _replay_mismatch(fp, _median_low1(per_variant[base])),
        "mismatch_v2": True,
        "display": fp.get("hardware", {}).get("display"),
        "gpus": fp.get("hardware", {}).get("gpus"),
    }
    (bdir / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    (bdir / "report.html").write_text(render_report(summary), encoding="utf-8")
    return summary


def zip_bundle(bdir: Path) -> Path:
    """One file to send back for analysis."""
    bdir = Path(bdir)
    zpath = bdir.parent / f"bench_{bdir.name}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(bdir.iterdir()):
            if p.is_file():
                z.write(p, f"{bdir.name}/{p.name}")
    return zpath


# ---------------------------------------------------------------------------- report

def _f(v, nd=1):
    if v is None:
        return "–"
    if isinstance(v, (int,)) and not isinstance(v, bool):
        return str(v)
    try:
        return f"{v:.{nd}f}"
    except (TypeError, ValueError):
        return html.escape(str(v))


RUN_COLS = (("avg_fps", "Avg FPS", 0), ("frametime_p50_ms", "Median ms", 2), ("frametime_p99_ms", "p99 ms", 2),
            ("low1_fps", "1% low", 0), ("pacing_error_ms", "Pacing err ms", 2), ("stutters", "Stutters", 0),
            ("display_p99_ms", "Display p99 ms", 2), ("not_displayed_pct", "Not shown %", 1),
            ("display_latency_p50_ms", "Latency p50 ms", 1), ("gpu_bound_pct", "GPU-bound %", 0),
            ("gpu_temp_c", "GPU °C", 0), ("gpu_clock_mhz", "GPU MHz", 0), ("cap_fps", "Capped at FPS", 0))


def render_report(s: dict) -> str:
    from . import abtest
    e = lambda x: html.escape(str(x))
    rep, pm = s["repeatability"], s["present_mode"]
    colour = {"pass": "#1e8449", "fail": "#c0392b", "capped": "#c0392b"}.get(rep.get("verdict"), "#b9770e")
    multi = len(s.get("variants") or []) > 1
    rows, n_by = [], {}
    for r in s["runs"]:
        w, m = r["window"], r["metrics"]
        if w["warmup"]:
            name = f"Warm-up {w['index'] + 1}"
        else:
            n_by[w.get("variant")] = n_by.get(w.get("variant"), 0) + 1
            name = (f"{e(w.get('variant'))} #{n_by[w.get('variant')]}" if multi else f"Run {n_by[w.get('variant')]}")
        style = ' style="color:#999"' if (w["warmup"] or not w["valid"]) else ""
        cells = "".join(f"<td>{_f(m.get(k), nd)}</td>" for k, _l, nd in RUN_COLS)
        note = e(w.get("note") or ("excluded (warm-up)" if w["warmup"] else ""))
        rows.append(f"<tr{style}><td>{name}</td>{cells}<td>{note}</td></tr>")
    head = "".join(f"<th>{e(l)}</th>" for _k, l, _nd in RUN_COLS)
    spread = "".join(
        f"<tr><td>{e(v['label'])}</td><td>{_f(v['median'], 2)}</td><td>{_f(v['min'], 2)} – {_f(v['max'], 2)}</td>"
        f"<td><b>{_f(v['spread_pct'])}%</b></td><td>{'yes' if v['trend'] else ''}</td></tr>"
        for v in rep.get("metrics", {}).values())
    ab = ""
    for c in s.get("comparisons") or []:
        vc = {"better": "#1e8449", "worse": "#c0392b", "mixed": "#b9770e"}.get(c["verdict"], "#444")
        mrows = "".join(f"<tr><td>{e(m['label'])}{'' if k in abtest.PRIMARY_KEYS else ' (info only)'}</td><td>{_f(m['a'], 2)}</td><td>{_f(m['b'], 2)}</td>"
                        f"<td>{_f(m['delta_pct'])}{'%' if m['delta_pct'] is not None else ''}</td><td>{e(m['result'])}</td></tr>"
                        for k, m in c["metrics"].items())
        ab += (f"<p style='font-size:17px;color:{vc}'><b>{e(c['verdict_text'])}</b></p><table><tr><th>Metric</th>"
               f"<th>{e(c['baseline'])}</th><th>{e(c['variant'])}</th><th>Change</th><th>Result</th></tr>{mrows}</table>")
    ab_html = f"<h2>A/B result</h2>{ab}" if ab else ""
    if pm.get("available"):
        pcol = {"good": "#1e8449", "warn": "#b9770e", "bad": "#c0392b"}[pm["level"]]
        modes = ", ".join(f"{e(k)} {v:.0f}%" for k, v in pm["modes"].items())
        sync = f"<p><b>{e(pm['sync_note'])}</b></p>" if pm.get("sync_note") else ""
        pm_html = (f"<p style='color:{pcol}'><b>{e(pm['dominant'])}</b> ({pm['dominant_pct']:.0f}% of frames)</p>"
                   f"<p>{e(pm['note'])}</p>{sync}<p class=m>All modes: {modes}. Sync interval: {_f(pm['sync_interval'], 0)}. "
                   f"Tearing allowed: {_f(pm['allows_tearing_pct'], 0)}% of frames. The app can't read whether G-Sync "
                   "is on; turn on NVIDIA's G-SYNC indicator to check each screen.</p>")
    else:
        pm_html = "<p>PresentMon did not report a present mode in this capture.</p>"
    surf = s.get("surface") or {}
    surf_html = ""
    if surf.get("available"):
        surf_html = (f"<p>iRacing renders <b>{surf['width']}×{surf['height']}</b> ({surf['megapixels']} megapixels) "
                     f"across {surf['monitors']} monitor{'s' if surf['monitors'] != 1 else ''}"
                     f"{' at ' + '/'.join(str(r) for r in surf['refresh']) + ' Hz' if surf.get('refresh') else ''}.</p>"
                     + (f"<p>{e(surf['note'])}</p>" if surf.get("note") else ""))
    caps = s.get("caps") or {}
    cap_html = ""
    if caps.get("available"):
        crow = "".join(f"<tr><td>{c['cap']}</td><td>{c['held_pct']:.1f}%</td><td>{'yes' if c['holds'] else ''}</td></tr>"
                       for c in caps["rows"])
        cap_html = (f"<h2>Which FPS cap would hold</h2><p><b>{e(caps['text'])}</b></p>"
                    f"<p class=m>Predicted from the uncapped runs. A cap also frees GPU time, so this is on the safe "
                    f"side; confirm with an A/B run at that cap.</p><table><tr><th>Cap</th><th>Frames in time</th>"
                    f"<th>Holds</th></tr>{crow}</table>")
    elif caps.get("reason"):
        cap_html = f"<h2>Which FPS cap would hold</h2><p>{e(caps['reason'])}</p>"
    bg = s.get("background") or []
    bg_html = ""
    if bg:
        brow = "".join(f"<tr><td>{e(b['name'])}</td><td>{b['avg_cores']:.2f}</td><td>{b['peak_cores']:.2f}</td>"
                       f"<td>{'' if b['protected'] else 'yes'}</td></tr>" for b in bg)
        sus = s.get("suspects") or []
        lead = (f"<p><b>Worth testing: {e(', '.join(sus))}.</b> Run the 'Background apps' A/B test to measure what "
                "they cost.</p>" if sus else "<p>No busy background apps found.</p>")
        bg_html = (f"<h2>Background apps while the replay ran</h2>{lead}<table><tr><th>Process</th><th>Avg cores</th>"
                   f"<th>Peak cores</th><th>Can be paused</th></tr>{brow}</table>")
    sa = s.get("stutter_attribution") or {}
    st_html = ""
    if sa.get("count"):
        tops = lambda ev: ", ".join(f"{t['name']} {t['cores']}" for t in ev.get("top", []))  # noqa: E731
        erow = "".join(
            f"<tr><td>{e(ev.get('variant', ''))} run {ev.get('run', '')}</td><td>{ev.get('at_s', '')} s</td>"
            f"<td>{ev.get('ms', '')} ms</td><td>{e(tops(ev))}</td>"
            f"<td>{_f(ev.get('driver_pct'))}%</td><td>{_f(ev.get('cpu_pct'), 0)}%</td></tr>" for ev in sa.get("events", []))
        st_html = (f"<h2>What was busy during stutters</h2><p><b>{e(sa.get('text', ''))}</b></p>"
                   f"<table><tr><th>Run</th><th>When</th><th>Frame</th><th>Busiest apps (cores)</th>"
                   f"<th>Driver time</th><th>Whole CPU</th></tr>{erow}</table>")
    cols = s.get("columns") or {}
    missing = [k for k in ("present_mode", "display_change", "until_displayed", "display_latency") if not cols.get(k)]
    col_note = (f"<p class=m>Columns not in this PresentMon log: {', '.join(missing)}.</p>" if missing else "")
    cfg, cam = s.get("config") or {}, s.get("camera") or {}
    title = "A/B test" if multi else "Replay bench"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{title} {e(s.get('created'))}</title>
<style>body{{font:14px Segoe UI,Arial,sans-serif;margin:24px;max-width:1200px;color:#222}}
table{{border-collapse:collapse;margin:8px 0 18px}}td,th{{border-bottom:1px solid #ddd;padding:4px 10px;text-align:right}}
td:first-child,th:first-child{{text-align:left}}.m{{color:#666}}h2{{margin-top:28px}}</style></head><body>
<h1>{title}</h1>
<p class=m>{e(s.get('created'))} · replay frame {_f(cfg.get('start_frame'), 0)} · car #{e(cam.get('car_number'))}
 · {_f(cfg.get('seconds'), 0)} s × {_f(cfg.get('runs'), 0)} runs{' per side' if multi else ''} (+{_f(cfg.get('warmup_runs'), 0)} warm-up)</p>
{surf_html}{ab_html}
<h2>Is the measurement repeatable?</h2>
<p style="font-size:17px;color:{colour}"><b>{e(rep.get('verdict_text', ''))}</b></p>
<table><tr><th>Metric</th><th>Median</th><th>Range</th><th>Spread</th><th>Steady drift</th></tr>{spread}</table>
{st_html}{bg_html}{cap_html}
<h2>Present path and tearing</h2>{pm_html}{col_note}
<h2>Every run</h2>
<table><tr><th></th>{head}<th>Note</th></tr>{''.join(rows)}</table>
<p class=m>p99 = 99th percentile frame time. 1% low = 1000 / p99. Pacing error = average change in frame time from one
frame to the next. Display = time between on-screen updates. Stutter = a frame over 2× the recent median and 8 ms longer.</p>
</body></html>"""
