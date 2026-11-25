"""Pre-flight safety fixes: untested writes locked, one-click restore of the original settings, capped runs."""
from __future__ import annotations

import random

import pytest

from iroptimizer import apply as applier, config_reader, demo, metrics, updates
from iroptimizer.frames import Frame


def _ini(tmp_path):
    _csv, ini_dir = demo.make(tmp_path)
    return ini_dir


CHANGE = [{"file": "renderer", "key": "LimitFrameRate", "proposed": "1"}]


def test_writes_locked_by_default_in_shipped_code():
    src = (applier.__file__)
    assert "WRITES_LOCKED = True" in open(src, encoding="utf-8").read()


def test_locked_apply_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(applier, "WRITES_LOCKED", True)
    ini_dir = _ini(tmp_path)
    before = (ini_dir / "rendererDX11Monitor.ini").read_bytes()
    with pytest.raises(applier.ChangesLocked):
        applier.apply_changes(CHANGE, ini_dir, "rendererDX11Monitor.ini")
    assert (ini_dir / "rendererDX11Monitor.ini").read_bytes() == before
    assert applier.list_backups(ini_dir) == []
    # a dry run (preview) is still allowed
    assert "simulación" in applier.apply_changes(CHANGE, ini_dir, "rendererDX11Monitor.ini", dry_run=True)[0]


def test_original_snapshot_taken_once_and_restored_in_one_call(tmp_path):
    ini_dir = _ini(tmp_path)
    first = applier.ensure_original(ini_dir)
    assert first and first.name.endswith("_original")
    assert applier.ensure_original(ini_dir) is None  # never replaced by a later snapshot
    applier.apply_changes(CHANGE, ini_dir, "rendererDX11Monitor.ini", label="later")
    assert config_reader.load(str(ini_dir)).renderer.get("LimitFrameRate") == "1"
    log = applier.restore_original(ini_dir)
    assert log and config_reader.load(str(ini_dir)).renderer.get("LimitFrameRate") == "0"
    assert applier.original_backup(ini_dir) == first  # the pre-restore copy is newer, original stays oldest
    assert any(d.name.endswith("_before_restore") for d in applier.list_backups(ini_dir))


def test_restore_original_without_backups(tmp_path):
    with pytest.raises(FileNotFoundError):
        applier.restore_original(tmp_path)


def _frames(ft_fn, n=3000, gpu_share=0.5, seed=1):
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        ft = ft_fn(rnd)
        out.append(Frame(t=i * 0.008, frametime=ft, gpu_busy=ft * gpu_share))
    return out


def test_capped_run_detected():
    m = metrics.run_metrics(_frames(lambda r: 8.333 + r.uniform(-0.05, 0.05) + (8 if r.random() < 0.005 else 0)))
    assert m["capped"] and abs(m["cap_fps"] - 120) < 2


def test_uncapped_gpu_bound_steady_run_not_capped():
    m = metrics.run_metrics(_frames(lambda r: 5.0 + r.uniform(-0.05, 0.05), gpu_share=0.97))
    assert not m["capped"]


def test_uncapped_varying_run_not_capped():
    m = metrics.run_metrics(_frames(lambda r: r.uniform(4, 7)))
    assert not m["capped"]


def test_capped_runs_cannot_pass():
    runs = [metrics.run_metrics(_frames(lambda r: 8.333 + r.uniform(-0.05, 0.05), seed=s)) for s in range(5)]
    rep = metrics.repeatability(runs)
    assert rep["verdict"] == "capped" and "NVIDIA" in rep["verdict_text"]
    assert rep["metrics"]["frametime_p99_ms"]["spread_pct"] < 5  # would have been a fake PASS


def test_update_check_points_at_this_fork():
    assert updates.REPO == "DeanRussell40/iracing-optimiser"
