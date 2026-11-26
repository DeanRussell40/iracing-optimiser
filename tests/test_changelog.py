import json

from iroptimizer import changelog, dashboard


def audit_result(**vals):
    items = [{"area": "Windows", "name": k.replace("_", " "), "value": v, "level": "info"} for k, v in vals.items()]
    items.append({"area": "Windows", "name": "Timer resolution", "value": "0.50 ms", "level": "info"})
    items.append({"area": "iRacing", "name": "Replay and race graphics differ", "value": "2", "level": "warn",
                  "summary": True})
    return {"items": items}


def test_first_snapshot_sets_the_start_and_later_changes_are_logged(tmp_path):
    assert changelog.record(tmp_path, changelog.snapshot(audit_result(Power_plan="Balanced"))) is None
    assert changelog.load(tmp_path) == []
    assert changelog.record(tmp_path, changelog.snapshot(audit_result(Power_plan="Balanced"))) is None
    e = changelog.record(tmp_path, changelog.snapshot(audit_result(Power_plan="High performance", HAGS="On")),
                         "a test started")
    assert e["changes"] == [{"area": "Windows", "name": "HAGS", "from": None, "to": "On"},
                            {"area": "Windows", "name": "Power plan", "from": "Balanced", "to": "High performance"}]
    assert changelog.describe(e) == "HAGS: now On; Power plan: Balanced to High performance"
    assert [x["kind"] for x in changelog.load(tmp_path)] == ["change"]
    snap = json.loads((tmp_path / changelog.LAST).read_text(encoding="utf-8"))
    assert "Windows/Timer resolution" not in snap and "iRacing/Replay and race graphics differ" not in snap


def test_unreadable_values_are_not_changes(tmp_path):
    changelog.record(tmp_path, changelog.snapshot(audit_result(Power_plan="Balanced")))
    assert changelog.record(tmp_path, changelog.snapshot(audit_result(Power_plan="Couldn't read"))) is not None
    # an unreadable value is left out, so it reads as "no longer read", not as a new value
    assert changelog.load(tmp_path)[-1]["changes"][0]["to"] is None


def test_notes(tmp_path):
    changelog.add_note(tmp_path, "  SimHub screen\\ncapture off  ")
    e = changelog.load(tmp_path)[0]
    assert e["kind"] == "note" and e["text"] == "SimHub screen\\ncapture off"
    try:
        changelog.add_note(tmp_path, "   ")
        raise AssertionError("empty note accepted")
    except ValueError:
        pass
    (tmp_path / changelog.LOG).open("a").write("not json\n")
    assert len(changelog.load(tmp_path)) == 1


def test_timeline_places_changes_between_tests():
    tests = [{"kind": "test", "t": "2026-11-26T10:00:00", "when": "x"},
             {"kind": "test", "t": "2026-11-26T08:00:00", "when": "y"}]
    changes = [{"kind": "change", "t": "2026-11-26T09:00:00",
                "changes": [{"area": "Windows", "name": "HAGS", "from": "On", "to": "Off"}]},
               {"kind": "note", "t": "2026-11-26T11:00:00", "text": "new cable"}]
    tl = dashboard.timeline(tests, changes)
    assert [r["t"][11:13] for r in tl] == ["11", "10", "09", "08"]
    assert tl[2]["text"] == "HAGS: On to Off" and tl[0]["title"] == "Note"


def test_api_records_and_notes(tmp_path):
    from test_ui import make_api
    from iroptimizer import audit
    vals = {"plan": "Balanced"}
    parts = {"windows": lambda: [audit._item("Windows", "Power plan", vals["plan"])], "nvidia": lambda: []}
    api = make_api(tmp_path, audit_parts=parts)
    assert api.audit()["ok"]
    vals["plan"] = "Ultimate Performance"
    assert api.audit()["ok"]
    assert api.add_note("SimHub capture off")["ok"] and not api.add_note("")["ok"]
    rows = api.view()["tests"]
    assert [r["kind"] for r in rows] == ["note", "change"]
    assert rows[1]["text"] == "Power plan: Balanced to Ultimate Performance"
