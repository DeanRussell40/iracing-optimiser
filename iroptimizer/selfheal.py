"""Recovers from a broken unpack at startup.

A PyInstaller one-file exe unpacks to a temp _MEI folder. If a new copy is started with the old copy's _PYI*
variables, it reuses the old folder instead of unpacking its own, and that folder is deleted (all but a few
locked DLLs) when the old copy exits: the window, PresentMon and the icons are gone. That happened on an update
started by an older build. Every start now checks that its own files are there; if not, it starts itself once
more with a clean environment (a fresh unpack) and exits. It also does that when the unpack folder is older than
this process: then it belongs to another copy, even if nothing has been deleted from it yet.

Each start also appends one line to startup.log in the app's local data folder, so a failed start can be traced.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

MARKERS = ("iroptimizer/webui/index.html", "tools/PresentMon.exe")
FLAG = "IRO_SELFHEAL"
MAX_AGE_S = 30.0  # a fresh unpack is made just before this process starts; an older folder belongs to another copy


def missing(base: Path) -> list[str]:
    return [m for m in MARKERS if not (base / m).is_file()]


def folder_age(base: Path) -> float | None:
    """Seconds between the unpack folder's creation and this process's start. A reused folder is much older:
    it was made when the old copy started. Checked by age because the old copy may not have deleted anything
    yet when this one starts (it deletes its folder a moment later, as it exits)."""
    try:
        import psutil
        return psutil.Process().create_time() - base.stat().st_ctime
    except Exception:
        return None


def _log_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "iRacingOptimizer"


def log(line: str, folder: Path | None = None) -> None:
    try:
        d = folder or _log_dir()
        d.mkdir(parents=True, exist_ok=True)
        f = d / "startup.log"
        if f.is_file() and f.stat().st_size > 200_000:
            f.replace(d / "startup.old.log")
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
    except OSError:
        pass


def check(frozen: bool | None = None, base: Path | None = None, environ: dict | None = None,
          relaunch=None, exit_=os._exit, folder: Path | None = None, age=folder_age) -> str:
    """Returns "source", "ok", "relaunched" or "gave up". Only acts in the exe."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if not frozen:
        return "source"
    environ = os.environ if environ is None else environ
    base = Path(base or getattr(sys, "_MEIPASS", ""))
    gone = missing(base)
    a = age(base)
    reused = a is not None and a > MAX_AGE_S
    pyi = sorted(k for k in environ if k.upper().startswith("_PYI") or k.upper() == "_MEIPASS2")
    log(f"start pid={os.getpid()} exe={sys.executable} unpack={base} missing={gone} age_s="
        f"{'?' if a is None else round(a, 1)} retry={environ.get(FLAG, '')} pyi={pyi}", folder)
    if not gone and not reused:
        return "ok"
    if environ.get(FLAG):
        log("unpack still broken after a clean restart; carrying on", folder)
        return "gave up"
    from .winsys import child_env
    env = child_env(dict(environ))
    env[FLAG] = "1"
    (relaunch or _relaunch)(env)
    log("restarted with a clean environment", folder)
    exit_(0)
    return "relaunched"


def _relaunch(env: dict) -> None:
    import subprocess  # nosec B404
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    exe = Path(sys.executable)
    subprocess.Popen([str(exe), *sys.argv[1:]], cwd=str(exe.parent), env=env,  # noqa: S603  # nosec B603
                     creationflags=flags, close_fds=True)
