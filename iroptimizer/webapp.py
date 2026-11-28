"""The main window: an HTML interface (iroptimizer/webui) in a native window (pywebview on Edge WebView2).

The page talks to the Api class below through pywebview's js_api bridge. Long tests run on a worker thread; the page
polls Api.poll() for progress, log lines and prompts. The Tk interface (gui.py) is kept as a fallback for PCs
without WebView2 and for the tray mode.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import __version__, abtest, applog, dashboard

APP = "iRacing Optimiser"
WEBUI = Path(__file__).parent / "webui"
_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
TESTS = ("repeat", "background", "guided", "powerplan", "restart", "graphics", "floorprobe", "costrank", "pick")
REOPEN_TIMEOUT_S = 900.0
AFTER_LOAD_S = 10.0  # the sim is still streaming the track when the SDK first shows the replay


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
    ended: float | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    answered: threading.Event = field(default_factory=threading.Event)
    cleanup: Callable[[], None] | None = None
    floor: float = 120.0
    rank_names: dict = field(default_factory=dict)
    pick: dict = field(default_factory=dict)


class Api:
    """Everything the page can ask for. Every method returns plain JSON-able data and never raises."""

    def __init__(self, out_dir: Path | None = None, *, run_bench: Callable | None = None,
                 driver_factory: Callable | None = None, capture_factory: Callable | None = None,
                 presentmon_exe: Callable[[], Path | None] | None = None, elevation: Callable[[], str | None] | None = None,
                 sampler_factory: Callable | None = None, pauser_factory: Callable | None = None,
                 ini_dir: Callable[[], Path] | None = None, scan_seconds: float = 5.0, opener: Callable = _open,
                 bench_overrides: dict | None = None, updater=None, settings_snapshot: Callable | None = None,
                 sim_ctl=None, sim_running: Callable[[], bool] | None = None,
                 simhub_capture: Callable | None = None, audit_parts: dict | None = None,
                 powercfg: Callable | None = None, reopener=None):
        self._out_dir = Path(out_dir) if out_dir else default_out_dir()
        self._bench_root = self._out_dir / "bench"
        applog.setup(self._out_dir)
        applog.get().info("app start %s, results in %s", __version__, self._out_dir)
        self._removed_empty = remove_empty_runs(self._bench_root)
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
        self._sim_running = sim_running
        self._scan_seconds = scan_seconds
        self._opener = opener
        self._bench_overrides = bench_overrides or {}
        self._machine = None
        self._resumed = 0
        self._update: dict = {"available": False, "state": "checking"}
        self._updater = updater
        self._snapshot_fn = settings_snapshot
        self._simhub_fn = simhub_capture
        self._audit_parts = audit_parts
        self._powercfg = powercfg
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
        from . import powerplan
        kw = {"run": powercfg} if powercfg else {}
        self._plan_restored = powerplan.restore_leftover(self._out_dir, **kw)  # a crashed test's plan switch
        from . import iniswitch
        self._ini_restored = iniswitch.restore_leftover(self._out_dir, **({"running": sim_running} if sim_running
                                                                          else {}))
        self._sim_ctl = sim_ctl
        if reopener is None and sim_ctl is None:
            from . import clicklearn
            reopener = clicklearn.default(self._out_dir)
        self._reopener = reopener

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
                if (d / "bench.json").is_file() and (not sj.is_file() or b'"mismatch_v2"' not in sj.read_bytes()):
                    bench.analyze_bundle(d)
            except Exception:
                continue

    def _changes(self) -> list[dict]:
        try:
            from . import changelog
            return changelog.load(self._out_dir)
        except Exception:
            return []

    def _snapshot(self) -> dict | None:
        """iRacing's graphics settings as they are on disk now."""
        try:
            if self._snapshot_fn:
                return self._snapshot_fn()
            from . import config_reader
            return config_reader.snapshot(config_reader.load())
        except Exception:
            return None

    def _settings_state(self) -> tuple[str | None, str | None]:
        """(fingerprint of the current graphics settings, replay-vs-race problem text or None)."""
        from . import config_reader
        snap = self._snapshot()
        if not snap:
            return None, None
        try:
            diffs = config_reader.replay_mismatch(snap, self._last_low1())
            return config_reader.settings_key(snap), config_reader.mismatch_text(diffs) or None
        except Exception:
            return None, None

    def audit(self) -> dict:
        """Reads every Windows, NVIDIA and iRacing setting worth knowing. Read only."""
        try:
            from . import audit
            kw = {"snapshot": self._snapshot, "low1": self._last_low1()}
            if self._audit_parts:
                kw.update(self._audit_parts)
            r = audit.run(**kw)
            self._record_settings(r, "you opened the audit")
            return {"ok": True, **r}
        except Exception as e:
            return {"ok": False, "error": f"Couldn't audit the PC: {e}"}

    def _record_settings(self, audit_result: dict | None = None, reason: str = "") -> None:
        """Logs settings that changed since the last snapshot (see changelog)."""
        try:
            from . import audit, changelog
            if audit_result is None:
                kw = {"snapshot": self._snapshot, **(self._audit_parts or {})}
                audit_result = audit.run(**kw)
            changelog.record(self._out_dir, changelog.snapshot(audit_result), reason)
        except Exception:
            pass

    def add_note(self, text) -> dict:
        try:
            from . import changelog
            changelog.add_note(self._out_dir, text)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _last_low1(self) -> float | None:
        """The latest measured 1% low, to tell whether iRacing's LOD frame-rate target could ever kick in."""
        try:
            hist = dashboard.load_history(self._bench_root)
            if not hist:
                return None
            b = hist[-1]
            return b["per_variant"][b["variants"][0]].get("low1_fps")
        except Exception:
            return None

    def _simhub_capture(self) -> bool | None:
        try:
            if self._simhub_fn:
                return self._simhub_fn()
            from . import capture
            return capture.simhub_capture_on()
        except Exception:
            return None

    def view(self) -> dict:
        try:
            self._reanalyse_old()
            focus = int(self._settings().get("focus", 1))
            key, problem = self._settings_state()
            v = dashboard.view(self._bench_root, focus, self._elevation_problem(), self._machine_line(),
                               current_key=key, settings_problem=problem,
                               capture={"simhub": self._simhub_capture()}, changes=self._changes())
            from . import version_label
            v["version"] = version_label()
            v["backups"] = self.backups()
            v["focus_labels"] = list(abtest.FOCUS_LABELS)
            v["picker"] = self._picker_state()
            v["job"] = self._job_state(0)
            v["update"] = self.update_status()
            v["notice"] = (f"Resumed {self._resumed} app(s) that a previous test left paused."
                           if self._resumed else "")
            self._resumed = 0
            return v
        except Exception as e:
            applog.get().exception("view failed")
            return {"error": f"Couldn't read the results: {e}", "trace": traceback.format_exc(limit=3)}

    # ------------------------------------------------------------------ graphics picker
    def _read_json(self, name: str) -> dict | None:
        try:
            d = json.loads((self._out_dir / name).read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else None
        except (OSError, ValueError):
            return None

    def _picker_state(self) -> dict:
        """Where the user is in restart check, floor check, cost ranking, pick, and the latest pick."""
        from . import floors
        pref = self._settings().get("min_fps", floors.DEFAULT_FLOOR)
        fc, cr, pk = (self._read_json(n) for n in ("floor_check.json", "cost_rank.json", "pick.json"))
        steps = [
            {"test": "floorprobe", "label": "Graphics floor check", "done": bool(fc),
             "result": (fc or {}).get("text", "")},
            {"test": "costrank", "label": "What costs the most frames", "done": bool(cr),
             "result": " ".join(r.get("name", "") + (" (real gain)" if r.get("real") else "")
                                for r in (cr or {}).get("rows", [])[:3])},
            {"test": "pick", "label": "Pick my graphics", "done": bool(pk), "result": (pk or {}).get("text", "")},
        ]
        pick = None
        if pk and pk.get("pick"):
            row = next((r for r in pk.get("rows") or [] if r.get("name") == pk["pick"]), {})
            pick = {"name": pk["pick"], "text": pk.get("text", ""), "changes": row.get("changes") or {},
                    "applied": bool(pk.get("applied")), "t": pk.get("t", "")}
        return {"min_fps": pref, "steps": steps, "pick": pick,
                "next": next((s_["test"] for s_ in steps if not s_["done"]), None)}

    def set_min_fps(self, value) -> dict:
        try:
            from . import i18n
            v = max(30, min(240, int(value)))
            i18n.save_setting("min_fps", v)
            return {"ok": True, "min_fps": v}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def apply_pick(self) -> dict:
        """Writes the latest pick into iRacing's race and replay settings, after a backup. iRacing must be closed:
        it writes its own copy back when it exits."""
        try:
            from . import apply as applier, config_reader, iniswitch
            pk = self._read_json("pick.json")
            if not pk or not pk.get("pick"):
                return {"ok": False, "error": "There's no pick yet. Run Pick my graphics first."}
            if self._job and self._job.state == "running":
                return {"ok": False, "error": "Wait for the test to finish first."}
            row = next((r for r in pk.get("rows") or [] if r.get("name") == pk["pick"]), {})
            changes = row.get("changes") or {}
            running = self._sim_running or applier.iracing_running
            if running():
                return {"ok": False, "error": "Close iRacing first: it writes its own settings back when it exits, "
                                              "which would undo the change."}
            if not changes:
                pk["applied"] = True
                (self._out_dir / "pick.json").write_text(json.dumps(pk, default=str), encoding="utf-8")
                return {"ok": True, "text": "The pick is your current settings, so nothing needed changing."}
            cfgi = config_reader.load(str(self._ini_dir()) if self._ini_dir else None)
            if not cfgi.renderer:
                return {"ok": False, "error": "iRacing's graphics settings file wasn't found."}
            from .iniio import IniFile
            f = IniFile(cfgi.renderer.path)
            lines = iniswitch.expand(f, changes)
            bdir = applier.backup(cfgi.directory, "before_pick")
            for key, val in lines.items():
                sec, _, k = key.rpartition("/")
                f.set(k, val, sec)
            f.save()
            back = IniFile(cfgi.renderer.path)
            bad = [k for k, v in lines.items() if str(back.get(k.rpartition("/")[2], k.rpartition("/")[0])) != str(v)]
            if bad:
                return {"ok": False, "error": f"Some values didn't read back ({', '.join(bad)}). Restore the backup "
                                              f"'{bdir.name}' on the Backups tab."}
            pk["applied"] = True
            pk["applied_t"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            (self._out_dir / "pick.json").write_text(json.dumps(pk, default=str), encoding="utf-8")
            self._record_settings(reason="the pick was applied")
            applog.get().info("pick applied: %s %s (backup %s)", pk["pick"], changes, bdir.name)
            return {"ok": True, "text": f"Applied: {pk['pick']}. Your old settings are saved as backup "
                                        f"'{bdir.name}' on the Backups tab."}
        except Exception as e:
            applog.get().exception("apply pick failed")
            return {"ok": False, "error": f"Couldn't apply the pick: {e}"}

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

    LOG_TAIL = 80

    def _job_state(self, since: int = 0) -> dict | None:
        """The whole state of the current (or last) test. Every answer is complete and numbered (seq), so the
        window can always redraw from it and drop an answer older than one it already has."""
        j = self._job
        if not j:
            return None
        with self._lock:
            self._seq = getattr(self, "_seq", 0) + 1
            seq = self._seq
            tail = j.logs[-self.LOG_TAIL:]
            n = len(j.logs)
            run = next((x["text"] for x in reversed(j.logs) if x["text"].lower().startswith(("run ", "warm-up"))
                        or " run " in x["text"].lower()), "")
        end = j.ended or time.time()
        el = end - j.started
        return {"id": f"{j.started:.3f}", "seq": seq, "test": j.test, "title": j.title, "state": j.state,
                "elapsed_s": el, "eta_s": j.eta_s,
                "progress": min(0.99, el / j.eta_s) if j.state == "running" and j.eta_s else 1.0,
                "logs": tail, "next": n, "run": run, "prompt": j.prompt, "result": j.result}

    def log_js(self, text) -> dict:
        """Script errors from the window, so a UI problem leaves a trace in app.log."""
        applog.get().warning("window: %s", str(text)[:2000])
        return {"ok": True}

    def poll(self, since=0) -> dict:
        try:
            st = self._job_state()
            if st:
                applog.get().debug("poll seq=%s state=%s run=%r logs=%s", st["seq"], st["state"], st["run"],
                                   st["next"])
            return {"job": st}
        except Exception as e:
            applog.get().exception("poll failed")
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
        _key, mismatch = self._settings_state()
        if mismatch and not opts.get("ignore_settings"):
            return {"ok": False, "error": mismatch, "fix": "settings_mismatch"}
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
        elif test == "powerplan":
            from . import powerplan
            kw = {"run": self._powercfg} if self._powercfg else {}
            try:
                found, active = powerplan.plans(**kw)
            except Exception as e:
                return {"ok": False, "error": f"Couldn't read the power plans: {e}"}
            other = powerplan.pick_other(found, active) if active else None
            if not other:
                return {"ok": False, "error": "There's no other power plan to compare with."}
            sw = powerplan.Switcher(self._out_dir, **kw)
            a, b = f"{found[active]} plan", f"{found[other]} plan"
            variants = [bench.Variant(a),
                        bench.Variant(b, description=f"The app switched Windows to {found[other]} and back.",
                                      apply=lambda: sw.switch(other, active),
                                      revert=lambda: self._log(job, "Power plan put back: " + (sw.restore() and
                                                                                            found[active] or "?")))]
            job.title = f"{b} vs {a}"
        elif test in ("restart", "graphics", "floorprobe"):
            sim = self._sim_ctl
            if sim is None:
                from . import simctl as sim
            reopen = lambda: self._reopen_sim(job, driver, sim)  # noqa: E731
            if test == "restart":
                variants = [bench.Variant("Before restart", settle_runs=1),
                            bench.Variant("After restart", settle_runs=1, apply=lambda: (
                                self._close_sim(job, driver, sim), reopen()),
                                description="The app closed iRacing and you reopened the same replay.")]
                variants[0].apply = variants[1].apply
                job.title = "Sim restart check: same settings, before vs after a restart"
            else:
                from . import apply as applier, config_reader, floors, iniswitch, safety
                changes = opts.get("changes") or {}
                if test == "floorprobe":
                    try:
                        cfg0 = config_reader.load(str(self._ini_dir()) if self._ini_dir else None)
                        have = cfg0.renderer.flat() if cfg0.renderer else {}
                    except Exception:
                        have = {}
                    changes = {k: v for k, v in floors.LOWEST.items() if k.lower() in have}
                    opts = {**opts, "label": "Lowest graphics"}
                    try:
                        floor = float(opts.get("floor") or self._settings().get("min_fps") or floors.DEFAULT_FLOOR)
                    except (TypeError, ValueError):
                        floor = floors.DEFAULT_FLOOR
                    job.floor = max(30.0, min(500.0, floor))
                if not isinstance(changes, dict) or not changes or len(changes) > 30:
                    return {"ok": False, "error": "No graphics settings chosen to test."}
                try:
                    changes = {str(k): safety.safe_value(v) for k, v in changes.items()}
                    cfgi = config_reader.load(str(self._ini_dir()) if self._ini_dir else None)
                except Exception as e:
                    return {"ok": False, "error": f"Can't use those settings: {e}"}
                if not cfgi.renderer:
                    return {"ok": False, "error": "iRacing's graphics settings file wasn't found."}
                for key in changes:
                    sec, _, k = key.rpartition("/")
                    if cfgi.renderer.get(k, sec or None) is None:
                        return {"ok": False, "error": f"{k} isn't in {cfgi.renderer.path.name}."}
                sw = iniswitch.IniSwitch(self._out_dir, cfgi.renderer.path, **({"running": self._sim_running}
                                                                              if self._sim_running else {}),
                                         backup=lambda: applier.backup(cfgi.directory, "before_test"))
                b_name = (str(opts.get("label") or "").strip() or "Changed settings")[:60]
                desc = ", ".join(f"{k.rpartition('/')[2]}={v}" for k, v in changes.items())

                def to_a():
                    self._close_sim(job, driver, sim)
                    sw.restore()
                    reopen()

                def to_b():
                    self._close_sim(job, driver, sim)
                    sw.set(changes)
                    self._log(job, "Changed: " + desc)
                    reopen()
                variants = [bench.Variant("Your settings", settle_runs=1, apply=to_a),
                            bench.Variant(b_name, settle_runs=1, apply=to_b, description=desc)]
                job.cleanup = lambda: self._finish_ini(job, driver, sim, sw)
                job.title = (f"Floor check: can graphics reach {job.floor:.0f} FPS?" if test == "floorprobe"
                             else f"{b_name} vs your settings")
        elif test == "pick":
            import dataclasses
            from . import apply as applier, config_reader, costrank, floors, iniswitch, picker
            sim = self._sim_ctl
            if sim is None:
                from . import simctl as sim
            try:
                fc = json.loads((self._out_dir / "floor_check.json").read_text(encoding="utf-8"))
                cr = json.loads((self._out_dir / "cost_rank.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {"ok": False, "error": "Run the Graphics floor check and What costs the most frames first: "
                                              "the picker works from their results."}
            try:
                cfgi = config_reader.load(str(self._ini_dir()) if self._ini_dir else None)
            except Exception as e:
                return {"ok": False, "error": f"Couldn't read iRacing's settings: {e}"}
            if not cfgi.renderer:
                return {"ok": False, "error": "iRacing's graphics settings file wasn't found."}
            if cfgi.renderer.get("DesiredFPSLimit") is None or cfgi.renderer.get("LimitFrameRate") is None:
                return {"ok": False, "error": "iRacing's frame limiter isn't in its settings file, so the cap "
                                              "options can't be tested."}
            pref = float(fc.get("floor") or floors.DEFAULT_FLOOR)
            ch = picker.choose(fc, cr.get("rows") or [], pref)
            lowered = ch["changes"]
            low_only = bool(lowered) and all(k in picker.LOW_PRIORITY for k in lowered)
            q_pick = 10 - len(ch["lowered"])
            base = "Picked settings" if lowered else "Your settings"
            opts_list = [{"name": "Your settings", "changes": {}, "quality": 10, "capped": False,
                          "low_priority_only": False}]
            if lowered:
                opts_list.append({"name": "Picked settings", "changes": dict(lowered), "quality": q_pick,
                                  "capped": False, "low_priority_only": low_only})
            opts_list += [
                {"name": f"{base}, {picker.CAP_HIGH} cap", "changes": {**lowered, **picker.cap_changes(picker.CAP_HIGH)},
                 "quality": q_pick if lowered else 10, "capped": True, "cap": picker.CAP_HIGH,
                 "low_priority_only": low_only},
                {"name": f"Your settings, {picker.CAP_LOW} cap", "changes": picker.cap_changes(picker.CAP_LOW),
                 "quality": 10, "capped": True, "cap": picker.CAP_LOW, "low_priority_only": False}]
            cfg = dataclasses.replace(cfg, runs=costrank.PER_LOAD * costrank.CYCLES, warmup_runs=0,
                                      per_load=costrank.PER_LOAD)
            sw = iniswitch.IniSwitch(self._out_dir, cfgi.renderer.path, **({"running": self._sim_running}
                                                                          if self._sim_running else {}),
                                     backup=lambda: applier.backup(cfgi.directory, "before_test"))

            def to_opt(c: dict):
                def f():
                    self._close_sim(job, driver, sim)
                    sw.restore()
                    if c:
                        sw.set(c)
                        self._log(job, "Changed: " + ", ".join(f"{k}={v}" for k, v in c.items()))
                    self._reopen_sim(job, driver, sim)
                return f
            variants = [bench.Variant(o["name"], settle_runs=1, apply=to_opt(o["changes"]),
                                      description=", ".join(f"{k}={v}" for k, v in o["changes"].items()))
                        for o in opts_list]
            job.pick = {"options": opts_list, "choice": ch, "preferred": pref}
            job.cleanup = lambda: self._finish_ini(job, driver, sim, sw)
            job.title = "Pick: smooth first, then looks, then FPS"
        elif test == "costrank":
            import dataclasses
            from . import apply as applier, config_reader, costrank, iniswitch
            sim = self._sim_ctl
            if sim is None:
                from . import simctl as sim
            try:
                cfgi = config_reader.load(str(self._ini_dir()) if self._ini_dir else None)
            except Exception as e:
                return {"ok": False, "error": f"Couldn't read iRacing's settings: {e}"}
            if not cfgi.renderer:
                return {"ok": False, "error": "iRacing's graphics settings file wasn't found."}
            flat = {k.lower(): v for k, v in (cfgi.renderer.as_dict().get("Graphics Options") or
                                              cfgi.renderer.flat()).items()}
            cands = costrank.candidates(flat)
            if not cands:
                return {"ok": False, "error": "Every setting the ranking tries is already at its lowest."}
            cfg = dataclasses.replace(cfg, runs=costrank.PER_LOAD * costrank.CYCLES, warmup_runs=0,
                                      per_load=costrank.PER_LOAD)
            sw = iniswitch.IniSwitch(self._out_dir, cfgi.renderer.path, **({"running": self._sim_running}
                                                                          if self._sim_running else {}),
                                     backup=lambda: applier.backup(cfgi.directory, "before_test"))

            def to(ch: dict | None):
                def f():
                    self._close_sim(job, driver, sim)
                    sw.restore()
                    if ch:
                        sw.set(ch)
                        self._log(job, "Changed: " + ", ".join(f"{k}={v}" for k, v in ch.items()))
                    self._reopen_sim(job, driver, sim)
                return f
            variants = [bench.Variant("Your settings", settle_runs=1, apply=to(None))] + [
                bench.Variant(name, settle_runs=1, apply=to(ch), description=", ".join(f"{k}={v}" for k, v in ch.items()))
                for name, ch in cands]
            job.rank_names = dict(cands)
            job.cleanup = lambda: self._finish_ini(job, driver, sim, sw)
            job.title = f"What costs the most frames: {len(cands)} settings, one step lower each"
        elif test == "guided":
            a = (str(opts.get("a") or "").strip() or "A")[:60]
            b = (str(opts.get("b") or "").strip() or "B")[:60]
            if a == b:
                return {"ok": False, "error": "A and B need different names."}
            where = {"Windows": "Windows settings", "NVIDIA": "the NVIDIA app",
                     "iRacing": "iRacing's graphics options"}.get(str(opts.get("where") or ""),
                                                                 "iRacing, Windows or the NVIDIA app")
            variants = [bench.Variant(a, manual=True, apply=lambda: self._ask(
                            job, f"Set up A: {a}", f"The replay is paused. Change it in {where}, "
                            "then press Done. Don't move the replay or the camera.")),
                        bench.Variant(b, manual=True, apply=lambda: self._ask(
                            job, f"Set up B: {b}", "The replay is paused. Change it, then press Done."))]
            job.title = f"{b} vs {a}"
        n = cfg.runs * (len(variants) if variants else 1) + cfg.warmup_runs
        job.eta_s = n * (cfg.seconds + cfg.settle_s + 2)
        if test in ("restart", "graphics", "floorprobe", "costrank", "pick"):
            plan = bench.schedule(variants, cfg.runs, cfg.warmup_runs, cfg.per_load)
            switches = sum(1 for k, (vi, _w) in enumerate(plan) if k == 0 or plan[k - 1][0] != vi)
            job.eta_s += switches * (90 + cfg.seconds + cfg.settle_s)  # restart, reload, one settling run
        self._job = job
        run_bench = self._run_bench or bench.run_bench
        driver = self._driver_factory() if self._driver_factory else bench.IRSDKDriver()
        if self._capture_factory:
            capture = self._capture_factory
        else:
            from . import presentmon
            capture = lambda p: presentmon.Capture(exe, p)  # noqa: E731

        log = applog.get()
        log.info("test start: %s (%s), %d runs x %.0f s", test, job.title, cfg.runs, cfg.seconds)

        def run():
            final = "error"  # the window shows the end only after the settings are put back
            try:
                self._record_settings(reason="a test started")
                extra = {"start_points": self._out_dir / "start_points.json"} if run_bench is bench.run_bench else {}
                bdir = run_bench(cfg, self._out_dir, driver, capture, log=lambda s: self._log(job, s), stop=job.stop,
                                 variants=variants, **extra)
                s = dashboard.load_bundle(bdir) or {}
                lines = [c["verdict_text"] for c in s.get("comparisons") or []]
                lines.append((s.get("repeatability") or {}).get("verdict_text", ""))
                if test == "pick":
                    from . import floors, picker
                    try:
                        full = json.loads((Path(bdir) / "summary.json").read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        full = {}
                    pv: dict = {}
                    for r in full.get("runs") or []:
                        w, m = r.get("window") or {}, r.get("metrics") or {}
                        if not w.get("warmup") and w.get("valid", True) and m:
                            pv.setdefault(w.get("variant"), []).append(m)
                    res = picker.judge(job.pick["options"], pv, job.pick["preferred"])
                    lines = [res["text"], job.pick["choice"]["reason"]] + [picker.row_line(r) for r in res["rows"]]
                    try:
                        (self._out_dir / "pick.json").write_text(json.dumps(
                            {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **res, "choice": job.pick["choice"]},
                            default=str), encoding="utf-8")
                    except OSError:
                        pass
                if test == "costrank":
                    from . import costrank
                    rows = costrank.rank(s.get("comparisons") or [], job.rank_names)
                    lines = costrank.lines(rows) + lines[-1:]
                    try:
                        (self._out_dir / "cost_rank.json").write_text(
                            json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "rows": rows}), encoding="utf-8")
                    except OSError:
                        pass
                if test == "floorprobe":
                    from . import floors
                    try:
                        full = json.loads((Path(bdir) / "summary.json").read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        full = {}
                    lows = floors.lows_by_variant(full)
                    fv = floors.verdict(lows.get("Your settings") or [], lows.get("Lowest graphics") or [],
                                        job.floor)
                    lines.insert(0, fv["text"])
                    self._save_floor(fv)
                job.result = {"id": Path(bdir).name, "lines": [x for x in lines if x],
                              "verdicts": [c.get("verdict") for c in s.get("comparisons") or []]}
                final = "done"
                self._log(job, "Finished.")
                log.info("test done: %s -> %s", job.result["id"], " | ".join(job.result["lines"]))
            except bench.Aborted:
                final = "stopped"
                self._log(job, "Stopped. Nothing was changed.")
                log.info("test stopped")
            except Exception as e:
                final = "error"
                job.result = {"error": str(e)}
                self._log(job, f"Error: {e}")
                log.exception("test failed")
            finally:
                job.prompt = None
                if job.cleanup:
                    try:
                        job.cleanup()
                    except Exception as e:
                        self._log(job, f"Couldn't put your iRacing settings back: {e}. The app will try again "
                                       "at its next start.")
                        log.exception("ini restore failed")
                job.ended = time.time()
                job.state = final
        threading.Thread(target=run, daemon=True).start()
        return {"ok": True, "eta_min": round(job.eta_s / 60, 1), "title": job.title}

    def _save_floor(self, fv: dict) -> None:
        """Keeps the latest floor check, for the picker to start from."""
        try:
            f = self._out_dir / "floor_check.json"
            f.write_text(json.dumps(fv | {"t": time.strftime("%Y-%m-%dT%H:%M:%S")}), encoding="utf-8")
        except OSError:
            pass

    def _close_sim(self, job: Job, driver, sim) -> None:
        try:
            driver.disconnect()
        except Exception:
            pass
        sim.close_sim(log=lambda s: self._log(job, s))

    def _reopen_sim(self, job: Job, driver, sim) -> None:
        """Reopens the replay: by itself with the learned clicks when it can, otherwise asks the user (and learns
        their clicks for next time). Continues by itself once the replay has loaded."""
        ro = self._reopener
        auto = False
        if ro is not None and ro.has_recipe():
            self._log(job, "Reopening the replay in iRacing by itself…")
            try:
                auto, why = ro.replay(job.stop.wait, log=lambda s: self._log(job, s))
            except Exception as e:
                auto, why = False, str(e)
            if job.stop.is_set():
                from .bench import Aborted
                raise Aborted()
            if not auto:
                self._log(job, f"Couldn't reopen it by itself ({why}), so you're asked instead; the app learns "
                               "your clicks again.")
        if not auto:
            job.prompt = {"id": f"{time.time():.3f}", "title": "Reopen the same replay in iRacing",
                          "text": "iRacing was closed to change a setting. In iRacing, open the same replay file "
                                  "again (Watch replay). Don't scrub: the test jumps back to its spot by itself. "
                                  "The app watches your clicks this time and reopens it by itself from then on.",
                          "ok": "OK", "auto": True}
            if ro is not None:
                try:
                    ro.learn_start()
                except Exception as e:
                    self._log(job, f"Couldn't watch the clicks: {e}")
            self._log(job, "Waiting for the replay to be reopened…")
            self._front(True)
        try:
            sim.wait_for_replay(driver, job.stop.wait, timeout_s=REOPEN_TIMEOUT_S)
        finally:
            if not auto:
                job.prompt = None
                self._front(False)
                if ro is not None:
                    ro.learn_stop()
        if not auto and ro is not None:
            try:
                n = ro.learn_finish()
                self._log(job, f"Learned the reopen ({n} click{'s' if n != 1 else ''}); next time it's automatic."
                          if n else "Couldn't learn the reopen from those clicks; you'll be asked again.")
            except Exception as e:
                self._log(job, f"Couldn't save the reopen clicks: {e}")
        self._log(job, "Replay is open again, letting it settle.")
        if job.stop.wait(AFTER_LOAD_S):
            from .bench import Aborted
            raise Aborted()

    def _finish_ini(self, job: Job, driver, sim, sw) -> None:
        """After a graphics test: iRacing must be closed before its settings file is put back, or it would write
        the test values back over the file when it exits."""
        if not sw.state.is_file():
            return
        self._close_sim(job, driver, sim)
        sw.restore()
        self._log(job, "Your iRacing settings were put back. iRacing was closed to do that; open it again as usual.")

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
            applog.get().exception("update failed")
            msg = str(e) if "is installed" in str(e) else f"Update failed, nothing was changed: {e}"
            return {"ok": False, "error": msg}
        u["state"] = "done"
        applog.get().info("update installed, restarting into %s", u.get("sha", "")[:12])
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


def remove_empty_runs(bench_root: Path) -> int:
    """Deletes result folders a test left behind with nothing in them (it stopped before any frames were
    saved): no bench.json and only empty files. Anything with data is kept."""
    n = 0
    if not Path(bench_root).is_dir():
        return 0
    import shutil
    for d in Path(bench_root).iterdir():
        try:
            if not d.is_dir() or (d / "bench.json").exists():
                continue
            files = [f for f in d.rglob("*") if f.is_file()]
            if all(f.stat().st_size == 0 for f in files) and time.time() - d.stat().st_mtime > 600:
                shutil.rmtree(d)
                n += 1
                applog.get().info("removed empty result folder %s", d.name)
        except OSError:
            continue
    return n


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
    threading.Thread(target=api._record_settings, kwargs={"reason": "the app started"}, daemon=True).start()
    try:
        from . import simwatch
        simwatch.start(api._out_dir)  # learns the sim's start-up command line for unattended restarts later
    except Exception:
        pass
    try:
        webview.start(gui="edgechromium" if sys.platform == "win32" else None, debug=bool(os.environ.get("IRO_DEBUG")))
    except Exception:
        traceback.print_exc()
        return False
    return True
