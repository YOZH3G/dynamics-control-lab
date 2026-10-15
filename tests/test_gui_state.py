import copy
import tempfile
import tkinter as tk
import unittest
import time
from pathlib import Path
from unittest.mock import patch

import main
from app.scenario_store import ScenarioStore
from app.session_store import read_laboratory_session, restore_calculation, write_session
from app.exploration import controller_setting_map
from app.simulation import RICH_ABSORBENT
from ui.scenario_editor import ScenarioEditorDialog
from tkinter import ttk


class ProbePage(ttk.Frame):
    """Minimal object page: the protocol main.py relies on, and nothing else."""
    chart_label = "Пробный вход"

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app, self.summary, self.result, self.draws = app, tk.StringVar(), None, 0

    def draw(self):
        self.draws += 1

    def signature(self):
        return [self.result is not None]

    def latest_result(self):
        return self.result

    def capture(self, *, copy_results=True):
        return dict(model_id="test_heater", result=copy.deepcopy(self.result) if copy_results else self.result)

    def restore(self, state):
        self.result = None if state is None else copy.deepcopy(state["result"])


def validate_probe_state(state):
    if state is not None and (not isinstance(state, dict) or state.get("model_id") != "test_heater"):
        raise ValueError("Пробный объект: неверный снимок.")
    return state


class GuiStateTests(unittest.TestCase):
    def test_internal_dividers_preview_then_apply_final_sizes(self):
        from types import SimpleNamespace
        import io
        app, root = self.app, self.root
        root.geometry("1366x900+0+0"); root.deiconify(); root.update()
        app._set_chart_mode("both"); root.update()
        for pane in (app.main_panes, app.chart_panes):
            horizontal = pane is app.main_panes
            legend = app.response_axis.get_legend()
            x, y = ((pane.sashpos(0), pane.winfo_height() // 2) if horizontal
                    else (pane.winfo_width() // 2, pane.sashpos(0)))
            self.assertIn(pane.identify(x, y), (0, "0"))
            canvases = (app.disturbance_canvas, app.response_canvas)
            with patch.object(canvases[0], "draw", wraps=canvases[0].draw) as upper, \
                    patch.object(canvases[1], "draw", wraps=canvases[1].draw) as lower:
                pane.event_generate("<ButtonPress-1>", x=x, y=y); root.update()
                self.assertIs(app._layout_drag_pane, pane)
                original_position = pane.sashpos(0)
                for offset in (10, 20, 30):
                    pane.event_generate("<B1-Motion>", x=x+offset if horizontal else x,
                                        y=y if horizontal else y+offset, state=256)
                    root.update()
                self.assertEqual(pane.sashpos(0), original_position)
                upper.assert_not_called(); lower.assert_not_called()
                pane.event_generate("<ButtonRelease-1>", x=x+30 if horizontal else x,
                                    y=y if horizontal else y+30); root.update()
                self.assertIsNone(app._layout_drag_pane)
                self.assertEqual(pane.sashpos(0), original_position+30)
                self.assertLessEqual(upper.call_count, 3); self.assertLessEqual(lower.call_count, 3)
            for canvas in canvases:
                widget = canvas.get_tk_widget()
                self.assertEqual(canvas.get_width_height(physical=True),
                                 (widget.winfo_width(), widget.winfo_height()))
                self.assertTrue(widget.winfo_ismapped())
            if not horizontal:
                self.assertIs(app.response_axis.get_legend(), legend)
        app.time_cursor.move(SimpleNamespace(inaxes=app.response_axis, xdata=30.))
        root.update_idletasks(); self.assertIn("t = 30", app.chart_readouts[1].get())
        png = io.BytesIO(); app.response_axis.figure.savefig(png, format="png", dpi=160)
        self.assertTrue(png.getvalue().startswith(b"\x89PNG"))

    def test_only_current_parameter_page_is_mapped_and_context_survives(self):
        app, root = self.app, self.root
        root.deiconify(); app._show_page("identification"); root.update()
        page = app.pages["identification"]
        entry = next(widget for widget in app.identification_page.winfo_children()
                     if isinstance(widget, main.ttk.Entry))
        entry.focus_force(); root.update()
        page.canvas.yview_moveto(.3); root.update()
        before = page.canvas.yview()[0]
        app._show_page("results"); root.update()
        self.assertFalse(page.winfo_ismapped())
        self.assertEqual([name for name, widget in app.pages.items() if widget.winfo_ismapped()],
                         ["results"])
        app._show_page("identification"); root.update()
        self.assertAlmostEqual(page.canvas.yview()[0], before, places=2)
        self.assertIs(root.focus_get(), entry)

    def test_failed_background_autosave_preserves_file_and_retries(self):
        app = self.app
        app.root.after_cancel(app._autosave_job)
        app._save_autosave()
        original = app.autosave_path.read_bytes()
        app.assignment_panel.student_conclusion.set("Повторная запись")
        with patch("main.write_session", side_effect=OSError("Disk unavailable")):
            app._autosave_tick()
            app._finish_autosave(wait=True)
        self.assertEqual(app.autosave_path.read_bytes(), original)
        self.assertIn("Disk unavailable", app.status_text.get())
        app._autosave_tick()
        app._finish_autosave(wait=True)
        _, _, laboratory = read_laboratory_session(app.autosave_path)
        self.assertEqual(laboratory["conclusion"], "Повторная запись")

    def test_autosave_writer_keeps_tk_responsive_and_shutdown_saves_newer_data(self):
        from threading import Event
        from app.session_store import write_session as real_write
        started, release = Event(), Event()
        app = self.app
        app.root.after_cancel(app._autosave_job)
        def blocked_write(*args):
            started.set()
            if not release.wait(5): raise RuntimeError("Writer test timed out")
            return real_write(*args)
        try:
            with patch("main.write_session", side_effect=blocked_write):
                app._autosave_tick()
                self.assertTrue(started.wait(2))
                ticks = []
                app.root.after(0, lambda: ticks.append(True)); app.root.update()
                self.assertTrue(ticks)
                app.assignment_panel.student_conclusion.set("Изменено во время записи")
                release.set(); app._finish_autosave(wait=True)
            app._save_autosave()
            _, _, restored = read_laboratory_session(app.autosave_path)
            self.assertEqual(restored["conclusion"], "Изменено во время записи")
            self.assertTrue(app.autosave_path.with_suffix(".json.bak").exists())
            with patch("main.write_session") as writer:
                app._save_autosave(); writer.assert_not_called()
        finally:
            release.set(); app._finish_autosave(wait=True)

    def test_autosave_detects_nested_result_changes_without_json_signature(self):
        page = self.prepare_absorber_errors()
        app = self.app; app._save_autosave()
        original = app.autosave_path.read_bytes()
        with patch("main.json.dumps", side_effect=AssertionError("No JSON comparison")):
            app._save_autosave()
        page.result["quality"]["response_rmse"] = 123.
        app._save_autosave()
        self.assertEqual(app.autosave_path.read_bytes(), original)
        self.assertIn("качества", app.status_text.get())

    def test_cursor_uses_blit_and_prunes_removed_axes(self):
        from types import SimpleNamespace
        app = self.app
        event = SimpleNamespace(inaxes=app.response_axis, xdata=20.)
        app.time_cursor.move(event); self.root.update_idletasks()
        with patch.object(app.response_canvas, "draw", wraps=app.response_canvas.draw) as draw:
            event.xdata = 25.; app.time_cursor.move(event); self.root.update_idletasks()
            app.time_cursor.clear(); self.root.update_idletasks()
            app.time_cursor.clear(); self.root.update_idletasks()
            draw.assert_not_called()
        twin = app.response_axis.twinx(); app.response_canvas.draw()
        app.time_cursor.move(event); self.root.update_idletasks()
        self.assertIn(twin, app.time_cursor.markers)
        app.response_axis.figure.delaxes(twin); app.response_canvas.draw()
        self.assertNotIn(twin, app.time_cursor.markers)
        app.response_axis.set_xlim(0, 40); app.response_canvas.draw()
        event.xdata = 30.; app.time_cursor.move(event)
        self.assertIn("t = 30", app.chart_readouts[1].get())

    def test_small_wheel_deltas_accumulate_and_same_layout_is_skipped(self):
        from types import SimpleNamespace
        app = self.app; page = app.pages["identification"]
        app._show_page("identification"); self.root.update_idletasks()
        page._scrolling_enabled = True
        with patch.object(page.canvas, "yview_scroll") as scroll:
            for _ in range(4): page._on_mousewheel(SimpleNamespace(delta=-30))
            scroll.assert_called_once_with(1, "units")
        event = SimpleNamespace(widget=self.root, width=1366, height=900)
        app._adapt_window(event)
        with patch.object(app.comparison_page.table, "configure") as table:
            app._adapt_window(event); table.assert_not_called()

    def test_identification_quality_is_visible_saved_and_checked(self):
        from app.identification import validate_experiment_state
        from app.exporting import write_experiment_report
        page = self.prepare_absorber_errors()
        self.assertIn("RMSE отклика", page.summary.get())
        self.assertIn("максимальный интервал", page.summary.get())
        snapshot = page.capture()
        page.restore(None); page.restore(snapshot)
        self.assertEqual(page.result["quality"], snapshot["result"]["quality"])
        bad = copy.deepcopy(snapshot); bad["result"]["quality"]["error_acceptable"] = False
        with self.assertRaises(ValueError): validate_experiment_state(bad)
        old = copy.deepcopy(snapshot); old["result"].pop("quality")
        page.restore(old)
        self.assertIn("старом результате", page.summary.get())
        page.restore(snapshot)
        path = Path(self.directory.name)/"quality.html"
        write_experiment_report(path, snapshot, "html")
        self.assertIn("RMSE отклика", path.read_text(encoding="utf-8"))

    def prepare_absorber_errors(self):
        app, page = self.app, self.app.identification_page
        app._show_page("identification")
        page.load_path(Path("data/identification_step.csv"))
        page.metadata["input_role"].set("η, доля"); page.metadata["output_role"].set("Xна, доля")
        page.calculate(); self.wait_for_task()
        app.absorber_form.chain = RICH_ABSORBENT
        app.absorber_form.controller_enabled.set(True); app.absorber_form.controller_type.set("PI")
        app.absorber_form.setpoint.set("30.5"); app.absorber_form.control_limit.set("20")
        app.absorber_form.simulation_duration.set("100"); app.absorber_form.time_constant.set("8"); app.absorber_form.delay.set("0")
        app.absorber_form.component_enabled.set(False); app.absorber_form.flow_enabled.set(False)
        for key, value in dict(k_errors="0;20", t_errors="0", l_errors="0").items():
            page.absorber_sensitivity.fields[key].set(value)
        app._show_page("identification")
        return page

    def test_identification_uncertainty_survives_session_and_reports(self):
        import functools
        from app import identification
        from app.exporting import write_experiment_report
        app, page = self.app, self.app.identification_page
        app._show_page("identification")
        page.load_path(Path("data/identification_step.csv"))
        self.assertEqual(str(page.uncertainty_button["state"]), "disabled")
        page.calculate(); self.wait_for_task()
        quick = functools.partial(identification.bootstrap_intervals, replicates=20)
        with patch("ui.identification_page.bootstrap_intervals", quick):
            page.estimate_uncertainty(); self.wait_for_task()
        record = page.result["uncertainty"]
        self.assertEqual(record["replicates"], 20)
        self.assertIn("95% интервалы", page.summary.get())
        report = Path(self.directory.name) / "uncertainty.html"
        write_experiment_report(report, page.capture(), "html")
        self.assertIn("95% интервалы", report.read_text(encoding="utf-8"))
        session = Path(self.directory.name) / "uncertainty.json"
        with patch("main.filedialog.asksaveasfilename", return_value=str(session)): app._save_comparison_session()
        page.restore(None)
        self.assertEqual(str(page.uncertainty_button["state"]), "disabled")
        with patch("main.filedialog.askopenfilename", return_value=str(session)): app._open_comparison_session()
        self.assertEqual(page.result["uncertainty"], record)
        self.assertIn("95% интервалы", page.summary.get())
        page.calculate(); self.wait_for_task()
        self.assertNotIn("uncertainty", page.result)

    def test_absorber_error_workflow_roundtrip_exports_and_atomic_import(self):
        import json
        from app.identification import validate_experiment_state
        from app.absorber_sensitivity_exporting import write_report
        from app.exporting import write_experiment_csv, write_experiment_report
        page = self.prepare_absorber_errors(); s = page.absorber_sensitivity
        s.start(); self.wait_for_task()
        self.assertIsNotNone(s.result, page.summary.get())
        self.assertTrue(page.result_current)
        s.selected.set(s.box["values"][1]); page.draw()
        self.app._set_chart_mode("both"); self.root.update()
        self.assertEqual(len(self.app.chart_panes.panes()), 2)
        self.assertEqual(len(self.app.response_axis.lines), 4)
        for axis, canvas in ((self.app.disturbance_axis, self.app.disturbance_canvas), (self.app.response_axis, self.app.response_canvas)):
            canvas.draw(); box = axis.get_legend().get_window_extent(canvas.get_renderer())
            self.assertGreaterEqual(box.y0, axis.figure.bbox.y0-1)
            self.assertLessEqual(box.y1, axis.figure.bbox.y1+1)
        snapshot = page.capture()
        for fmt in ("csv", "html", "pdf"):
            path = Path(self.directory.name)/f"absorber-errors.{fmt}"
            write_report(path, s.result, s.index(), fmt)
            self.assertGreater(path.stat().st_size, 1000)
        for fmt in ("html", "pdf"):
            write_experiment_report(Path(self.directory.name)/f"full.{fmt}", snapshot, fmt)
        write_experiment_csv(Path(self.directory.name)/"full.csv", snapshot)
        session = Path(self.directory.name)/"absorber.json"
        with patch("main.filedialog.asksaveasfilename", return_value=str(session)): self.app._save_comparison_session()
        page.restore(None)
        with patch("main.filedialog.askopenfilename", return_value=str(session)): self.app._open_comparison_session()
        self.assertEqual(page.capture(), snapshot)
        old = copy.deepcopy(snapshot); old.pop("absorber_sensitivity"); old["view"] = "Отклик"
        self.assertIsNone(validate_experiment_state(old)["absorber_sensitivity"]["result"])
        for key in ("metrics", "input_state", "origin", "selected"):
            bad = json.loads(session.read_text(encoding="utf-8"))
            block = bad["laboratory"]["identification"]["absorber_sensitivity"]
            if key == "metrics": block["result"]["cases"][0]["metrics"]["iae"] = 0.
            if key == "input_state": block["result"]["input_state"]["time_constant"] = "12"
            if key == "origin": block["result"]["origin"]["parameters"]["gain"] = 99.
            if key == "selected": block["selected"] = 99
            session.write_text(json.dumps(bad), encoding="utf-8")
            with patch("main.filedialog.askopenfilename", return_value=str(session)): self.app._open_comparison_session()
            self.assertEqual(page.capture(), snapshot)
        s.fields["iae"].set("2")
        self.assertTrue(page.result_current)
        self.assertIn("Снимок прежних", s.summary.get())
        s.fields["iae"].set(snapshot["absorber_sensitivity"]["fields"]["iae"])
        self.app.absorber_form.time_constant.set("9")
        self.assertTrue(s.capture()["stale"])
        stale_report = Path(self.directory.name)/"stale.html"
        write_experiment_report(stale_report, page.capture(), "html")
        self.assertIn("Снимок прежних данных или настроек", stale_report.read_text(encoding="utf-8"))
        self.assertFalse(self.app.signal_mode_box.winfo_manager())

    def test_absorber_requirement_edit_cancels_worker_and_sign_roles_checked(self):
        from threading import Event
        page = self.prepare_absorber_errors(); s = page.absorber_sensitivity
        accepted = []; gate = Event()
        self.app._start_task("Проверка", lambda cancel, progress: gate.wait(2), accepted.append, "identification")
        task = self.app._task
        s.fields["iae"].set("2")
        self.assertTrue(task.cancel.is_set()); self.assertTrue(page.result_current)
        gate.set(); task.thread.join(3); self.root.update()
        self.assertEqual(accepted, [])
        page.metadata["output_role"].set("Xог, доля")
        page.calculate(); self.wait_for_task()
        s.start()
        self.assertIsNone(self.app._task)
        self.assertIn("Выход CSV", page.summary.get())

    def test_identification_errors_workflow_old_sessions_and_atomic_import(self):
        from app.tank_state import validate_tank_state
        from app.tank_sensitivity_exporting import write_report
        app, page = self.app, self.app.tank_page
        app._show_page("tank")
        page.generate(); self.wait_for_task()
        page.identify(); self.wait_for_task()
        for key, value in dict(k_errors="0;20", t_errors="0", l_errors="0").items():
            page.sensitivity.fields[key].set(value)
        revision = page.revision
        page.sensitivity.start(); self.wait_for_task()
        self.assertIsNotNone(page.sensitivity.result)
        self.assertEqual(page.revision, revision)
        page.sensitivity.selected.set(page.sensitivity.box["values"][1]); page.draw()
        app._set_chart_mode("both"); self.root.update()
        self.assertEqual(len(app.chart_panes.panes()), 2)
        self.assertEqual(len(app.response_axis.lines), 4)
        for axis, canvas in ((app.disturbance_axis, app.disturbance_canvas),(app.response_axis,app.response_canvas)):
            canvas.draw(); renderer = canvas.get_renderer()
            box = axis.get_legend().get_window_extent(renderer)
            self.assertGreaterEqual(box.y0, axis.figure.bbox.y0)
            self.assertLessEqual(box.y1, axis.figure.bbox.y1+1)
        snapshot = page.capture()
        validate_tank_state(snapshot)
        for fmt in ("csv", "html", "pdf"):
            path = Path(self.directory.name)/f"errors.{fmt}"
            write_report(path, snapshot["sensitivity"], 1, fmt)
            self.assertGreater(path.stat().st_size, 1000)
        session = Path(self.directory.name)/"errors.json"
        with patch("main.filedialog.asksaveasfilename", return_value=str(session)): app._save_comparison_session()
        page.restore(None)
        with patch("main.filedialog.askopenfilename", return_value=str(session)): app._open_comparison_session()
        self.assertEqual(page.capture(), snapshot)
        old = copy.deepcopy(snapshot)
        for key in ("sensitivity_fields", "sensitivity", "selected_sensitivity"): old.pop(key)
        old["view"] = "PI"
        restored = validate_tank_state(old)
        self.assertIsNone(restored["sensitivity"])
        self.assertEqual(restored["selected_sensitivity"], 0)
        import json
        payload = json.loads(session.read_text(encoding="utf-8"))
        payload["laboratory"]["tank"]["sensitivity"]["cases"][0]["run"]["metrics"]["iae"] = 0.
        session.write_text(json.dumps(payload), encoding="utf-8")
        with patch("main.filedialog.askopenfilename", return_value=str(session)): app._open_comparison_session()
        self.assertEqual(page.capture(), snapshot)
        page.sensitivity.fields["iae"].set("200")
        self.assertEqual(page.revision, revision)
        self.assertIn("Снимок прежних настроек", page.sensitivity.summary.get())

    def test_editing_error_requirements_cancels_task_without_invalidating_fit(self):
        from threading import Event
        app, page = self.app, self.app.tank_page
        app._show_page("tank")
        started, release = Event(), Event()
        accepted = []
        def slow(cancel, progress):
            started.set(); release.wait(2); return "late"
        page.perform("Ошибки идентификации", slow, accepted.append)
        self.assertTrue(started.wait(1))
        revision = page.revision
        page.sensitivity.fields["iae"].set("200")
        release.set(); self.wait_for_task()
        self.assertEqual(accepted, [])
        self.assertEqual(page.revision, revision)

    def test_common_model_results_restore_and_reject_corrupt_snapshot(self):
        import numpy as np
        from app.session_store import validate_laboratory
        from unittest.mock import patch
        import copy
        self.app._calculate()
        self.wait_for_task()
        expected = copy.deepcopy(self.app.model_results["absorber"])
        self.app._show_page("model_result")
        self.app._set_chart_mode("both")
        self.root.update_idletasks()
        self.assertEqual(len(self.app.chart_panes.panes()), 2)
        self.assertTrue(self.app.disturbance_axis.lines)
        self.assertTrue(self.app.response_axis.lines)
        np.testing.assert_array_equal(self.app.response_axis.lines[0].get_ydata(),
                                      np.asarray(expected["signals"]["xog"])*100)
        saved = self.app._capture_laboratory()
        validated = validate_laboratory(saved)
        self.app.model_results.clear()
        self.app._restore_laboratory(validated, self.app.last_calculation)
        self.assertEqual(self.app.model_results["absorber"], expected)
        self.assertEqual(self.app.current_page, "model_result")
        old = copy.deepcopy(saved)
        old.pop("model_results")
        old["page"] = "disturbances"
        old.pop("selected_model")
        old = validate_laboratory(old)
        self.app._restore_laboratory(old, self.app.last_calculation)
        self.assertEqual(self.app.model_results["absorber"], expected)
        damaged = copy.deepcopy(saved)
        damaged["model_results"]["absorber"]["units"]["xog"] = "м"
        with self.assertRaises(ValueError):
            validate_laboratory(damaged)
        self.assertEqual(self.app.model_results["absorber"], expected)
        self.app.selected_model.set("Бак")
        self.app._select_model()
        self.app.tank_page.calculate()
        self.wait_for_task()
        tank = copy.deepcopy(self.app.model_results["level_tank"])
        self.app._show_page("model_result")
        self.assertEqual(self.app.selected_model.get(), "Бак")
        self.assertEqual(self.app.response_axis.get_ylabel(), "м")
        for axis, canvas in ((self.app.disturbance_axis, self.app.disturbance_canvas),
                             (self.app.response_axis, self.app.response_canvas)):
            canvas.draw()
            legend = axis.get_legend().get_window_extent(canvas.get_renderer())
            self.assertLessEqual(legend.y1, axis.figure.bbox.y1 + 1)
            self.assertGreaterEqual(legend.y0, axis.figure.bbox.y0 - 1)
        np.testing.assert_array_equal(self.app.response_axis.lines[0].get_ydata(), tank["signals"]["level"])
        self.app._set_chart_mode("response")
        self.app._set_chart_mode("both")
        self.root.update_idletasks()
        self.assertEqual(len(self.app.chart_panes.panes()), 2)
        with patch("ui.model_result_page.filedialog.askopenfilename", return_value="missing.json"):
            self.app.model_result_page.open()
        self.assertEqual(self.app.model_results["level_tank"], tank)
        self.assertFalse(self.app.nav_buttons["controller"].winfo_manager())
        self.app.nav_buttons["disturbances"].invoke()
        self.assertEqual(self.app.current_page, "tank")
        self.assertEqual(self.app.selected_model.get(), "Бак")

    def test_valid_result_import_cancels_same_model_worker_and_invalid_import_preserves_it(self):
        import threading
        from app.physical_models import MODELS
        from app.model_contract import write_result
        from app.tank import DEFAULTS
        self.app.selected_model.set("Бак")
        self.app._select_model()
        raw = MODELS.run("level_tank", {"parameters": dict(DEFAULTS, horizon=40.)})
        imported = MODELS.result("level_tank", raw)
        imported["configuration"]["marker"] = "imported"
        path = Path(self.directory.name)/"imported.json"
        write_result(path, imported, MODELS)
        gate = threading.Event()
        def late(cancel, progress):
            gate.wait(3)
            return dict(raw, revision=self.app.tank_page.revision)
        self.app._start_task("Запоздалый расчёт", late, self.app.tank_page.accept_result, "tank")
        task = self.app._task
        try:
            with patch("ui.model_result_page.filedialog.askopenfilename", return_value="missing.json"):
                self.app.model_result_page.open()
            self.assertIs(self.app._task, task)
            self.assertFalse(task.cancel.is_set())
            with patch("ui.model_result_page.filedialog.askopenfilename", return_value=str(path)):
                self.app.model_result_page.open()
            self.assertTrue(task.cancel.is_set())
            self.assertIsNone(self.app._task)
            gate.set()
            task.thread.join(3)
            self.root.update()
            self.assertEqual(self.app.model_results["level_tank"], imported)
        finally:
            gate.set()
            task.thread.join(3)

    def test_test_only_model_uses_common_view_without_model_specific_branch(self):
        import numpy as np
        from app.model_contract import ModelRegistry
        from app.physical_models import MODELS
        from test_physical_models import heater
        from unittest.mock import patch
        registry = ModelRegistry((*MODELS.all(), heater()))
        native = registry.run("test_heater", {"parameters": {"capacity": 4., "initial": 20.}})
        result = registry.result("test_heater", native)
        with patch.object(main, "MODELS", registry), patch("ui.model_result_page.MODELS", registry), patch("app.session_store.MODELS", registry):
            self.app.model_results["test_heater"] = result
            self.app.selected_model.set("Тестовый нагреватель")
            self.app._select_model()
            self.root.update_idletasks()
            self.assertEqual(self.app.current_page, "model_result")
            self.assertEqual(self.app.response_axis.get_ylabel(), "°C")
            self.assertEqual(self.app.disturbance_axis.get_ylabel(), "Вт")
            np.testing.assert_array_equal(self.app.response_axis.lines[0].get_ydata(), [20., 20.5, 21.])
            saved = self.app._capture_laboratory()
            from app.session_store import validate_laboratory
            validated = validate_laboratory(saved)
            self.app._restore_laboratory(validated, self.app.last_calculation)
            self.assertEqual(self.app.selected_model.get(), "Тестовый нагреватель")
            self.assertEqual(self.app.model_results["test_heater"], result)
            self.app.selected_model.set("Абсорбер")
            self.app._select_model()

    def test_object_page_connects_through_registry_without_main_changes(self):
        import dataclasses
        from app.model_contract import ModelRegistry
        from app.physical_models import MODELS
        from app.session_store import validate_laboratory
        from test_physical_models import heater
        probe = dataclasses.replace(heater(), entry_page="probe", page="test_gui_state:ProbePage",
                                    validate_state=validate_probe_state)
        registry = ModelRegistry((*MODELS.all(), probe))
        with self.assertRaises(ValueError):
            ModelRegistry((*MODELS.all(), dataclasses.replace(probe, validate_state=None)))
        with self.assertRaises(ValueError):
            ModelRegistry((*MODELS.all(), dataclasses.replace(probe, entry_page="tank")))
        self.app._close_application()
        self.root = tk.Tk()
        self.root.withdraw()
        with patch.object(main, "MODELS", registry), patch("app.session_store.MODELS", registry), \
                patch("ui.model_result_page.MODELS", registry), patch.object(main, "ScenarioStore", return_value=self.store):
            self.app = app = main.DynamicsControlApp(self.root)
            page = app.probe_page
            self.assertIs(app.object_pages["probe"], page)
            app.selected_model.set("Тестовый нагреватель"); app._select_model()
            self.assertEqual(app.current_page, "probe")
            self.assertEqual(app.page_title.get(), "Тестовый нагреватель: опыт и регулирование")
            self.assertEqual(app._upper_chart_label(), "Пробный вход")
            self.assertGreater(page.draws, 0)
            self.assertEqual(app.topbar_context.get(), "Тест подключения")
            self.assertEqual(app._task_signature("probe"), ["Тестовый нагреватель", [False]])
            page.result = registry.run("test_heater", {"parameters": {"capacity": 4., "initial": 20.}})
            laboratory = validate_laboratory(app._capture_laboratory())
            self.assertEqual(laboratory["probe"]["result"], page.result)
            expected = page.result
            page.result = None
            app.model_results.pop("test_heater", None)
            app._restore_laboratory(laboratory, app.last_calculation)
            self.assertEqual(page.result, expected)
            self.assertEqual(app.current_page, "probe")
            self.assertEqual(app.model_results["test_heater"], registry.result("test_heater", expected))
            broken = app._capture_laboratory()
            broken["probe"] = {"model_id": "other"}
            with self.assertRaises(ValueError):
                validate_laboratory(broken)
            app._start_task("Пробный расчёт", lambda cancel, progress: 1 / 0, lambda value: None, "probe")
            self.wait_for_task()
            self.assertIn("division by zero", page.summary.get())
            app.selected_model.set("Абсорбер"); app._select_model()

    def test_heater_page_runs_shares_results_and_survives_session(self):
        import functools
        from app import identification
        from app.session_store import validate_laboratory
        app, page = self.app, self.app.heater_page
        app.selected_model.set("Нагреватель"); app._select_model()
        self.assertEqual(app.current_page, "heater")
        self.assertIn("Ёмкость с нагревателем", app.topbar_context.get())
        page.calculate(); self.wait_for_task()
        self.assertIsNotNone(page.result, page.summary.get())
        self.assertEqual(app.response_axis.get_ylabel(), "Температура, °C")
        page.calculate_pi(); self.wait_for_task()
        self.assertEqual(page.pi["metrics"]["status"], "settled", page.summary.get())
        self.assertIn("PI: Kp=3", page.summary.get())
        page.estimate_uncertainty()
        self.assertIn("Сначала оцените K, T, L", page.summary.get())
        page.identify(); self.wait_for_task()
        self.assertEqual(page.view.get(), "Идентификация")
        self.assertIn("Физические значения: K=2.132", page.summary.get())
        with patch("ui.heater_page.bootstrap_intervals", functools.partial(identification.bootstrap_intervals, replicates=20)):
            page.estimate_uncertainty(); self.wait_for_task()
        self.assertIn("95% интервалы", page.summary.get())
        page.tune()
        self.assertEqual(page.fields["integral_time"].get(), format(page.fit["time_constant"], ".4g"))
        self.assertIsNotNone(page.fit)
        page.view.set("PI")
        self.assertIn("Снимок прежних настроек", page.summary.get())
        page.calculate_pi(); self.wait_for_task()
        self.assertNotIn("Снимок прежних настроек", page.summary.get())
        self.assertEqual(page.pi["metrics"]["status"], "settled", page.summary.get())
        page.fields["noise"].set("0.1")
        page.identify()
        self.assertIn("Сначала рассчитайте свободный отклик", page.summary.get())
        page.fields["noise"].set("0.05")
        app._show_page("model_result")
        self.assertEqual(app.response_axis.get_ylabel(), "°C")
        app._show_page("heater")
        snapshot = page.capture()
        session = Path(self.directory.name) / "heater.json"
        with patch("main.filedialog.asksaveasfilename", return_value=str(session)): app._save_comparison_session()
        page.restore(None)
        self.assertIsNone(page.pi)
        with patch("main.filedialog.askopenfilename", return_value=str(session)): app._open_comparison_session()
        self.assertEqual(page.capture(), snapshot)
        self.assertEqual(app.current_page, "heater")
        laboratory = app._capture_laboratory()
        laboratory["selected_model"] = "absorber"
        with self.assertRaises(ValueError):
            validate_laboratory(laboratory)
        page.fields["power"].set("abc")
        page.calculate()
        self.assertIn("числовые параметры", page.summary.get())
        app.selected_model.set("Абсорбер"); app._select_model()
        self.assertNotEqual(app.current_page, "heater")

    def test_tank_complete_workflow_roundtrip_units_and_atomic_import(self):
        import json
        import numpy as np
        from app.tank_exporting import write_tank_report
        from app.tank_state import validate_tank_state
        app, page = self.app, self.app.tank_page
        absorber = copy.deepcopy(app._capture_input_state())
        app._show_page("tank")
        page.generate(); self.wait_for_task()
        self.assertGreater(len(page.rows),17)
        page.identify(); self.wait_for_task()
        self.assertIsNotNone(page.fit, page.summary.get())
        self.assertLess(page.fit["delay"],1.)
        self.assertAlmostEqual(page.fit["gain"],200.,delta=15.)
        import functools
        from app import identification
        with patch("ui.tank_page.bootstrap_intervals", functools.partial(identification.bootstrap_intervals, replicates=20)):
            page.estimate_uncertainty(); self.wait_for_task()
        self.assertEqual(page.fit["uncertainty"]["replicates"], 20, page.summary.get())
        self.assertIn("95% интервалы", page.summary.get())
        from app.tank import DEFAULTS, simulate
        separate=simulate(dict(DEFAULTS,step_time=10.,pump_step=.0005))
        import csv
        independent=Path(self.directory.name)/"independent-tank.csv"
        with independent.open("w",newline="",encoding="utf-8") as stream:
            writer=csv.writer(stream,delimiter=";"); writer.writerow(page.headers)
            writer.writerows(zip(separate["time"],separate["pump"],separate["level"]))
        original_fit=copy.deepcopy(page.fit)
        page.same_signals.set(True)
        with patch("ui.tank_page.filedialog.askopenfilename",return_value=str(independent)):
            page.validate_file()
        self.wait_for_task()
        self.assertIsNotNone(page.validation,page.summary.get())
        self.assertEqual(page.fit,original_fit)
        independent.unlink()
        page.fields["gain"].set("0.02")
        page.check_pi(); self.wait_for_task()
        self.assertIsNotNone(page.pi,page.summary.get())
        self.assertEqual(len(page.pi["cases"]),8)
        snapshot=page.capture()
        validate_tank_state(snapshot)
        for path in (("result",),("pi","cases",0,"controller"),("result","baseline_pump"),("fit","origin","units")):
            malformed=copy.deepcopy(snapshot)
            target=malformed
            for key in path[:-1]: target=target[key]
            if path[-1]=="controller": target[path[-1]]=None
            else: del target[path[-1]]
            with self.subTest(path=path),self.assertRaises(ValueError): page.restore(malformed)
            self.assertEqual(page.capture(),snapshot)
        with patch("main.filedialog.asksaveasfilename",return_value=str(Path(self.directory.name)/"tank-session.json")):
            app._save_comparison_session()
        page.restore(None)
        with patch("main.filedialog.askopenfilename",return_value=str(Path(self.directory.name)/"tank-session.json")):
            app._open_comparison_session()
        self.assertEqual(page.capture(),snapshot)
        self.assertEqual(app.selected_model.get(),"Бак")
        self.assertEqual(app._capture_input_state(),absorber)
        for fmt in ("csv","html","pdf"):
            output=Path(self.directory.name)/f"tank.{fmt}"
            write_tank_report(output,snapshot,fmt)
            self.assertGreater(output.stat().st_size,1000)
        malformed=copy.deepcopy(snapshot)
        malformed["pi"]["cases"][0]["pump"][0]=1000.
        with self.assertRaises(ValueError): validate_tank_state(malformed)
        session=Path(self.directory.name)/"tank-session.json"
        payload=json.loads(session.read_text(encoding="utf-8")); payload["laboratory"]["tank"]=malformed
        session.write_text(json.dumps(payload),encoding="utf-8")
        with patch("main.filedialog.askopenfilename",return_value=str(session)),patch("main.messagebox.showerror"):
            app._open_comparison_session()
        self.assertEqual(page.capture(),snapshot)
        # Physical units convert before fitting, without an absorber percentage scale.
        arrays=page.arrays()
        page.restoring=True
        page.rows=[[str(float(t)/60),str(float(q)*1000),str(float(h)*100)] for t,q,h in zip(*arrays)]
        for var,value in zip(page.units,("мин","л/с","см")): var.set(value)
        page.restoring=False
        np.testing.assert_allclose(page.arrays(),arrays,rtol=1e-12)
        app._show_page("disturbances")
        self.assertEqual(app.selected_model.get(),"Абсорбер")
        self.assertEqual(app._capture_input_state(),absorber)

    def test_complete_experiment_workflow_restores_snapshots_and_exports_reports(self):
        import csv
        import json
        import numpy as np
        from app.exporting import write_experiment_report, write_experiment_csv
        app, page = self.app, self.app.identification_page
        page.load_path(Path(main.__file__).parent / "data" / "identification_step.csv")
        page.metadata["input_role"].set("η, доля")
        page.metadata["output_role"].set("Xна, доля")
        page.metadata["prediction"].set("Выход вырастет; K около 0.4")
        page.calculate()
        self.wait_for_task()
        fit = copy.deepcopy(page.result)
        time_values = np.linspace(0, 120, 601)
        u = np.where(time_values < 15, .2, .7)
        y = .3 + fit["gain"]*.5*-np.expm1(-np.maximum(time_values-15-fit["delay"],0)/fit["time_constant"])
        path = Path(self.directory.name) / "independent.csv"
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(page.headers)
            writer.writerows(zip(time_values,u,y))
        page.same_signals.set(True)
        with patch("ui.identification_page.filedialog.askopenfilename", return_value=str(path)):
            page.validate_file()
        self.wait_for_task()
        self.assertLess(page.validation["rmse"], 1e-12)
        page.pi_settings["horizon"].set("160")
        page.check_pi()
        self.wait_for_task()
        self.assertIsNotNone(page.pi_result)
        self.assertEqual(page.view.get(), "Регулирование")
        app._show_page("identification")
        snapshot = page.capture()
        app._save_autosave()
        path.unlink()
        page.restore(None)
        app._restore_autosave()
        self.assertEqual(page.capture(), snapshot)
        for fmt in ("html", "pdf"):
            output = Path(self.directory.name)/f"experiment.{fmt}"
            from app.exporting import _write_pdf
            with patch("app.exporting._write_pdf", wraps=_write_pdf) as export_pdf:
                write_experiment_report(output, page.capture(), fmt)
            if fmt == "pdf":
                from matplotlib.backends.backend_agg import FigureCanvasAgg
                for figure in export_pdf.call_args.args[1]:
                    if len(figure.axes) != 3:
                        continue
                    positions = [axis.get_position().bounds for axis in figure.axes]
                    canvas = FigureCanvasAgg(figure)
                    canvas.draw()
                    for axis, position in zip(figure.axes, positions):
                        np.testing.assert_allclose(axis.get_position().bounds, position, atol=1e-12)
                        bounds = axis.yaxis.label.get_window_extent(canvas.get_renderer())
                        self.assertGreaterEqual(bounds.x0, 0)
                        self.assertLessEqual(bounds.x1, figure.bbox.width)
            self.assertGreater(output.stat().st_size, 1000)
        write_experiment_csv(Path(self.directory.name)/"all.csv", snapshot)
        before = json.dumps(page.pi_result, sort_keys=True)
        page.units[0].set("мин")
        self.assertFalse(page.result_current)
        self.assertIn("прежних", app.response_subtitle.get())
        self.assertEqual(json.dumps(page.pi_result, sort_keys=True), before)
        broken = copy.deepcopy(snapshot)
        broken["pi_result"]["runs"][0]["metrics"]["final_error"] = "broken"
        from app.identification import validate_experiment_state
        with self.assertRaises(ValueError):
            validate_experiment_state(broken)

    def test_experiment_session_restores_without_source_and_old_sessions_clear_it(self):
        import json
        import numpy as np
        app, page = self.app, self.app.identification_page
        source = Path(self.directory.name) / "source.csv"
        source.write_bytes((Path(main.__file__).parent / "data" / "identification_step.csv").read_bytes())
        page.load_path(source)
        app._show_page("identification")
        page.calculate()
        self.wait_for_task()
        original = page.result["model"].copy()
        page.view.set("Остаток")
        app._save_autosave()
        self.assertTrue(app.autosave_path.exists())
        source.unlink()
        page.restore(None)
        app._restore_autosave()
        self.assertEqual(app.current_page, "identification")
        self.assertEqual(page.view.get(), "Остаток")
        self.assertTrue(page.result_current)
        np.testing.assert_array_equal(page.result["model"], original)
        page.columns[0].set(page.columns[1].get())
        app._save_autosave()
        page.restore(None)
        app._restore_autosave()
        self.assertFalse(page.result_current)
        self.assertEqual(str(page.apply_button["state"]), "disabled")
        np.testing.assert_array_equal(page.result["model"], original)
        old = json.loads(app.autosave_path.read_text(encoding="utf-8"))
        del old["laboratory"]["identification"]
        old["laboratory"]["page"] = "disturbances"
        app.autosave_path.write_text(json.dumps(old), encoding="utf-8")
        app._restore_autosave()
        self.assertIsNone(page.result)
        self.assertEqual(page.rows, [])

    def test_corrupt_experiment_session_does_not_replace_the_successful_fit(self):
        import json
        app, page = self.app, self.app.identification_page
        page.load_path(Path(main.__file__).parent / "data" / "identification_step.csv")
        page.calculate()
        self.wait_for_task()
        original = page.result
        laboratory = app._capture_laboratory()
        laboratory["identification"]["result"]["time"][1] = 0
        path = Path(self.directory.name) / "bad-experiment.json"
        path.write_text(json.dumps(dict(version=2, comparison_counter=0, runs=[], laboratory=laboratory)), encoding="utf-8")
        with self.assertRaises(ValueError):
            read_laboratory_session(path)
        self.assertIs(page.result, original)

    def test_foreign_scenario_and_optional_prediction_restore_without_replacing_signal(self):
        import json
        import numpy as np
        app = self.app
        original = app.last_calculation["final_response"].copy()
        laboratory = app._capture_laboratory()
        laboratory["scenario"]["name"] = "Сценарий другого компьютера"
        laboratory["scenario_name"] = laboratory["scenario"]["name"]
        del laboratory["last_calculation"]["prediction"]
        laboratory["last_calculation"]["final_response"] = "invalid"
        path = Path(self.directory.name) / "foreign.json"
        path.write_text(json.dumps({"version": 2, "comparison_counter": 0, "runs": [],
                                    "laboratory": laboratory}), encoding="utf-8")
        runs, counter, restored = read_laboratory_session(path)
        result = restore_calculation(restored["last_calculation"])
        app.comparison_page.runs, app.comparison_page.counter = runs, counter
        app._restore_laboratory(restored, result)
        self.assertIn("Сценарий другого компьютера", app.scenario_library.by_name)
        self.assertEqual(app.scenario_library.selected.get(), "Сценарий другого компьютера")
        self.assertIsNone(app.last_prediction)
        np.testing.assert_array_equal(app.last_calculation["final_response"], original)

    def test_new_window_automatically_restores_saved_laboratory(self):
        import numpy as np
        original = self.app.last_calculation["final_response"].copy()
        self.app.assignment_panel.student_conclusion.set("Восстановлено после запуска")
        self.app.absorber_form.component_value.set("-")
        self.app._close_application()
        self.root = tk.Tk()
        self.root.withdraw()
        with patch.object(main, "ScenarioStore", return_value=self.store):
            self.app = main.DynamicsControlApp(self.root)
        self.assertEqual(self.app.assignment_panel.student_conclusion.get(), "Восстановлено после запуска")
        self.assertEqual(self.app.absorber_form.component_value.get(), "-")
        np.testing.assert_array_equal(self.app.last_calculation["final_response"], original)
        self.assertIn("автосохранения", self.app.status_text.get())

    def test_full_laboratory_restores_draft_result_and_assignment_without_using_attempt(self):
        import numpy as np
        app = self.app
        app.assignment_panel.current_lesson["attempt_limit"] = 3
        app.assignment_panel.assignment_enabled.set(True)
        app.assignment_panel.predicted_direction.set("Уменьшится")
        app.assignment_panel.predicted_fastest.set("Без сравнения")
        app.assignment_panel.predicted_correction.set("Регулятор выключен")
        app.assignment_panel.predicted_steady.set("26.27")
        app._calculate()
        self.wait_for_task()
        original = app.last_calculation
        app.comparison_page.add_current()
        app.assignment_panel.learning_mode.set(True)
        app.assignment_panel.learning_step = 4
        app.assignment_panel.student_name.set("Студент")
        app.assignment_panel.student_conclusion.set("Проверен баланс")
        app.absorber_form.component_value.set("-")
        app.assignment_panel.predicted_steady.set("26,")
        path = Path(self.directory.name) / "laboratory.json"
        write_session(path, app.comparison_page.runs, app.comparison_page.counter, app._capture_laboratory())
        runs, counter, laboratory = read_laboratory_session(path)
        result = restore_calculation(laboratory["last_calculation"])
        app._reset()
        app.assignment_panel.student_name.set("")
        app.assignment_panel.assignment_attempts = 0
        app.comparison_page.runs, app.comparison_page.counter = runs, counter
        app._restore_laboratory(laboratory, result)
        np.testing.assert_array_equal(app.last_calculation["final_response"], original["final_response"])
        np.testing.assert_array_equal(app.last_calculation["time"], original["time"])
        self.assertEqual(app.absorber_form.component_value.get(), "-")
        self.assertEqual(app.assignment_panel.predicted_steady.get(), "26,")
        self.assertEqual(app.assignment_panel.assignment_attempts, 1)
        self.assertEqual(app.assignment_panel.assignment_evaluation, original["evaluation"])
        self.assertEqual(app.assignment_panel.learning_step, 4)
        self.assertEqual(app.assignment_panel.student_name.get(), "Студент")
        self.assertEqual(app.assignment_panel.student_conclusion.get(), "Проверен баланс")

    def test_broken_nested_import_does_not_change_current_work(self):
        import json
        app = self.app
        path = Path(self.directory.name) / "broken.json"
        write_session(path, [], 0, app._capture_laboratory())
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload["laboratory"]["last_calculation"]["input_state"]["delay"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        result, form = app.last_calculation, app._capture_input_state()
        with patch.object(main.filedialog, "askopenfilename", return_value=str(path)):
            app._open_comparison_session()
        self.assertIs(app.last_calculation, result)
        self.assertEqual(app._capture_input_state(), form)

    def test_autosave_recovers_backup_and_preserves_corrupt_copy(self):
        app = self.app
        app.assignment_panel.student_conclusion.set("Первая корректная копия")
        app._save_autosave()
        app.assignment_panel.student_conclusion.set("Вторая копия")
        app._save_autosave()
        app.autosave_path.write_text("broken", encoding="utf-8")
        app.assignment_panel.student_conclusion.set("")
        app._restore_autosave()
        self.assertEqual(app.assignment_panel.student_conclusion.get(), "Первая корректная копия")
        self.assertEqual(app.autosave_path.with_suffix(".json.corrupt").read_text(encoding="utf-8"), "broken")
        self.assertIn("резервного", app.status_text.get())
        app.autosave_path.unlink()
        app.assignment_panel.student_conclusion.set("")
        app._restore_autosave()
        self.assertEqual(app.assignment_panel.student_conclusion.get(), "Первая корректная копия")

    def test_map_selection_explains_independent_observations(self):
        from types import SimpleNamespace
        result = self.app.last_calculation
        self.app.analysis_panel.map_data = controller_setting_map(
            result["chain"], result["model_values"], result["component_fraction"],
            result["flow_fraction"], result["dynamics"], "PI", (6,), (2,), 1, 0.8,
        )
        self.app._show_page("tuning_map")
        self.app.analysis_panel.select_map_cell(SimpleNamespace(inaxes=self.app.disturbance_axis, xdata=0, ydata=0))
        description = self.app.analysis_panel.map_selection_summary.get()
        self.assertIn("Насыщение η", description)
        self.assertIn("Вывод относится только к длительности этого опыта", description)
        self.assertEqual(self.app.analysis_panel.map_selection, (0, 0))

    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        self.root.withdraw()
        self.directory = tempfile.TemporaryDirectory()
        self.store = ScenarioStore(Path(self.directory.name) / "scenarios.json")
        with patch.object(main, "ScenarioStore", return_value=self.store):
            self.app = main.DynamicsControlApp(self.root)
        self.app._apply_scenario_data(self.app.scenario_library.scenarios[1])
        self.app._calculate()
        self.wait_for_task()
        self.root.update_idletasks()

    def wait_for_task(self):
        deadline = time.monotonic() + 10
        while self.app._task is not None:
            self.assertLess(time.monotonic(), deadline, "Background task timed out")
            self.root.update()
            time.sleep(0.005)

    def test_background_map_keeps_tk_timer_responsive_and_can_be_cancelled(self):
        app = self.app
        app._show_page("tuning_map")
        original = app.last_calculation
        ticks = []
        self.root.after(0, lambda: ticks.append(True))
        app.analysis_panel.map_grid_size.set("15")
        app.analysis_panel.calculate_map()
        task = app._task
        self.root.update()
        self.assertTrue(ticks)
        self.assertIsNotNone(task)
        app._cancel_task()
        task.thread.join(2)
        self.assertFalse(task.thread.is_alive())
        self.assertIsNone(app.analysis_panel.map_data)
        self.assertIs(app.last_calculation, original)

    def test_changed_form_cancels_work_but_equivalent_number_does_not(self):
        from threading import Event
        release = Event()
        original = self.app.last_calculation
        def slow_result(cancel, progress):
            release.wait(2)
            return original
        self.app._start_task("Проверка", slow_result, lambda result: self.fail("Stale result applied"))
        task = self.app._task
        self.app.absorber_form.component_value.set(self.app.absorber_form.component_value.get().replace(".", ",") + "00")
        self.assertIs(self.app._task, task)
        self.app.absorber_form.component_value.set("0.2")
        self.assertIsNone(self.app._task)
        self.assertTrue(task.cancel.is_set())
        release.set()
        task.thread.join(2)
        self.assertIs(self.app.last_calculation, original)

    def test_cancelled_prediction_does_not_consume_attempt_and_clear_is_undoable(self):
        app = self.app
        app.assignment_panel.current_lesson["attempt_limit"] = 3
        app.assignment_panel.assignment_enabled.set(True)
        app.assignment_panel.predicted_direction.set("Уменьшится")
        app.assignment_panel.predicted_fastest.set("Без сравнения")
        app.assignment_panel.predicted_correction.set("Регулятор выключен")
        app.assignment_panel.predicted_steady.set("26.27")
        original = app.last_calculation
        app._calculate()
        app._cancel_task()
        self.assertEqual(app.assignment_panel.assignment_attempts, 0)
        self.assertIs(app.last_calculation, original)
        app._reset()
        self.assertIsNone(app.last_calculation)
        app.comparison_page.counter = 7
        app._undo_clear_experiment()
        self.assertIs(app.last_calculation, original)
        self.assertEqual(app.assignment_panel.assignment_attempts, 0)
        self.assertEqual(app.comparison_page.counter, 7)

    def test_defaults_restore_disabled_controller_and_preserve_variant_title(self):
        app = self.app
        caption = "Применён: Снижение состава на 15% · вариант 5."
        app.active_scenario.set(caption)
        app.absorber_form.controller_type.set("PID")
        app.absorber_form.proportional_gain.set("19")
        app.absorber_form.integral_time.set("3")
        app._restore_experiment_defaults()
        self.assertEqual(app.absorber_form.controller_type.get(), "PI")
        self.assertEqual(app.absorber_form.proportional_gain.get(), "2")
        self.assertEqual(app.absorber_form.integral_time.get(), "20")
        self.assertEqual(app.active_scenario.get(), caption)

    def test_new_task_wins_even_when_old_worker_finishes_later(self):
        from threading import Event
        release = Event()
        applied = []
        self.app._start_task("Старый", lambda cancel, progress: release.wait(2), applied.append)
        old_task = self.app._task
        self.app._start_task("Новый", lambda cancel, progress: "new", applied.append)
        self.wait_for_task()
        release.set()
        old_task.thread.join(2)
        self.root.update()
        self.assertEqual(applied, ["new"])

    def test_chart_layout_preserves_result_and_restores_saved_mode(self):
        app = self.app
        original = app.last_calculation
        app._set_chart_mode("response")
        self.root.update_idletasks()
        self.assertEqual(len(app.chart_panes.panes()), 1)
        self.assertIs(app.last_calculation, original)
        app._set_chart_mode("both")
        self.root.update_idletasks()
        self.assertEqual(len(app.chart_panes.panes()), 2)
        app._set_chart_mode("response")
        app._close_application()
        self.root = tk.Tk()
        self.root.withdraw()
        with patch.object(main, "ScenarioStore", return_value=self.store):
            self.app = main.DynamicsControlApp(self.root)
        self.root.update_idletasks()
        self.assertEqual(self.app.chart_mode, "response")
        self.assertEqual(len(self.app.chart_panes.panes()), 1)

    def test_comparison_reopen_restores_previous_chart_mode(self):
        app = self.app
        app._set_chart_mode("response")
        app._show_page("comparison")
        app._close_application()
        self.root = tk.Tk()
        self.root.withdraw()
        with patch.object(main, "ScenarioStore", return_value=self.store):
            self.app = main.DynamicsControlApp(self.root)
        self.root.update_idletasks()
        self.assertEqual(self.app.current_page, "comparison")
        self.assertEqual(self.app.chart_mode, "input")
        self.app._show_page("disturbances")
        self.assertEqual(self.app.chart_mode, "response")

    def test_free_calculation_preserves_page_and_navigation_preserves_scroll(self):
        app = self.app
        app._show_page("controller")
        self.root.update_idletasks()
        page = app.pages["controller"]
        page.canvas.yview_moveto(0.5)
        expected = page.canvas.yview()[0]
        app._show_page("dynamics")
        app._show_page("controller")
        self.root.update_idletasks()
        self.assertAlmostEqual(page.canvas.yview()[0], expected, places=2)
        app._calculate()
        self.wait_for_task()
        self.assertEqual(app.current_page, "controller")

    def test_comparison_selection_retains_curve_colour_and_has_wide_table(self):
        app = self.app
        app.comparison_page.add_current()
        app.comparison_page.add_current()
        self.root.update_idletasks()
        second = app.comparison_page.runs[1]
        app.comparison_page.table.selection_set(second["id"])
        app.comparison_page.draw()
        self.assertEqual(app.disturbance_axis.lines[0].get_color(), app.comparison_page.color(second))
        self.assertEqual(str(app.comparison_page.table.tag_configure(second["id"], "foreground")), app.comparison_page.color(second))
        self.root.deiconify()
        self.root.geometry("1366x768")
        self.root.update()
        self.assertGreater(app.comparison_page.table.winfo_width(), 545)
        self.assertGreater(app.chart_panes.winfo_height(), 250)

    def test_renaming_run_redraws_comparison_and_reports_status(self):
        app = self.app
        app.comparison_page.add_current()
        run = app.comparison_page.runs[0]
        app.comparison_page.table.selection_set(run["id"])
        with patch.object(app.comparison_page, "ask_name", return_value="Новый опыт: проверка"):
            app.comparison_page.rename()
        self.assertEqual(run["name"], "Новый опыт: проверка")
        self.assertEqual(app.disturbance_axis.lines[0].get_label(), "Новый опыт")
        self.assertEqual(app.status_text.get(), "Название опыта изменено")

    def test_units_preserve_precision_snapshot_and_physical_result(self):
        import numpy as np
        app = self.app
        app.absorber_form.component_value.set("0.12345678901234567")
        app._calculate()
        self.wait_for_task()
        original = app.last_calculation["final_response"].copy()
        app.absorber_form.disturbance_units.set("%")
        app.absorber_form.switch_disturbance_units()
        self.assertEqual(app.absorber_form.component_value.get(), "12.345678901234567")
        self.assertNotIn("Параметры изменены", app.export_panel.export_summary.get())
        app._calculate()
        self.wait_for_task()
        np.testing.assert_array_equal(app.last_calculation["final_response"], original)
        saved = app._capture_input_state()
        app.absorber_form.disturbance_units.set("Доля")
        app.absorber_form.switch_disturbance_units()
        self.assertEqual(app.absorber_form.component_value.get(), "0.12345678901234567")
        app._restore_input_state(saved, preserve_result=True)
        self.assertEqual(app.absorber_form._disturbance_units, "percent")
        self.assertEqual(app.absorber_form.component_value.get(), "12.345678901234567")
        self.assertNotIn("Параметры изменены", app.export_panel.export_summary.get())

    def test_unit_switch_preserves_running_task_and_rejects_invalid_draft(self):
        from threading import Event
        release = Event()
        app = self.app
        app._start_task("test", lambda cancel, progress: release.wait(2), lambda result: None)
        task = app._task
        app.absorber_form.disturbance_units.set("%")
        app.absorber_form.switch_disturbance_units()
        self.assertIs(app._task, task)
        app.absorber_form.component_value.set("-")
        app.absorber_form.disturbance_units.set("Доля")
        app.absorber_form.switch_disturbance_units()
        self.assertEqual(app.absorber_form._disturbance_units, "percent")
        self.assertEqual(app.absorber_form.component_value.get(), "-")
        self.assertTrue(app.absorber_form.component_error.get())
        release.set()

    def test_sensitivity_units_change_without_changing_values(self):
        app = self.app
        app.analysis_panel.sensitivity_parameter.set("Возмущение состава")
        app.analysis_panel.sensitivity_values.set("0,1; 0,25")
        self.assertEqual(app.analysis_panel.parse_sensitivity_values(), (0.1, 0.25))
        app.absorber_form.disturbance_units.set("%")
        app.absorber_form.switch_disturbance_units()
        self.assertEqual(app.analysis_panel.parse_sensitivity_values(), (0.1, 0.25))
        self.assertEqual(app.analysis_panel.sensitivity_values.get(), "1E+1; 25")

    def test_learning_route_keeps_free_navigation_and_visits_all_steps(self):
        app = self.app
        app.assignment_panel.learning_mode.set(False)
        for page in ("scenarios", "disturbances", "controller", "results", "comparison", "export"):
            app.assignment_panel.advance_step()
            self.assertEqual(app.current_page, page)
            self.assertIn(f"{app.assignment_panel.learning_step}/6", app.route_stage.get())
        app._show_page("sensitivity")
        self.assertEqual(app.current_page, "sensitivity")
        self.assertEqual(app.assignment_panel.learning_step, 6)
        self.assertEqual(list(app.nav_buttons)[:3], ["scenarios", "disturbances", "dynamics"])

    def tearDown(self):
        if hasattr(self, "app"):
            self.app._finish_autosave(wait=True)
        if hasattr(self, "root"):
            self.root.update_idletasks()
            for callback in self.root.tk.splitlist(self.root.tk.call("after", "info")):
                self.root.after_cancel(callback)
            self.root.destroy()
        if hasattr(self, "directory"):
            self.directory.cleanup()

    def test_numeric_equivalence_and_inactive_fields_do_not_make_result_stale(self):
        self.app.absorber_form.component_value.set("-0,1500")
        self.app.absorber_form.integral_time.set("999")
        self.assertNotIn("Параметры изменены", self.app.export_panel.export_summary.get())
        self.app.absorber_form.component_value.set("-0.2")
        self.assertIn("Параметры изменены", self.app.export_panel.export_summary.get())
        self.app.absorber_form.component_value.set("-0.15")
        self.assertNotIn("Параметры изменены", self.app.export_panel.export_summary.get())

    def test_chain_and_controller_changes_keep_the_same_calculation_snapshot(self):
        result = self.app.last_calculation
        snapshot = copy.deepcopy(result["input_state"])
        self.app.absorber_form.select_chain("lean_gas")
        self.assertIs(self.app.last_calculation, result)
        self.assertEqual(result["chain"], RICH_ABSORBENT)
        self.assertIn("Параметры изменены", self.app.export_panel.export_summary.get())
        self.app.absorber_form.set_controller_mode(True)
        self.assertIs(self.app.last_calculation, result)
        self.assertEqual(result["input_state"], snapshot)
        self.app.comparison_page.add_current()
        self.assertEqual(self.app.comparison_page.runs[-1]["input_state"], snapshot)

    def test_teacher_quality_requirements_and_absolute_tolerance_survive_editor_round_trip(self):
        scenario = copy.deepcopy(self.app.scenario_library.scenarios[1])
        scenario["name"] = "Quality assignment"
        scenario["steady_absolute_tolerance"] = 2e-6
        scenario["lesson"]["controller_target"] = dict(type="PI", require_settled=True,
                                                       max_iae=0.012, max_saturation_duration=4)
        scenario = self.store.save(scenario)
        editor = ScenarioEditorDialog(self.root, self.store, lambda *_: None,
                                      lambda *_: None, lambda: None, main.BACKGROUND)
        try:
            editor._load_scenario(scenario)
            editor._variables["time_constant"].set("10,5")
            editor._variables["target_gain_min"].set("1,5")
            editor._variables["target_gain_max"].set("2,5")
            restored = editor._collect_scenario()
            self.assertEqual(restored["time_constant"], 10.5)
            self.assertEqual(restored["lesson"]["controller_target"]["gain_min"], 1.5)
            self.assertEqual(restored["lesson"]["controller_target"]["gain_max"], 2.5)
            self.assertEqual(restored["steady_absolute_tolerance"], 2e-6)
            self.assertEqual({k: v for k, v in restored["lesson"]["controller_target"].items() if k not in ("gain_min", "gain_max")}, scenario["lesson"]["controller_target"])
        finally:
            editor.destroy()

    def test_sensor_settings_round_trip_and_teacher_dependent_fields(self):
        import numpy as np
        app = self.app
        app.absorber_form.set_controller_mode(True)
        app.absorber_form.controller_type.set("PID")
        app.absorber_form.extension_values["noise_std"].set("0,05")
        app.absorber_form.extension_values["noise_seed"].set("2026")
        app.absorber_form.extension_values["derivative_filter_time"].set("0,5")
        app.absorber_form.extension_values["actuator_time_constant"].set("2")
        app.absorber_form.extension_values["actuator_rate_limit"].set("2")
        app._calculate()
        self.wait_for_task()
        result = app.last_calculation
        restored = restore_calculation(app._capture_laboratory()["last_calculation"])
        for key in ("control", "commanded_control", "measurement", "final_response"):
            np.testing.assert_array_equal(restored[key], result[key])
        app.absorber_form.extension_values["actuator_time_constant"].set("3")
        self.assertIn("Параметры изменены", app.export_panel.export_summary.get())
        editor = ScenarioEditorDialog(self.root, self.store, lambda *_: None,
                                      lambda *_: None, lambda: None, main.BACKGROUND)
        try:
            editor._load_scenario(self.store.scenarios[-1])
            editor._editable = True
            editor._variables["controller_enabled"].set(True)
            editor._variables["noise_std"].set("0")
            editor._update_dependent_states()
            self.assertEqual(str(editor._extension_entries["noise_seed"]["state"]), "disabled")
            editor._variables["noise_std"].set("0,05")
            self.assertEqual(str(editor._extension_entries["noise_seed"]["state"]), "normal")
        finally:
            editor.destroy()

    def test_both_charts_remain_visible_after_closed_loop_redraw(self):
        app = self.app
        self.root.deiconify()
        self.root.geometry("1366x1000+0+0")
        self.root.tk.call("tk", "scaling", 96 / 72)
        self.root.update()
        app.absorber_form.set_controller_mode(True)
        app._calculate()
        self.wait_for_task()
        app._set_chart_mode("both")
        deadline = time.monotonic() + .4
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertEqual(app.chart_mode, "both")
        self.assertGreater(app.chart_panes.sashpos(0), 100)
        self.assertTrue(all(card.winfo_ismapped() for card in app.chart_cards))

    def test_shared_cursor_modes_and_identification_workflow(self):
        from types import SimpleNamespace
        import numpy as np
        app = self.app
        page = app.identification_page
        page.load_path(Path(main.__file__).parent / "data" / "identification_step.csv")
        app._show_page("identification")
        page.calculate()
        self.wait_for_task()
        self.assertIsNotNone(page.result)
        self.assertAlmostEqual(page.result["time_constant"], 8, delta=.08)
        original_model = app.absorber_form.model_values.copy()
        original_gain = app.absorber_form.proportional_gain.get()
        app.time_cursor.move(SimpleNamespace(inaxes=app.response_axis, xdata=20))
        self.assertTrue(all(readout.get().startswith("t = 20 с") for readout in app.chart_readouts))
        np.testing.assert_array_equal(page.result["residual"], page.result["output"] - page.result["model"])
        page.apply()
        self.assertEqual(app.absorber_form.model_values, original_model)
        self.assertEqual(app.absorber_form.proportional_gain.get(), original_gain)
        self.assertAlmostEqual(float(app.absorber_form.delay.get()), 2, delta=.2)
        self.assertEqual(app.current_page, "controller")
        app.absorber_form.set_controller_mode(True)
        app._calculate()
        self.wait_for_task()
        for mode, ylabel, axes in (("Только ошибка", "Ошибка e(t), п.п.", 1),
                                  ("Только управление", "η, %", 1),
                                  ("Ошибка и управление", "Ошибка e(t), п.п.", 2)):
            app.controller_signal_mode.set(mode)
            app._redraw_signal_mode()
            self.assertEqual(app.disturbance_axis.get_ylabel(), ylabel)
            self.assertEqual(len(app.disturbance_axis.figure.axes), axes)
        before = page.result
        page.columns[0].set(page.columns[1].get())
        page.calculate()
        self.assertIs(page.result, before)
        self.assertIn("три разных столбца", page.summary.get())

    def test_identification_completion_preserves_navigation_and_changed_selection_cancels(self):
        from threading import Event
        from app.identification import identify_step
        from test_identification import experiment
        app = self.app
        page = app.identification_page
        result = dict(identify_step(*experiment()), source="example.csv", columns=("t", "u", "y"), units=("с", "как в файле", "как в файле"))
        app._show_page("identification")
        release = Event()
        def calculate(cancel, progress):
            release.wait(2)
            return result
        app._start_task("Идентификация", calculate, page.accept, "identification")
        app._show_page("disturbances")
        title = app.primary_chart_title.get()
        release.set()
        self.wait_for_task()
        self.assertIs(page.result, result)
        self.assertEqual(app.primary_chart_title.get(), title)
        app._show_page("identification")
        app.comparison_page.add_current()
        self.assertEqual(app.current_page, "comparison")
        self.assertTrue(app._charts_show_comparison)
        release.clear()
        app._start_task("Идентификация", calculate, page.accept, "identification")
        task = app._task
        page.units[0].set("мин")
        self.assertIsNone(app._task)
        self.assertTrue(task.cancel.is_set())
        self.assertIs(page.result, result)
        release.set()
        task.thread.join(2)

    def test_model_edits_keep_the_calculation_and_report_uses_its_graphs(self):
        result = self.app.last_calculation
        self.app.absorber_form.open_model_parameters()
        self.app.absorber_form.model_dialog._variables["eta"].set("70")
        self.app.absorber_form.model_dialog._apply_values()
        self.assertIs(self.app.last_calculation, result)
        self.assertIn("Параметры изменены", self.app.export_panel.export_summary.get())
        self.app.comparison_page.add_current()
        self.assertIn("Теория", self.app.comparison_page.details.get())
        captured = []
        self.app.last_prediction = {"steady": 0.9}

        def writer(path, exported, title, lesson, evaluation, *args, **kwargs):
            captured.append(exported)
            self.assertEqual(title, result["title"])
            self.assertEqual(lesson, result["lesson"])
            self.assertEqual(kwargs["prediction"], result["prediction"])
            self.assertEqual(self.app.response_axis.get_ylabel(), "X, %")
            return Path(path)

        with patch.object(main.filedialog, "asksaveasfilename", return_value=str(Path(self.directory.name) / "report.html")):
            self.app.export_panel.save_lab_report("html", writer)
        self.assertEqual(captured, [result])


if __name__ == "__main__":
    unittest.main()
