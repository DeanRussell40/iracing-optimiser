"""Windows probes and actions the optimiser needs around a bench run.

* Elevation: if iRacing runs as administrator and this app does not, Windows silently drops the SDK's
  replay commands (seek, play, camera). The bench must detect that up front.
* Render surface: the size of iRacing's window and which monitors it covers. On a triple-screen rig
  without NVIDIA Surround, the primary monitor's mode (2560x1440) says nothing about the real load.
* Background CPU: which other processes use CPU while the sim runs (SimHub, simmixer…). On the first real
  bench these caused every stutter.
* Pausing processes: suspend chosen background apps for a measured run and always resume them afterwards.

Every function degrades to a harmless default off Windows so the rest of the app stays testable.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import psutil

IRACING_PREFIX = "iracingsim"
# Never suspend or blame these: the sim itself, Windows, GPU driver and this app
PROTECTED = {
    "system", "system idle process", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "svchost.exe", "dwm.exe", "explorer.exe", "audiodg.exe", "fontdrvhost.exe",
    "memory compression", "nvcontainer.exe", "nvdisplay.container.exe", "presentmon.exe", "conhost.exe",
    "msmpeng.exe", "searchindexer.exe", "taskmgr.exe", "iracingservice.exe", "iracingservice64.exe",
    "iracingui.exe", "iracingoptimizer.exe", "python.exe", "claude.exe",
}
PROTECTED_PREFIXES = (IRACING_PREFIX, "presentmon", "nvidia", "eos", "easyanticheat")


def is_windows() -> bool:
    return sys.platform == "win32"


def is_protected(name: str) -> bool:
    n = (name or "").lower()
    return n in PROTECTED or n.startswith(PROTECTED_PREFIXES)


# ---------------------------------------------------------------------------- elevation

def self_elevated() -> bool:
    if not is_windows():
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def find_iracing() -> psutil.Process | None:
    for p in psutil.process_iter(["name"]):
        if (p.info["name"] or "").lower().startswith(IRACING_PREFIX):
            return p
    return None


def process_elevated(pid: int) -> bool | None:
    """True if the process runs elevated, None if unknown. A non-elevated caller can't open an elevated
    process's token, so 'access denied' there means elevated."""
    if not is_windows():
        return None
    import ctypes
    from ctypes import wintypes
    k32, adv = ctypes.windll.kernel32, ctypes.windll.advapi32
    PROCESS_QUERY_LIMITED_INFORMATION, TOKEN_QUERY, TokenElevation = 0x1000, 0x0008, 20
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        tok = wintypes.HANDLE()
        if not adv.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok)):
            return True if ctypes.GetLastError() == 5 else None  # 5 = ERROR_ACCESS_DENIED
        try:
            elev = wintypes.DWORD()
            size = wintypes.DWORD()
            if adv.GetTokenInformation(tok, TokenElevation, ctypes.byref(elev), ctypes.sizeof(elev), ctypes.byref(size)):
                return bool(elev.value)
            return None
        finally:
            k32.CloseHandle(tok)
    finally:
        k32.CloseHandle(h)


def elevation_mismatch() -> str | None:
    """A plain-English problem description when iRacing is elevated and this app is not, else None."""
    p = find_iracing()
    if not p or self_elevated():
        return None
    if process_elevated(p.pid):
        return ("iRacing is running as administrator, but this app is not. Windows blocks a normal app from "
                "sending commands to an administrator app, so the bench can't move the replay.\n\n"
                "Click 'Restart as administrator', or untick 'Run this program as an administrator' on "
                "iRacingUI.exe (Properties > Compatibility) and restart iRacing.")
    return None


def relaunch_as_admin(args: list[str] | None = None) -> bool:
    """Starts a new elevated copy of this app (UAC prompt). Returns whether it was launched."""
    if not is_windows():
        return False
    import ctypes
    import subprocess
    exe = sys.executable
    params = subprocess.list2cmdline(args or []) if getattr(sys, "frozen", False) else \
        subprocess.list2cmdline([os.path.abspath(sys.argv[0]), *(args or [])])
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    return rc > 32


# ---------------------------------------------------------------------------- render surface

def monitors() -> list[dict]:
    """All monitors: position, size, refresh rate, primary flag."""
    if not is_windows():
        return []
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    out: list[dict] = []

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]

    class DEVMODEW(ctypes.Structure):
        _fields_ = [("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD),
                    ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
                    ("dmFields", wintypes.DWORD), ("dmPositionX", wintypes.LONG), ("dmPositionY", wintypes.LONG),
                    ("dmDisplayOrientation", wintypes.DWORD), ("dmDisplayFixedOutput", wintypes.DWORD),
                    ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
                    ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short), ("dmFormName", wintypes.WCHAR * 32),
                    ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD),
                    ("dmPelsHeight", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
                    ("dmDisplayFrequency", wintypes.DWORD)]

    MonitorEnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                         ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def cb(hmon, _hdc, _rc, _lp):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(mi)
        if u32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcMonitor
            dm = DEVMODEW()
            dm.dmSize = ctypes.sizeof(dm)
            hz = dm.dmDisplayFrequency if u32.EnumDisplaySettingsW(mi.szDevice, -1, ctypes.byref(dm)) else None
            out.append({"device": mi.szDevice, "left": r.left, "top": r.top, "width": r.right - r.left,
                        "height": r.bottom - r.top, "refresh": hz, "primary": bool(mi.dwFlags & 1)})
        return True

    u32.EnumDisplayMonitors(None, None, MonitorEnumProc(cb), 0)
    return out


def window_rect(pid: int) -> dict | None:
    """Largest visible top-level window of a process: {left, top, width, height}."""
    if not is_windows():
        return None
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    best: list[dict] = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lp):
        wpid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid and u32.IsWindowVisible(hwnd):
            r = wintypes.RECT()
            if u32.GetWindowRect(hwnd, ctypes.byref(r)):
                best.append({"left": r.left, "top": r.top, "width": r.right - r.left, "height": r.bottom - r.top})
        return True

    u32.EnumWindows(EnumWindowsProc(cb), 0)
    return max(best, key=lambda w: w["width"] * w["height"]) if best else None


def covered_monitors(win: dict, mons: list[dict]) -> list[dict]:
    """Monitors the window overlaps by at least half of the monitor's area."""
    out = []
    for m in mons:
        w = min(win["left"] + win["width"], m["left"] + m["width"]) - max(win["left"], m["left"])
        h = min(win["top"] + win["height"], m["top"] + m["height"]) - max(win["top"], m["top"])
        if w > 0 and h > 0 and w * h >= 0.5 * m["width"] * m["height"]:
            out.append(m)
    return out


def describe_surface(win: dict | None, mons: list[dict]) -> dict:
    """What iRacing actually renders to, in a form the report can explain."""
    if not win:
        return {"available": False}
    cov = covered_monitors(win, mons)
    refresh = sorted({m["refresh"] for m in cov if m.get("refresh")})
    surround = len(cov) == 1 and win["width"] >= 2.5 * win["height"]  # one very wide "monitor" = Surround
    info = {"available": True, "width": win["width"], "height": win["height"], "monitors": len(cov),
            "refresh": refresh, "megapixels": round(win["width"] * win["height"] / 1e6, 1),
            "surround": surround}
    if len(cov) > 1:
        info["note"] = (f"iRacing renders one {win['width']}x{win['height']} window across {len(cov)} separate "
                        "monitors (not NVIDIA Surround). G-Sync usually can't run on a window spanning several "
                        "displays, so expect it to be off or only on one screen.")
    return info


def render_surface() -> dict:
    p = find_iracing()
    if not p:
        return {"available": False}
    return describe_surface(window_rect(p.pid), monitors())


# ---------------------------------------------------------------------------- background CPU

class BackgroundSampler:
    """Samples per-process CPU in a thread. Results are in cores (1.0 = one full logical CPU)."""

    def __init__(self, interval_s: float = 1.0):
        self.interval_s = interval_s
        self.samples: list[dict] = []  # {"t": .., "procs": {name: cores}}
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self._procs: dict[int, psutil.Process] = {}

    def _tick(self) -> dict[str, float]:
        seen: dict[str, float] = {}
        for p in psutil.process_iter(["name"]):
            if p.pid not in self._procs:
                self._procs[p.pid] = p
                try:
                    p.cpu_percent(None)  # first call primes the counter
                except psutil.Error:
                    pass
                continue
            try:
                c = self._procs[p.pid].cpu_percent(None) / 100
            except psutil.Error:
                continue
            name = p.info["name"] or str(p.pid)
            if c > 0.005 and not name.lower().startswith(IRACING_PREFIX) and name.lower() != "system idle process":
                seen[name] = seen.get(name, 0) + c
        return seen

    def _loop(self):
        self._tick()
        while not self._stop.wait(self.interval_s):
            try:
                self.samples.append({"t": time.time(), "procs": self._tick()})
            except Exception:
                pass

    def start(self):
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join(timeout=3)


def summarize_background(samples: list[dict], t0: float | None = None, t1: float | None = None,
                         top: int = 8) -> list[dict]:
    """Average CPU (cores) per process name over a time window, highest first."""
    s = [x for x in samples if (t0 is None or x["t"] >= t0) and (t1 is None or x["t"] < t1)]
    if not s:
        return []
    tot: dict[str, float] = {}
    peak: dict[str, float] = {}
    for x in s:
        for n, c in x["procs"].items():
            tot[n] = tot.get(n, 0) + c
            peak[n] = max(peak.get(n, 0), c)
    rows = [{"name": n, "avg_cores": v / len(s), "peak_cores": peak[n], "protected": is_protected(n)}
            for n, v in tot.items()]
    return sorted(rows, key=lambda r: -r["avg_cores"])[:top]


def suspects(rows: list[dict], min_avg_cores: float = 0.05) -> list[str]:
    """Background apps worth testing with an A/B pause: busy and not part of Windows, the GPU driver or iRacing."""
    return [r["name"] for r in rows if not r["protected"] and r["avg_cores"] >= min_avg_cores]


# ---------------------------------------------------------------------------- pausing processes

PAUSED_STATE = "paused_processes.json"


class ProcessPauser:
    """Suspends processes by name and always resumes them. A state file records what is paused, so a crash
    can be recovered with resume_leftovers() on the next start."""

    def __init__(self, state_dir: Path):
        self.state = Path(state_dir) / PAUSED_STATE
        self.paused: list[int] = []

    def _save(self):
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text(json.dumps({"pids": self.paused}), encoding="utf-8")

    def pause(self, names: list[str]) -> list[str]:
        """Returns the names actually paused (skips protected and inaccessible ones)."""
        want = {n.lower() for n in names if not is_protected(n)}
        me = psutil.Process()
        never = {me.pid, *(q.pid for q in me.parents())}  # never freeze this app or what launched it
        done = []
        for p in psutil.process_iter(["name"]):
            n = (p.info["name"] or "")
            if n.lower() in want and p.pid not in never:
                try:
                    p.suspend()
                    self.paused.append(p.pid)
                    done.append(n)
                    self._save()
                except psutil.Error:
                    pass
        return sorted(set(done))

    def resume(self) -> None:
        for pid in self.paused:
            try:
                psutil.Process(pid).resume()
            except psutil.Error:
                pass
        self.paused = []
        try:
            self.state.unlink()
        except OSError:
            pass


def resume_leftovers(state_dir: Path) -> int:
    """Resumes anything a crashed run left suspended. Returns how many processes were resumed."""
    st = Path(state_dir) / PAUSED_STATE
    if not st.is_file():
        return 0
    n = 0
    try:
        for pid in json.loads(st.read_text(encoding="utf-8")).get("pids", []):
            try:
                psutil.Process(int(pid)).resume()
                n += 1
            except (psutil.Error, ValueError):
                pass
    finally:
        try:
            st.unlink()
        except OSError:
            pass
    return n
