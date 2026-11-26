"""Audit my PC: reads every Windows, NVIDIA and iRacing setting that can matter for smoothness and lists its
current value. Read only: nothing here changes a setting.

Flags are deliberately conservative. "bad" only where the effect is well established (a power-saving plan, the
shader cache off). Everything that is folklore or depends on the PC (HAGS, MPO, core parking, timer resolution,
low latency mode) is shown as "test", with a ready A/B test, because only a measurement on this PC can say.

Each item: {"area", "name", "value", "level": ok|info|test|warn|bad, "note", "test": {"a", "b"} | None}.
"""
from __future__ import annotations

import ctypes
import re
import subprocess  # nosec B404
import sys
from typing import Callable

WIN = sys.platform == "win32"


def _item(area, name, value, level="info", note="", test=None) -> dict:
    return {"area": area, "name": name, "value": "Couldn't read" if value is None else str(value),
            "level": level if value is not None else "info", "note": note, "test": test}


def _reg(root: str, path: str, name: str):
    if not WIN:
        return None
    import winreg
    from .hardware import _reg as r
    return r(getattr(winreg, root), path, name)


def _run(args: list[str], timeout: float = 8.0) -> str:
    if not WIN:
        return ""
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout,  # noqa: S603  # nosec B603
                              creationflags=flags).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------------------------------------------------------------------------- Windows

POWER_PLANS = {
    "381b4222-f694-41f0-9685-ff5bb260df2e": "Balanced",
    "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c": "High performance",
    "a1841308-3541-4fab-bc81-f71556f20b4a": "Power saver",
    "e9a42b02-d5df-448d-aa00-03f14749eb61": "Ultimate Performance",
}


def parse_active_scheme(text: str) -> tuple[str | None, str | None]:
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\s*\(([^)]*)\)", text or "", re.I)
    return (m.group(1).lower(), m.group(2).strip()) if m else (None, None)


def parse_ac_value(text: str) -> int | None:
    """The AC (plugged in) value from `powercfg /qh` output for one setting."""
    m = re.search(r"AC Power Setting Index:\s*0x([0-9a-f]+)", text or "", re.I)
    return int(m.group(1), 16) if m else None


def windows_items(run: Callable = _run, reg: Callable = _reg, timer: Callable | None = None) -> list[dict]:
    A = "Windows"
    out = []
    guid, name = parse_active_scheme(run(["powercfg", "/getactivescheme"]))
    plan = POWER_PLANS.get(guid or "", name)
    if guid is None:
        out.append(_item(A, "Power plan", None))
    elif plan == "Power saver":
        out.append(_item(A, "Power plan", plan, "bad", "Power saver holds the processor back. Use Balanced or High "
                         "performance."))
    else:
        other = "High performance" if plan == "Balanced" else "Balanced"
        out.append(_item(A, "Power plan", plan, "test", "On a Ryzen 7800X3D, Balanced normally performs the same "
                         "as High or Ultimate Performance, with less heat. The app can test this by itself: it "
                         "switches the plan, measures both and puts yours back.",
                         {"auto": "powerplan", "label": "Test automatically"}))
    park = parse_ac_value(run(["powercfg", "/qh", "SCHEME_CURRENT", "SUB_PROCESSOR", "CPMINCORES"]))
    out.append(_item(A, "Core parking (minimum cores unparked)", None if park is None else f"{park}%",
                     "info", "100% means no cores are parked. On a single-CCD 7800X3D, parking rarely matters. "
                     "Shown for completeness."))

    gm = reg("HKEY_CURRENT_USER", r"Software\Microsoft\GameBar", "AutoGameModeEnabled")
    out.append(_item(A, "Game Mode", "On (Windows default)" if gm is None else ("On" if gm else "Off"), "test",
                     "Game Mode lowers background work while a game runs. It usually helps or makes no difference.",
                     {"a": "Game Mode off", "b": "Game Mode on"}))

    hags = reg("HKEY_LOCAL_MACHINE", r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "HwSchMode")
    out.append(_item(A, "Hardware-accelerated GPU scheduling", {2: "On", 1: "Off"}.get(hags, "Windows default"),
                     "test", "Results differ by driver; there is no general right answer. Changing it needs a Windows "
                     "restart, so it needs a two-session test (coming next)."))

    mpo = reg("HKEY_LOCAL_MACHINE", r"SOFTWARE\Microsoft\Windows\Dwm", "OverlayTestMode")
    out.append(_item(A, "Multiplane overlay (MPO)", "Off" if mpo == 5 else "On (Windows default)", "test",
                     "MPO has caused flicker and stutter on some multi-monitor NVIDIA setups. Changing it needs a "
                     "Windows restart, so it needs a two-session test (coming next)."))

    dvr = reg("HKEY_CURRENT_USER", r"Software\Microsoft\Windows\CurrentVersion\GameDVR", "HistoricalCaptureEnabled")
    out.append(_item(A, "Game Bar background recording", "On" if dvr else "Off",
                     "warn" if dvr else "ok", "It records the screen all the time, which costs smoothness." if dvr
                     else "Windows isn't recording the screen in the background."))

    g = reg("HKEY_CURRENT_USER", r"Software\Microsoft\DirectX\UserGpuPreferences", "DirectXUserGlobalSettings")
    wo = None if not isinstance(g, str) or "SwapEffectUpgradeEnable=" not in g else "SwapEffectUpgradeEnable=1" in g
    out.append(_item(A, "Optimizations for windowed games", "Windows default" if wo is None else ("On" if wo else "Off"),
                     "info", "Lets a borderless game like yours present frames like full screen, with lower latency. "
                     "iRacing picks up a change only when it restarts, so this can't be tested on a paused replay."))

    hvci = reg("HKEY_LOCAL_MACHINE", r"SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios"
               r"\HypervisorEnforcedCodeIntegrity", "Enabled")
    out.append(_item(A, "Memory integrity (Core isolation)", "On" if hvci else "Off", "info",
                     "A security feature. It costs a few percent in some games. The app won't suggest turning off "
                     "security. Shown so you know it's there."))

    t = (timer or timer_resolution)()
    out.append(_item(A, "Timer resolution", None if t is None else f"{t:.2f} ms", "info",
                     "The current system timer. Games set it themselves while running, so tools that force it rarely "
                     "change anything on Windows 11."))
    out.append(_item(A, "Startup apps", startup_apps(reg) or "None found", "info",
                     "Apps that start with Windows. Busy ones show up in the background test."))
    return out


def timer_resolution() -> float | None:
    if not WIN:
        return None
    try:
        mn, mx, cur = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_ulong()
        if ctypes.windll.ntdll.NtQueryTimerResolution(ctypes.byref(mn), ctypes.byref(mx), ctypes.byref(cur)) != 0:
            return None
        return cur.value / 10000.0
    except Exception:
        return None


def startup_apps(reg: Callable = _reg) -> str:
    if not WIN:
        return ""
    import winreg
    names = []
    for root, path in (("HKEY_CURRENT_USER", r"Software\Microsoft\Windows\CurrentVersion\Run"),
                       ("HKEY_LOCAL_MACHINE", r"Software\Microsoft\Windows\CurrentVersion\Run")):
        try:
            with winreg.OpenKey(getattr(winreg, root), path) as k:
                i = 0
                while True:
                    try:
                        names.append(winreg.EnumValue(k, i)[0])
                    except OSError:
                        break
                    i += 1
        except OSError:
            continue
    return ", ".join(dict.fromkeys(names))


# ---------------------------------------------------------------------------- NVIDIA (driver profile, via NVAPI)

# Setting ids and values from NVIDIA's public NvApiDriverSettings.h. An unknown value is shown raw, never guessed.
NV_SETTINGS = {
    0x1194F158: ("G-SYNC mode", {0: "Off", 1: "Full screen only", 2: "Full screen and windowed"}),
    0x10A879CF: ("Monitor technology", {0: "G-SYNC", 1: "G-SYNC forced off", 2: "G-SYNC not allowed", 3: "ULMB",
                                        4: "Fixed refresh"}),
    0x00A879CF: ("Vertical sync", {0x60925292: "Use the app setting", 0x08416747: "Off", 0x47814940: "On",
                                   0x18888888: "Fast", 0x32610244: "On (half refresh)", 0x71271021: "On (third refresh)",
                                   0x13245256: "On (quarter refresh)"}),
    0x10835002: ("Max frame rate", None),
    0x0005F543: ("Low latency mode", {0: "Off", 1: "On", 2: "Ultra"}),
    0x1057EB71: ("Power management mode", {0: "Normal (adaptive)", 1: "Prefer maximum performance",
                                           2: "Driver controlled", 3: "Prefer consistent performance",
                                           4: "Prefer minimum power", 5: "Optimal power"}),
    0x00198FFF: ("Shader cache", {0: "Off", 1: "On"}),
    0x00AC8497: ("Shader cache size", None),
    0x00CE2691: ("Texture filtering quality", {0xFFFFFFF6: "High quality", 0: "Quality", 10: "Performance",
                                              20: "High performance"}),
}
_NV_IDS = {"Initialize": 0x0150E828, "CreateSession": 0x0694D52E, "LoadSettings": 0x375DBD6B,
           "GetBaseProfile": 0xDA8466A0, "FindApplicationByName": 0xEEE566B2, "GetSetting": 0x73BF8338,
           "DestroySession": 0xDAD9CFF8, "FindProfileByName": 0x7E4A9A0B}
IRACING_EXE = "iRacingSim64DX11.exe"


class _Setting(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("name", ctypes.c_uint16 * 2048), ("id", ctypes.c_uint32),
                ("type", ctypes.c_uint32), ("location", ctypes.c_uint32), ("is_predefined", ctypes.c_uint32),
                ("predefined_valid", ctypes.c_uint32), ("predefined", ctypes.c_uint8 * 4100),
                ("current", ctypes.c_uint8 * 4100)]


class _App(ctypes.Structure):  # NVDRS_APPLICATION_V3
    _fields_ = [("version", ctypes.c_uint32), ("is_predefined", ctypes.c_uint32), ("app", ctypes.c_uint16 * 2048),
                ("friendly", ctypes.c_uint16 * 2048), ("launcher", ctypes.c_uint16 * 2048),
                ("file_in_folder", ctypes.c_uint16 * 2048), ("flags", ctypes.c_uint32),
                ("command_line", ctypes.c_uint16 * 2048)]


def _wstr(s: str):
    buf = (ctypes.c_uint16 * 2048)()
    for i, ch in enumerate(s[:2047]):
        buf[i] = ord(ch)
    return buf


NV_NOT_SET = -160          # NVAPI_SETTING_NOT_FOUND: not set in this profile, so the driver default applies
_APP_SIZES = {3: 20492, 2: 16392, 1: 12296}  # NVDRS_APPLICATION_V3/V2/V1
IRACING_PROFILES = ("iRacing", "iRacing Simulator")


def read_nvidia_profiles() -> dict:
    """{"global": {id: value}, "app": {id: value}, "codes": {id: [global code, app code]}, "profile": text}
    or {"error": text}. Read only."""
    if not WIN:
        return {"error": "Not on Windows."}
    try:
        dll = ctypes.WinDLL("nvapi64.dll")
    except OSError:
        return {"error": "The NVIDIA driver's NVAPI isn't installed."}
    qi = dll.nvapi_QueryInterface
    qi.restype, qi.argtypes = ctypes.c_void_p, [ctypes.c_uint32]

    def fn(name, *argtypes):
        p = qi(_NV_IDS[name])
        if not p:
            raise OSError(f"NVAPI has no {name}")
        return ctypes.CFUNCTYPE(ctypes.c_int, *argtypes)(p)

    try:
        if fn("Initialize")() != 0:
            return {"error": "NVAPI didn't start."}
        sess = ctypes.c_void_p()
        if fn("CreateSession", ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(sess)) != 0:
            return {"error": "Couldn't open the driver settings."}
        try:
            fn("LoadSettings", ctypes.c_void_p)(sess)
            get = fn("GetSetting", ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Setting))
            codes: dict[int, list] = {sid: [None, None] for sid in NV_SETTINGS}

            def read(profile, slot) -> dict:
                vals = {}
                for sid in NV_SETTINGS:
                    st = _Setting()
                    st.version = ctypes.sizeof(_Setting) | (1 << 16)
                    rc = get(sess, profile, sid, ctypes.byref(st))
                    codes[sid][slot] = rc
                    if rc == 0:
                        vals[sid] = int.from_bytes(bytes(st.current[:4]), "little")
                return vals

            base = ctypes.c_void_p()
            fn("GetBaseProfile", ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(sess, ctypes.byref(base))
            out = {"global": read(base, 0), "app": {}, "codes": codes}
            prof, tried = ctypes.c_void_p(), []
            find = fn("FindApplicationByName", ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                      ctypes.c_void_p)
            found = None
            for ver, size in _APP_SIZES.items():
                buf = ctypes.create_string_buffer(32768)
                ctypes.c_uint32.from_buffer(buf).value = size | (ver << 16)
                rc = find(sess, _wstr(IRACING_EXE), ctypes.byref(prof), buf)
                tried.append(f"app v{ver}: {rc}")
                if rc == 0:
                    found = f"found by {IRACING_EXE}"
                    break
            if not found:
                byname = fn("FindProfileByName", ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
                for name in IRACING_PROFILES:
                    rc = byname(sess, _wstr(name), ctypes.byref(prof))
                    tried.append(f"{name!r}: {rc}")
                    if rc == 0:
                        found = f"found by name ({name})"
                        break
            out["profile"] = found or "not found (NVAPI " + ", ".join(tried) + ")"
            if found:
                out["app"] = read(prof, 1)
            return out
        finally:
            fn("DestroySession", ctypes.c_void_p)(sess)
    except Exception as e:
        return {"error": f"Couldn't read the NVIDIA settings: {e}"}


def _nv_text(sid: int, v: int) -> str:
    name, values = NV_SETTINGS[sid]
    if values is None:
        if sid == 0x10835002:
            return "Off" if v == 0 else f"{v} fps"
        return "Driver default" if v in (0, 0xFFFFFFFF) else f"{v} MB"
    return values.get(v, f"Other (0x{v:X})")


def nvidia_items(read: Callable = read_nvidia_profiles) -> list[dict]:
    A = "NVIDIA"
    r = read()
    if r.get("error"):
        return [_item(A, "Driver settings", r["error"], "info", "Without them the NVIDIA settings can't be audited.")]
    out = []
    if r.get("profile"):
        ok = r["profile"].startswith("found")
        out.append(_item(A, "iRacing driver profile", "Found" if ok else "Not found", "info",
                         "Per-game settings come from it, else from the global settings." if ok else
                         "Only global settings could be read, so any setting made for iRacing alone isn't shown. "
                         f"Details: {r['profile']}."))
    for sid, (name, _vals) in NV_SETTINGS.items():
        app, glob = r.get("app", {}).get(sid), r.get("global", {}).get(sid)
        v = app if app is not None else glob
        if v is None:
            codes = [c for c in (r.get("codes") or {}).get(sid, []) if c is not None]
            if codes and all(c == NV_NOT_SET for c in codes):
                out.append(_item(A, name, "Driver default", "info", "Not set anywhere, so the driver's default applies."))
            else:
                out.append(_item(A, name, None, "info", f"NVAPI answered {codes}." if codes else ""))
            continue
        where = "iRacing profile" if app is not None else "global"
        val = f"{_nv_text(sid, v)} ({where})"
        level, note, test = "info", "", None
        if sid == 0x00198FFF and v == 0:
            level, note = "bad", "With the shader cache off, iRacing rebuilds shaders every session, which stutters."
        elif sid == 0x1194F158:
            level, note, test = "test", ("G-SYNC smooths uneven frame times without V-Sync's tearing trade-off, on "
                                         "monitors that support it."), {"a": "G-Sync off", "b": "G-Sync on"}
        elif sid == 0x10A879CF and v == 4:
            note = "With fixed refresh the monitors don't use G-SYNC, whatever G-SYNC mode says. The G-SYNC test covers both."
        elif sid == 0x0005F543:
            level, note, test = "test", "Changes latency and sometimes 1% lows. Worth one test.", \
                {"a": "Low latency off", "b": "Low latency on"}
        elif sid == 0x10835002 and v:
            note = "A driver cap. It shows as the cap in every test, so the app's numbers stop at it."
        elif sid == 0x1057EB71 and v == 1:
            note = "Keeps the card at full clocks. It adds power and heat, rarely frames, on a GPU-bound PC."
        out.append(_item(A, name, val, level, note, test))
    return out


# ---------------------------------------------------------------------------- iRacing

HEAVY = ["MaxCarsToDraw", "DynamicShadowMaps", "ShadowMapType", "ShadowDetail", "DynamicShadowTrim",
         "VirtualMirrors", "MaxCockpitMirrors", "MirrorDetail", "MaxCarsToDrawInMirrors", "SSAO", "SSRLevel",
         "SSRRainOnly", "DepthOfField", "MotionBlurStrength", "HeatHaze", "Particles", "ObjectDetail", "CarDetail",
         "GrandstandDetail", "CrowdDetail", "PitObjectDetail", "FoliageDetail", "NumDynamicCubemaps",
         "TwoPassTrees", "SkyRefreshRate", "LODPct", "LODMinFPSTarget", "MSAA", "AAMode", "SSAA", "ShaderQuality",
         "TextureQuality", "AnisotropicFiltering"]


def iracing_items(snap: dict | None) -> list[dict]:
    A = "iRacing"
    if not snap:
        return [_item(A, "Graphics settings", None, "info", "The iRacing settings folder wasn't found.")]
    from .config_reader import replay_mismatch
    secs = {s.lower(): kv for s, kv in (snap.get("renderer") or {}).items()}
    race = {k.lower(): (k, v) for k, v in (secs.get("graphics options") or {}).items()}
    rep = {k.lower(): v for k, v in (secs.get("replay graphics") or {}).items()}
    diff = {d["key"].lower() for d in replay_mismatch(snap)}
    out = [_item(A, "Settings file", snap.get("renderer_file") or None, "info",
                 "The file iRacing reads its graphics from. It writes it when the sim closes.")]
    if diff:
        it = _item(A, "Replay and race graphics differ", f"{len(diff)} setting{'s' if len(diff) != 1 else ''}", "warn",
                   "Tests run on a replay, so they'd measure the replay values. If you already made them the same in "
                   "the sim, iRacing hasn't saved them yet: it writes them when the sim closes. Close iRacing, then "
                   "press Read again.")
        it["summary"] = True
        out.append(it)
    for key in HEAVY:
        if key.lower() not in race:
            continue
        k, v = race[key.lower()]
        rv = rep.get(key.lower())
        differs = key.lower() in diff
        val = f"{v.strip()} race, {rv.strip()} replay" if differs else v.strip()
        out.append(_item(A, k, val, "warn" if differs else "test",
                         "Replay uses a different value, so tests won't match races." if differs else
                         "A heavy option on triple screens. The graphics ladder tests it.",
                         {"a": f"{k} {v.strip()}", "b": f"{k} lower"}))
    return out


# ---------------------------------------------------------------------------- everything

def run(snapshot: Callable | None = None, windows: Callable = windows_items, nvidia: Callable = nvidia_items) -> dict:
    items = []
    for part in (windows, nvidia):
        try:
            items += part()
        except Exception as e:
            items.append(_item("App", "Audit error", f"{type(e).__name__}: {e}", "info"))
    try:
        snap = snapshot() if snapshot else None
    except Exception:
        snap = None
    items += iracing_items(snap)
    rows = [i for i in items if not i.get("summary")]
    counts = {lv: sum(1 for i in rows if i["level"] == lv) for lv in ("bad", "warn", "test", "ok", "info")}
    counts["attention"] = counts["bad"] + counts["warn"]
    return {"items": items, "counts": counts}
