"""Page of an object described in a file: free response, K, T, L, PI settings and a PI run."""

from app.identification import quality_text, uncertainty_text
from app.model_contract import ModelRegistry, copy_data
from app.object_file import DESCRIBED, VIEWS
from ui.object_page import ObjectPage

STATUS = dict(settled="установилось", horizon="не установилось за горизонт")


class ResponsePage(ObjectPage):
    fit_missing = "Сначала оцените K, T, L по актуальному отклику."

    def __init__(self, parent, app, model):
        self.description = DESCRIBED[model.id]
        super().__init__(parent, app, model, self.description.field_defaults(), VIEWS,
                         "Задайте параметры и рассчитайте свободный отклик на ступень входа.")
        d = self.description
        self.chart_label = d.input_info[0]
        self.object_name = f"объекта «{d.name}»"
        self.identify_response = d.identify
        self.pi_settings = d.pi_settings
        self.result = self.pi = None
        parameters = tuple(p.key for p in model.parameters)
        self.heading(d.name, d.hint)
        self.add_fields(parameters)
        self.add_button("Свободный отклик", self.calculate, pady=4)
        self.hint("Идентификация: свободный отклик на ступень входа, к измерению добавляется шум датчика.", pady=(4, 2))
        self.add_fields(((("noise", f"Шум датчика σ, {d.unit}")), ("seed", "Число для генератора")))
        for text, command in (("Оценить K, T, L по отклику", self.identify), ("Оценить неопределённость K, T, L", self.estimate_uncertainty),
                              ("Настроить PI по модели", self.tune)):
            self.add_button(text, command)
        self.add_fields((("setpoint", f"Задание, {d.unit}"), ("gain", f"Kp, {d.input_info[1]}/{d.unit}"), ("integral_time", "Ti, с")))
        self.add_button("Рассчитать с PI", self.calculate_pi, pady=4)
        self.add_view_list(VIEWS)
        self.add_summary()
        self.add_fields((("title", "Название опыта"), ("prediction", "Прогноз"), ("conclusion", "Вывод")), width=None)
        self.hint("Графики, JSON, CSV и отчёты HTML/PDF доступны в разделе «Результат модели».")
        for text, command in (("Сохранить сеанс JSON", app._save_comparison_session), ("Открыть сеанс JSON", app._open_comparison_session)):
            self.add_button(text, command)
        self.trace(parameters, self.changed)
        self.trace(("noise", "seed", "setpoint", "gain", "integral_time"), self.settings_changed)
        self.view.trace_add("write", lambda *_: self.draw())

    def latest_result(self):
        return self.pi or self.result

    def common_result(self, run):
        return ModelRegistry((self.model,)).result(self.model.id, run)

    def inputs(self):
        try:
            values = {p.key: float(self.fields[p.key].get().replace(",", ".")) for p in self.model.parameters}
        except ValueError as error:
            raise ValueError(f"{self.description.name}: заполните числовые параметры.") from error
        return self.description.values(values)

    def controller(self):
        return {key: float(self.fields[key].get().replace(",", ".")) for key in ("gain", "integral_time", "setpoint")}

    def run(self, name, controller, accept):
        try:
            p = self.inputs()
            controller = controller() if controller else None
        except ValueError as error:
            self.fail(error if self.description.name in str(error) else "PI: Kp, Ti и задание должны быть числами."); return
        revision, simulate = self.revision, self.description.simulate
        self.perform(name, lambda cancel, progress: dict(
            simulate(p, controller=controller, cancel=cancel, progress=progress), revision=revision), accept)

    def calculate(self):
        self.run("Свободный отклик", None, self.accept_result)

    def calculate_pi(self):
        self.run("Опыт с PI", self.controller, self.accept_pi)

    def accept_result(self, result):
        self.result = result
        self.app.model_results[self.model.id] = self.common_result(result)
        self.view.set("Свободный отклик")
        self.draw()
        self.app._set_status("Свободный отклик рассчитан")

    def accept_pi(self, result):
        self.pi = result
        self.app.model_results[self.model.id] = self.common_result(result)
        self.view.set("PI")
        self.draw()
        self.app._set_status("Опыт с PI рассчитан")

    def fit_text(self, fit):
        d = self.description
        return (f"Оценка по отклику: K={fit['gain']:.4g} {d.unit}/{d.input_info[1]}, T={fit['time_constant']:.4g} с, "
                f"L={fit['delay']:.4g} с; RMSE {fit['rmse']:.3g} {d.unit} при шуме σ={fit['noise']:g} {d.unit}.\n" + quality_text(fit)
                + ("\n" + uncertainty_text(fit["uncertainty"]) if fit.get("uncertainty") else ""))

    def describe(self, record):
        d = self.description
        output = record[d.output_state]
        text = f"Выход «{d.state_info[d.output_state][0]}»: исходное значение {output[0]:.4g} {d.unit}, конечное {output[-1]:.4g} {d.unit}."
        if record["controller"] is not None:
            m, c = record["metrics"], record["controller"]
            settling = "—" if m["settling_time"] is None else f"{m['settling_time']:.4g} с"
            text += (f" PI: Kp={c['gain']:g}, Ti={c['integral_time']:g} с, задание {c['setpoint']:g} {d.unit}; конечная ошибка "
                     f"{m['final_error']:.3g}; IAE {m['iae']:.4g}; перерегулирование {m['overshoot_percent']:.3g}%; установление "
                     f"{settling}; насыщение {m['saturation_duration']:.4g} с; {STATUS[m['status']]}.")
        return text

    def draw(self):
        if getattr(self.app, "current_page", None) != self.kind:
            return
        app, d = self.app, self.description
        app._remove_controller_axis(); app.analysis_panel.remove_map_colorbar(); app.analysis_panel.clear_map_click_callback()
        app._style_axis(app.disturbance_axis, "Время, с", f"{d.input_info[0]}, {d.input_info[1]}")
        app._style_axis(app.response_axis, "Время, с", f"{d.state_info[d.output_state][0]}, {d.unit}")
        view = self.view.get()
        record = self.pi if view == "PI" else self.fit if view == "Идентификация" else self.result
        if record is not None and view == "Идентификация":
            app.disturbance_axis.plot(record["time"], record["input"], label=d.input_info[0])
            app.response_axis.plot(record["time"], record["output"], label="Измерение с шумом")
            app.response_axis.plot(record["time"], record["model"], label="Модель K, T, L")
            stale = not self.fit_current()
            self.summary.set(("Снимок прежних настроек; повторите расчёт. " if stale else "") + self.fit_text(record))
        elif record is not None:
            app.disturbance_axis.plot(record["time"], record[d.input], label=d.input_info[0])
            app.response_axis.plot(record["time"], record[d.output_state], label=d.state_info[d.output_state][0])
            app.response_axis.plot(record["time"], record["measured"], linestyle=":", label=d.measured_label)
            stale = record["revision"] != self.revision
            if record["controller"] is not None:
                app.response_axis.axhline(record["controller"]["setpoint"], color="#64748B", linestyle="--", label="Задание")
                try:
                    stale = stale or self.controller() != record["controller"]
                except ValueError:
                    stale = True
            self.summary.set(("Снимок прежних настроек; повторите расчёт. " if stale else "") + self.describe(record))
        app.primary_chart_title.set(d.input_info[0]); app.primary_chart_subtitle.set(f"Вход объекта, {d.input_info[1]}")
        app.response_chart_title.set(f"{d.name}: {view}")
        app.response_subtitle.set(d.context)
        for axis, canvas in ((app.disturbance_axis, app.disturbance_canvas), (app.response_axis, app.response_canvas)):
            axis.figure.set_layout_engine("constrained")
            if axis.lines:
                app._place_legend_above(axis)
                app._enable_legend_toggles(axis, canvas)
            canvas.draw_idle()
        app._charts_show_comparison = False

    def capture(self, *, copy_results=True):
        snapshot = dict(model_id=self.model.id, fields={k: v.get() for k, v in self.fields.items()}, revision=self.revision,
                        view=self.view.get(), result=self.result, pi=self.pi, fit=self.fit)
        return copy_data(snapshot) if copy_results else snapshot

    def restore(self, state):
        state = self.description.validate_state(state)
        defaults = self.description.field_defaults()
        self.restoring = True
        try:
            for key, variable in self.fields.items():
                variable.set(defaults[key] if state is None else state["fields"][key])
            self.revision = 0 if state is None else state["revision"]
            self.result, self.pi, self.fit = (None, None, None) if state is None else (state["result"], state["pi"], state.get("fit"))
            self.view.set(VIEWS[0] if state is None else state["view"])
        finally:
            self.restoring = False
        self.summary.set("Работа с объектом восстановлена." if state else "Задайте параметры и рассчитайте свободный отклик на ступень входа.")
        self.draw()
