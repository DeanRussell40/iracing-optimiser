"""Updating the exe from inside the app, so new builds replace the old one instead of piling up in Downloads as
"iRacingOptimizer (1).exe", "(2)" and so on.

Source: the fixed "latest" build of this repository (the same link as the README). A build is "newer" when its
SHA-256 (from SHA256SUMS.txt in the same release) differs from the running exe. Nothing downloads until the user
clicks Update. The download must match that SHA-256 before it replaces anything.

Replacing a running exe on Windows: it can't be overwritten, but it can be renamed. So the running exe is renamed
to iRacingOptimizer.old, the new one is put in place as iRacingOptimizer.exe in the same folder, started, and this
copy exits. The .old file is deleted at the next start. A re-downloaded "iRacingOptimizer (1).exe" is folded back
into iRacingOptimizer.exe the same way.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess  # nosec B404
import sys
import urllib.request
from pathlib import Path
from typing import Callable

from .updates import REPO, disabled_by_env

BASE = f"https://github.com/{REPO}/releases/download/latest/"
EXE_URL = BASE + "iRacingOptimizer.exe"
SUMS_URL = BASE + "SHA256SUMS.txt"
EXE_NAME = "iRacingOptimizer.exe"
OLD_NAME = "iRacingOptimizer.old"
PART_NAME = "iRacingOptimizer.download"
MAX_EXE = 300 * 1024 * 1024
_SUM_RE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?iRacingOptimizer\.exe\s*$", re.M)
UA = {"User-Agent": "iRacingOptimizer-updater"}


def running_exe() -> Path | None:
    """The exe this app runs from, or None when running from source."""
    return Path(sys.executable).resolve() if getattr(sys, "frozen", False) else None


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_sums(text: str) -> str | None:
    m = _SUM_RE.search(text or "")
    return m.group(1).lower() if m else None


def _get(url: str, timeout: float):
    # fixed https URLs on github.com, never built from user input
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)  # nosec B310


def latest_sha(timeout: float = 8.0) -> str | None:
    with _get(SUMS_URL, timeout) as r:
        return parse_sums(r.read(10_000).decode("utf-8", "replace"))


def cleanup(exe: Path | None = None) -> None:
    """Deletes what a previous update left behind."""
    exe = exe or running_exe()
    if not exe:
        return
    for n in (OLD_NAME, PART_NAME):
        try:
            (exe.parent / n).unlink()
        except OSError:
            pass


def check(timeout: float = 8.0, exe: Path | None = None, fetch: Callable[[], str | None] | None = None) -> dict:
    """{"available": bool, ...}. Never raises: no network just means no update offered."""
    exe = exe or running_exe()
    if not exe or disabled_by_env():
        return {"available": False, "reason": "not an installed exe" if not exe else "disabled"}
    try:
        want = (fetch or (lambda: latest_sha(timeout)))()
        if not want:
            return {"available": False, "reason": "no checksum published"}
        have = sha256(exe)
    except Exception as e:
        return {"available": False, "reason": f"check failed: {e}"}
    return {"available": want != have, "sha": want, "current": have,
            "renamed": exe.name.lower() != EXE_NAME.lower()}


def install(want_sha: str, exe: Path | None = None, download: Callable[[Path], None] | None = None,
            launch: Callable[[Path], None] | None = None, progress: Callable[[float], None] | None = None) -> Path:
    """Downloads the new exe, checks it, swaps it in and starts it. Returns the new exe's path.
    The caller closes this app afterwards. On any failure the running exe is left as it was."""
    exe = exe or running_exe()
    if not exe:
        raise RuntimeError("Updating only works from the exe.")
    folder = exe.parent
    part, old, target = folder / PART_NAME, folder / OLD_NAME, folder / EXE_NAME
    cleanup(exe)
    if download:
        download(part)
    else:
        with _get(EXE_URL, 60) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while chunk := r.read(1 << 20):
                done += len(chunk)
                if done > MAX_EXE:
                    raise RuntimeError("The download is larger than expected.")
                f.write(chunk)
                if progress and total:
                    progress(done / total)
    got = sha256(part)
    if got != want_sha.lower():
        part.unlink(missing_ok=True)
        raise RuntimeError("The download didn't match the published checksum, so nothing was changed.")
    os.replace(exe, old)  # a running exe can be renamed, not overwritten
    try:
        os.replace(part, target)  # also replaces an older iRacingOptimizer.exe left next to a "(1)" copy
    except OSError:
        os.replace(old, exe)  # put things back
        raise
    (launch or _launch)(target)
    return target


def _launch(exe: Path) -> None:
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    from .winsys import child_env
    subprocess.Popen([str(exe)], cwd=str(exe.parent), creationflags=flags, close_fds=True,  # noqa: S603  # nosec B603
                     env=child_env())
