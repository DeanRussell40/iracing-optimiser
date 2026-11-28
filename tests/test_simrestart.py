"""Closing and reopening the sim, and changing iRacing graphics settings for a test with a guaranteed put-back."""
from __future__ import annotations

import time

import pytest

from iroptimizer import bench, iniswitch, simctl, webapp
from test_ui import make_api, wait_done

INI = "[Graphics]\nShadowMapSize=2048 ; shadow detail\nMSAA=4\n[Replay]\nMSAA=4\n"


@pytest.fixture(autouse=True)
def _short(monkeypatch):
    monkeypatch.setattr(bench, "MIN_SECONDS", 0.1)
    monkeypatch.setattr(webapp, "AFTER_LOAD_S", 0.01)


def ini_dir(tmp_path):
    d = tmp_path / "ini"
    d.mkdir(exist_ok=True)
    (d / "rendererDX11Monitor.ini").write_text(INI, encoding="utf-8")
    return d


# ---------------------------------------------------------------- simctl

def test_close_sim_asks_then_ends_it():
    alive, asked, ended = {10}, [], []
    t = [0.0]

    def sleep(s): t[0] += s
    simctl.close_sim(polite_s=3, hard_s=3, pids=lambda: sorted(alive), post_close=lambda p: asked.append(p),
                     terminate=lambda p: (ended.append(p), alive.discard(p)), sleep=sleep, clock=lambda: t[0])
    assert asked == [10] and ended == [10]


def test_close_sim_that_closes_politely_is_not_ended():
    alive = {10}
    simctl.close_sim(pids=lambda: sorted(alive), post_close=lambda p: alive.discard(p),
                     terminate=lambda p: pytest.fail("ended a sim that closed"), sleep=lambda s: None)


def test_close_sim_gives_up_loudly():
    t = [0.0]
    with pytest.raises(RuntimeError, match="couldn't be closed"):
        simctl.close_sim(polite_s=1, hard_s=1, pids=lambda: [10], post_close=lambda p: 1, terminate=lambda p: None,
                         sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])


class SlowDriver:
    def __init__(self, ready_after):
        self.n, self.ready_after = 0, ready_after
    def connect(self):
        self.n += 1
        if self.n < self.ready_after:
            raise RuntimeError("not running")
    def in_replay(self): return True
    def current_frame(self): return 100
    def disconnect(self): pass


def test_wait_for_replay_retries_until_loaded():
    d = SlowDriver(3)
    simctl.wait_for_replay(d, lambda s: False)
    assert d.n == 3


def test_wait_for_replay_stops_when_asked():
    with pytest.raises(bench.Aborted):
        simctl.wait_for_replay(SlowDriver(99), lambda s: True)


# ---------------------------------------------------------------- iniswitch

def test_set_and_restore_keep_comments_and_other_sections(tmp_path):
    f = ini_dir(tmp_path) / "rendererDX11Monitor.ini"
    backups = []
    sw = iniswitch.IniSwitch(tmp_path, f, running=lambda: False, backup=lambda: backups.append(1))
    sw.set({"Graphics/MSAA": "2"})
    sw.set({"Graphics/MSAA": "1", "Graphics/ShadowMapSize": "1024"})  # originals kept from the first change
    txt = f.read_text(encoding="utf-8")
    assert "MSAA=1\n" in txt and "ShadowMapSize=1024 ; shadow detail" in txt and "[Replay]\nMSAA=4" in txt
    assert backups == [1]
    assert sw.restore() == {"Graphics/MSAA": "4", "Graphics/ShadowMapSize": "2048"}
    assert f.read_text(encoding="utf-8") == INI and not sw.state.exists()
    assert sw.restore() is None


def test_never_writes_while_the_sim_runs(tmp_path):
    f = ini_dir(tmp_path) / "rendererDX11Monitor.ini"
    with pytest.raises(RuntimeError, match="still running"):
        iniswitch.IniSwitch(tmp_path, f, running=lambda: True).set({"Graphics/MSAA": "2"})
    assert f.read_text(encoding="utf-8") == INI


def test_unknown_key_changes_nothing(tmp_path):
    f = ini_dir(tmp_path) / "rendererDX11Monitor.ini"
    with pytest.raises(RuntimeError, match="isn't in"):
        iniswitch.IniSwitch(tmp_path, f, running=lambda: False).set({"Graphics/Nope": "1"})
    assert f.read_text(encoding="utf-8") == INI


def test_leftover_restored_only_when_sim_is_closed(tmp_path):
    f = ini_dir(tmp_path) / "rendererDX11Monitor.ini"
    iniswitch.IniSwitch(tmp_path, f, running=lambda: False).set({"Graphics/MSAA": "0"})  # then a "crash"
    assert iniswitch.restore_leftover(tmp_path, running=lambda: True) is None
    assert "MSAA=0" in f.read_text(encoding="utf-8")
    assert iniswitch.restore_leftover(tmp_path, running=lambda: False) == {"Graphics/MSAA": "4"}
    assert f.read_text(encoding="utf-8") == INI


# ---------------------------------------------------------------- the test end to end

class FakeSim:
    """The sim as the test sees it: closing it makes the replay vanish until it is 'reopened'."""
    def __init__(self, ini):
        self.ini, self.running, self.seen = ini, True, []

    def close_sim(self, log=print):
        self.running = False

    def wait_for_replay(self, driver, stop_wait, timeout_s=0):
        self.seen.append(self.ini.read_text(encoding="utf-8"))  # what the sim would load
        self.running = True
        driver.connect()


def test_graphics_test_switches_settings_with_restarts_and_puts_them_back(tmp_path):
    f = ini_dir(tmp_path) / "rendererDX11Monitor.ini"
    sim = FakeSim(f)
    api = make_api(tmp_path, sim_ctl=sim, sim_running=lambda: sim.running)
    r = api.start("graphics", {"seconds": 0.2, "runs": 3, "warmup": 0, "label": "MSAA 2x",
                               "changes": {"Graphics/MSAA": "2"}})
    assert r["ok"], r
    j = wait_done(api)
    assert j["state"] == "done", j["logs"][-4:]
    loaded = ["MSAA=2\n" in s for s in sim.seen]
    assert loaded == [False, True, False, True], loaded  # A, then B A B blocks: every switch is a fresh start
    assert f.read_text(encoding="utf-8") == INI and not (tmp_path / iniswitch.STATE).exists()
    assert not sim.running  # closed so its exit can't write the test values back
    assert any("settling run" in l["text"] for l in j["logs"])
    assert list((tmp_path / "ini" / "iroptimizer_backups").iterdir())


def test_graphics_test_rejects_unknown_and_unsafe_values(tmp_path):
    ini_dir(tmp_path)
    api = make_api(tmp_path, sim_ctl=FakeSim(None), sim_running=lambda: False)
    assert "isn't in" in api.start("graphics", {"seconds": 0.2, "changes": {"Graphics/Nope": "1"}})["error"]
    assert not api.start("graphics", {"seconds": 0.2, "changes": {"Graphics/MSAA": "2\n[x]"}})["ok"]
    assert not api.start("graphics", {"seconds": 0.2, "changes": {}})["ok"]


def test_restart_check_restarts_at_every_switch(tmp_path):
    f = ini_dir(tmp_path) / "rendererDX11Monitor.ini"
    sim = FakeSim(f)
    api = make_api(tmp_path, sim_ctl=sim, sim_running=lambda: sim.running)
    assert api.start("restart", {"seconds": 0.2, "runs": 3, "warmup": 0})["ok"]
    assert wait_done(api)["state"] == "done"
    assert len(sim.seen) == 4 and f.read_text(encoding="utf-8") == INI


def test_plain_key_changes_race_and_replay_copies(tmp_path):
    """iRacing uses [Replay Graphics] while showing a replay, so a test on a replay must change that copy too."""
    f = ini_dir(tmp_path) / "rendererDX11Monitor.ini"
    sw = iniswitch.IniSwitch(tmp_path, f, running=lambda: False)
    sw.set({"MSAA": "0"})
    txt = f.read_text(encoding="utf-8")
    assert "[Graphics]\nShadowMapSize=2048 ; shadow detail\nMSAA=0\n[Replay]\nMSAA=0\n" == txt
    assert sw.restore() == {"Graphics/MSAA": "4", "Replay/MSAA": "4"}
    assert f.read_text(encoding="utf-8") == INI


# ---------------------------------------------------------------- floor probe

from iroptimizer import floors  # noqa: E402


def test_floor_uses_second_worst_run():
    assert floors.floor_low1([130, 118, 125, 128, 131]) == 125
    assert floors.floor_low1([130, 118, 125]) == 118
    assert floors.floor_low1([]) is None


def test_floor_verdicts():
    assert floors.verdict([98] * 5, [135] * 5)["verdict"] == "reachable"
    v = floors.verdict([98] * 5, [110, 111, 112, 113, 114])
    assert v["verdict"] == "not_reachable" and "CPU or the sim" in v["text"] and v["lowest"] == 111
    assert floors.verdict([125] * 5, [140] * 5)["verdict"] == "met"
    assert floors.verdict([], [140])["verdict"] == "unknown"


def test_floor_probe_runs_lowest_against_yours_and_saves_the_answer(tmp_path):
    f = ini_dir(tmp_path) / "rendererDX11Monitor.ini"
    f.write_text(INI + "[Graphics Options]\nShaderQuality=3\n", encoding="utf-8")
    original = f.read_text(encoding="utf-8")
    sim = FakeSim(f)
    api = make_api(tmp_path, sim_ctl=sim, sim_running=lambda: sim.running)
    r = api.start("floorprobe", {"seconds": 0.2, "runs": 3, "warmup": 0, "floor": 120})
    assert r["ok"] and "120" in r["title"], r
    j = wait_done(api)
    assert j["state"] == "done", j["logs"][-4:]
    assert any("ShaderQuality=0" in s for s in sim.seen)  # only keys the file has are changed
    assert f.read_text(encoding="utf-8") == original
    assert "floor check" in j["result"]["lines"][0] or "FPS" in j["result"]["lines"][0]
    assert (tmp_path / "floor_check.json").is_file()
