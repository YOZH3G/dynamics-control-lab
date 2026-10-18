"""Identification estimates and a separate physical absorber snapshot."""

import copy
import tkinter as tk
from tkinter import filedialog, ttk

from app.absorber_sensitivity import FIELD_DEFAULTS, calculate, parse_fields
from app.absorber_sensitivity_exporting import assessment_text, draw_responses, write_report
from app.tank_sensitivity_exporting import case_label, draw_region
from app.model_contract import copy_data


VIEW = "Ошибки на абсорбере"


class AbsorberSensitivity(ttk.Frame):
    def __init__(self, page):
        super().__init__(page, style="CardBody.TFrame")
        self.page = page
        self.fields = {key: tk.StringVar(value=value) for key, value in FIELD_DEFAULTS.items()}
        self.result = None
        self.selected = tk.StringVar()
        self.summary = tk.StringVar(value="Оцените CSV для η → выбранная концентрация; включите PI в настройках абсорбера.")
        self.columnconfigure(1, weight=1)
        ttk.Label(self, text="Ошибки оценки: абсорбер", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(self, text="K/T/L из CSV настраивают PI. Объект, возмущения, задание и ограничения взяты из текущих настроек абсорбера и одинаковы во всех опытах. Списки разделяйте ;",
                  style="Hint.TLabel", wraplength=320).grid(row=1, column=0, columnspan=2, sticky="ew", pady=8)
        labels = (("k_errors", "Ошибки K, %"), ("t_errors", "Ошибки T, %"), ("l_errors", "Отклонения L, с"),
                  ("final_error", "Предел |eкон|, доля"), ("iae", "Предел IAE, доля·с"),
                  ("overshoot_percent", "Предел выброса, %"), ("settling_time", "Предел tуст, с"),
                  ("saturation_duration", "Предел tнас, с"))
        for row, (key, label) in enumerate(labels, 2):
            ttk.Label(self, text=label, style="Body.TLabel").grid(row=row, column=0, sticky="w")
            ttk.Entry(self, textvariable=self.fields[key], width=14).grid(row=row, column=1, sticky="ew")
            self.fields[key].trace_add("write", self.changed)
        ttk.Button(self, text="Проверить PI на абсорбере", command=self.start).grid(row=10, column=0, columnspan=2, sticky="ew", pady=5)
        self.box = ttk.Combobox(self, textvariable=self.selected, state="disabled")
        self.box.grid(row=11, column=0, columnspan=2, sticky="ew")
        self.box.bind("<<ComboboxSelected>>", lambda _event: self.page.draw())
        ttk.Label(self, textvariable=self.summary, style="Body.TLabel", wraplength=320).grid(row=12, column=0, columnspan=2, sticky="ew", pady=8)
        self.buttons = []
        for row, fmt in enumerate(("csv", "html", "pdf"), 13):
            button = ttk.Button(self, text=f"Абсорбер: ошибки PI {fmt.upper()}", command=lambda f=fmt: self.export(f), state="disabled")
            button.grid(row=row, column=0, columnspan=2, sticky="ew", pady=3); self.buttons.append(button)

    def configuration(self):
        return {key: var.get() for key, var in self.fields.items()}

    def changed(self, *_):
        self.page.app.absorber._invalidate_task()
        if self.page.view.get() == VIEW: self.draw()

    def index(self):
        labels = [] if self.result is None else [case_label(c) for c in self.result["cases"]]
        return labels.index(self.selected.get()) if self.selected.get() in labels else 0

    def stale(self):
        r, page, app = self.result, self.page, self.page.app
        return r is not None and (r["origin"]["data_revision"] != page.generation
            or r["origin"]["result_revision"] != page.result_revision
            or r["fields"] != self.configuration()
            or app.absorber._normalized_input_state(r["input_state"]) != app.absorber._normalized_input_state(app.absorber._capture_input_state()))

    def refresh(self, selected=0):
        labels = [] if self.result is None else [case_label(c) for c in self.result["cases"]]
        self.box.configure(values=labels, state="readonly" if labels else "disabled")
        self.selected.set(labels[selected] if labels else "")
        for button in self.buttons: button.configure(state="normal" if labels else "disabled")

    def capture(self, *, copy_results=True):
        return dict(fields=self.configuration(), result=copy_data(self.result) if copy_results else self.result,
                    selected=self.index(), stale=self.stale())

    def restore(self, state):
        for key, var in self.fields.items(): var.set(state["fields"][key])
        self.result = copy_data(state["result"])
        self.refresh(state["selected"])

    def start(self):
        page, app = self.page, self.page.app
        if not page.result_current or page.metadata["input_role"].get() != "η, доля":
            page.summary.set("Нужна актуальная оценка CSV с подтверждённым входом η, доля."); return
        expected_role = "Xог, доля" if app.absorber.absorber_form.chain == "lean_gas" else "Xна, доля"
        if page.metadata["output_role"].get() != expected_role:
            page.summary.set("Выход CSV должен совпадать с выбранной цепью абсорбера: " + expected_role); return
        try:
            request = dict(chain=app.absorber.absorber_form.chain, parameters=copy.deepcopy(app.absorber.absorber_form.model_values),
                component=app.absorber.absorber_form.read_fraction(app.absorber.absorber_form.component_enabled.get(), app.absorber.absorber_form.component_value, app.absorber.absorber_form.component_error, app.absorber.absorber_form.component_entry),
                flow=app.absorber.absorber_form.read_fraction(app.absorber.absorber_form.flow_enabled.get(), app.absorber.absorber_form.flow_value, app.absorber.absorber_form.flow_error, app.absorber.absorber_form.flow_entry),
                dynamics=app.absorber.absorber_form.read_dynamic_parameters(), controller=app.absorber.absorber_form.read_controller_parameters())
            if request["controller"] is None or request["controller"]["controller_type"] != "PI":
                raise ValueError("Включите PI в настройках абсорбера и задайте концентрацию, отличную от исходной.")
            fields = self.configuration()
            grid, requirements = parse_fields(fields)
        except ValueError as error:
            page.summary.set(str(error)); return
        origin = page.origin()
        local = {key: float(page.result[key]) for key in ("gain", "time_constant", "delay")}
        state = app.absorber._capture_input_state()
        def accept(result):
            self.result = result; self.refresh(); page.view.set(VIEW); page.draw()
            app._set_status("Проверка ошибок оценки на абсорбере выполнена")
        app._start_task("Ошибки оценки: абсорбер", lambda cancel, progress: dict(
            calculate(request, local, grid, requirements, cancel=cancel, progress=progress),
            origin=origin, input_state=state, fields=fields), accept, "identification")

    def draw(self):
        app, result = self.page.app, self.result
        app._remove_controller_axis()
        app.signal_mode_box.grid_remove()
        for axis in (app.disturbance_axis, app.response_axis):
            axis.figure.set_layout_engine("constrained"); axis.set_in_layout(True); app._style_axis(axis, "", "")
        if result is not None:
            case = result["cases"][self.index()]; m = case["metrics"]; c = case["run"]["configuration"]["controller"]
            passed = sum(c["assessment"]["passed"] for c in result["cases"])
            settled = "не подтверждено" if m["settling_time"] is None else f"{m['settling_time']:.5g} с"
            self.summary.set(f"Требования выполнены в {passed} из {len(result['cases'])} узлов. Только проверенная сетка.\n"
                + "\n".join(f"{c['name']}: {assessment_text(c)}" for c in result["controls"]) + "\n"
                + f"Выбранный PI: Kp={c['controller_gain']:.5g}; Ti={c['integral_time']:.5g} с.\n"
                + f"|eкон|={abs(m['final_error']):.5g}; IAE={m['iae']:.5g} доля·с; выброс={m['overshoot_percent']:.5g}%.\n"
                + f"tуст={settled}; tнас={m['saturation_duration']:.5g} с.\n{assessment_text(case)}"
                + ("\nСнимок прежних настроек; повторите расчёт." if self.stale() else ""))
            draw_region(app.disturbance_axis, result, case["offsets"][2]); draw_responses(app.response_axis, result, self.index())
            for axis, canvas in ((app.disturbance_axis, app.disturbance_canvas), (app.response_axis, app.response_canvas)):
                app._place_legend_above(axis); app._enable_legend_toggles(axis, canvas)
        app.primary_chart_title.set("Проверенные ошибки оценки K/T")
        app.primary_chart_subtitle.set("Выбранный срез ΔL, с: " + (f"{result['cases'][self.index()]['offsets'][2]:g}" if result else "—"))
        app.response_chart_title.set("PI на неизменном абсорбере")
        app.response_subtitle.set("Исходный, номинальный и выбранный PI; концентрация в долях")
        app.disturbance_canvas.draw_idle(); app.response_canvas.draw_idle(); app._charts_show_comparison = False
        app._refresh_chart_modes()

    def export(self, fmt):
        if self.result is None: return
        path = filedialog.asksaveasfilename(title="Ошибки оценки на абсорбере", defaultextension=f".{fmt}", initialfile=f"absorber_identification_errors.{fmt}")
        if path:
            try:
                write_report(path, dict(copy_data(self.result), exported_stale=self.stale()), self.index(), fmt)
                self.page.app._set_status("Отчёт об ошибках оценки сохранён")
            except (ValueError, OSError) as error: self.page.summary.set(str(error))
