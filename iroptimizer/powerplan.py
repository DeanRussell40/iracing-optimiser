"""Windows power plans: read, switch for a test, and always switch back.

Before switching, the plan to go back to is written to a state file. A normal end of test restores it and deletes
the file; if the app or PC crashes mid-test, the next start finds the file and restores the plan then.
"""
from __future__ import annotations

import json
import re
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import Callable

STATE = "powerplan_restore.json"
BALANCED = "381b4222-f694-41f0-9685-ff5bb260df2e"
HIGH = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
_GUID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\s*\(([^)]*)\)(\s*\*)?", re.I)


def _powercfg(*args: str) -> str:
    if sys.platform != "win32":
        raise OSError("Power plans exist only on Windows.")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    r = subprocess.run(["powercfg", *args], capture_output=True, text=True, timeout=15,  # noqa: S603  # nosec B603
                       creationflags=flags)
    if r.returncode != 0:
        raise OSError((r.stdout or r.stderr or "powercfg failed").strip())
    return r.stdout or ""


def plans(run: Callable = _powercfg) -> tuple[dict[str, str], str | None]:
    """({guid: name}, active guid) from `powercfg /list`."""
    found, active = {}, None
    for m in _GUID.finditer(run("/list")):
        g = m.group(1).lower()
        found[g] = m.group(2).strip()
        if m.group(3):
            active = g
    return found, active


def pick_other(found: dict[str, str], active: str) -> str | None:
    """What to compare the current plan with: Balanced, or High performance when already on Balanced."""
    if active != BALANCED and BALANCED in found:
        return BALANCED
    if HIGH in found and HIGH != active:
        return HIGH
    return next((g for g in found if g != active), None)


class Switcher:
    def __init__(self, state_dir: Path, run: Callable = _powercfg):
        self.state = Path(state_dir) / STATE
        self.run = run

    def switch(self, to: str, back_to: str) -> None:
        self.state.parent.mkdir(parents=True, exist_ok=True)
        if not self.state.is_file():  # keep the first original if a crash left one
            self.state.write_text(json.dumps({"restore": back_to}), encoding="utf-8")
        self.run("/setactive", to)

    def restore(self) -> str | None:
        """Puts back the plan saved before switching, if any. Returns its guid."""
        try:
            g = json.loads(self.state.read_text(encoding="utf-8"))["restore"]
        except (OSError, ValueError, KeyError):
            return None
        self.run("/setactive", g)
        self.state.unlink(missing_ok=True)
        return g


def restore_leftover(state_dir: Path, run: Callable = _powercfg) -> str | None:
    """At start: restores a plan a crashed test left switched."""
    try:
        return Switcher(state_dir, run).restore()
    except Exception:
        return None
