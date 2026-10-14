"""Repeatable desktop measurements in an isolated, temporary session."""

import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
import tkinter as tk
from types import SimpleNamespace

from .exporting import write_experiment_report
from .identification import experiment_columns, identify_step, read_experiment
from .session_store import read_laboratory_session, write_session
from .simulation import RICH_ABSORBENT
from .tank import DEFAULTS, simulate


def _working_set():
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ("peak", "working", "paged_peak", "paged",
                                               "nonpaged_peak", "nonpaged", "pagefile", "pagefile_peak")]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    query = ctypes.windll.psapi.GetProcessMemoryInfo
    query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    if not query(wintypes.HANDLE(-1), ctypes.byref(counters), counters.cb):
        return None
    return counters.working


def run_check(output_path, app_class, version):
    destination = Path(output_path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    report = dict(version=version, frozen=bool(getattr(sys, "frozen", False)),
                  python=sys.version, status="failed", metrics={})
    root = app = None

    def measure(name, callback, count=3):
        samples = []
        for _ in range(count):
            started = time.perf_counter()
            callback()
            samples.append(time.perf_counter() - started)
        report["metrics"][name] = dict(median_s=statistics.median(samples), samples_s=samples)

    try:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            started = time.perf_counter()
            root = tk.Tk()
            root.withdraw()
            app = app_class(root, folder / "scenarios.json")
            root.update_idletasks()
            report["gui_construction_s"] = time.perf_counter() - started
            destination.with_suffix(".ready").write_text("ready", encoding="utf-8")
            root.after_cancel(app._autosave_job)
            report["memory_gui_bytes"] = _working_set()

            def wait():
                deadline = time.monotonic() + 90
                while app._task is not None:
                    if time.monotonic() > deadline:
                        raise RuntimeError("Background calculation timed out")
                    root.update()
                    time.sleep(.002)

            app._apply_scenario_data(app.scenario_library.scenarios[1])
            measure("absorber_calculation_and_display", lambda: (app._calculate(), wait()))
            measure("tank_calculation", lambda: simulate(DEFAULTS))
            source = Path(__file__).resolve().parent.parent / "data" / "identification_step.csv"
            headers, rows = read_experiment(source)
            arrays = experiment_columns(headers, rows, headers, ("с", "как в файле", "как в файле"))
            measure("csv_import", lambda: read_experiment(source))
            measure("identification", lambda: identify_step(*arrays))
            page = app.identification_page
            app._show_page("identification")
            page.load_path(source)
            page.metadata["input_role"].set("η, доля")
            page.metadata["output_role"].set("Xна, доля")
            page.calculate()
            wait()
            app.absorber_form.chain = RICH_ABSORBENT
            app.absorber_form.controller_enabled.set(True)
            app.absorber_form.controller_type.set("PI")
            for variable, value in ((app.absorber_form.setpoint, "30.5"), (app.absorber_form.control_limit, "20"),
                                    (app.absorber_form.simulation_duration, "100"), (app.absorber_form.time_constant, "8"),
                                    (app.absorber_form.delay, "0")):
                variable.set(value)
            app.absorber_form.component_enabled.set(False)
            app.absorber_form.flow_enabled.set(False)
            sensitivity = page.absorber_sensitivity
            for key, value in dict(k_errors="-20;0;20", t_errors="-20;0;20", l_errors="0;1").items():
                sensitivity.fields[key].set(value)
            measure("error_grid_18_and_display", lambda: (sensitivity.start(), wait()), 1)
            if sensitivity.result is None or len(sensitivity.result["cases"]) != 18:
                raise RuntimeError(page.summary.get())
            measure("session_capture", app._capture_laboratory)
            laboratory = app._capture_laboratory()
            session = folder / "session.json"
            measure("session_write", lambda: write_session(session, [], 0, laboratory), 2)
            measure("session_read", lambda: read_laboratory_session(session), 2)
            report["session_bytes"] = session.stat().st_size
            report["memory_loaded_bytes"] = _working_set()
            app._save_autosave()
            measure("unchanged_autosave", app._save_autosave)
            for extension in ("html", "pdf"):
                measure(extension + "_export", lambda ext=extension: write_experiment_report(
                    folder / ("experiment." + ext), page.capture(), ext), 1)
            root.geometry("1366x768+0+0")
            root.deiconify()
            root.update()
            app._set_chart_mode("both")
            root.update_idletasks()
            measure("draw_both", lambda: (app.disturbance_canvas.draw(), app.response_canvas.draw()))
            event = SimpleNamespace(inaxes=app.response_axis, xdata=30.)
            measure("cursor_move", lambda: (app.time_cursor.move(event), root.update_idletasks()))
            measure("cursor_clear", lambda: (app.time_cursor.clear(), root.update_idletasks()))
            report["divider_drag"] = {}
            for name, pane in (("width", app.main_panes), ("height", app.chart_panes)):
                root.update()
                horizontal = name == "width"
                x, y = ((pane.sashpos(0), pane.winfo_height() // 2) if horizontal
                        else (pane.winfo_width() // 2, pane.sashpos(0)))
                pane.event_generate("<ButtonPress-1>", x=x, y=y)
                root.update()
                samples = []
                for offset in (10, 20, 30, 20, 10):
                    started = time.perf_counter()
                    pane.event_generate("<B1-Motion>", x=x+offset if horizontal else x,
                                        y=y if horizontal else y+offset, state=256)
                    root.update()
                    samples.append(time.perf_counter() - started)
                started = time.perf_counter()
                pane.event_generate("<ButtonRelease-1>", x=x+10 if horizontal else x,
                                    y=y if horizontal else y+10)
                root.update()
                report["divider_drag"][name] = dict(motion_samples_s=samples,
                    max_motion_s=max(samples), release_s=time.perf_counter()-started)
                if len(app.chart_panes.panes()) != 2:
                    raise RuntimeError("Both charts must remain visible after dragging")
            def switch_pages():
                for name in ("results", "tank", "identification"):
                    app._show_page(name)
                    root.update_idletasks()
            measure("page_switch_cycle", switch_pages)
            for _ in range(10):
                switch_pages()
            root.after(500, root.quit)
            root.mainloop()
            report["memory_after_page_cycles_bytes"] = _working_set()
            idle_cpu = time.process_time()
            root.after(1000, root.quit)
            root.mainloop()
            report["idle_cpu_s_per_second"] = time.process_time() - idle_cpu
            gaps, previous = [], [time.perf_counter()]
            deadline = time.perf_counter() + 1.5

            def heartbeat():
                now = time.perf_counter()
                gaps.append(now - previous[0])
                previous[0] = now
                if now < deadline:
                    root.after(10, heartbeat)

            app.assignment_panel.student_conclusion.set("Performance check")
            root.after(10, heartbeat)
            root.after(20, app._autosave_tick)
            cpu = time.process_time()
            root.after(1500, root.quit)
            root.mainloop()
            report["autosave_max_event_gap_s"] = max(gaps)
            report["autosave_cpu_s"] = time.process_time() - cpu
            app._finish_autosave(wait=True)
            _runs, _counter, saved = read_laboratory_session(app.autosave_path)
            if saved["conclusion"] != "Performance check":
                raise RuntimeError("Background autosave did not preserve the measured session")
            app._close_application()
            root = None
            report["status"] = "passed"
    except Exception as error:
        report["error"] = str(error)
    finally:
        if root is not None:
            if app is not None:
                app._finish_autosave(wait=True)
            root.destroy()
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if report["status"] == "passed" else 1
