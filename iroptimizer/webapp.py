"""The main window: an HTML interface (iroptimizer/webui) in a native window (pywebview on Edge WebView2).

The page talks to the Api class below through pywebview's js_api bridge. Long tests run on a worker thread; the page
polls Api.poll() for progress, log lines and prompts. The Tk interface (gui.py) is kept as a fallback for PCs
without WebView2 and for the tray mode.
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import __version__, abtest, dashboard

APP = "iRacing Optimiser"
WEBUI = Path(__file__).parent / "webui"
_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
TESTS = ("repeat", "background", "guided")


def default_out_dir() -> Path:
    from . import config_reader
    d = config_reader.documents_dir() / "iRacingOptimizer"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _open(p: Path) -> None:
    if sys.platform == "win32":
        os.startfile(p)  # noqa: S606  (a path the app built itself, checked by the caller)
    else:
        import webbrowser
        webbrowser.open(Path(p).resolve().as_uri())


@dataclass
class Job:
    test: str
    title: str
    started: float
    eta_s: float
    state: str = "running"          # running | done | stopped | error
    logs: list = field(default_factory=list)
    prompt: dict | None = None
    result: dict | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    answered: threading.Event = field(default_factory=threading.Event)


class Api:
    """Everything the page can ask for. Every method returns plain JSON-able data and never raises."""

    def __init__(self, out_dir: Path | None = None, *, run_bench: Callable | None = None,
                 driver_factory: Callable | None = None, capture_factory: Callable | None = None,
                 presentmon_exe: Callable[[], Path | None] | None = None, elevation: Callable[[], str | None] | None = None,
                 sampler_factory: Callable | None = None, pauser_factory: Callable | None = None,
                 ini_dir: Callable[[], Path] | None = None, scan_seconds: float = 5.0, opener: Callable = _open,
                 bench_overrides: dict | None = None, updater=None):
        self._out_dir = Path(out_dir) if out_dir else default_out_dir()
        self._bench_root = self._out_dir / "bench"
        self._window = None
        self._job: Job | None = None
        self._lock = threading.Lock()
        self._run_bench = run_bench
        self._driver_factory = driver_factory
        self._capture_factory = capture_factory
        self._presentmon_exe = presentmon_exe
        self._elevation = elevation
        self._sampler_factory = sampler_factory
        self._pauser_factory = pauser_factory
        self._ini_dir = ini_dir
        self._scan_seconds = scan_seconds
        self._opener = opener
        self._bench_overrides = bench_overrides or {}
        self._machine = None
        self._resumed = 0
        self._update: dict = {"available": False, "state": "checking"}
        self._updater = updater
        try:
            from . import presentmon
            presentmon.ensure_cached()  # a copy outside the temp folder, in case that folder disappears
        except Exception:
            pass
        try:
            from . import winsys
            self._resumed = winsys.resume_leftovers(self._out_dir)
        except Exception:
            pass

    # ------------------------------------------------------------------ dependencies (real ones load lazily)
    def _elevation_problem(self) -> str | None:
        try:
            if self._elevation:
                return self._elevation()
            from . import winsys
            return winsys.elevation_mismatch()
        except Exception:
            return None

    def _ini(self) -> Path | None:
        try:
            if self._ini_dir:
                return Path(self._ini_dir())
            from . import config_reader
            return Path(config_reader.find_iracing_dir())
        except Exception:
            return None

    def _machine_line(self) -> str:
        if self._machine is None:
            try:
                from . import hardware
                hw = hardware.get_hardware()
                gpu = ((hw.get("gpus") or [{}])[0].get("name") or "").replace("NVIDIA GeForce ", "")
                import platform
                self._machine = " · ".join(x for x in (platform.node(), gpu) if x)
            except Exception:
                self._machine = ""
        return self._machine

    def _settings(self) -> dict:
        try:
            from . import i18n
            return i18n.load_settings()
        except Exception:
            return {}

    # ------------------------------------------------------------------ state
    def _reanalyse_old(self) -> None:
        """Bundles analysed by an older build lack newer results (stutter attribution, current verdict rules).
        Re-analysing from the saved frames brings them up to date, once."""
        if getattr(self, "_reanalysed", False):
            return
        self._reanalysed = True
        from . import bench
        if not self._bench_root.is_dir():
            return
        for d in self._bench_root.iterdir():
            try:
                sj = d / "summary.json"
                if (d / "bench.json").is_file() and (not sj.is_file() or b'"stutter_attribution"' not in sj.read_bytes()):
                    bench.analyze_bundle(d)
            except Exception:
                continue

    def view(self) -> dict:
        try:
            self._reanalyse_old()
            focus = int(self._settings().get("focus", 1))
            v = dashboard.view(self._bench_root, focus, self._elevation_problem(), self._machine_line())
            from . import version_label
            v["version"] = version_label()
            v["backups"] = self.backups()
            v["focus_labels"] = list(abtest.FOCUS_LABELS)
            v["job"] = self._job_state(0)
            v["update"] = self.update_status()
            v["notice"] = (f"Resumed {self._resumed} app(s) that a previous test left paused."
                           if self._resumed else "")
            self._resumed = 0
            return v
        except Exception as e:
            return {"error": f"Couldn't read the results: {e}", "trace": traceback.format_exc(limit=3)}

    def set_focus(self, value) -> dict:
        try:
            from . import i18n
            v = max(0, min(4, int(value)))
            i18n.save_setting("focus", v)
            return {"ok": True, "focus": dashboard.focus_text(v, [])}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------ tests
    def _log(self, job: Job, text: str) -> None:
        with self._lock:
            job.logs.append({"t": time.strftime("%H:%M:%S"), "text": str(text)})

    def _job_state(self, since: int) -> dict | None:
        j = self._job
        if not j:
            return None
        with self._lock:
            logs = j.logs[since:]
            n = len(j.logs)
        el = time.time() - j.started
        return {"test": j.test, "title": j.title, "state": j.state, "elapsed_s": el, "eta_s": j.eta_s,
                "progress": min(0.99, el / j.eta_s) if j.state == "running" and j.eta_s else 1.0,
                "logs": logs, "next": n, "prompt": j.prompt, "result": j.result}

    def poll(self, since=0) -> dict:
        try:
            return {"job": self._job_state(int(since or 0))}
        except Exception as e:
            return {"error": str(e)}

    def scan_background(self) -> dict:
        """Samples per-app CPU for a few seconds and returns the busy apps that could be paused."""
        try:
            from . import winsys
            smp = self._sampler_factory() if self._sampler_factory else winsys.BackgroundSampler(interval_s=1.0)
            smp.start()
            time.sleep(self._scan_seconds)
            smp.stop()
            rows = winsys.summarize_background(smp.samples, top=12)
            names = winsys.suspects(rows)
            cores = {r["name"]: r["avg_cores"] for r in rows}
            return {"ok": True, "apps": [{"name": n, "cores": round(cores.get(n, 0), 2)} for n in names]}
        except Exception as e:
            return {"ok": False, "error": f"Couldn't scan background apps: {e}"}

    def start(self, test: str, opts: dict | None = None) -> dict:
        try:
            return self._start(test, opts or {})
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _start(self, test: str, opts: dict) -> dict:
        from . import bench
        if test not in TESTS:
            return {"ok": False, "error": "Unknown test."}
        if self._job and self._job.state == "running":
            return {"ok": False, "error": "A test is already running."}
        problem = self._elevation_problem()
        if problem:
            return {"ok": False, "error": problem, "fix": "relaunch_admin"}
        try:
            cfg = bench.BenchConfig(seconds=float(opts.get("seconds") or 60), runs=int(opts.get("runs") or 5),
                                    warmup_runs=int(opts.get("warmup") if opts.get("warmup") is not None else 1),
                                    **self._bench_overrides)
            bench.validate(cfg)
        except (TypeError, ValueError) as e:
            return {"ok": False, "error": f"Check the test length: {e}"}
        exe = None
        if not self._capture_factory:
            from . import presentmon
            exe = self._presentmon_exe() if self._presentmon_exe else presentmon.find_exe([self._out_dir / "tools"])
            if not exe:
                return {"ok": False, "error": "PresentMon, which measures the frames, is missing from this copy of "
                        "the app. Close the app completely and open it again."}

        variants, title = None, "Measure my PC"
        job = Job(test, title, time.time(), 0)
        if test == "background":
            names = [str(n) for n in opts.get("names") or [] if isinstance(n, str) and n.strip()]
            if not names:
                return {"ok": False, "error": "No apps chosen to pause."}
            from . import winsys
            pauser = self._pauser_factory(self._out_dir) if self._pauser_factory else winsys.ProcessPauser(self._out_dir)
            variants = [bench.Variant(dashboard.BG_RUNNING),
                        bench.Variant(dashboard.BG_PAUSED, description=", ".join(names),
                                      apply=lambda: self._log(job, "Paused: " + (", ".join(pauser.pause(names))
                                                                                 or "nothing (no access?)")),
                                      revert=pauser.resume)]
            job.title = "Background apps: running vs paused"
        elif test == "guided":
            a = (str(opts.get("a") or "").strip() or "A")[:60]
            b = (str(opts.get("b") or "").strip() or "B")[:60]
            if a == b:
                return {"ok": False, "error": "A and B need different names."}
            variants = [bench.Variant(a, manual=True, apply=lambda: self._ask(
                            job, f"Set up A: {a}", "The replay is paused. Change it in iRacing or the NVIDIA app, "
                            "then press Done. Don't move the replay or the camera.")),
                        bench.Variant(b, manual=True, apply=lambda: self._ask(
                            job, f"Set up B: {b}", "The replay is paused. Change it, then press Done."))]
            job.title = f"{b} vs {a}"
        n = cfg.runs * (len(variants) if variants else 1) + cfg.warmup_runs
        job.eta_s = n * (cfg.seconds + cfg.settle_s + 2)
        self._job = job
        run_bench = self._run_bench or bench.run_bench
        driver = self._driver_factory() if self._driver_factory else bench.IRSDKDriver()
        if self._capture_factory:
            capture = self._capture_factory
        else:
            from . import presentmon
            capture = lambda p: presentmon.Capture(exe, p)  # noqa: E731

        def run():
            try:
                bdir = run_bench(cfg, self._out_dir, driver, capture, log=lambda s: self._log(job, s), stop=job.stop,
                                 variants=variants)
                s = dashboard.load_bundle(bdir) or {}
                lines = [c["verdict_text"] for c in s.get("comparisons") or []]
                lines.append((s.get("repeatability") or {}).get("verdict_text", ""))
                job.result = {"id": Path(bdir).name, "lines": [x for x in lines if x]}
                job.state = "done"
                self._log(job, "Finished.")
            except bench.Aborted:
                job.state = "stopped"
                self._log(job, "Stopped. Nothing was changed.")
            except Exception as e:
                job.state = "error"
                job.result = {"error": str(e)}
                self._log(job, f"Error: {e}")
            finally:
                job.prompt = None
        threading.Thread(target=run, daemon=True).start()
        return {"ok": True, "eta_min": round(job.eta_s / 60, 1), "title": job.title}

    def _ask(self, job: Job, title: str, text: str) -> None:
        """Blocks the test thread until the user presses Done. Brings the window over iRacing and beeps."""
        job.answered.clear()
        job.prompt = {"id": f"{time.time():.3f}", "title": title, "text": text}
        self._log(job, title)
        self._front(True)
        try:
            while not job.answered.wait(0.2):
                if job.stop.is_set():
                    from .bench import Aborted
                    raise Aborted()
        finally:
            job.prompt = None
            self._front(False)

    def _front(self, on: bool) -> None:
        w = self._window
        try:
            if on and sys.platform == "win32":
                import winsound
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            if w is not None:
                if on:
                    w.restore()
                w.on_top = on
        except Exception:
            pass

    def answer(self, prompt_id=None) -> dict:
        j = self._job
        if j and j.prompt and (prompt_id is None or prompt_id == j.prompt["id"]):
            j.answered.set()
            return {"ok": True}
        return {"ok": False}

    def stop(self) -> dict:
        if self._job and self._job.state == "running":
            self._job.stop.set()
            self._job.answered.set()
            return {"ok": True}
        return {"ok": False}

    # ------------------------------------------------------------------ files
    def _bundle(self, bundle_id: str) -> Path | None:
        if not isinstance(bundle_id, str) or not _ID_RE.match(bundle_id):
            return None
        d = (self._bench_root / bundle_id).resolve()
        return d if d.parent == self._bench_root.resolve() and d.is_dir() else None

    def open_report(self, bundle_id) -> dict:
        d = self._bundle(bundle_id)
        if not d or not (d / "report.html").is_file():
            return {"ok": False, "error": "That report no longer exists."}
        self._opener(d / "report.html")
        return {"ok": True}

    def open_results_folder(self) -> dict:
        self._bench_root.mkdir(parents=True, exist_ok=True)
        self._opener(self._bench_root)
        return {"ok": True}

    def relaunch_admin(self) -> dict:
        try:
            from . import winsys
            if winsys.relaunch_as_admin():
                if self._window is not None:
                    threading.Timer(0.3, self._window.destroy).start()
                return {"ok": True}
            return {"ok": False, "error": "Windows didn't start the app as administrator."}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------ updates
    def _check_update(self) -> None:
        up = self._updater
        if up is None:
            from . import selfupdate as up
        up.cleanup()
        r = up.check()
        r["state"] = "ready" if r.get("available") else "current"
        self._update = r

    def start_update_check(self) -> None:
        threading.Thread(target=self._check_update, daemon=True).start()

    def update_status(self) -> dict:
        u = self._update
        return {"available": bool(u.get("available")), "state": u.get("state"), "error": u.get("error"),
                "progress": u.get("progress")}

    def update_install(self) -> dict:
        """Downloads the newest build, swaps it in, starts it and closes this window."""
        u = self._update
        if not u.get("available") or u.get("state") in ("installing", "done"):
            return {"ok": False, "error": "No update to install."}
        if self._job and self._job.state == "running":
            return {"ok": False, "error": "Wait for the test to finish first."}
        up = self._updater
        if up is None:
            from . import selfupdate as up
        u["state"] = "installing"
        try:
            up.install(u["sha"], progress=lambda f: u.__setitem__("progress", f))
        except Exception as e:
            u["state"], u["error"] = "ready", str(e)
            return {"ok": False, "error": f"Update failed, nothing was changed: {e}"}
        u["state"] = "done"
        if self._window is not None:
            threading.Timer(0.5, self._window.destroy).start()
        return {"ok": True}

    def open_classic(self) -> dict:
        """Opens the older window, which still has the tools not yet moved here (profiles, diagnostics, VR)."""
        try:
            import subprocess  # nosec B404
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, "--classic"]
            else:
                cmd = [sys.executable, str(Path(__file__).resolve().parents[1] / "app.py"), "--classic"]
            from .winsys import child_env
            subprocess.Popen(cmd, env=child_env())  # noqa: S603  # nosec B603
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------ backups
    def backups(self) -> dict:
        from . import apply as applier
        ini = self._ini()
        if not ini or not ini.is_dir():
            return {"available": False, "items": [], "text": "iRacing's settings folder wasn't found."}
        try:
            applier.ensure_original(ini)
        except Exception:
            pass
        items = []
        orig = applier.original_backup(ini)
        for d in applier.list_backups(ini):
            label = d.name.split("_", 2)[-1] if d.name.count("_") >= 2 else ""
            why = {"original": "Your settings before the app changed anything", "before_restore":
                   "Saved automatically before a restore", "manual": "Saved by you"}.get(label, label.replace("_", " ") or "Backup")
            items.append({"id": d.name, "when": _stamp_when(d.name), "why": why, "original": d == orig,
                          "files": sorted(p.name for p in d.iterdir() if p.is_file())})
        return {"available": True, "items": items, "folder": str(ini), "locked": bool(getattr(applier, "WRITES_LOCKED", False))}

    def backup_now(self) -> dict:
        from . import apply as applier
        ini = self._ini()
        if not ini:
            return {"ok": False, "error": "iRacing's settings folder wasn't found."}
        try:
            d = applier.backup(ini, "manual")
            return {"ok": True, "id": d.name}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def restore(self, backup_id) -> dict:
        from . import apply as applier
        ini = self._ini()
        if not ini:
            return {"ok": False, "error": "iRacing's settings folder wasn't found."}
        try:
            if not any(d.name == backup_id for d in applier.list_backups(ini)):
                return {"ok": False, "error": "That backup no longer exists."}
            applier.backup(ini, "before_restore")
            log = applier.restore(ini, backup_id)
            try:
                from . import drift
                drift.clear(self._out_dir)
            except Exception:
                pass
            return {"ok": True, "log": log}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def _stamp_when(name: str) -> str:
    m = re.match(r"^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})", name)
    if not m:
        return name
    from datetime import datetime
    try:
        d = datetime(*map(int, m.groups()))
    except ValueError:
        return name
    return f"{d.day} {d:%b %Y}, {d:%I:%M %p}".replace(" 0", " ")


def main(out_dir: Path | None = None) -> bool:
    """Opens the window. Returns False when no web view is available, so the caller can fall back to Tk."""
    try:
        import webview
    except Exception:
        return False
    api = Api(out_dir)
    index = WEBUI / "index.html"
    window = webview.create_window(APP, url=str(index), js_api=api, width=1320, height=900, min_size=(960, 680),
                                   background_color="#111317", text_select=True)
    api._window = window
    api.start_update_check()
    try:
        webview.start(gui="edgechromium" if sys.platform == "win32" else None, debug=bool(os.environ.get("IRO_DEBUG")))
    except Exception:
        traceback.print_exc()
        return False
    return True
