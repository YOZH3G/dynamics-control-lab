"""Stirred tank with a heater: form, free response, PI and charts; state is kept apart from other models."""

from app.heater import (FIELD_DEFAULTS, MODEL_ID, MODEL_VERSION, VIEWS, equilibrium_temperature, identify, pi_tuning,
                        time_constant, validate_inputs, validate_state)
from app.identification import quality_text, uncertainty_text
from app.model_contract import copy_data
from app.physical_models import MODELS, run_model
from ui.object_page import ObjectPage

PARAMETERS = tuple(p.key for p in MODELS.get(MODEL_ID).parameters)
STATUS = dict(physical_stop="закипание", unreachable="задание недостижимо", settled="установилось", horizon="не установилось за горизонт")


class HeaterPage(ObjectPage):
    chart_label = "Мощность"
    object_name = "нагревателя"
    fit_missing = "Сначала оцените K, T, L по актуальному отклику."
    identify_response = staticmethod(identify)
    pi_settings = staticmethod(pi_tuning)

    def __init__(self, parent, app):
        super().__init__(parent, app, MODEL_ID, FIELD_DEFAULTS, VIEWS,
                         "Задайте параметры ёмкости и рассчитайте свободный отклик на ступень.")
        self.result = self.pi = None
        self.heading("Ёмкость с нагревателем", "M·c·dT/dt = W·c·(Tвх − T) + P − UA·(T − Tср). Опыт начинается в равновесии при P0.")
        self.add_fields(PARAMETERS)
        self.add_button("Свободный отклик", self.calculate, pady=4)
        self.hint("Идентификация: свободный отклик на ступень одной мощности, к измеренной температуре добавляется шум датчика.",
                  pady=(4, 2))
        self.add_fields((("noise", "Шум датчика σ, °C"), ("seed", "Число для генератора")))
        for text, command in (("Оценить K, T, L по отклику", self.identify), ("Оценить неопределённость K, T, L", self.estimate_uncertainty),
                              ("Настроить PI по модели", self.tune)):
            self.add_button(text, command)
        self.add_fields((("setpoint", "Задание T, °C"), ("gain", "Kp, кВт/К"), ("integral_time", "Ti, с")))
        self.add_button("Рассчитать с PI", self.calculate_pi, pady=4)
        self.add_view_list(VIEWS)
        self.add_summary()
        self.add_fields((("title", "Название опыта"), ("prediction", "Прогноз"), ("conclusion", "Вывод")), width=None)
        self.hint("Графики, JSON, CSV и отчёты HTML/PDF доступны в разделе «Результат модели».")
        for text, command in (("Сохранить сеанс JSON", app._save_comparison_session), ("Открыть сеанс JSON", app._open_comparison_session)):
            self.add_button(text, command)
        self.trace((*PARAMETERS, "noise", "seed"), self.changed)
        # Controller settings do not change the experiment: an identified model stays current.
        self.trace(("setpoint", "gain", "integral_time"), self.settings_changed)
        self.view.trace_add("write", lambda *_: self.draw())

    def latest_result(self):
        """Native run shown on the common result page after a session is restored."""
        return self.pi or self.result

    def controller(self):
        return {key: float(self.fields[key].get().replace(",", ".")) for key in ("gain", "integral_time", "setpoint")}

    def inputs(self):
        try:
            values = {key: float(self.fields[key].get().replace(",", ".")) for key in PARAMETERS}
        except ValueError as error:
            raise ValueError("Нагреватель: заполните числовые параметры.") from error
        return validate_inputs(values)

    def run(self, name, request, accept):
        revision = self.revision
        self.perform(name, lambda cancel, progress: dict(
            run_model(MODEL_ID, request, cancel=cancel, progress=progress), revision=revision), accept)

    def calculate(self):
        try:
            p = self.inputs()
        except ValueError as error:
            self.fail(error); return
        self.run("Отклик нагревателя", dict(parameters=p), self.accept_result)

    def calculate_pi(self):
        try:
            p = self.inputs()
            controller = self.controller()
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

    def fit_text(self, fit):
        physical = fit["physical"]
        return (f"Оценка по отклику: K={fit['gain']:.4g} К/кВт, T={fit['time_constant']:.4g} с, L={fit['delay']:.4g} с; "
                f"RMSE {fit['rmse']:.3g} К при шуме σ={fit['noise']:g} °C. Физические значения: K={physical['gain']:.4g} К/кВт, "
                f"τ={physical['time_constant']:.4g} с, запаздывание датчика {physical['delay']:g} с.\n" + quality_text(fit)
                + ("\n" + uncertainty_text(fit["uncertainty"]) if fit.get("uncertainty") else ""))

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
        view = self.view.get()
        record = self.pi if view == "PI" else self.fit if view == "Идентификация" else self.result
        if record is not None and view == "Идентификация":
            app.disturbance_axis.plot(record["time"], record["input"], label="Мощность")
            app.response_axis.plot(record["time"], record["output"], label="Измеренная с шумом")
            app.response_axis.plot(record["time"], record["model"], label="Модель K, T, L")
            stale = record["revision"] != self.revision
            self.summary.set(("Снимок прежних настроек; повторите расчёт. " if stale else "") + self.fit_text(record))
        elif record is not None:
            app.disturbance_axis.plot(record["time"], record["power"], label="Мощность")
            app.disturbance_axis.axhline(record["parameters"]["power_limit"], color="#B91C1C", linestyle="--", label="Pmax")
            app.response_axis.plot(record["time"], record["temperature"], label="Температура в ёмкости")
            app.response_axis.plot(record["time"], record["measured"], linestyle=":", label="Измеренная")
            if record["controller"] is not None:
                app.response_axis.axhline(record["controller"]["setpoint"], color="#64748B", linestyle="--", label="Задание")
            stale = record["revision"] != self.revision
            if record["controller"] is not None:
                try:
                    stale = stale or self.controller() != record["controller"]
                except ValueError:
                    stale = True
            self.summary.set(("Снимок прежних настроек; повторите расчёт. " if stale else "") + self.describe(record))
        app.primary_chart_title.set("Мощность нагревателя"); app.primary_chart_subtitle.set("Мощность в кВт; ступени температуры входа и расхода — в параметрах")
        app.response_chart_title.set(f"Нагреватель: {view}")
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
                        revision=self.revision, view=self.view.get(), result=self.result, pi=self.pi, fit=self.fit)
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
            self.fit = None if state is None else state.get("fit")
            self.view.set(VIEWS[0] if state is None else state["view"])
        finally:
            self.restoring = False
        self.summary.set("Работа с нагревателем восстановлена." if state else "Задайте параметры ёмкости и рассчитайте свободный отклик на ступень.")
        self.draw()
