"""Closing and reopening the iRacing sim between A/B variants, for settings iRacing only reads at start.

iRacing reads its graphics settings (rendererDX11Monitor.ini) when the sim starts and writes its in-memory copy back
when it exits. So a settings variant must: close the sim completely, then write the file, then reopen the sim on the
same replay. Writing while the sim runs would be overwritten at exit.

Reopening: the sim's own command line has no replay in it (the iRacing window hands the replay over after start),
so for now the user reopens the replay with one click in iRacing, and the test notices by itself when it has loaded
(through the telemetry SDK) and carries on.
"""
from __future__ import annotations

import time
from typing import Callable

SIM = "iracingsim64dx11.exe"


def sim_pids() -> list[int]:
    import psutil
    out = []
    for p in psutil.process_iter(["pid", "name"]):
        if (p.info["name"] or "").lower() == SIM:
            out.append(p.info["pid"])
    return out


def _post_close(pid: int) -> int:
    """Asks every top-level window of pid to close (as clicking X would). Returns how many were asked."""
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    WM_CLOSE = 0x0010
    hits = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lp):
        owner = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and u32.IsWindowVisible(hwnd):
            hits.append(hwnd)
        return True
    u32.EnumWindows(cb, 0)
    for h in hits:
        u32.PostMessageW(h, WM_CLOSE, 0, 0)
    return len(hits)


def _terminate(pid: int) -> None:
    import psutil
    try:
        psutil.Process(pid).terminate()
    except psutil.Error:
        pass


def close_sim(log: Callable[[str], None] = lambda s: None, polite_s: float = 25.0, hard_s: float = 15.0,
              pids: Callable[[], list[int]] = sim_pids, post_close: Callable[[int], int] = _post_close,
              terminate: Callable[[int], None] = _terminate, sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic) -> None:
    """Closes the sim and waits until it has gone. Asks nicely first; ends it if it hasn't closed in polite_s.
    Raises when it is still running after that."""
    running = pids()
    if not running:
        return
    for pid in running:
        try:
            post_close(pid)
        except Exception:
            pass
    log("Closing iRacing…")
    t = clock()
    while pids() and clock() - t < polite_s:
        sleep(0.5)
    left = pids()
    if left:
        log("iRacing didn't close by itself, ending it.")
        for pid in left:
            terminate(pid)
        t = clock()
        while pids() and clock() - t < hard_s:
            sleep(0.5)
    if pids():
        raise RuntimeError("iRacing is still running and couldn't be closed, so the setting wasn't changed.")
    sleep(2.0)  # let the files close


def wait_for_replay(driver, stop_wait: Callable[[float], bool], timeout_s: float = 900.0,
                    clock: Callable[[], float] = time.monotonic) -> None:
    """Waits until the sim is running and showing a replay with a readable position, connecting driver to it.
    stop_wait(seconds) sleeps and returns True when the test was stopped."""
    t = clock()
    while True:
        try:
            driver.connect()
            if driver.in_replay() and driver.current_frame() is not None:
                return
        except Exception:
            pass
        try:
            driver.disconnect()
        except Exception:
            pass
        if clock() - t > timeout_s:
            raise RuntimeError("The replay wasn't reopened in time, so the test stopped. Your settings were put "
                               "back.")
        if stop_wait(2.0):
            from .bench import Aborted
            raise Aborted()
