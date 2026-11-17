"""v0.11: grabación automática."""
from __future__ import annotations

import csv

from iroptimizer import autorec


def test_autoarm_one_start_per_sim_launch():
    a = autorec.AutoArm()
    assert a.step(False, False) is False
    assert a.step(True, False) is True       # abre iRacing → arranca
    assert a.step(True, True) is False       # grabando
    assert a.step(True, False) is False      # paró (a mano o por fin): no rearranca con iRacing abierto
    assert a.step(False, False) is False     # cierra iRacing → se rearma
    assert a.step(True, False) is True


def test_autoarm_manual_recording_blocks_auto():
    a = autorec.AutoArm()
    assert a.step(True, True) is False       # ya grababas a mano al abrir iRacing
    assert a.step(True, False) is False      # al parar no arranca sola


def _write(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["t", "on_track", "replay"])
        w.writeheader()
        for i, (on, rep) in enumerate(rows):
            w.writerow({"t": i, "on_track": on, "replay": rep})


def test_too_short_and_discard(tmp_path):
    p = tmp_path / "session_x.csv"
    _write(p, [(1, 0)] * 400 + [(0, 0)] * 2000 + [(1, 1)] * 2000)  # 100 s en pista a 4 Hz; garaje y repetición no cuentan
    assert autorec.on_track_seconds(p, 4) == 100
    assert autorec.too_short(p, 4)
    for ext in (".json", ".presentmon.csv.gz"):
        (tmp_path / f"session_x{ext}").write_text("x")
    autorec.discard(p)
    assert not list(tmp_path.iterdir())
    _write(p, [(1, 0)] * 800)  # 200 s
    assert not autorec.too_short(p, 4)


def test_autostart_only_for_exe():
    assert autorec.autostart_command() is None  # con Python suelto no se ofrece
