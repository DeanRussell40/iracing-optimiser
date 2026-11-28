"""Changing iRacing graphics settings for a test, and always putting them back.

Only existing keys are changed, one line at a time (iniio keeps comments, order and line endings), and only while
the sim is closed: iRacing writes its in-memory settings back to the file when it exits, which would undo a change
made while it runs. Every change is read back after writing.

Before the first change, the original values go into a state file. A normal end of test puts them back and deletes
the file. If the app or PC crashes mid-test, the next start finds the file and puts them back then, once the sim is
closed. A full backup of the iRacing settings files is also taken before the first change (apply.backup).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .iniio import IniFile

STATE = "ini_restore.json"


def _running_default() -> bool:
    from .apply import iracing_running
    return iracing_running()


def expand(f: IniFile, values: dict[str, str]) -> dict[str, str]:
    """{"Key": v} means every section that has Key (iRacing keeps a [Graphics Options] and a [Replay Graphics] copy
    of most settings, and uses the replay copy while showing a replay); {"Section/Key": v} means just that one.
    Returns {"Section/Key": v} for each line to change. Raises if a key isn't in the file."""
    out = {}
    secs = {}
    for sec, key, _i in f._index:
        secs.setdefault(key.lower(), []).append((sec, key))
    for name, v in values.items():
        sec, _, k = name.rpartition("/")
        hits = [(s_, k_) for s_, k_ in secs.get(k.lower(), []) if not sec or s_.lower() == sec.lower()]
        if not hits:
            raise RuntimeError(f"{k} isn't in the settings file, so nothing was changed.")
        for s_, k_ in hits:
            out[f"{s_}/{k_}"] = v
    return out


class IniSwitch:
    def __init__(self, state_dir: Path, ini_path: Path, running: Callable[[], bool] = _running_default,
                 backup: Callable[[], object] | None = None):
        self.state = Path(state_dir) / STATE
        self.ini = Path(ini_path)
        self.running = running
        self.backup = backup

    def _guard(self):
        if self.running():
            raise RuntimeError("iRacing is still running, so its settings file can't be changed safely.")

    def _write(self, values: dict[str, str]) -> None:
        f = IniFile(self.ini)
        for key, val in values.items():
            sec, _, k = key.rpartition("/")
            if not f.set(k, val, sec or None):
                raise RuntimeError(f"{k} isn't in {self.ini.name}, so it wasn't changed.")
        f.save()
        back = IniFile(self.ini)
        for key, val in values.items():
            sec, _, k = key.rpartition("/")
            got = back.get(k, sec or None)
            if str(got).strip() != str(val).strip():
                raise RuntimeError(f"{k} reads back as {got!r} after writing {val!r}.")

    def set(self, values: dict[str, str]) -> None:
        """values: {"Key" or "Section/Key": value} (see expand). Saves the originals first (only once per test, so a crash keeps the
        real originals)."""
        self._guard()
        f = IniFile(self.ini)
        values = expand(f, values)
        if not self.state.is_file():
            orig = {}
            for key in values:
                sec, _, k = key.rpartition("/")
                v = f.get(k, sec or None)
                if v is None:
                    raise RuntimeError(f"{k} isn't in {self.ini.name}, so it wasn't changed.")
                orig[key] = v
            if self.backup:
                self.backup()
            self.state.parent.mkdir(parents=True, exist_ok=True)
            self.state.write_text(json.dumps({"ini": str(self.ini), "restore": orig}), encoding="utf-8")
        else:
            data = json.loads(self.state.read_text(encoding="utf-8"))
            missing = [k for k in values if k not in data["restore"]]
            if missing:
                for key in missing:
                    sec, _, k = key.rpartition("/")
                    v = f.get(k, sec or None)
                    if v is None:
                        raise RuntimeError(f"{k} isn't in {self.ini.name}, so it wasn't changed.")
                    data["restore"][key] = v
                self.state.write_text(json.dumps(data), encoding="utf-8")
        self._write({k: str(v) for k, v in values.items()})

    def restore(self) -> dict[str, str] | None:
        """Puts the original values back. Returns them, or None when nothing was changed."""
        try:
            data = json.loads(self.state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        self._guard()
        self.ini = Path(data.get("ini") or self.ini)
        self._write(data["restore"])
        self.state.unlink(missing_ok=True)
        return data["restore"]


def restore_leftover(state_dir: Path, running: Callable[[], bool] = _running_default) -> dict | None:
    """At start: puts back settings a crashed test left changed, if the sim is closed. Never raises."""
    st = Path(state_dir) / STATE
    if not st.is_file():
        return None
    try:
        if running():
            return None
        return IniSwitch(state_dir, Path(json.loads(st.read_text(encoding="utf-8"))["ini"]), running).restore()
    except Exception:
        return None
