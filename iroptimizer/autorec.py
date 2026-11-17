"""Grabación automática (v0.11): arranca al abrir iRacing y descarta sesiones demasiado cortas.

Lógica sin interfaz para poder probarla: la ventana (gui.py) solo pregunta a `AutoArm` si debe arrancar y usa
`too_short`/`discard` al terminar. El inicio con Windows (clave Run del usuario) está en `autostart_*`.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

SIM_PROCS = {"iracingsim64dx11.exe", "iracingsim64.exe"}
MIN_ON_TRACK_S = 180  # menos de 3 min en pista (entrar y salir, mirar el coche) no se guarda
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "iRacingOptimizer"


class AutoArm:
    """Decide cuándo arrancar la grabación automática.

    - Arranca una vez por cada vez que abres iRacing.
    - Si paras la grabación a mano (o ya la tenías en marcha), no vuelve a arrancar hasta que cierres iRacing.
    """

    def __init__(self):
        self.armed = True

    def step(self, sim_running: bool, monitor_running: bool) -> bool:
        if not sim_running:
            self.armed = True
            return False
        if monitor_running:
            self.armed = False
            return False
        if self.armed:
            self.armed = False
            return True
        return False


def sim_running() -> bool:
    try:
        import psutil
        return any((p.info.get("name") or "").lower() in SIM_PROCS for p in psutil.process_iter(["name"]))
    except Exception:
        return False


def on_track_seconds(csv_path: Path, hz: float) -> float:
    """Segundos en pista (sin repeticiones) de una sesión grabada."""
    n = 0
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("on_track") == "1" and r.get("replay") != "1":
                    n += 1
    except OSError:
        return 0.0
    return n / max(hz, 0.1)


def too_short(csv_path: Path, hz: float, min_s: float = MIN_ON_TRACK_S) -> bool:
    return on_track_seconds(csv_path, hz) < min_s


def discard(csv_path: Path) -> None:
    """Borra una sesión descartada: CSV, JSON y registro de PresentMon (con o sin comprimir)."""
    csv_path = Path(csv_path)
    stem = csv_path.with_suffix("")
    for p in (csv_path, stem.with_suffix(".json"), Path(f"{stem}.presentmon.csv"), Path(f"{stem}.presentmon.csv.gz")):
        try:
            p.unlink()
        except OSError:
            pass


# ------------------------------------------------------------------ inicio con Windows

def autostart_command() -> str | None:
    """Comando para la clave Run: solo con el .exe (con Python suelto no tiene sentido)."""
    if not getattr(sys, "frozen", False):
        return None
    return f'"{sys.executable}" --tray'


def autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            v, _t = winreg.QueryValueEx(k, RUN_NAME)
            return bool(v)
    except OSError:
        return False


def set_autostart(on: bool) -> bool:
    """Añade o quita la app del inicio de Windows (solo para tu usuario, sin administrador)."""
    if sys.platform != "win32":
        return False
    import winreg
    cmd = autostart_command()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                if not cmd:
                    return False
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False
