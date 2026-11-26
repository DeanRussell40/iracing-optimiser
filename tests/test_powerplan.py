import json

from iroptimizer import powerplan

LIST = """Existing Power Schemes (* Active)
-----------------------------------
Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced)
Power Scheme GUID: 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c  (High performance)
Power Scheme GUID: 1a2b3c4d-0000-1111-2222-333344445555  (Ultimate Performance) *
"""


class FakePowercfg:
    def __init__(self, active="1a2b3c4d-0000-1111-2222-333344445555"):
        self.active, self.calls = active, []

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "/setactive":
            self.active = args[1]
            return ""
        return "\n".join(l.replace(" *", "") + (" *" if self.active in l else "") for l in LIST.splitlines())


def test_plans_and_pick():
    pc = FakePowercfg()
    found, active = powerplan.plans(pc)
    assert active == "1a2b3c4d-0000-1111-2222-333344445555" and found[active] == "Ultimate Performance"
    assert powerplan.pick_other(found, active) == powerplan.BALANCED
    assert powerplan.pick_other(found, powerplan.BALANCED) == powerplan.HIGH


def test_switch_restores_and_survives_a_crash(tmp_path):
    pc = FakePowercfg()
    sw = powerplan.Switcher(tmp_path, pc)
    sw.switch(powerplan.BALANCED, pc.active)
    original = json.loads((tmp_path / powerplan.STATE).read_text())["restore"]
    assert pc.active == powerplan.BALANCED and original.startswith("1a2b")
    # crash: a new start restores it
    assert powerplan.restore_leftover(tmp_path, pc) == original and pc.active == original
    assert not (tmp_path / powerplan.STATE).exists()
    assert powerplan.restore_leftover(tmp_path, pc) is None


def test_api_power_plan_test_runs_automatically_and_puts_the_plan_back(tmp_path, monkeypatch):
    from iroptimizer import bench
    monkeypatch.setattr(bench, "MIN_SECONDS", 0.1)
    from test_ui import make_api, wait_done
    pc = FakePowercfg()
    api = make_api(tmp_path, powercfg=pc)
    r = api.start("powerplan", {"seconds": 0.3, "runs": 3, "warmup": 0})
    assert r["ok"], r
    j = wait_done(api)
    assert j["state"] == "done", j
    assert pc.active.startswith("1a2b")  # put back
    assert ("/setactive", powerplan.BALANCED) in pc.calls
    assert j["title"] == "Balanced plan vs Ultimate Performance plan"
    assert not (tmp_path / powerplan.STATE).exists()
