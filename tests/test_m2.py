"""Milestone 2: A/B engine, cap prediction, focus slider, background apps, render surface, bench pre-flight."""
from __future__ import annotations

import json
import subprocess
import sys
import time

import psutil
import pytest

from iroptimizer import abtest, bench, metrics, winsys
from iroptimizer.frames import Frame
from test_bench import FakeCapture, FakeDriver, NoGpu


@pytest.fixture(autouse=True)
def _short_runs(monkeypatch):
    monkeypatch.setattr(bench, "MIN_SECONDS", 0.1)


class NoBg:
    samples = [{"t": time.time() + 1, "procs": {"SimHubWPF.exe": 0.9, "pythonw.exe": 0.3, "svchost.exe": 0.2}}]
    def start(self): pass
    def stop(self): pass


def run(p99, low1, st=0.0, fps=86.0, pacing=0.5):
    return {"frametime_p99_ms": p99, "low1_fps": low1, "stutters_per_min": st, "avg_fps": fps, "pacing_error_ms": pacing}


# ---------------------------------------------------------------- A/B verdicts

def test_compare_detects_real_improvement():
    a = [run(30.2, 33, 180), run(28.0, 35, 150), run(33.0, 30, 210)]
    b = [run(14.0, 71, 0.2), run(14.1, 70, 0), run(13.9, 72, 1)]
    c = abtest.compare(a, b, "SimHub running", "SimHub paused")
    assert c["verdict"] == "better"
    assert c["metrics"]["stutters_per_min"]["result"] == "better"
    assert "33 → 71" in c["verdict_text"]


def test_compare_noise_is_same():
    a = [run(14.0, 71), run(14.2, 70), run(13.9, 72)]
    b = [run(14.1, 71), run(13.8, 72), run(14.2, 70)]
    assert abtest.compare(a, b)["verdict"] == "same"


def test_compare_worse_and_overlap_guard():
    a = [run(14.0, 71), run(14.1, 71), run(14.0, 71)]
    assert abtest.compare(a, [run(16.0, 62), run(16.2, 61), run(16.1, 62)])["verdict"] == "worse"
    # median looks better but one run overlaps the baseline: not proven
    assert abtest.compare(a, [run(13.0, 77), run(13.1, 76), run(14.5, 69)])["metrics"]["frametime_p99_ms"]["result"] == "same"


def test_schedule_is_balanced():
    vs = [bench.Variant("A"), bench.Variant("B")]
    plan = bench.schedule(vs, runs=4, warmup_runs=1)
    assert plan[0] == (0, True)
    measured = [v for v, w in plan if not w]
    assert measured == [0, 1, 1, 0, 0, 1, 1, 0]
    assert bench.schedule([bench.Variant("A")], 3, 1) == [(0, True), (0, False), (0, False), (0, False)]


# ---------------------------------------------------------------- caps and focus

def test_predict_caps_uncapped_gpu_bound():
    # ~86 fps median with a tail to 14 ms (as on the real clean bench)
    fts = [11.6] * 9800 + [13.0] * 150 + [14.0] * 50
    c = abtest.predict_caps(fts, refresh_hz=240)
    assert c["best"] == 72  # 80 needs 12.5 ms: 2% of frames are slower
    assert all(r["cap"] <= 237 for r in c["rows"])
    assert not abtest.predict_caps([10.0] * 50)["available"]


def test_choose_respects_focus_and_stability():
    C = abtest.Candidate
    cands = [C("fast", 120, 2, True, 110), C("mid", 100, 5, True, 92), C("pretty", 80, 9, True, 75),
             C("prettier but stutters", 80, 12, False, 40)]
    assert abtest.choose(cands, 0).name == "fast"
    assert abtest.choose(cands, 4).name == "pretty"
    assert abtest.choose(cands, 2).name in ("fast", "mid", "pretty")
    assert abtest.choose([C("x", 60, 1, False)], 0) is None


# ---------------------------------------------------------------- winsys helpers

def test_surface_triple_non_surround():
    mons = [{"left": x, "top": 0, "width": 2560, "height": 1440, "refresh": 240, "primary": x == 2560}
            for x in (0, 2560, 5120)]
    s = winsys.describe_surface({"left": 0, "top": 0, "width": 7680, "height": 1440}, mons)
    assert s["monitors"] == 3 and s["width"] == 7680 and s["refresh"] == [240]
    assert "G-Sync" in s["note"] and not s["surround"]
    surround = winsys.describe_surface({"left": 0, "top": 0, "width": 7680, "height": 1440},
                                       [{"left": 0, "top": 0, "width": 7680, "height": 1440, "refresh": 144}])
    assert surround["surround"] and "note" not in surround


def test_background_summary_and_suspects():
    samples = [{"t": 1, "procs": {"SimHubWPF.exe": 1.0, "svchost.exe": 0.3, "pythonw.exe": 0.2}},
               {"t": 2, "procs": {"SimHubWPF.exe": 0.8, "pythonw.exe": 0.4, "Discord.exe": 0.01}}]
    rows = winsys.summarize_background(samples)
    assert rows[0]["name"] == "SimHubWPF.exe" and rows[0]["avg_cores"] == pytest.approx(0.9)
    assert winsys.suspects(rows) == ["SimHubWPF.exe", "pythonw.exe"]  # svchost is protected, Discord too small


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX sleep child")
def test_pauser_suspends_and_resumes(tmp_path):
    import shutil
    exe = tmp_path / "bgapp_m2test"
    shutil.copy(shutil.which("sleep"), exe)
    # started through a shell that exits, so the app isn't its parent (the app never pauses its own children)
    out = subprocess.run(["sh", "-c", f'"{exe}" 30 >/dev/null 2>&1 & echo $!'], capture_output=True, text=True)
    child = psutil.Process(int(out.stdout.strip()))
    time.sleep(0.2)
    try:
        name = psutil.Process(child.pid).name()
        pz = winsys.ProcessPauser(tmp_path)
        assert pz.pause([name, "python3", "pytest"]) == [name]  # this test's own process is never frozen
        assert psutil.Process(child.pid).status() == psutil.STATUS_STOPPED
        assert (tmp_path / winsys.PAUSED_STATE).is_file()
        pz.resume()
        assert psutil.Process(child.pid).status() != psutil.STATUS_STOPPED
        assert not (tmp_path / winsys.PAUSED_STATE).exists()
        # crash recovery
        psutil.Process(child.pid).suspend()
        (tmp_path / winsys.PAUSED_STATE).write_text(json.dumps({"pids": [child.pid]}))
        assert winsys.resume_leftovers(tmp_path) == 1
        assert psutil.Process(child.pid).status() != psutil.STATUS_STOPPED
    finally:
        child.kill()


def test_protected_processes_are_never_paused(tmp_path):
    assert winsys.ProcessPauser(tmp_path).pause(["iRacingSim64DX11.exe", "dwm.exe"]) == []


# ---------------------------------------------------------------- bench pre-flight and A/B loop

def test_preflight_problem_stops_bench(tmp_path):
    with pytest.raises(RuntimeError, match="administrator"):
        bench.run_bench(bench.BenchConfig(seconds=1, runs=3), tmp_path, FakeDriver(), FakeCapture,
                        log=lambda s: None, sampler=NoGpu(), fingerprint=dict, background=NoBg(),
                        preflight=lambda: "iRacing is running as administrator", surface=dict)


def test_validate_rejects_blank_and_tiny(monkeypatch):
    monkeypatch.setattr(bench, "MIN_SECONDS", 10)
    for bad in (bench.BenchConfig(seconds=0), bench.BenchConfig(seconds=5), bench.BenchConfig(runs=2)):
        with pytest.raises(ValueError):
            bench.validate(bad)


def test_seek_ignored_mentions_admin(tmp_path):
    class Ignores(FakeDriver):
        def seek(self, frame):  # elevated iRacing drops the command and keeps playing
            self.playing_since = self.playing_since or time.time()
    cfg = bench.BenchConfig(seconds=0.3, runs=3, warmup_runs=0, settle_s=0, seek_timeout_s=0.5)
    with pytest.raises(RuntimeError, match="administrator"):
        bench.run_bench(cfg, tmp_path, Ignores(), FakeCapture, log=lambda s: None, sampler=NoGpu(),
                        fingerprint=dict, background=NoBg(), preflight=None, surface=dict)


def test_ab_run_end_to_end(tmp_path):
    calls = []
    vs = [bench.Variant("Background apps running"),
          bench.Variant("Background apps paused", apply=lambda: calls.append("pause"), revert=lambda: calls.append("resume"))]
    cfg = bench.BenchConfig(seconds=0.5, runs=3, warmup_runs=1, settle_s=0.02, lead_trim_s=0.05)
    bdir = bench.run_bench(cfg, tmp_path, FakeDriver(), FakeCapture, log=lambda s: None, sampler=NoGpu(),
                           fingerprint=dict, background=NoBg(), preflight=None,
                           surface=lambda: {"available": True, "width": 7680, "height": 1440, "monitors": 3,
                                            "refresh": [240], "megapixels": 11.1, "note": "spans 3"},
                           variants=vs)
    # plan: warm A, A B B A A B -> pause/resume pairs balance, and the last revert always runs
    assert calls.count("pause") == calls.count("resume") >= 2
    s = json.loads((bdir / "summary.json").read_text(encoding="utf-8"))
    assert s["kind"] == "ab" and len(s["comparisons"]) == 1
    assert s["comparisons"][0]["variant"] == "Background apps paused"
    assert {r["window"]["variant"] for r in s["runs"]} == {v.name for v in vs}
    assert s["caps"]["available"]
    html = (bdir / "report.html").read_text(encoding="utf-8")
    assert "A/B result" in html and "7680×1440" in html


def test_sync_note_flags_tearing():
    frs = [Frame(t=i, frametime=11.6, present_mode="Hardware: Independent Flip", allows_tearing=True, sync_interval=0)
           for i in range(200)]
    pm = metrics.present_mode_summary(frs)
    assert "tearing" in pm["sync_note"] and "does not show that G-Sync" in pm["note"]
