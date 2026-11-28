"""The floors-then-quality picker."""
from __future__ import annotations

import json

import pytest

from iroptimizer import bench, picker, webapp
from test_simrestart import FakeSim, ini_dir
from test_ui import make_api, wait_done

ROWS = [
    {"name": "MSAA 4x to 2x", "changes": {"MSAASamples": "2"}, "real": True, "low1_gain": 12},
    {"name": "Grandstands and crowd one step lower", "changes": {"GrandstandDetail": "1", "CrowdDetail": "0"},
     "real": True, "low1_gain": 6},
    {"name": "Particles one step lower", "changes": {"ParticleDetail": "1"}, "real": False, "low1_gain": 1},
    {"name": "Cars drawn 20 to 28", "changes": {"MaxCarsToDraw": "28"}, "real": False, "low1_gain": -5},
]


def test_choose_gives_up_least_important_first_and_stops_when_enough():
    ch = picker.choose({"verdict": "reachable", "yours": 110}, ROWS, 120)
    assert ch["lowered"] == ["Grandstands and crowd one step lower", "MSAA 4x to 2x"]
    ch = picker.choose({"verdict": "reachable", "yours": 124}, ROWS, 120)
    assert ch["lowered"] == ["Grandstands and crowd one step lower"]  # 124 + 4.2 clears 126
    assert "Particles" not in str(ch) and "MaxCarsToDraw" not in ch["changes"]


def test_choose_lowers_nothing_when_graphics_cant_help_or_dont_need_to():
    assert picker.choose({"verdict": "not_reachable", "yours": 98}, ROWS)["changes"] == {}
    assert picker.choose({"verdict": "met", "yours": 130}, ROWS)["changes"] == {}


def run(p50=8.3, p99=10.0, st=0.0, on=99.8, fps=120.0, low1=None):
    return {"frametime_p50_ms": p50, "frametime_p99_ms": p99, "stutters_per_min": st, "on_time_pct": on,
            "avg_fps": fps, "low1_fps": low1 if low1 is not None else 1000 / p99}


def test_smoothness_rules():
    assert picker.smooth([run()] * 5, capped=True)[0]
    assert not picker.smooth([run(on=97)] * 5, capped=True)[0]
    assert picker.smooth([run(on=97)] + [run()] * 4, capped=True)[0]  # one bad run is forgiven
    assert not picker.smooth([run(st=3)] * 5, capped=False)[0]
    assert not picker.smooth([run(p50=8, p99=16)] * 5, capped=False)[0]
    assert not picker.smooth([run()] * 2, capped=True)[0]


def test_judge_prefers_looks_then_lets_cheap_detail_buy_the_preferred_fps():
    opts = [{"name": "Your settings", "quality": 10, "capped": False, "low_priority_only": False},
            {"name": "Picked, 120 cap", "quality": 9, "capped": True, "cap": 120, "low_priority_only": True},
            {"name": "Your settings, 80 cap", "quality": 10, "capped": True, "cap": 80, "low_priority_only": False}]
    pv = {"Your settings": [run(p50=9, p99=16, fps=110)] * 5,             # not smooth
          "Picked, 120 cap": [run(p50=8.33, p99=8.6, fps=120)] * 5,
          "Your settings, 80 cap": [run(p50=12.5, p99=12.9, fps=80)] * 5}
    res = picker.judge(opts, pv, 120)
    assert res["pick"] == "Picked, 120 cap" and "drive" not in res["text"]
    opts[1]["low_priority_only"] = False  # giving up MSAA: looks win, and the 80 pick asks for a drive test
    res = picker.judge(opts, pv, 120)
    assert res["pick"] == "Your settings, 80 cap" and "drive a few laps" in res["text"]
    assert "smooth" in picker.row_line(res["rows"][2]) and "not smooth" in picker.row_line(res["rows"][0])


def test_judge_with_nothing_smooth_changes_nothing():
    res = picker.judge([{"name": "A", "quality": 10, "capped": False}], {"A": [run(st=5)] * 5})
    assert res["pick"] is None and "stay as they are" in res["text"]


@pytest.fixture(autouse=True)
def _short(monkeypatch):
    monkeypatch.setattr(bench, "MIN_SECONDS", 0.1)
    monkeypatch.setattr(webapp, "AFTER_LOAD_S", 0.01)


INI = ("[Graphics Options]\nMSAASamples=4\nGrandstandDetail=2\nCrowdDetail=1\nLimitFrameRate=0\nDesiredFPSLimit=236\n"
       "[Replay Graphics]\nGrandstandDetail=2\nCrowdDetail=1\n")


def test_pick_needs_earlier_results(tmp_path):
    ini_dir(tmp_path)
    api = make_api(tmp_path, sim_ctl=FakeSim(None), sim_running=lambda: False)
    assert "floor check" in api.start("pick", {"seconds": 0.2})["error"]


def test_pick_runs_options_and_restores(tmp_path):
    f = ini_dir(tmp_path) / "rendererDX11Monitor.ini"
    f.write_text(INI, encoding="utf-8")
    (tmp_path / "floor_check.json").write_text(json.dumps({"verdict": "reachable", "yours": 110, "floor": 120}))
    (tmp_path / "cost_rank.json").write_text(json.dumps({"rows": ROWS}))
    sim = FakeSim(f)
    api = make_api(tmp_path, sim_ctl=sim, sim_running=lambda: sim.running)
    r = api.start("pick", {"seconds": 0.2})
    assert r["ok"], r
    j = wait_done(api, timeout=60)
    assert j["state"] == "done", j["logs"][-4:]
    loads = sim.seen
    assert any("DesiredFPSLimit=80" in t and "MSAASamples=4" in t for t in loads)
    assert any("DesiredFPSLimit=120" in t and "MSAASamples=2" in t and "CrowdDetail=0" in t for t in loads)
    assert f.read_text(encoding="utf-8") == INI
    assert (tmp_path / "pick.json").is_file()
    assert any("Lowered what costs frames" in l for l in j["result"]["lines"])
