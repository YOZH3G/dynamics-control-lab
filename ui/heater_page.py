"""Stirred tank with a heater: form, free response, PI and charts; state is kept apart from other models."""

import tkinter as tk
from tkinter import ttk

from app.heater import (FIELD_DEFAULTS, MODEL_ID, MODEL_VERSION, VIEWS, equilibrium_temperature, time_constant,
                        validate_inputs, validate_state)
from app.model_contract import copy_data
from app.physical_models import MODELS, run_model

LABELS = (("mass", "Масса жидкости M, кг"), ("heat_capacity", "Теплоёмкость c, кДж/(кг·К)"),
          ("flow", "Расход W, кг/с"), ("inlet_temperature", "Температура входа Tвх, °C"),
          ("loss_coefficient", "Теплопотери UA, кВт/К"), ("ambient_temperature", "Температура среды Tср, °C"),
          ("power_limit", "Предел мощности Pmax, кВт"), ("power", "Исходная мощность P0, кВт"),
          ("step_time", "Время ступени, с"), ("power_step", "Ступень мощности, кВт"),
          ("inlet_step", "Ступень Tвх, °C"), ("flow_step", "Ступень расхода, кг/с"),
          ("sensor_delay", "Запаздывание датчика, с"), ("horizon", "Горизонт, с"), ("sample_time", "Период расчёта, с"))
STATUS = dict(physical_stop="закипание", unreachable="задание недостижимо", settled="установилось", horizon="не установилось за горизонт")


class HeaterPage(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, style="CardBody.TFrame", padding=12)
        self.app = app
        self.fields = {k: tk.StringVar(value=v) for k, v in FIELD_DEFAULTS.items()}
        self.view = tk.StringVar(value=VIEWS[0])
        self.summary = tk.StringVar(value="Задайте параметры ёмкости и рассчитайте свободный отклик на ступень.")
        self.result = self.pi = None
        self.revision = 0
        self.restoring = False
        self.columnconfigure(1, weight=1)
        ttk.Label(self, text="Ёмкость с нагревателем", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(self, text="M·c·dT/dt = W·c·(Tвх − T) + P − UA·(T − Tср). Опыт начинается в равновесии при P0.",
                  style="Hint.TLabel", wraplength=320).grid(row=1, column=0, columnspan=2, sticky="ew", pady=8)
        row = 2
        for key, label in LABELS:
            ttk.Label(self, text=label, style="Body.TLabel").grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(self, textvariable=self.fields[key], width=14).grid(row=row, column=1, sticky="ew")
            row += 1
        ttk.Button(self, text="Свободный отклик", command=self.calculate).grid(row=row, column=0, columnspan=2, sticky="ew", pady=4); row += 1
        for key, label in (("setpoint", "Задание T, °C"), ("gain", "Kp, кВт/К"), ("integral_time", "Ti, с")):
            ttk.Label(self, text=label, style="Body.TLabel").grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(self, textvariable=self.fields[key], width=14).grid(row=row, column=1, sticky="ew")
            row += 1
        ttk.Button(self, text="Рассчитать с PI", command=self.calculate_pi).grid(row=row, column=0, columnspan=2, sticky="ew", pady=4); row += 1
        ttk.Combobox(self, textvariable=self.view, values=VIEWS, state="readonly").grid(row=row, column=0, columnspan=2, sticky="ew"); row += 1
        ttk.Label(self, textvariable=self.summary, style="Body.TLabel", wraplength=320).grid(row=row, column=0, columnspan=2, sticky="ew", pady=8); row += 1
        for key, label in (("title", "Название опыта"), ("prediction", "Прогноз"), ("conclusion", "Вывод")):
            ttk.Label(self, text=label, style="Body.TLabel").grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(self, textvariable=self.fields[key]).grid(row=row, column=1, sticky="ew")
            row += 1
        ttk.Label(self, text="Графики, JSON, CSV и отчёты HTML/PDF доступны в разделе «Результат модели».",
                  style="Hint.TLabel", wraplength=320).grid(row=row, column=0, columnspan=2, sticky="ew", pady=8); row += 1
        for text, command in (("Сохранить сеанс JSON", app._save_comparison_session), ("Открыть сеанс JSON", app._open_comparison_session)):
            ttk.Button(self, text=text, command=command).grid(row=row, column=0, columnspan=2, sticky="ew", pady=3); row += 1
        for key in LABELS:
            self.fields[key[0]].trace_add("write", self.changed)
        for key in ("setpoint", "gain", "integral_time"):
            self.fields[key].trace_add("write", self.changed)
        self.view.trace_add("write", lambda *_: self.draw())

    def signature(self):
        return (self.revision, tuple(v.get() for v in self.fields.values()))

    def changed(self, *_):
        if self.restoring:
            return
        self.revision += 1
        if getattr(self.app, "_task", None) is not None and self.app._task_kind == "heater":
            self.app._cancel_task()
        self.draw()

    def inputs(self):
        try:
            values = {key: float(self.fields[key].get().replace(",", ".")) for key, _ in LABELS}
        except ValueError as error:
            raise ValueError("Нагреватель: заполните числовые параметры.") from error
        return validate_inputs(values)

    def fail(self, error):
        self.summary.set(str(error))
        self.app._set_status(str(error), error=True)

    def run(self, name, request, accept):
        revision = self.revision
        self.app._start_task(name, lambda cancel, progress: dict(
            run_model(MODEL_ID, request, cancel=cancel, progress=progress), revision=revision), accept, "heater")

    def calculate(self):
        try:
            p = self.inputs()
        except ValueError as error:
            self.fail(error); return
        self.run("Отклик нагревателя", dict(parameters=p), self.accept_result)

    def calculate_pi(self):
        try:
            p = self.inputs()
            controller = {key: float(self.fields[key].get().replace(",", ".")) for key in ("gain", "integral_time", "setpoint")}
        except ValueError as error:
            self.fail(error if "Нагреватель" in str(error) else "PI: Kp, Ti и задание должны быть числами."); return
        self.run("PI нагревателя", dict(parameters=p, controller=controller), self.accept_pi)

    def accept_result(self, result):
        self.result = result
        self.app.model_results[MODEL_ID] = MODELS.result(MODEL_ID, result)
        self.view.set("Свободный отклик")
        self.draw()
        self.app._set_status("Отклик нагревателя рассчитан")

    def accept_pi(self, result):
        self.pi = result
        self.app.model_results[MODEL_ID] = MODELS.result(MODEL_ID, result)
        self.view.set("PI")
        self.draw()
        self.app._set_status("Опыт с PI рассчитан")

    def describe(self, record):
        p = record["parameters"]
        end = equilibrium_temperature(p, p["power"] + p["power_step"], p["flow"] + p["flow_step"], p["inlet_temperature"] + p["inlet_step"])
        text = (f"Исходное равновесие T0={record['initial_temperature']:.4g} °C. τ = M·c/(W·c + UA) = {time_constant(p):.4g} с; "
                f"усиление по мощности {1 / (p['flow'] * p['heat_capacity'] + p['loss_coefficient']):.4g} К/кВт; "
                f"запаздывание датчика {p['sensor_delay']:g} с.")
        if record["controller"] is None:
            text += f" После ступени равновесие {end:.4g} °C."
        else:
            m = record["metrics"]
            settling = "—" if m["settling_time"] is None else f"{m['settling_time']:.4g} с"
            text += (f" PI: Kp={record['controller']['gain']:g} кВт/К, Ti={record['controller']['integral_time']:g} с, задание "
                     f"{record['controller']['setpoint']:g} °C; конечная ошибка {m['final_error']:.3g} К; IAE {m['iae']:.4g} К·с; "
                     f"перерегулирование {m['overshoot_percent']:.3g}%; установление {settling}; насыщение {m['saturation_duration']:.4g} с; "
                     f"{STATUS[m['status']]}.")
        if record["event"]:
            text += f" Опыт остановлен: вода закипела при t={record['event']['time']:.4g} с."
        return text

    def draw(self):
        if getattr(self.app, "current_page", None) != "heater":
            return
        app = self.app
        app._remove_controller_axis(); app.analysis_panel.remove_map_colorbar(); app.analysis_panel.clear_map_click_callback()
        app._style_axis(app.disturbance_axis, "Время, с", "Мощность, кВт")
        app._style_axis(app.response_axis, "Время, с", "Температура, °C")
        record = self.pi if self.view.get() == "PI" else self.result
        if record is not None:
            app.disturbance_axis.plot(record["time"], record["power"], label="Мощность")
            app.disturbance_axis.axhline(record["parameters"]["power_limit"], color="#B91C1C", linestyle="--", label="Pmax")
            app.response_axis.plot(record["time"], record["temperature"], label="Температура в ёмкости")
            app.response_axis.plot(record["time"], record["measured"], linestyle=":", label="Измеренная")
            if record["controller"] is not None:
                app.response_axis.axhline(record["controller"]["setpoint"], color="#64748B", linestyle="--", label="Задание")
            stale = record["revision"] != self.revision
            self.summary.set(("Снимок прежних настроек; повторите расчёт. " if stale else "") + self.describe(record))
        app.primary_chart_title.set("Мощность нагревателя"); app.primary_chart_subtitle.set("Мощность в кВт; ступени температуры входа и расхода — в параметрах")
        app.response_chart_title.set(f"Нагреватель: {self.view.get()}")
        app.response_subtitle.set("Энергетический баланс перемешиваемой ёмкости; датчик с чистым запаздыванием")
        for axis, canvas in ((app.disturbance_axis, app.disturbance_canvas), (app.response_axis, app.response_canvas)):
            axis.figure.set_layout_engine("constrained")
            if axis.lines:
                app._place_legend_above(axis)
                app._enable_legend_toggles(axis, canvas)
            canvas.draw_idle()
        app._charts_show_comparison = False

    def capture(self, *, copy_results=True):
        snapshot = dict(model_id=MODEL_ID, model_version=MODEL_VERSION, fields={k: v.get() for k, v in self.fields.items()},
                        revision=self.revision, view=self.view.get(), result=self.result, pi=self.pi)
        return copy_data(snapshot) if copy_results else snapshot

    def restore(self, state):
        state = validate_state(state)
        self.restoring = True
        try:
            for key, variable in self.fields.items():
                variable.set(FIELD_DEFAULTS[key] if state is None else state["fields"][key])
            self.revision = 0 if state is None else state["revision"]
            self.result = None if state is None else state["result"]
            self.pi = None if state is None else state["pi"]
            self.view.set(VIEWS[0] if state is None else state["view"])
        finally:
            self.restoring = False
        self.summary.set("Работа с нагревателем восстановлена." if state else "Задайте параметры ёмкости и рассчитайте свободный отклик на ступень.")
        self.draw()
