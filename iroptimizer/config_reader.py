"""Localiza Documentos\\iRacing y carga renderer*.ini, app.ini y core.ini."""
from __future__ import annotations

from .i18n import _

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .iniio import IniFile

from .safety import RENDERER_FILES, redact_path, safe_renderer


def documents_dir() -> Path:
    """Carpeta Documentos real (respeta la redirección a OneDrive)."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            import uuid

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

            u = uuid.UUID("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}")  # FOLDERID_Documents
            g = GUID(u.fields[0], u.fields[1], u.fields[2],
                     (ctypes.c_ubyte * 8).from_buffer_copy(u.bytes[8:]))
            p = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p)) == 0:
                path = Path(p.value)
                ctypes.windll.ole32.CoTaskMemFree(p)
                return path
        except Exception:
            pass
    return Path(os.path.expanduser("~")) / "Documents"


def find_iracing_dir(override: str | None = None) -> Path:
    if override:
        return Path(override)
    env = os.environ.get("IRACING_DIR")
    if env:
        return Path(env)
    return documents_dir() / "iRacing"


@dataclass
class IRacingConfig:
    directory: Path
    renderer: IniFile | None = None
    app: IniFile | None = None
    core: IniFile | None = None
    renderer_candidates: list[tuple[str, float]] = field(default_factory=list)

    @property
    def renderer_name(self) -> str | None:
        return self.renderer.path.name if self.renderer else None


def load(ini_dir: str | None = None, renderer: str | None = None) -> IRacingConfig:
    d = find_iracing_dir(ini_dir)
    if not d.is_dir():
        raise FileNotFoundError(_("No encuentro la carpeta de iRacing: {d}  (usa --ini-dir)", d=d))
    cfg = IRacingConfig(directory=d)

    cands = [(n, (d / n).stat().st_mtime) for n in RENDERER_FILES if (d / n).is_file()]
    cfg.renderer_candidates = sorted(cands, key=lambda x: -x[1])
    if renderer:
        cfg.renderer = IniFile(d / safe_renderer(renderer))
    else:
        # Prioridad: Monitor si existe (pantalla plana); si no, el modificado más reciente.
        names = [n for n, _mt in cfg.renderer_candidates]
        pick = "rendererDX11Monitor.ini" if "rendererDX11Monitor.ini" in names else (names[0] if names else None)
        if pick:
            cfg.renderer = IniFile(d / pick)

    for attr, name in (("app", "app.ini"), ("core", "core.ini")):
        if (d / name).is_file():
            setattr(cfg, attr, IniFile(d / name))
    return cfg


def snapshot(cfg: IRacingConfig) -> dict:
    return {
        "directory": redact_path(str(cfg.directory)),  # sin tu nombre de usuario si compartes la sesión
        "renderer_file": cfg.renderer_name,
        "renderer_candidates": cfg.renderer_candidates,
        "renderer": cfg.renderer.as_dict() if cfg.renderer else {},
        "app": cfg.app.as_dict() if cfg.app else {},
        "core": cfg.core.as_dict() if cfg.core else {},
    }


def flat_from_snapshot(section_dict: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for sec in section_dict.values():
        for k, v in sec.items():
            out.setdefault(k.lower(), v)
    return out


def _replay_keys(section_dict: dict) -> dict:
    return {f"{sec}/{k}": v for sec, kv in (section_dict or {}).items() for k, v in kv.items()
            if "replay" in k.lower() or "replay" in sec.lower() or "spectat" in k.lower()}


def settings_key(snap: dict) -> str | None:
    """Short fingerprint of the graphics settings a test ran with: the renderer ini (minus window placement) plus
    any replay-related keys in app.ini. Results with a different key were measured on different settings."""
    import hashlib
    import json as _json
    rend = snap.get("renderer") or {}
    if not rend:
        return None
    keep = {sec: kv for sec, kv in rend.items() if not any(w in sec.lower() for w in ("window", "position"))}
    blob = _json.dumps({"renderer": keep, "replay": _replay_keys(snap.get("app") or {})}, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]  # nosec B324 (a fingerprint, not security)


VIEW_TOGGLES = ("HideCockpitObstructions", "DriverHands", "SteeringWheel")


def _race_replay(snap: dict) -> tuple[dict, dict] | None:
    secs = {s.lower(): kv for s, kv in (snap.get("renderer") or {}).items()}
    race, rep = secs.get("graphics options"), secs.get("replay graphics")
    if not race or not rep:
        return None
    return ({k.lower(): (k, v.strip()) for k, v in race.items()}, {k.lower(): (k, v.strip()) for k, v in rep.items()})


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _raw_diffs(snap: dict) -> list[dict]:
    rr = _race_replay(snap)
    if not rr:
        return []
    race, rep = rr
    return [{"key": rep[k][0], "race": race[k][1], "replay": rep[k][1]}
            for k in rep if k in race and race[k][1] != rep[k][1]]


def replay_mismatch(snap: dict, low1: float | None = None) -> list[dict]:
    """Settings that differ between [Graphics Options] (racing) and [Replay Graphics] (what replays, and so every
    test, run with) and that can change what a test measures. Only keys present in both are compared.

    Differences that can't matter are left out: motion-blur camera switches when motion blur strength is 0 on both
    sides, shadow trim when dynamic shadows are off on both, and the LOD frame-rate target when the measured 1% low
    is above both targets (so LOD never kicks in). The cockpit view toggles are reported by view_toggle_diffs()
    instead. This reads the ini, which iRacing only writes when it saves or exits."""
    rr = _race_replay(snap)
    if not rr:
        return []
    race, rep = rr

    def both(key: str, test) -> bool:
        a, b = race.get(key.lower()), rep.get(key.lower())
        return a is not None and b is not None and test(a[1]) and test(b[1])

    zero = lambda v: _num(v) == 0  # noqa: E731
    out = []
    for d in _raw_diffs(snap):
        k = d["key"].lower()
        if k in {t.lower() for t in VIEW_TOGGLES}:
            continue
        if k.startswith("motionblur") and k != "motionblurstrength" and both("MotionBlurStrength", zero):
            continue
        if k == "dynamicshadowtrim" and both("DynamicShadowMaps", zero):
            continue
        if k == "lodminfpstarget" and low1 is not None:
            targets = [_num(d["race"]), _num(d["replay"])]
            if all(t is not None for t in targets) and low1 > max(targets):
                continue
        out.append(d)
    return out


def view_toggle_diffs(snap: dict) -> list[dict]:
    """Cockpit view toggles (hide obstructions, driver hands, steering wheel) that differ between race and replay.
    They aren't on the Graphics menu and cost the GPU only a little, so they're reported apart from real
    mismatches."""
    names = {t.lower() for t in VIEW_TOGGLES}
    return [d for d in _raw_diffs(snap) if d["key"].lower() in names]


def mismatch_text(diffs: list[dict], limit: int = 6) -> str:
    if not diffs:
        return ""
    shown = "; ".join(f"{d['key']} {d['race']} in races, {d['replay']} in replays" for d in diffs[:limit])
    more = f" and {len(diffs) - limit} more" if len(diffs) > limit else ""
    return (f"iRacing's settings file shows replay graphics that differ from race graphics: {shown}{more}. Tests "
            "run on a replay, so the results would describe settings you don't race with. This is read from the "
            "file, and iRacing only writes menu changes to it when it saves or exits: if the Graphics menu already "
            "matches, close the sim once so it saves, then check again.")
