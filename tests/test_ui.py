"""New main window: dashboard view model and the Api the page calls."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from iroptimizer import bench, dashboard, webapp
from test_bench import FakeCapture, FakeDriver, NoGpu


@pytest.fixture(autouse=True)
def _short_runs(monkeypatch):
    monkeypatch.setattr(bench, "MIN_SECONDS", 0.1)


def _run(variant, low1, p99, st, gpu=98.0, fps=86.0, warm=False, valid=True):
    return {"window": {"variant": variant, "warmup": warm, "valid": valid},
            "metrics": {"avg_fps": fps, "low1_fps": low1, "frametime_p99_ms": p99, "stutters_per_min": st,
                        "gpu_bound_pct": gpu, "gpu_util_pct": gpu, "pacing_error_ms": 0.5}}


def write_bundle(root: Path, name: str, created: str, variants: list[dict], runs: list[dict], comparisons=(),
                 rep=None, suspects=(), sync_note="", caps=None):
    d = root / "bench" / name
    d.mkdir(parents=True)
    s = {"kind": "ab" if comparisons else "bench", "created": created, "variants": variants, "runs": runs,
         "comparisons": list(comparisons), "repeatability": rep or {"verdict": "pass", "verdict_text": "PASS: 1.4%",
                                                                      "metrics": {"frametime_p99_ms": {"spread_pct": 1.4}}},
         "caps": caps or {"available": True, "best": 72, "text": "Your PC holds a cap of 72 FPS on this replay."},
         "suspects": list(suspects), "background": [],
         "present_mode": {"sync_note": sync_note},
         "surface": {"available": True, "width": 7680, "height": 1440, "monitors": 3}}
    (d / "summary.json").write_text(json.dumps(s), encoding="utf-8")
    (d / "report.html").write_text("<html>report</html>", encoding="utf-8")
    return d


def history_dir(tmp_path: Path) -> Path:
    """A clean-run history like Dean's first evening: baseline with busy apps, then the background A/B."""
    write_bundle(tmp_path, "20261126_155000", "2026-11-26T15:50:00",
                 [{"name": "Current settings"}],
                 [_run("Current settings", 33, 30.2, 180, fps=76, warm=True)] +
                 [_run("Current settings", v, 30.0, 180, fps=76) for v in (33, 32, 34)],
                 suspects=["SimHubWPF.exe", "simmixer.exe"],
                 sync_note="Sync interval 0 and tearing allowed: frames are shown with tearing.")
    cmp = {"baseline": dashboard.BG_RUNNING, "variant": dashboard.BG_PAUSED, "verdict": "better",
           "verdict_text": "Background apps paused is better than Background apps running: 1% low 33 to 71 fps.",
           "metrics": {"low1_fps": {"a": 33, "b": 71}}}
    write_bundle(tmp_path, "20261126_161400", "2026-11-26T16:14:00",
                 [{"name": dashboard.BG_RUNNING}, {"name": dashboard.BG_PAUSED, "description": "SimHubWPF.exe, simmixer.exe"}],
                 [_run(dashboard.BG_RUNNING, v, 30.0, 180, fps=76) for v in (33, 32, 34)] +
                 [_run(dashboard.BG_PAUSED, v, 14.0, 0.2, fps=86) for v in (71, 70, 72)],
                 comparisons=[cmp], suspects=["SimHubWPF.exe"],
                 sync_note="Sync interval 0 and tearing allowed: frames are shown with tearing.")
    return tmp_path


# ---------------------------------------------------------------- dashboard

def test_empty_view_asks_for_first_measurement(tmp_path):
    v = dashboard.view(tmp_path / "bench")
    assert v["next"]["test"] == "auto" and v["chart"] == [] and v["tests"] == []
    assert v["readouts"][0]["value"] == "–"
    assert any(f["title"] == "Nothing measured yet" for f in v["findings"])


def test_view_after_background_ab(tmp_path):
    v = dashboard.view(history_dir(tmp_path) / "bench", focus=1)
    # readouts show the proven-better setup, with the gain since the first run
    assert v["readouts"][1]["value"] == "71 fps" and v["readouts"][1]["note"] == "+38 since first run"
    assert v["readouts"][3]["value"] == "GPU"
    assert v["headline"] == "Background apps were costing you smoothness"
    titles = [f["title"] for f in v["findings"]]
    assert "Background apps were costing you smoothness" in titles
    assert "Frames are shown with tearing" in titles and "The graphics card is the limit" in titles
    # next step after background: the G-Sync A/B the tearing calls for
    assert v["next"]["test"] == "guided" and v["next"]["b"] == "G-Sync on"
    assert [p["value"] for p in v["chart"]] == [33, 33, 71]
    assert v["tests"][0]["verdict"] == "Better" and v["tests"][-1]["test"] == "Repeatability check"
    assert "7680 × 1440" in v["summary"][0]
    assert "no compromises" in v["summary"][-1]
    assert "repeatable to 1.4%" in v["subtitle"]


def test_next_step_tests_busy_apps_first(tmp_path):
    write_bundle(tmp_path, "b1", "2026-11-26T15:50:00", [{"name": "Current settings"}],
                 [_run("Current settings", 33, 30, 180) for _ in range(3)], suspects=["SimHubWPF.exe"])
    v = dashboard.view(tmp_path / "bench")
    assert v["next"]["test"] == "background"
    f = next(f for f in v["findings"] if f["title"].startswith("Busy background apps"))
    assert f["action"]["id"] == "test:background"


def test_capped_baseline_is_flagged(tmp_path):
    write_bundle(tmp_path, "b1", "2026-11-26T15:50:00", [{"name": "Current settings"}],
                 [_run("Current settings", 118, 8.6, 0) for _ in range(3)],
                 rep={"verdict": "capped", "verdict_text": "Capped at 120 fps."})
    v = dashboard.view(tmp_path / "bench")
    assert v["next"]["label"] == "Measure again uncapped"
    assert v["findings"][0]["title"].startswith("A frame rate cap")


def test_elevation_problem_comes_first(tmp_path):
    v = dashboard.view(tmp_path / "bench", elevation_problem="iRacing runs as administrator.")
    assert v["findings"][0]["level"] == "bad" and v["findings"][0]["action"]["id"] == "relaunch_admin"


def test_broken_bundles_are_skipped(tmp_path):
    (tmp_path / "bench" / "junk").mkdir(parents=True)
    (tmp_path / "bench" / "junk" / "summary.json").write_text("{not json", encoding="utf-8")
    assert dashboard.load_history(tmp_path / "bench") == []


# ---------------------------------------------------------------- Api

class FakePauser:
    def __init__(self, d): self.calls = []
    def pause(self, names): self.calls.append(("pause", names)); return names
    def resume(self): self.calls.append(("resume",))


def make_api(tmp_path, **kw):
    return webapp.Api(tmp_path, capture_factory=FakeCapture, driver_factory=FakeDriver,
                      elevation=lambda: None, ini_dir=lambda: tmp_path / "ini",
                      bench_overrides={"settle_s": 0.02, "lead_trim_s": 0.05},
                      run_bench=lambda *a, **k: bench.run_bench(*a, sampler=NoGpu(), fingerprint=dict, preflight=None,
                                                                surface=dict, background=NoBgSampler(), **k), **kw)


class NoBgSampler:
    samples: list = []
    def start(self): pass
    def stop(self): pass


def wait_done(api, timeout=20):
    t = time.time()
    while time.time() - t < timeout:
        j = api.poll(0)["job"]
        if j["state"] != "running":
            return j
        if j["prompt"]:
            api.answer(j["prompt"]["id"])
        time.sleep(0.05)
    raise AssertionError("test never finished")


def test_api_view_never_raises(tmp_path):
    api = make_api(tmp_path)
    v = api.view()
    assert "error" not in v and v["backups"]["available"] is False and len(v["focus_labels"]) == 5


def test_api_rejects_bad_input(tmp_path):
    api = make_api(tmp_path)
    assert not api.start("format_c")["ok"]
    assert "length" in api.start("repeat", {"seconds": 0.01})["error"]
    assert not api.start("background", {"names": []})["ok"]
    assert not api.start("guided", {"a": "x", "b": "x"})["ok"]
    assert not api.open_report("../../etc")["ok"]
    assert not api.open_report("nope")["ok"]


def test_api_elevation_blocks_start(tmp_path):
    api = webapp.Api(tmp_path, elevation=lambda: "iRacing runs as administrator.", capture_factory=FakeCapture)
    r = api.start("repeat", {"seconds": 1, "runs": 3})
    assert not r["ok"] and r["fix"] == "relaunch_admin"


def test_api_guided_ab_prompts_and_finishes(tmp_path):
    api = make_api(tmp_path)
    r = api.start("guided", {"seconds": 0.3, "runs": 3, "warmup": 0, "a": "G-Sync off", "b": "G-Sync on"})
    assert r["ok"] and r["title"] == "G-Sync on vs G-Sync off"
    assert not api.start("repeat", {"seconds": 0.3})["ok"]  # one test at a time
    j = wait_done(api)
    assert j["state"] == "done", j["logs"][-3:]
    assert any("Set up B: G-Sync on" in l["text"] for l in j["logs"])
    opened = []
    api._opener = opened.append
    assert api.open_report(j["result"]["id"])["ok"] and opened[0].name == "report.html"
    v = api.view()
    assert v["tests"][0]["test"] == "G-Sync on vs G-Sync off"


def test_api_background_pauses_and_resumes(tmp_path):
    pz = FakePauser(tmp_path)
    api = make_api(tmp_path, pauser_factory=lambda d: pz)
    assert api.start("background", {"seconds": 0.3, "runs": 3, "warmup": 0, "names": ["SimHubWPF.exe"]})["ok"]
    assert wait_done(api)["state"] == "done"
    assert pz.calls.count(("pause", ["SimHubWPF.exe"])) == pz.calls.count(("resume",)) >= 1


def test_api_stop_aborts(tmp_path):
    api = make_api(tmp_path)
    assert api.start("guided", {"seconds": 5, "runs": 3, "warmup": 0})["ok"]
    time.sleep(0.3)
    assert api.stop()["ok"]
    j = api.poll(0)["job"]
    t = time.time()
    while j["state"] == "running" and time.time() - t < 10:
        time.sleep(0.05)
        j = api.poll(0)["job"]
    assert j["state"] == "stopped"


def test_api_backups_and_restore(tmp_path):
    ini = tmp_path / "ini"
    ini.mkdir()
    (ini / "app.ini").write_text("[Graphics]\nx=1\n", encoding="utf-8")
    api = make_api(tmp_path)
    b = api.backups()
    assert b["available"] and len(b["items"]) == 1 and b["items"][0]["original"]
    (ini / "app.ini").write_text("[Graphics]\nx=2\n", encoding="utf-8")
    r = api.restore(b["items"][0]["id"])
    assert r["ok"], r
    assert "x=1" in (ini / "app.ini").read_text(encoding="utf-8")
    assert not api.restore("../evil")["ok"]


def test_webui_files_present_and_csp_strict():
    html = (webapp.WEBUI / "index.html").read_text(encoding="utf-8")
    assert "Content-Security-Policy" in html and "default-src 'self'" in html and "http" not in html.split("<body>")[1]
    js = (webapp.WEBUI / "app.js").read_text(encoding="utf-8")
    # every method the page calls exists on the Api
    import re
    for name in set(re.findall(r'call\("(\w+)"', js)):
        assert callable(getattr(webapp.Api, name, None)), name


def test_page_handles_every_next_step():
    js = (webapp.WEBUI / "app.js").read_text(encoding="utf-8")
    for t in ("auto", "background", "guided", "repeat"):
        assert f'"{t}"' in js, t


# ---------------------------------------------------------------- the app never blames or pauses itself

def test_own_family_excluded_from_background(monkeypatch):
    import subprocess, sys as _sys
    from iroptimizer import winsys
    child = subprocess.Popen([_sys.executable, "-c", "import time\nwhile True: pass"])  # a busy child, like WebView2
    try:
        smp = winsys.BackgroundSampler(interval_s=0.2)
        smp.start()
        time.sleep(1.2)
        smp.stop()
        import psutil
        name = psutil.Process(child.pid).name()
        seen = {n for x in smp.samples for n in x["procs"]}
        assert name not in seen or all(x["procs"].get(name, 0) < 0.5 for x in smp.samples)
        assert child.pid in winsys.own_family()
    finally:
        child.kill()
    assert winsys.is_protected("iRacingOptimizer (1).exe")


# ---------------------------------------------------------------- self-update

def test_parse_sums():
    from iroptimizer import selfupdate
    h = "a" * 64
    assert selfupdate.parse_sums(f"{h}  iRacingOptimizer.exe\r\n") == h
    assert selfupdate.parse_sums("garbage") is None


def test_check_and_install_swap(tmp_path):
    from iroptimizer import selfupdate
    exe = tmp_path / "iRacingOptimizer (1).exe"
    exe.write_bytes(b"old build")
    (tmp_path / "iRacingOptimizer.exe").write_bytes(b"older copy")
    new = b"new build"
    import hashlib
    want = hashlib.sha256(new).hexdigest()
    assert selfupdate.check(exe=exe, fetch=lambda: want)["available"]
    assert not selfupdate.check(exe=exe, fetch=lambda: hashlib.sha256(b"old build").hexdigest())["available"]
    assert not selfupdate.check(exe=exe, fetch=lambda: (_ for _ in ()).throw(OSError("offline")))["available"]
    launched = []
    target = selfupdate.install(want, exe=exe, download=lambda p: p.write_bytes(new), launch=launched.append)
    assert target.name == "iRacingOptimizer.exe" and target.read_bytes() == new and launched == [target]
    assert not exe.exists() and (tmp_path / selfupdate.OLD_NAME).read_bytes() == b"old build"
    selfupdate.cleanup(target)
    assert not (tmp_path / selfupdate.OLD_NAME).exists()


def test_install_refuses_bad_download(tmp_path):
    from iroptimizer import selfupdate
    exe = tmp_path / "iRacingOptimizer.exe"
    exe.write_bytes(b"old build")
    with pytest.raises(RuntimeError, match="checksum"):
        selfupdate.install("b" * 64, exe=exe, download=lambda p: p.write_bytes(b"tampered"), launch=lambda p: None)
    assert exe.read_bytes() == b"old build" and not (tmp_path / selfupdate.PART_NAME).exists()


def test_api_update_flow(tmp_path):
    class Up:
        installed = []
        def cleanup(self): pass
        def check(self): return {"available": True, "sha": "c" * 64}
        def install(self, sha, progress=None): self.installed.append(sha)
    up = Up()
    api = make_api(tmp_path, updater=up)
    assert api.update_status()["state"] == "checking"
    api._check_update()
    assert api.update_status()["available"]
    assert api.update_install()["ok"] and up.installed == ["c" * 64]
    assert not api.update_install()["ok"]  # not twice


def test_old_bundles_are_reanalysed(tmp_path):
    api = make_api(tmp_path)
    assert api.start("repeat", {"seconds": 0.3, "runs": 3, "warmup": 0})["ok"]
    j = wait_done(api)
    d = tmp_path / "bench" / j["result"]["id"]
    s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    s.pop("stutter_attribution")
    (d / "summary.json").write_text(json.dumps(s), encoding="utf-8")  # as an older build wrote it
    api2 = make_api(tmp_path)
    api2.view()
    assert "stutter_attribution" in json.loads((d / "summary.json").read_text(encoding="utf-8"))
