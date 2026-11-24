"""Milestone 1: full PresentMon parsing, pacing metrics, repeatability and the replay bench loop."""
from __future__ import annotations

import csv
import json
import random
import time
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from iroptimizer import bench, frames as framelib, metrics

FIX = Path(__file__).parent / "fixtures" / "real"

V2_HEADER = ["Application", "ProcessID", "SwapChainAddress", "PresentRuntime", "SyncInterval", "PresentFlags",
             "AllowsTearing", "PresentMode", "CPUStartDateTime", "MsBetweenPresents", "MsBetweenDisplayChange",
             "MsInPresentAPI", "MsUntilDisplayed", "MsCPUBusy", "MsGPUBusy", "MsDisplayLatency"]


def _dt(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S.%f")


def write_v2(path: Path, t_start: float, t_end: float, ft_ms: float = 4.0, mode="Hardware: Independent Flip",
             jitter: float = 0.2, seed: int = 1, drop_every: int = 0, other_app_every: int = 0):
    rnd = random.Random(seed)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(V2_HEADER)
        t, i = t_start, 0
        while t < t_end:
            ft = ft_ms + rnd.uniform(-jitter, jitter)
            app = "dwm.exe" if other_app_every and i % other_app_every == 0 else "iRacingSim64DX11.exe"
            shown = not (drop_every and i % drop_every == 0)
            w.writerow([app, 1, "0x1", "DXGI", 0, 512, 1, mode, _dt(t), f"{ft:.3f}", f"{ft:.3f}" if shown else "NA",
                        "0.1", f"{ft * 1.5:.3f}" if shown else "NA", f"{ft * 0.6:.3f}", f"{ft * 0.95:.3f}", f"{ft * 2:.3f}"])
            t += ft / 1000
            i += 1


def test_load_v2_columns(tmp_path):
    p = tmp_path / "pm.csv"
    t0 = time.time()
    write_v2(p, t0, t0 + 1, drop_every=10, other_app_every=7)
    frs, info = framelib.load(p)
    assert info["columns"]["present_mode"] == "PresentMode"
    assert info["columns"]["display_change"] == "MsBetweenDisplayChange"
    assert all(fr.present_mode == "Hardware: Independent Flip" for fr in frs)
    assert any(fr.displayed is False for fr in frs) and any(fr.displayed for fr in frs)
    assert abs(frs[0].t - t0) < 0.01  # local-time timestamps parse back to epoch


def test_load_fork_v1_fixture_has_no_present_mode():
    frs, info = framelib.load(FIX / "session_20261101_194811.presentmon.csv.gz")
    assert len(frs) > 100_000
    assert info["columns"]["present_mode"] is None
    assert frs[0].present_mode is None and frs[0].cpu_busy is not None


def test_bundle_roundtrip(tmp_path):
    p = tmp_path / "pm.csv"
    t0 = time.time()
    write_v2(p, t0, t0 + 1, drop_every=5)
    frs, _ = framelib.load(p)
    out = framelib.write_bundle_csv(frs, tmp_path / "frames.csv.gz")
    back, info = framelib.load(out)
    assert info["format"] == "bundle" and len(back) == len(frs)
    assert back[3].present_mode == frs[3].present_mode
    assert [b.displayed for b in back] == [a.displayed for a in frs]
    assert abs(back[10].t - frs[10].t) < 1e-5


def test_run_metrics_on_real_session():
    frs, _ = framelib.load(FIX / "session_20261101_194811.presentmon.csv.gz")
    m = metrics.run_metrics(frs[:20_000])
    assert 30 < m["avg_fps"] < 400
    assert m["frametime_p99_ms"] >= m["frametime_p50_ms"]
    assert m["low1_fps"] == pytest.approx(1000 / m["frametime_p99_ms"])
    assert "display_p99_ms" not in m  # v1 fixture has no display column


def test_stutter_and_pacing():
    smooth = [framelib.Frame(t=i, frametime=5.0) for i in range(500)]
    m = metrics.run_metrics(smooth)
    assert m["stutters"] == 0 and m["pacing_error_ms"] == 0
    spiky = [framelib.Frame(t=i, frametime=50.0 if i in (200, 400) else 5.0) for i in range(500)]
    assert metrics.run_metrics(spiky)["stutters"] == 2


@pytest.mark.parametrize("mode,kind", [("Hardware: Independent Flip", "independent"),
                                       ("Hardware Composed: Independent Flip", "independent"),
                                       ("Composed: Flip", "composed_flip"),
                                       ("Composed: Copy with GPU GDI", "copy"),
                                       ("Other", "unknown")])
def test_present_mode_classification(mode, kind):
    assert metrics.classify_present_mode(mode) == kind


def test_repeatability_verdicts():
    mk = lambda p99: {"frametime_p99_ms": p99, "avg_fps": 200}
    assert metrics.repeatability([mk(8.0), mk(8.1), mk(8.2)])["verdict"] == "pass"
    bad = metrics.repeatability([mk(8.0), mk(9.0), mk(10.0), mk(11.0)])
    assert bad["verdict"] == "fail" and "heat soak" in bad["verdict_text"]
    assert metrics.repeatability([mk(8.0), mk(8.1)])["verdict"] == "insufficient"


# ---------------------------------------------------------------- bench loop with a fake sim and capture

class FakeDriver:
    def __init__(self, replay=True):
        self.replay, self.frame, self.playing_since, self.calls = replay, 1000, None, []

    def connect(self): self.calls.append("connect")
    def in_replay(self): return self.replay
    def disconnect(self): self.calls.append("disconnect")

    def current_frame(self):
        if self.playing_since is not None:
            return self.frame + int((time.time() - self.playing_since) * 60)
        return self.frame

    def current_camera(self): return bench.Camera(3, "42", 5, 0)
    def set_camera(self, cam): self.calls.append(("cam", cam.car_number))

    def seek(self, frame):
        self.pause()
        self.frame = frame
        self.calls.append(("seek", frame))

    def play(self): self.playing_since = time.time()

    def pause(self):
        if self.playing_since is not None:
            self.frame = self.current_frame()
            self.playing_since = None


class FakeCapture:
    def __init__(self, path, mode="Composed: Flip"):
        self.path, self.mode, self.error, self.started_at = Path(path), mode, None, None

    def start(self):
        self.started_at = time.time()
        return True

    def stop(self):
        write_v2(self.path, self.started_at, time.time(), ft_ms=2.0, mode=self.mode)


class NoGpu:
    samples: list = []
    def start(self): pass
    def stop(self): pass


def test_bench_end_to_end(tmp_path):
    drv = FakeDriver()
    cfg = bench.BenchConfig(seconds=0.6, runs=3, warmup_runs=1, settle_s=0.05, lead_trim_s=0.05)
    bdir = bench.run_bench(cfg, tmp_path, drv, FakeCapture, log=lambda s: None, sampler=NoGpu(),
                           fingerprint=lambda: {"hardware": {"display": {"width": 7680, "height": 1440, "refresh": 240}}})
    assert drv.calls.count(("seek", 1000)) == 4
    s = json.loads((bdir / "summary.json").read_text())
    assert len(s["runs"]) == 4 and s["runs"][0]["window"]["warmup"]
    assert all(r["metrics"]["frames"] > 100 for r in s["runs"])
    assert s["repeatability"]["runs"] == 3 and s["repeatability"]["verdict"] in ("pass", "fail")
    assert s["present_mode"]["kind"] == "composed_flip"
    assert (bdir / "report.html").read_text().count("Run ") >= 3
    assert (bdir / "presentmon_raw.csv.gz").is_file() and not (bdir / "presentmon_raw.csv").exists()
    z = bdir.parent / f"bench_{bdir.name}.zip"
    assert {"bench.json", "frames.csv.gz", "report.html"} <= {Path(n).name for n in zipfile.ZipFile(z).namelist()}
    # re-analysis from the bundle alone gives the same verdict
    assert bench.analyze_bundle(bdir)["repeatability"]["verdict"] == s["repeatability"]["verdict"]


def test_bench_refuses_when_not_in_replay(tmp_path):
    with pytest.raises(RuntimeError, match="not showing a replay"):
        bench.run_bench(bench.BenchConfig(), tmp_path, FakeDriver(replay=False), FakeCapture, log=lambda s: None,
                        sampler=NoGpu(), fingerprint=dict)


def test_bench_flags_replay_not_playing(tmp_path):
    class Stuck(FakeDriver):
        def play(self): pass  # replay never moves
    cfg = bench.BenchConfig(seconds=0.4, runs=3, warmup_runs=0, settle_s=0.0, lead_trim_s=0.0)
    bdir = bench.run_bench(cfg, tmp_path, Stuck(), FakeCapture, log=lambda s: None, sampler=NoGpu(), fingerprint=dict)
    s = json.loads((bdir / "summary.json").read_text())
    assert all(not r["window"]["valid"] for r in s["runs"])
    assert s["repeatability"]["verdict"] == "insufficient"
