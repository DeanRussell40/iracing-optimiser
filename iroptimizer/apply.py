"""Aplica cambios a los .ini con copia de seguridad, y restaura."""
from __future__ import annotations

from .i18n import _

import json
import shutil
from datetime import datetime
from pathlib import Path

import psutil

from .iniio import IniFile
from .safety import UnsafeInput, WRITABLE_FILES, safe_ini_target, safe_renderer, safe_stamp

BACKUP_DIR = "iroptimizer_backups"
ORIGINAL_LABEL = "original"

# Locked until a setting can be proven by an A/B test on the replay bench. The recommendations, profiles,
# FOV and drift re-apply all come from fixed rules of thumb, not from measurements on this PC, so the app
# must not write them. Restoring a backup is still allowed: that only ever puts back the user's own files.
WRITES_LOCKED = True
LOCKED_MESSAGE = ("Changing iRacing settings is switched off in this version.\n\n"
                  "These suggestions come from fixed rules of thumb, not from tests on your PC. The app will only "
                  "change a setting once it can prove the change helps with an A/B test on the Replay bench "
                  "(next milestone).\n\nRestoring a backup still works.")


class ChangesLocked(RuntimeError):
    """Writing untested ini changes is switched off (see WRITES_LOCKED)."""
BACKUP_FILES = ("rendererDX11Monitor.ini", "rendererDX11OpenXR.ini", "rendererDX11OpenVR.ini",
                "rendererDX11Oculus.ini", "rendererDX11.ini", "app.ini", "core.ini")


def iracing_running() -> bool:
    for p in psutil.process_iter(["name"]):
        if (p.info["name"] or "").lower().startswith("iracingsim"):
            return True
    return False


def backup(ini_dir: Path, label: str = "") -> Path:
    """Copia todos los .ini relevantes a iroptimizer_backups/<fecha>[_label]/."""
    import re
    ini_dir = Path(ini_dir)
    label = re.sub(r"[^\w\-]", "_", label, flags=re.UNICODE)[:40]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S") + (f"_{label}" if label else "")
    bdir = ini_dir / BACKUP_DIR / stamp
    bdir.mkdir(parents=True, exist_ok=True)
    for name in BACKUP_FILES:
        if (ini_dir / name).is_file():
            shutil.copy2(ini_dir / name, bdir / name)
    return bdir


def apply_changes(changes: list[dict], ini_dir: Path, renderer_file: str | None,
                  dry_run: bool = False, label: str = "", state_base: Path | None = None) -> list[str]:
    """changes: [{file: 'renderer'|'app', key, proposed, section?}]. Solo toca claves existentes."""
    if not changes:
        return [_("No hay cambios que aplicar.")]
    if not dry_run and WRITES_LOCKED:
        raise ChangesLocked(LOCKED_MESSAGE)
    if not dry_run and iracing_running():
        raise RuntimeError(_("Cierra iRacing antes de aplicar: el simulador reescribe los .ini al salir."))
    files = {"renderer": safe_renderer(renderer_file), "app": "app.ini"}
    log, opened, applied = [], {}, []
    for c in changes:
        if c.get("file") not in files:
            log.append(_("  ! tipo de archivo no permitido: {p0}", p0=str(c.get('file'))[:20]))
            continue
        fname = files[c["file"]]
        path = safe_ini_target(ini_dir, fname)
        if not path.is_file():
            log.append(_("  ! {fname} no existe, se omite {p1}", fname=fname, p1=c['key']))
            continue
        ini = opened.get(fname) or opened.setdefault(fname, IniFile(path))
        before = ini.get(c["key"], c.get("section"))
        if before is None:
            log.append(_("  ! {fname}: {p1} no existe, se omite", fname=fname, p1=c['key']))
            continue
        if str(before) == str(c["proposed"]):
            applied.append(c)
            continue
        try:
            ini.set(c["key"], c["proposed"], c.get("section"))
        except UnsafeInput as e:
            log.append(f"  ! {fname}: {c['key']} rechazado ({e})")
            continue
        applied.append(c)
        log.append(f"  {fname}: {c['key']} {before} → {c['proposed']}")
    if dry_run:
        return [_("(simulación, no se ha escrito nada)")] + log
    bdir = backup(ini_dir, label)
    for ini in opened.values():
        ini.save()
    if state_base is not None:  # recordar lo aplicado para detectar cambios posteriores
        from . import drift
        drift.record(state_base, applied, renderer_file)
    return [_("Copia de seguridad: {bdir}", bdir=bdir)] + log


def apply(recs_path: Path, ini_dir: Path, max_priority: int = 2, only_keys: list[str] | None = None,
          dry_run: bool = False) -> list[str]:
    data = json.loads(Path(recs_path).read_text(encoding="utf-8"))
    recs = [r for r in data["recommendations"] if r.get("key") and r["priority"] <= max_priority]
    if only_keys:
        wanted = {k.lower() for k in only_keys}
        recs = [r for r in recs if r["key"].lower() in wanted]
    return apply_changes(recs, ini_dir, data.get("renderer_file"), dry_run, "recomendaciones")


def list_backups(ini_dir: Path) -> list[Path]:
    root = Path(ini_dir) / BACKUP_DIR
    return sorted((d for d in root.iterdir() if d.is_dir()), reverse=True) if root.is_dir() else []


def original_backup(ini_dir: Path) -> Path | None:
    """The oldest backup: the user's settings before this app (or its upstream version) first changed anything."""
    dirs = list_backups(ini_dir)
    marked = [d for d in dirs if d.name.endswith("_" + ORIGINAL_LABEL)]
    return (marked or dirs)[-1] if dirs else None


def ensure_original(ini_dir: Path) -> Path | None:
    """Takes a one-off snapshot of the user's untouched settings the first time the app sees the folder.
    Does nothing when any backup already exists (the oldest one is then the original)."""
    ini_dir = Path(ini_dir)
    if not ini_dir.is_dir() or list_backups(ini_dir):
        return None
    if not any((ini_dir / n).is_file() for n in BACKUP_FILES):
        return None
    return backup(ini_dir, ORIGINAL_LABEL)


def restore_original(ini_dir: Path) -> list[str]:
    """One-click undo of everything: puts back the oldest backup (saving the current state first)."""
    src = original_backup(ini_dir)
    if not src:
        raise FileNotFoundError("There is no original backup yet.")
    if iracing_running():
        raise RuntimeError("Close iRacing before restoring.")
    backup(ini_dir, "before_restore")
    return restore(ini_dir, src.name)


def restore(ini_dir: Path, stamp: str | None = None) -> list[str]:
    dirs = list_backups(ini_dir)
    if not dirs:
        raise FileNotFoundError(_("No hay copias de seguridad."))
    src = (Path(ini_dir) / BACKUP_DIR / safe_stamp(stamp)) if stamp else dirs[0]
    if not src.is_dir():
        raise FileNotFoundError(_("No existe la copia {stamp}", stamp=stamp))
    if iracing_running():
        raise RuntimeError(_("Cierra iRacing antes de restaurar."))
    out = []
    for f in src.iterdir():
        if f.name not in WRITABLE_FILES or not f.is_file() or f.is_symlink():
            continue  # solo restauramos .ini conocidos de iRacing
        shutil.copy2(f, safe_ini_target(ini_dir, f.name))
        out.append(f"Restored {f.name} from {src.name}")
    return out
