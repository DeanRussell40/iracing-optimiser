"""Screen-capture tools: apps that copy the screen many times a second (recording, streaming, ambient lighting).
They compete with the sim for the GPU and the desktop compositor and are a known cause of stutter.

Detection is by process name, plus SimHub's own switch: SimHub's ambient lighting captures the screen only when
"EnableCaptureEngine" is true in PluginsData\\Common\\SimuLightSettings.json. The file is only read, never written
(SimHub rewrites it on save).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

SCREEN_CAPTURE = {
    "obs64.exe": "OBS",
    "obs32.exe": "OBS",
    "streamlabs obs.exe": "Streamlabs",
    "xsplit.core.exe": "XSplit",
    "nvidia share.exe": "NVIDIA overlay (Instant Replay or recording)",
    "gamebar.exe": "Xbox Game Bar",
    "gamebarftserver.exe": "Xbox Game Bar",
    "medal.exe": "Medal",
    "overwolf.exe": "Overwolf",
    "bdcam.exe": "Bandicam",
    "prismatik.exe": "Prismatik (ambient lighting)",
    "ambibox.exe": "AmbiBox (ambient lighting)",
    "hyperion.exe": "Hyperion (ambient lighting)",
    "hyperiond.exe": "Hyperion (ambient lighting)",
    "signalrgb.exe": "SignalRGB (screen ambience)",
}
SIMHUB_EXE = "simhubwpf.exe"


def capture_apps(names) -> list[str]:
    """Friendly names of screen-capture tools among the given process names, without repeats."""
    return list(dict.fromkeys(SCREEN_CAPTURE[n.lower()] for n in names if n and n.lower() in SCREEN_CAPTURE))


def simhub_dirs() -> list[Path]:
    out = []
    try:
        import psutil
        for p in psutil.process_iter(["name", "exe"]):
            if (p.info["name"] or "").lower() == SIMHUB_EXE and p.info["exe"]:
                out.append(Path(p.info["exe"]).parent)
    except Exception:
        pass
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        if os.environ.get(env):
            out.append(Path(os.environ[env]) / "SimHub")
    return list(dict.fromkeys(out))


def simhub_capture_on(dirs: list[Path] | None = None) -> bool | None:
    """True when SimHub's screen capture is switched on, False when off, None when SimHub isn't found."""
    for d in dirs if dirs is not None else simhub_dirs():
        f = Path(d) / "PluginsData" / "Common" / "SimuLightSettings.json"
        try:
            data = json.loads(f.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        v = data.get("EnableCaptureEngine") if isinstance(data, dict) else None
        if isinstance(v, bool):
            return v
    return None


SIMHUB_ON_TEXT = ("SimHub's screen capture is switched on (EnableCaptureEngine in SimuLightSettings.json). It copies "
                  "the screen continuously for ambient lighting, a known cause of stutter. If you turned ambient "
                  "lighting off, the capture engine itself is still on, or SimHub hasn't saved yet: switch the "
                  "screen capture off in SimHub's lighting settings and close SimHub once so it saves.")
