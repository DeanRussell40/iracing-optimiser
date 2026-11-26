from pathlib import Path

from iroptimizer import selfheal


def unpack(tmp_path: Path, complete=True) -> Path:
    base = tmp_path / "_MEI1"
    for m in selfheal.MARKERS if complete else selfheal.MARKERS[:0]:
        (base / m).parent.mkdir(parents=True, exist_ok=True)
        (base / m).write_text("x")
    base.mkdir(exist_ok=True)
    return base


def test_source_runs_do_nothing(tmp_path):
    assert selfheal.check(frozen=False, folder=tmp_path) == "source"


def test_complete_unpack_is_ok_and_logged(tmp_path):
    r = selfheal.check(frozen=True, base=unpack(tmp_path), environ={}, folder=tmp_path / "log", age=lambda b: 2.0)
    assert r == "ok" and "missing=[]" in (tmp_path / "log" / "startup.log").read_text(encoding="utf-8")


def test_broken_unpack_restarts_once_with_a_clean_environment(tmp_path):
    seen, exits = [], []
    env = {"_PYI_APPLICATION_HOME_DIR": "C:/old/_MEI9", "_MEIPASS2": "x", "PATH": "p"}
    r = selfheal.check(frozen=True, base=unpack(tmp_path, complete=False), environ=env,
                       relaunch=seen.append, exit_=exits.append, folder=tmp_path / "log", age=lambda b: 1.0)
    assert r == "relaunched" and exits == [0]
    new = seen[0]
    assert "_PYI_APPLICATION_HOME_DIR" not in new and "_MEIPASS2" not in new and new["PATH"] == "p"
    assert new["PYINSTALLER_RESET_ENVIRONMENT"] == "1" and new[selfheal.FLAG] == "1"
    # the restarted copy, if still broken, gives up instead of looping
    r = selfheal.check(frozen=True, base=unpack(tmp_path, complete=False), environ=new,
                       relaunch=seen.append, exit_=exits.append, folder=tmp_path / "log", age=lambda b: 1.0)
    assert r == "gave up" and len(seen) == 1


def test_an_old_unpack_folder_is_another_copys_even_when_complete(tmp_path):
    seen = []
    r = selfheal.check(frozen=True, base=unpack(tmp_path), environ={}, relaunch=seen.append, exit_=lambda c: None,
                       folder=tmp_path / "log", age=lambda b: 600.0)
    assert r == "relaunched" and seen
    assert selfheal.check(frozen=True, base=unpack(tmp_path), environ={}, folder=tmp_path / "log",
                          age=lambda b: None) == "ok"  # can't tell: carry on
    assert selfheal.folder_age(tmp_path) is not None
