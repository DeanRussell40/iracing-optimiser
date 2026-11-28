"""Reopening the replay in iRacing without anyone clicking: the app learns the user's own clicks once.

iRacing only lets its own app (iRacingUI) start the sim, the app refuses debugging switches, its iracing:// links
can't open a replay, and its page hides its buttons from accessibility tools. So the reopen is learned instead:

1. The first time a test needs the replay reopened, the user does it as usual. Meanwhile the app notes every left
   click inside the iRacing window, with a small picture of the screen around it, until the sim starts. The last few
   clicks before the sim started (at most MAX_STEPS, within LEARN_WINDOW_S) are the reopen: "Watch", and a confirm
   button if iRacing shows one.
2. Every later reopen plays those clicks back. Before each click the app brings the iRacing window forward, puts the
   pointer on the spot and checks the picture still matches. If it doesn't (the list moved, the window changed size,
   iRacing changed its look), nothing is clicked and the user is asked instead, which also teaches it again.

Coordinates are kept relative to the iRacing window's inside area, in physical pixels.
"""
from __future__ import annotations

import base64
import json
import sys
import threading
import time
from pathlib import Path
from typing import Callable

FILE = "reopen_clicks.json"
MAX_STEPS = 3
LEARN_WINDOW_S = 20.0
PATCH_W, PATCH_H = 160, 48
MAX_DIFF = 18.0          # mean grey-level difference (0-255) that still counts as the same picture
MATCH_WAIT_S = 20.0      # after the sim closes, the iRacing window can take a while to come back
SIM_START_WAIT_S = 90.0


def diff(a: bytes, b: bytes) -> float:
    if not a or len(a) != len(b):
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


class Reopener:
    """plat: the Windows side (see _Win). Pure logic here so it can be tested with a fake screen."""

    def __init__(self, state_dir: Path, plat=None, clock: Callable[[], float] = time.monotonic):
        self.path = Path(state_dir) / FILE
        self.plat = plat
        self.clock = clock
        self._rec = None

    # ---- stored recipe
    def recipe(self) -> dict | None:
        try:
            r = json.loads(self.path.read_text(encoding="utf-8"))
            return r if r.get("steps") else None
        except (OSError, ValueError):
            return None

    def has_recipe(self) -> bool:
        return self.recipe() is not None

    def forget(self) -> None:
        self.path.unlink(missing_ok=True)

    # ---- learning
    def learn_start(self) -> None:
        if self.plat is None:
            return
        self.learn_stop()
        self._rec = self.plat.record_clicks()

    def learn_stop(self) -> None:
        if self._rec is not None:
            try:
                self._rec.stop()
            except Exception:
                pass

    def learn_finish(self) -> int:
        """Call once the replay is open again. Saves the clicks that started the sim; returns how many (0 = none)."""
        rec, self._rec = self._rec, None
        if rec is None:
            return 0
        rec.stop()
        t_sim = rec.sim_started_at
        clicks = [c for c in rec.clicks if c.get("inside")]
        if t_sim is None or not clicks:
            return 0
        clicks = [c for c in clicks if t_sim - LEARN_WINDOW_S <= c["t"] <= t_sim][-MAX_STEPS:]
        if not clicks:
            return 0
        steps = []
        for i, c in enumerate(clicks):
            gap = 1.0 if i == 0 else max(1.0, min(5.0, c["t"] - clicks[i - 1]["t"]))
            steps.append({"x": c["x"], "y": c["y"], "gap_s": round(gap, 2), "pw": c["pw"], "ph": c["ph"],
                          "patch": base64.b64encode(c["patch"]).decode("ascii")})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"client": clicks[-1]["client"], "steps": steps,
                                         "learned": time.strftime("%Y-%m-%d %H:%M:%S")}), encoding="utf-8")
        return len(steps)

    # ---- playing back
    def replay(self, stop_wait: Callable[[float], bool], log: Callable[[str], None] = lambda s: None) -> tuple[bool, str]:
        """Plays the learned clicks. Returns (True, "") once the sim has started, else (False, why). Clicks nothing
        whose picture doesn't match."""
        r = self.recipe()
        if r is None or self.plat is None:
            return False, "no learned clicks"
        p = self.plat
        for n, s in enumerate(r["steps"], 1):
            if stop_wait(s["gap_s"]):
                return False, "stopped"
            want = base64.b64decode(s["patch"])
            t0, best = self.clock(), 255.0
            while True:
                win = p.find_ui()
                if win is not None:
                    client = p.client_rect(win)  # (screen x, screen y, w, h)
                    if client and [client[2], client[3]] == list(r["client"]):
                        p.foreground(win)
                        sx, sy = client[0] + s["x"], client[1] + s["y"]
                        p.move(sx, sy)
                        if stop_wait(0.4):
                            return False, "stopped"
                        got = p.grab(sx, sy, s["pw"], s["ph"])
                        best = min(best, diff(want, got))
                        if best <= MAX_DIFF:
                            p.click(sx, sy)
                            log(f"Clicked step {n} of {len(r['steps'])} in iRacing.")
                            break
                if self.clock() - t0 > MATCH_WAIT_S:
                    if win is None:
                        return False, "the iRacing window wasn't found"
                    return False, (f"the iRacing window doesn't look the same at step {n} "
                                   f"(difference {best:.0f}, limit {MAX_DIFF:.0f})")
                if stop_wait(1.0):
                    return False, "stopped"
        t0 = self.clock()
        while not p.sim_running():
            if self.clock() - t0 > SIM_START_WAIT_S:
                return False, "the clicks didn't start the sim"
            if stop_wait(0.5):
                return False, "stopped"
        return True, ""


def default(state_dir: Path) -> Reopener | None:
    if sys.platform != "win32":
        return None
    try:
        return Reopener(state_dir, _Win())
    except Exception:
        return None


# ------------------------------------------------------------------------------------------------ Windows side

class _Recording:
    def __init__(self, plat: "_Win"):
        import queue
        self.plat = plat
        self.clicks: list[dict] = []
        self.sim_started_at: float | None = None
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._hook_tid = None
        threading.Thread(target=self._hook, daemon=True).start()
        threading.Thread(target=self._work, daemon=True).start()

    def _hook(self):
        import ctypes
        from ctypes import wintypes
        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        self._hook_tid = k32.GetCurrentThreadId()
        WH_MOUSE_LL, WM_LBUTTONDOWN = 14, 0x0201

        class MSLL(ctypes.Structure):
            _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]
        LRESULT = ctypes.c_ssize_t
        PROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        u32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        u32.CallNextHookEx.restype = LRESULT

        @PROC
        def cb(code, wp, lp):
            if code >= 0 and wp == WM_LBUTTONDOWN:
                m = ctypes.cast(lp, ctypes.POINTER(MSLL)).contents
                self._q.put((time.monotonic(), m.pt.x, m.pt.y))
            return u32.CallNextHookEx(None, code, wp, lp)
        self._cb = cb
        u32.SetWindowsHookExW.restype = wintypes.HHOOK
        h = u32.SetWindowsHookExW(WH_MOUSE_LL, cb, None, 0)
        msg = wintypes.MSG()
        while not self._stop.is_set() and u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        if h:
            u32.UnhookWindowsHookEx(h)

    def _work(self):
        import queue
        self.plat.dpi_aware()
        while not self._stop.is_set():
            if self.sim_started_at is None and self.plat.sim_running():
                self.sim_started_at = time.monotonic()
            try:
                t, x, y = self._q.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                win = self.plat.find_ui()
                inside = win is not None and self.plat.root_at(x, y) == win
                c = self.plat.client_rect(win) if inside else None
                if not c or not (c[0] <= x < c[0] + c[2] and c[1] <= y < c[1] + c[3]):
                    self.clicks.append({"t": t, "inside": False})
                    continue
                patch = self.plat.grab(x, y, PATCH_W, PATCH_H)
                self.clicks.append({"t": t, "inside": True, "x": x - c[0], "y": y - c[1], "client": [c[2], c[3]],
                                    "pw": PATCH_W, "ph": PATCH_H, "patch": patch})
            except Exception:
                pass

    def stop(self):
        if self._stop.is_set():
            return
        self._stop.set()
        if self._hook_tid:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(self._hook_tid, 0x0012, 0, 0)  # WM_QUIT


class _Win:
    UI = "iracingui.exe"

    def __init__(self):
        import ctypes  # noqa: F401  (fails off Windows)
        self.dpi_aware()

    @staticmethod
    def dpi_aware():
        import ctypes
        try:
            ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
        except Exception:
            pass

    def sim_running(self) -> bool:
        from .simctl import sim_pids
        return bool(sim_pids())

    def record_clicks(self) -> _Recording:
        return _Recording(self)

    def find_ui(self):
        """The visible main window of iRacingUI.exe, or None."""
        import ctypes
        from ctypes import wintypes
        import psutil
        u32 = ctypes.windll.user32
        self.dpi_aware()
        pids = {p.info["pid"] for p in psutil.process_iter(["pid", "name"]) if (p.info["name"] or "").lower() == self.UI}
        if not pids:
            return None
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cb(hwnd, _):
            owner = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value in pids and u32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(64)
                u32.GetClassNameW(hwnd, buf, 64)
                if buf.value == "Chrome_WidgetWin_1":
                    found.append(hwnd)
            return True
        u32.EnumWindows(cb, 0)
        return found[0] if found else None

    def root_at(self, x: int, y: int):
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        u32.WindowFromPoint.restype = wintypes.HWND
        u32.GetAncestor.restype = wintypes.HWND
        h = u32.WindowFromPoint(wintypes.POINT(x, y))
        return u32.GetAncestor(h, 2) if h else None  # GA_ROOT

    def client_rect(self, hwnd):
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        if not hwnd or u32.IsIconic(hwnd):
            return None
        r, p = wintypes.RECT(), wintypes.POINT(0, 0)
        if not u32.GetClientRect(hwnd, ctypes.byref(r)) or not u32.ClientToScreen(hwnd, ctypes.byref(p)):
            return None
        return (p.x, p.y, r.right - r.left, r.bottom - r.top)

    def foreground(self, hwnd) -> None:
        import ctypes
        u32 = ctypes.windll.user32
        if u32.IsIconic(hwnd):
            u32.ShowWindow(hwnd, 9)  # SW_RESTORE
        if u32.GetForegroundWindow() != hwnd:
            u32.keybd_event(0x12, 0, 0, 0)  # a tap of Alt lets a background app bring a window forward
            u32.keybd_event(0x12, 0, 2, 0)
            u32.SetForegroundWindow(hwnd)

    def move(self, x: int, y: int) -> None:
        import ctypes
        ctypes.windll.user32.SetCursorPos(int(x), int(y))

    def click(self, x: int, y: int) -> None:
        import ctypes
        u32 = ctypes.windll.user32
        u32.SetCursorPos(int(x), int(y))
        u32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.05)
        u32.mouse_event(0x0004, 0, 0, 0, 0)

    def grab(self, cx: int, cy: int, w: int, h: int) -> bytes:
        """Grey levels of the w x h screen area centred on (cx, cy), one byte a pixel."""
        import ctypes
        from ctypes import wintypes
        u32, g32 = ctypes.windll.user32, ctypes.windll.gdi32
        x, y = int(cx - w // 2), int(cy - h // 2)
        u32.GetDC.restype = wintypes.HDC
        g32.CreateCompatibleDC.restype = wintypes.HDC
        g32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        g32.SelectObject.restype = wintypes.HGDIOBJ
        g32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        g32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        g32.DeleteDC.argtypes = [wintypes.HDC]
        u32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        g32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HDC,
                               ctypes.c_int, ctypes.c_int, wintypes.DWORD]
        g32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                  ctypes.c_void_p, wintypes.UINT]
        g32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        g32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]

        class BIH(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]
        sdc = u32.GetDC(None)
        mdc = g32.CreateCompatibleDC(sdc)
        bmp = g32.CreateCompatibleBitmap(sdc, w, h)
        old = g32.SelectObject(mdc, bmp)
        try:
            g32.BitBlt(mdc, 0, 0, w, h, sdc, x, y, 0x00CC0020)  # SRCCOPY
            bih = BIH(ctypes.sizeof(BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
            buf = (ctypes.c_ubyte * (w * h * 4))()
            g32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bih), 0)
            raw = bytes(buf)
        finally:
            g32.SelectObject(mdc, old)
            g32.DeleteObject(bmp)
            g32.DeleteDC(mdc)
            u32.ReleaseDC(None, sdc)
        return bytes((raw[i] * 29 + raw[i + 1] * 150 + raw[i + 2] * 77) >> 8 for i in range(0, len(raw), 4))
