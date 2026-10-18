"""Shared result viewer and exports for every registered physical model."""

import tkinter as tk
from tkinter import filedialog, ttk

from app.physical_models import MODELS
from app.model_contract import read_result, write_result
from app.model_output import draw_signals, signal_groups, write_model_report


class ModelResultPage(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, style="CardBody.TFrame", padding=12)
        self.app = app
        self.summary = tk.StringVar(value="Сначала выполните расчёт выбранной модели.")
        self.columnconfigure(0, weight=1)
        ttk.Label(self, text="Результат модели", style="CardTitle.TLabel").grid(sticky="w")
        ttk.Label(self, text="Снимок рассчитанных сигналов. Для сохранения всей работы используйте учебный сеанс.",
                  style="Hint.TLabel", wraplength=320).grid(sticky="ew", pady=8)
        ttk.Label(self, textvariable=self.summary, style="Body.TLabel", wraplength=320).grid(sticky="ew", pady=8)
        self.groups = {}
        for key, label in (("input", "Группа входов"), ("output", "Группа выходов")):
            ttk.Label(self, text=label, style="Body.TLabel").grid(sticky="w", pady=(4, 0))
            variable = tk.StringVar()
            box = ttk.Combobox(self, textvariable=variable, state="readonly")
            box.grid(sticky="ew", pady=3)
            box.bind("<<ComboboxSelected>>", lambda _event: self.draw())
            self.groups[key] = (variable, box)
        self.exports = []
        for label, command in (("Сохранить результат JSON", self.save),
                               ("Открыть результат JSON", self.open),
                               ("Экспорт сигналов CSV", lambda: self.export("csv")),
                               ("Отчёт о модели HTML", lambda: self.export("html")),
                               ("Отчёт о модели PDF", lambda: self.export("pdf"))):
            button = ttk.Button(self, text=label, command=command)
            button.grid(sticky="ew", pady=3)
            if command != self.open:
                self.exports.append(button)

    def current(self):
        model = MODELS.named(self.app.selected_model.get())
        return self.app.model_results.get(model.id)

    def draw(self):
        if self.app.current_page != "model_result":
            return
        model = MODELS.named(self.app.selected_model.get())
        result = self.current()
        for button in self.exports:
            button.configure(state="normal" if result is not None else "disabled")
        self.summary.set("Сначала выполните расчёт выбранной модели." if result is None else
                         f"{model.name}: {len(result['time'])} точек; версия расчёта {model.version}. Событий: {len(result['events'])}. Снимок последнего расчёта; изменения формы в него не входят.")
        app = self.app
        app._remove_controller_axis(); app.absorber.analysis_panel.remove_map_colorbar(); app.absorber.analysis_panel.clear_map_click_callback()
        groups = {} if result is None else signal_groups(result, MODELS)
        for role, axis, canvas in (("input", app.disturbance_axis, app.disturbance_canvas),
                                   ("output", app.response_axis, app.response_canvas)):
            axis.figure.set_layout_engine("constrained")
            axis.set_in_layout(True)
            app._style_axis(axis, "Время, с", "")
            choices = {f"{dict(input='Вход', output='Выход', error='Ошибка', state='Состояние').get(group, group)}, {unit}": values
                       for (group, unit), values in groups.items()
                       if (group == "input") == (role == "input")}
            variable, box = self.groups[role]
            box.configure(values=tuple(choices), state="readonly" if choices else "disabled")
            if variable.get() not in choices:
                variable.set(next(iter(choices), ""))
            signals = choices.get(variable.get(), [])
            if result is not None and signals:
                draw_signals(axis, result, signals)
                app._place_legend_above(axis)
                app._enable_legend_toggles(axis, canvas)
            canvas.draw_idle()
        app.primary_chart_title.set(f"{model.name}: входы")
        app.primary_chart_subtitle.set("Сигналы последнего расчёта")
        app.response_chart_title.set(f"{model.name}: выходы")
        app.response_subtitle.set("Единицы определяет выбранная модель")
        app._charts_show_comparison = False

    def save(self):
        result = self.current()
        if result is None:
            return
        path = filedialog.asksaveasfilename(title="Сохранить результат модели", defaultextension=".json",
                   filetypes=(("Результат модели JSON", "*.json"),), initialfile="dynamics_control_model_result.json")
        if path:
            try:
                write_result(path, result, MODELS)
                self.app._set_status("Результат модели сохранён")
            except (ValueError, OSError) as error:
                self.app._set_status(str(error), error=True)

    def open(self):
        path = filedialog.askopenfilename(title="Открыть результат модели", filetypes=(("Результат модели JSON", "*.json"),))
        if not path:
            return
        try:
            result = read_result(path, MODELS)
        except ValueError as error:
            self.app._set_status(str(error), error=True)
            return
        self.app._cancel_task(announce=False)
        self.app.model_results[result["model_id"]] = result
        self.app.selected_model.set(MODELS.get(result["model_id"]).name)
        self.app._show_page("model_result")
        self.app._set_status("Открыт снимок результата; параметры учебной работы не изменены")

    def export(self, format_name):
        result = self.current()
        if result is None:
            return
        path = filedialog.asksaveasfilename(title="Экспорт результата модели", defaultextension=f".{format_name}",
                   filetypes=((format_name.upper(), f"*.{format_name}"),), initialfile=f"dynamics_control_model_result.{format_name}")
        if path:
            try:
                write_model_report(path, result, format_name, MODELS)
                self.app._set_status("Результат модели экспортирован")
            except (ValueError, OSError) as error:
                self.app._set_status(str(error), error=True)
