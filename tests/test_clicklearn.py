"""Learned reopen clicks and the saved start point."""
from __future__ import annotations

import json

from iroptimizer import bench, clicklearn
from iroptimizer.clicklearn import Reopener
from test_simrestart import FakeSim, _short, ini_dir  # noqa: F401  (_short: short runs)
from test_ui import make_api, wait_done

PATCH = bytes([100]) * (clicklearn.PATCH_W * clicklearn.PATCH_H)


class Rec:
    def __init__(self, clicks, sim_at):
        self.clicks, self.sim_started_at, self.stopped = clicks, sim_at, False

    def stop(self):
        self.stopped = True


class Screen:
    """A fake iRacing window at (100, 50), 1000 x 600, whose pixels are `look` everywhere."""
    def __init__(self, look=PATCH, client=(100, 50, 1000, 600)):
        self.look, self.client, self.clicked, self.sim = look, client, [], False
        self.rec = None

    def record_clicks(self):
        return self.rec

    def find_ui(self):
        return 1 if self.client else None

    def client_rect(self, w):
        return self.client

    def foreground(self, w):
        pass

    def move(self, x, y):
        pass

    def grab(self, x, y, w, h):
        return self.look

    def click(self, x, y):
        self.clicked.append((x, y))
        self.sim = True

    def sim_running(self):
        return self.sim


def click(t, x, y, inside=True):
    return {"t": t, "inside": inside, "x": x, "y": y, "client": [1000, 600], "pw": clicklearn.PATCH_W,
            "ph": clicklearn.PATCH_H, "patch": PATCH}


def nowait(_s):
    return False


def test_learns_the_last_clicks_before_the_sim_started(tmp_path):
    scr = Screen()
    r = Reopener(tmp_path, scr)
    scr.rec = Rec([click(1, 5, 5), click(50, 10, 10, inside=False), click(80, 300, 200), click(82, 400, 220),
                   click(200, 1, 1)], sim_at=83)
    r.learn_start()
    assert r.learn_finish() == 2  # the old click (over 20 s before) and the one after the sim started are left out
    steps = r.recipe()["steps"]
    assert [(s["x"], s["y"]) for s in steps] == [(300, 200), (400, 220)] and steps[1]["gap_s"] == 2.0


def test_nothing_learned_when_the_sim_never_started(tmp_path):
    scr = Screen()
    r = Reopener(tmp_path, scr)
    scr.rec = Rec([click(1, 5, 5)], sim_at=None)
    r.learn_start()
    assert r.learn_finish() == 0 and not r.has_recipe()


def learned(tmp_path, scr):
    r = Reopener(tmp_path, scr, clock=iter(range(0, 10_000, 5)).__next__)
    scr.rec = Rec([click(10, 300, 200)], sim_at=11)
    r.learn_start()
    r.learn_finish()
    return r


def test_replays_the_click_in_window_coordinates(tmp_path):
    scr = Screen()
    r = learned(tmp_path, scr)
    scr.client = (200, 100, 1000, 600)  # the window moved; same size, so the click follows it
    assert r.replay(nowait) == (True, "")
    assert scr.clicked == [(500, 300)]


def test_clicks_nothing_when_the_window_looks_different(tmp_path):
    scr = Screen()
    r = learned(tmp_path, scr)
    scr.look = bytes([200]) * len(PATCH)
    ok, why = r.replay(nowait)
    assert not ok and "doesn't look the same" in why and scr.clicked == []
    scr.look, scr.client = PATCH, (100, 50, 800, 600)  # window size changed
    ok, why = r.replay(nowait)
    assert not ok and scr.clicked == []


# ---------------------------------------------------------------- in a test run

class FakeReopener:
    def __init__(self, recipe, works=True):
        self.recipe_, self.works, self.calls = recipe, works, []

    def has_recipe(self):
        return self.recipe_

    def replay(self, stop_wait, log=print):
        self.calls.append("replay")
        return (True, "") if self.works else (False, "the iRacing window doesn't look the same")

    def learn_start(self):
        self.calls.append("learn")

    def learn_stop(self):
        pass

    def learn_finish(self):
        self.calls.append("saved")
        self.recipe_ = True
        return 1


def test_restart_check_reopens_by_itself_once_learned(tmp_path):
    f = ini_dir(tmp_path) / "rendererDX11Monitor.ini"
    sim = FakeSim(f)
    ro = FakeReopener(recipe=False)
    api = make_api(tmp_path, sim_ctl=sim, sim_running=lambda: sim.running, reopener=ro)
    assert api.start("restart", {"seconds": 0.2, "runs": 3, "warmup": 0})["ok"]
    j = wait_done(api)
    assert j["state"] == "done", j["logs"][-4:]
    assert ro.calls[:2] == ["learn", "saved"] and ro.calls[2:] == ["replay"] * 3  # asked once, then automatic
    assert any("next time it's automatic" in l["text"] for l in j["logs"])


def test_failed_auto_reopen_falls_back_to_asking(tmp_path):
    f = ini_dir(tmp_path) / "rendererDX11Monitor.ini"
    sim = FakeSim(f)
    ro = FakeReopener(recipe=True, works=False)
    api = make_api(tmp_path, sim_ctl=sim, sim_running=lambda: sim.running, reopener=ro)
    assert api.start("restart", {"seconds": 0.2, "runs": 3, "warmup": 0})["ok"]
    j = wait_done(api)
    assert j["state"] == "done"
    assert ro.calls[:3] == ["replay", "learn", "saved"]
    assert any("Couldn't reopen it by itself" in l["text"] for l in j["logs"])


# ---------------------------------------------------------------- saved start point

class D:
    def __init__(self, rid="88766485"):
        self.rid = rid

    def replay_id(self):
        return self.rid


def test_start_point_is_saved_and_used_after_a_fresh_open(tmp_path):
    p = tmp_path / "start_points.json"
    cam = bench.Camera(3, "12", 4, 1)
    assert bench.start_point(p, D(), 250_000, cam) == (250_000, cam)  # placed by the user: saved
    assert json.loads(p.read_text())["88766485"]["frame"] == 250_000
    other = bench.Camera(0, "0", 1, 1)
    assert bench.start_point(p, D(), 12, other) == (250_000, cam)  # just opened: jumps to the saved spot
    assert bench.start_point(p, D(), 300_000, other) == (300_000, other)  # placed somewhere new: that wins
    assert bench.start_point(p, D("1"), 12, other) == (12, other)  # another replay: nothing saved
    assert bench.start_point(p, object(), 12, other) == (12, other)  # driver can't tell the replay
