"""Learns how iRacing starts the sim, so tests can later restart it on a replay by themselves.

While the app is open, it looks for iRacingSim64DX11.exe every few seconds. The first time it sees each new sim
process it writes that process's command line to logs/sim_cmdline.log in the results folder. Anything that looks
like a token or credential is replaced with <redacted>. The app runs as administrator, like the sim, so it can
read the command line.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Callable

SIM = "iracingsim64dx11.exe"
LOG = Path("logs") / "sim_cmdline.log"
_SECRET_NAME = re.compile(r"(token|auth|pass|pwd|secret|key|session|ticket|cred|sig)", re.I)
_LONG_OPAQUE = re.compile(r"^[A-Za-z0-9+/=_\-.]{24,}$")


def redact(args: list[str]) -> tuple[list[str], bool]:
    """Hides values that look like tokens. Paths and short switches are kept."""
    out, hid = [], False
    prev_secret = False
    for a in args:
        name, eq, val = a.partition("=")
        if eq and _SECRET_NAME.search(name):
            out.append(f"{name}=<redacted>")
            hid = True
        elif prev_secret or (_LONG_OPAQUE.match(a) and "\\" not in a and "/" not in a.replace("+/", "")):
            out.append(f"<redacted, {len(a)} chars>")
            hid = True
        else:
            out.append(a)
        prev_secret = a.startswith("-") and bool(_SECRET_NAME.search(a)) and not eq
    return out, hid


def scan(seen: set, folder: Path, procs: Callable | None = None) -> list[dict]:
    """Logs the command line of each sim process not seen before. Returns what it logged."""
    if procs is None:
        import psutil

        def procs():
            for p in psutil.process_iter(["pid", "name"]):
                if (p.info["name"] or "").lower() == SIM:
                    try:
                        yield p.info["pid"], p.cmdline()
                    except psutil.Error as e:
                        yield p.info["pid"], [f"<couldn't read: {type(e).__name__}>"]
    logged = []
    for pid, cmd in procs():
        if pid in seen:
            continue
        seen.add(pid)
        args, hid = redact(list(cmd))
        entry = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "pid": pid, "args": args, "redacted": hid}
        f = Path(folder) / LOG
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(f"{entry['t']} pid={pid} redacted={hid} args={args!r}\n")
        logged.append(entry)
    return logged


def start(folder: Path, every_s: float = 5.0) -> threading.Thread:
    seen: set = set()

    def loop():
        while True:
            try:
                scan(seen, folder)
            except Exception:
                pass
            time.sleep(every_s)
    t = threading.Thread(target=loop, daemon=True, name="simwatch")
    t.start()
    return t
