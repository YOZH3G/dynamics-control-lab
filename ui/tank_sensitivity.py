"""Tank identification-error form, validated snapshots and grid views."""

import copy
import tkinter as tk
from tkinter import filedialog, ttk

from app.tank_sensitivity import FIELD_DEFAULTS, calculate, parse_fields
from app.tank_sensitivity_exporting import assessment_text, case_label, draw_region, draw_responses, write_report
from app.model_contract import copy_data


VIEW = "Ошибки идентификации"


class TankSensitivity(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, style="CardBody.TFrame")
        self.page = parent
        self.fields = {key: tk.StringVar(value=value) for key, value in FIELD_DEFAULTS.items()}
        self.result = None
        self.selected = tk.StringVar()
        self.summary = tk.StringVar(value="Оцените актуальные K/T/L, затем задайте ошибки оценки и требования.")
        self.columnconfigure(1, weight=1)
        ttk.Label(self, text="Ошибки идентификации и PI", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(self, text="Меняется оценка для настройки PI. Нелинейный бак остаётся прежним. Списки разделяйте ;", style="Hint.TLabel", wraplength=320).grid(row=1, column=0, columnspan=2, sticky="ew", pady=8)
        labels = (("k_errors", "Ошибки K, %"), ("t_errors", "Ошибки T, %"), ("l_errors", "Отклонения L, с"),
                  ("final_error", "Предел |eкон|, м"), ("iae", "Предел IAE, м·с"),
                  ("overshoot_percent", "Предел выброса, %"), ("settling_time", "Предел tуст, с"),
                  ("saturation_duration", "Предел tнас, с"))
        for row, (key, label) in enumerate(labels, 2):
            ttk.Label(self, text=label, style="Body.TLabel").grid(row=row, column=0, sticky="w")
            ttk.Entry(self, textvariable=self.fields[key], width=14).grid(row=row, column=1, sticky="ew")
            self.fields[key].trace_add("write", self.page.settings_changed)
        row = len(labels)+2
        ttk.Button(self, text="Проверить ошибки идентификации", command=self.start).grid(row=row, column=0, columnspan=2, sticky="ew", pady=5)
        self.box = ttk.Combobox(self, textvariable=self.selected, state="disabled")
        self.box.grid(row=row+1, column=0, columnspan=2, sticky="ew")
        self.box.bind("<<ComboboxSelected>>", lambda _event: self.page.draw())
        ttk.Label(self, textvariable=self.summary, style="Body.TLabel", wraplength=320).grid(row=row+2, column=0, columnspan=2, sticky="ew", pady=8)
        self.buttons = []
        for i, (label, fmt) in enumerate((("Ошибки PI: CSV", "csv"), ("Ошибки PI: HTML", "html"), ("Ошибки PI: PDF", "pdf"))):
            button = ttk.Button(self, text=label, command=lambda f=fmt: self.export(f), state="disabled")
            button.grid(row=row+3+i, column=0, columnspan=2, sticky="ew", pady=3); self.buttons.append(button)

    def configuration(self):
        return dict(fields={key: var.get() for key, var in self.fields.items()},
                    controller={key: self.page.fields[key].get() for key in ("gain", "integral_time", "setpoint")})

    def index(self):
        labels = [] if self.result is None else [case_label(c) for c in self.result["cases"]]
        return labels.index(self.selected.get()) if self.selected.get() in labels else 0

    def restore(self, state):
        values = FIELD_DEFAULTS if state is None else state["sensitivity_fields"]
        for key, var in self.fields.items(): var.set(values[key])
        self.result = None if state is None else copy_data(state["sensitivity"])
        self.refresh(0 if state is None else state["selected_sensitivity"])

    def refresh(self, selected=0):
        labels = [] if self.result is None else [case_label(c) for c in self.result["cases"]]
        self.box.configure(values=labels, state="readonly" if labels else "disabled")
        self.selected.set(labels[selected] if labels else "")
        for button in self.buttons: button.configure(state="normal" if labels else "disabled")

    def start(self):
        page = self.page
        if page.fit is None or page.fit["revision"] != page.revision:
            page.fail("Сначала оцените актуальную локальную модель бака."); return
        try:
            p = page.inputs()
            controller = {key: float(page.fields[key].get().replace(",", ".")) for key in ("gain", "integral_time", "setpoint")}
            configuration = self.configuration()
            grid, requirements = parse_fields(configuration["fields"])
        except ValueError as error:
            page.fail(error); return
        local = copy.deepcopy(page.fit)
        revision = page.revision
        def accept(result):
            self.result = result; self.refresh()
            page.view.set(VIEW); page.draw()
            page.app._set_status("Проверка ошибок идентификации выполнена")
        page.perform("Ошибки идентификации", lambda cancel, progress: dict(calculate(p, local, controller, grid, requirements, cancel=cancel, progress=progress),
                     revision=revision, origin=local["origin"], configuration=configuration), accept)

    def draw(self):
        app = self.page.app
        result = self.result
        index = self.index()
        if result is None:
            self.summary.set("Сначала выполните проверку ошибок идентификации.")
        else:
            case = result["cases"][index]
            passed = sum(c["assessment"]["passed"] for c in result["cases"])
            stale = result["revision"] != self.page.revision or result["configuration"] != self.configuration()
            controller = case["run"]["controller"]
            m = case["run"]["metrics"]
            settled = "не подтверждено" if m["settling_time"] is None else f"{m['settling_time']:.6g} с"
            self.summary.set(f"Требования выполнены в {passed} из {len(result['cases'])} узлов. Только проверенная сетка.\n"
                + "\n".join(f"{c['name']}: {assessment_text(c)}" for c in result["controls"]) + "\n"
                + f"Выбранный PI: Kp={controller['gain']:.6g}; Ti={controller['integral_time']:.6g} с.\n"
                + f"|eкон|={abs(m['final_error']):.5g} м; IAE={m['iae']:.5g} м·с; выброс={m['overshoot_percent']:.5g}%.\n"
                + f"tуст={settled}; tнас={m['saturation_duration']:.5g} с.\n{assessment_text(case)}"
                + ("\nСнимок прежних настроек; повторите расчёт." if stale else ""))
        for axis, canvas in ((app.disturbance_axis, app.disturbance_canvas), (app.response_axis, app.response_canvas)):
            axis.figure.set_layout_engine("constrained"); axis.set_in_layout(True)
            app._style_axis(axis, "", "")
        if result is not None:
            draw_region(app.disturbance_axis, result, result["cases"][index]["offsets"][2])
            draw_responses(app.response_axis, result, index)
            for axis, canvas in ((app.disturbance_axis, app.disturbance_canvas), (app.response_axis, app.response_canvas)):
                app._place_legend_above(axis); app._enable_legend_toggles(axis, canvas)
        app.primary_chart_title.set("Проверенные ошибки оценки K/T")
        app.primary_chart_subtitle.set("Выбранный срез ΔL, с: " + (f"{result['cases'][index]['offsets'][2]:g}" if result else "—"))
        app.response_chart_title.set("PI на неизменном нелинейном баке")
        app.response_subtitle.set("Исходный, номинальный и выбранный регуляторы")
        app.disturbance_canvas.draw_idle(); app.response_canvas.draw_idle()
        app._charts_show_comparison = False

    def export(self, fmt):
        if self.result is None: return
        path = filedialog.asksaveasfilename(title="Ошибки идентификации и PI", defaultextension=f".{fmt}",
                filetypes=((fmt.upper(), f"*.{fmt}"),), initialfile=f"tank_identification_errors.{fmt}")
        if path:
            try:
                snapshot = copy_data(self.result)
                snapshot["exported_stale"] = snapshot["revision"] != self.page.revision or snapshot["configuration"] != self.configuration()
                write_report(path, snapshot, self.index(), fmt)
                self.page.app._set_status("Отчёт об ошибках идентификации сохранён")
            except (ValueError, OSError) as error: self.page.fail(error)
