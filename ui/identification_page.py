"""Experiment import and fitting controls; calculation is owned by the app worker."""

import tkinter as tk
import copy
import uuid
import numpy as np
from pathlib import Path
from tkinter import filedialog, ttk

from app.identification import experiment_columns, export_identification, identify_step, read_experiment, validate_experiment_state, quality_text
from app.experiment_control import validate_step, compare_pi
from .absorber_sensitivity import AbsorberSensitivity, VIEW as ABSORBER_VIEW


def settling_text(value):
    return "не подтверждено" if value is None else f"{value:.4g} с"


class IdentificationPage(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, style="CardBody.TFrame", padding=12)
        self.app = app
        self.headers, self.rows = [], []
        self.generation = 0
        self.result = None
        self.result_current = False
        self.source = ""
        self.experiment_id = uuid.uuid4().hex
        self.result_revision = 0
        self.applied_origin = None
        self.validation = None
        self.pi_result = None
        self.metadata = {key: tk.StringVar(value="Не определён" if key in ("input_role", "output_role") else "")
                         for key in ("title", "description", "input_role", "output_role", "regime", "comment", "prediction", "explanation", "conclusion")}
        self.same_signals = tk.BooleanVar(value=False)
        self.pi_settings = {key: tk.StringVar(value=value) for key, value in
                            (("setpoint", "0.4"), ("horizon", "100"), ("gain", "1"), ("integral_time", "8"))}
        self.columns = [tk.StringVar() for _ in range(3)]
        self.units = [tk.StringVar(value=value) for value in ("с", "как в файле", "как в файле")]
        self.delimiter = tk.StringVar(value=";")
        self.view = tk.StringVar(value="Отклик")
        self.summary = tk.StringVar(value="Импортируйте CSV с одной постоянной ступенью. Нужны минимум 5 точек до изменения и 12 после него.")
        self.columnconfigure(1, weight=1)
        ttk.Label(self, text="Идентификация по CSV", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(self, text="Разделитель CSV", style="Body.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Combobox(self, textvariable=self.delimiter, values=(";", ",", "табуляция"), state="readonly", width=12).grid(row=1, column=1, sticky="ew")
        ttk.Button(self, text="Импортировать CSV", command=self.import_file).grid(row=2, column=0, columnspan=2, sticky="ew", pady=6)
        ttk.Button(self, text="Учебный пример", command=lambda: self.load_path(Path(__file__).resolve().parent.parent / "data" / "identification_step.csv")).grid(row=3, column=0, columnspan=2, sticky="ew")
        self.boxes = []
        for index, label in enumerate(("Время", "Вход", "Выход")):
            row = 4 + index * 2
            ttk.Label(self, text=label, style="Body.TLabel").grid(row=row, column=0, sticky="w", pady=(10, 0))
            box = ttk.Combobox(self, textvariable=self.columns[index], state="readonly")
            box.grid(row=row, column=1, sticky="ew", pady=(10, 0))
            self.boxes.append(box)
            ttk.Label(self, text="Единицы", style="Hint.TLabel").grid(row=row + 1, column=0, sticky="w")
            ttk.Combobox(self, textvariable=self.units[index], values=("с", "мин") if index == 0 else ("как в файле", "%"),
                         state="readonly", width=12).grid(row=row + 1, column=1, sticky="ew")
        ttk.Button(self, text="Оценить K, T и L", command=self.calculate).grid(row=10, column=0, columnspan=2, sticky="ew", pady=10)
        ttk.Combobox(self, textvariable=self.view, values=("Отклик", "Остаток", "Проверка", "Регулирование", ABSORBER_VIEW), state="readonly").grid(row=11, column=0, columnspan=2, sticky="ew")
        ttk.Label(self, textvariable=self.summary, style="Body.TLabel", wraplength=320).grid(row=12, column=0, columnspan=2, sticky="ew", pady=10)
        self.apply_button = ttk.Button(self, text="Применить T и L", command=self.apply, state="disabled")
        self.apply_button.grid(row=13, column=0, columnspan=2, sticky="ew")
        self.export_buttons = []
        for row, fmt in enumerate(("csv", "json"), 14):
            button = ttk.Button(self, text=f"Экспорт {fmt.upper()}", command=lambda fmt=fmt: self.export(fmt), state="disabled")
            button.grid(row=row, column=0, columnspan=2, sticky="ew", pady=3)
            self.export_buttons.append(button)
        ttk.Label(self, text="Маршрут: эксперимент → модель и остаток → T и L → автоподбор регулятора. K описывает эксперимент; баланс абсорбера сохраняет своё усиление. Начните с учебного примера, затем импортируйте собственный опыт.",
                  style="Hint.TLabel", wraplength=320).grid(row=16, column=0, columnspan=2, sticky="ew", pady=12)
        for row, (key, label) in enumerate((("title", "Название эксперимента"), ("description", "Источник данных"),
                ("input_role", "Назначение входа"), ("output_role", "Назначение выхода"), ("regime", "Рабочий режим"),
                ("comment", "Комментарий"), ("prediction", "Прогноз направления и K"),
                ("explanation", "Объяснение остатка"), ("conclusion", "Вывод по эксперименту")), 17):
            ttk.Label(self, text=label, style="Body.TLabel").grid(row=row, column=0, sticky="w", pady=3)
            if key in ("input_role", "output_role"):
                options = ("Не определён", "η, доля", "Состав", "Расход") if key == "input_role" else ("Не определён", "Xог, доля", "Xна, доля")
                ttk.Combobox(self, textvariable=self.metadata[key], values=options, state="readonly").grid(row=row, column=1, sticky="ew")
            else:
                ttk.Entry(self, textvariable=self.metadata[key]).grid(row=row, column=1, sticky="ew")
        ttk.Label(self, text="Учебное задание: до оценки спрогнозируйте знак и величину K. После оценки объясните остаток и запишите вывод. Ответы сохраняются без автоматического выставления баллов.",
                  style="Hint.TLabel", wraplength=320).grid(row=26, column=0, columnspan=2, sticky="ew", pady=8)
        ttk.Checkbutton(self, text="Те же сигналы и единицы", variable=self.same_signals).grid(row=27, column=0, columnspan=2, sticky="w")
        ttk.Button(self, text="Проверить на другом CSV", command=self.validate_file).grid(row=28, column=0, columnspan=2, sticky="ew", pady=5)
        for row, (key, label) in enumerate((("setpoint", "Задание, доля"), ("horizon", "Горизонт проверки, с"),
                                           ("gain", "Исходное Kp"), ("integral_time", "Исходное Ti, с")), 29):
            ttk.Label(self, text=label, style="Body.TLabel").grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(self, textvariable=self.pi_settings[key]).grid(row=row, column=1, sticky="ew")
        ttk.Button(self, text="Сравнить PI-регуляторы", command=self.check_pi).grid(row=33, column=0, columnspan=2, sticky="ew", pady=6)
        ttk.Button(self, text="Полный протокол HTML", command=lambda: self.export_protocol("html")).grid(row=34, column=0, columnspan=2, sticky="ew")
        ttk.Button(self, text="Полный протокол PDF", command=lambda: self.export_protocol("pdf")).grid(row=35, column=0, columnspan=2, sticky="ew", pady=5)
        ttk.Button(self, text="Сеанс и все результаты JSON", command=self.app._save_comparison_session).grid(row=36, column=0, columnspan=2, sticky="ew")
        ttk.Button(self, text="Все результаты CSV", command=self.export_all_csv).grid(row=37, column=0, columnspan=2, sticky="ew", pady=5)
        self.absorber_sensitivity = AbsorberSensitivity(self)
        self.absorber_sensitivity.grid(row=38, column=0, columnspan=2, sticky="ew", pady=12)
        for variable in (*self.columns, *self.units, self.delimiter):
            variable.trace_add("write", self.changed)
        self.view.trace_add("write", self.view_changed)
        for key in ("input_role", "output_role"):
            self.metadata[key].trace_add("write", self.changed)

    def signature(self):
        return (self.generation, *(v.get() for v in (*self.columns, *self.units, self.delimiter,
                self.metadata["input_role"], self.metadata["output_role"], *self.pi_settings.values())), self.same_signals.get(),
                self.result_revision, tuple(self.absorber_sensitivity.configuration().values()))

    def view_changed(self, *_):
        if self.view.get() == ABSORBER_VIEW:
            self.draw(); return
        if self.result is None:
            return
        if self.view.get() == "Проверка" and self.validation is not None:
            r = self.validation
            self.summary.set(f"Независимая проверка: {r['source']}; RMSE всей записи={r['rmse']:.5g}; ошибка {r['normalized_rmse']*100:.3g}%. K/T/L не изменялись. {r['warning']}\n" + quality_text(r))
        elif self.view.get() == "Регулирование" and self.pi_result is not None:
            self.summary.set("\n".join(f"{r['name']}: Kp={r['gain']:.5g}; Ti={r['integral_time']:.5g}; ошибка={r['metrics']['final_error']:.5g}; перерегулирование={r['metrics']['overshoot_percent']:.3g}%; IAE={r['metrics']['iae']:.5g}; установление={settling_text(r['metrics']['settling_time'])}; насыщение={r['metrics']['saturation_duration']:.3g} с."
                + (" Задание недостижимо." if not r['metrics']['reachable'] else "")
                + (" Выход вне диапазона 0…1." if r['metrics'].get('output_out_of_range') else "") for r in self.pi_result["runs"]))
        else:
            r = self.result
            self.summary.set(f"K={r['gain']:.6g}; T={r['time_constant']:.6g} с; L={r['delay']:.6g} с. RMSE всей записи={r['rmse']:.5g}. Оценена подгонка к {r['source']}.\n" + quality_text(r))
        self.draw()

    def changed(self, *_):
        self.app._invalidate_task()
        self.generation += 1
        self.result_current = False
        if self.result is not None:
            self.apply_button.configure(state="disabled")
            self.summary.set("Выбор изменён — повторите оценку. Графики и экспорт сохраняют последний успешный опыт.")
            self.draw()

    def import_file(self):
        path = filedialog.askopenfilename(title="Эксперимент CSV", filetypes=(("CSV", "*.csv"),))
        if path:
            self.load_path(path)

    def load_path(self, path):
        try:
            delimiter = "\t" if self.delimiter.get() == "табуляция" else self.delimiter.get()
            headers, rows = read_experiment(path, delimiter)
        except (ValueError, OSError, UnicodeError) as error:
            self.summary.set(str(error))
            return
        self.headers, self.rows, self.source = headers, rows, str(path)
        self.generation += 1
        self.changed()
        for index, box in enumerate(self.boxes):
            box.configure(values=headers)
            self.columns[index].set(headers[index])
        self.summary.set(f"{Path(path).name}: {len(rows)} строк. Проверьте столбцы и единицы, затем оцените модель.")

    def calculate(self):
        try:
            columns = tuple(v.get() for v in self.columns)
            units = tuple(v.get() for v in self.units)
            arrays = experiment_columns(self.headers, self.rows, columns, units)
        except ValueError as error:
            self.summary.set(str(error))
            return
        metadata = dict(source=Path(self.source).name, columns=columns, units=units)
        self.app._start_task("Идентификация", lambda cancel, progress: dict(
            identify_step(*arrays, cancel=cancel, progress=progress), **metadata), self.accept, "identification")

    def accept(self, result):
        self.result = result
        self.result_revision += 1
        self.result.setdefault("origin", self.origin())
        self.result_current = True
        self.summary.set(f"K = {result['gain']:.6g}; T = {result['time_constant']:.6g} с; L = {result['delay']:.6g} с. "
                         f"RMSE всей записи = {result['rmse']:.5g}; ошибка {result['normalized_rmse'] * 100:.3g}% амплитуды. "
                         f"{result['source']}. Время ступени определено внутри интервала {result['step_interval'][0]:g}–{result['step_interval'][1]:g} с. Оценена только подгонка к исходному эксперименту.\n" + quality_text(result))
        self.apply_button.configure(state="normal")
        for button in self.export_buttons:
            button.configure(state="normal")
        self.app._set_status("Идентификация выполнена")
        self.draw()

    def origin(self):
        return dict(experiment_id=self.experiment_id, data_revision=self.generation,
                    result_revision=self.result_revision, source=Path(self.source).name,
                    metadata={k: v.get() for k, v in self.metadata.items()},
                    parameters={k: float(self.result[k]) for k in ("gain", "time_constant", "delay")})

    def validate_file(self):
        if not self.result_current or not self.same_signals.get() or any(
                self.metadata[k].get() == "Не определён" for k in ("input_role", "output_role")):
            self.summary.set("Сначала оцените модель, задайте назначение сигналов и подтвердите одинаковые сигналы и единицы проверочной записи.")
            return
        path = filedialog.askopenfilename(title="Независимая проверочная запись", filetypes=(("CSV", "*.csv"),))
        if not path:
            return
        try:
            headers, rows = read_experiment(path, "\t" if self.delimiter.get() == "табуляция" else self.delimiter.get())
            arrays = experiment_columns(headers, rows, tuple(v.get() for v in self.columns), tuple(v.get() for v in self.units))
            if all(np.array_equal(a, self.result[k]) for a, k in zip(arrays, ("time", "input", "output"))):
                raise ValueError("Эта запись совпадает с данными подгонки. Выберите независимый эксперимент.")
        except (ValueError, OSError, UnicodeError) as error:
            self.summary.set(str(error))
            return
        parameters, origin = copy.deepcopy(self.result), self.origin()
        def calculate(cancel, progress):
            return dict(validate_step(*arrays, parameters, cancel=cancel), source=Path(path).name, origin=origin)
        self.app._start_task("Проверка модели", calculate, self.accept_validation, "identification")

    def accept_validation(self, result):
        self.validation = result
        self.view.set("Проверка")
        self.summary.set(f"Независимая проверка: {result['source']}; RMSE всей записи={result['rmse']:.5g}; ошибка {result['normalized_rmse']*100:.3g}%. K/T/L не изменялись. {result['warning']}\n" + quality_text(result))
        self.draw()
        self.app._set_status("Независимая проверка выполнена")

    def check_pi(self):
        if not self.result_current:
            self.summary.set("Повторите идентификацию перед проверкой регулятора.")
            return
        try:
            settings = {k: float(v.get().replace(",", ".")) for k, v in self.pi_settings.items()}
        except ValueError:
            self.summary.set("Настройки проверки PI должны быть числами.")
            return
        result, origin = copy.deepcopy(self.result), self.origin()
        roles = (self.metadata["input_role"].get(), self.metadata["output_role"].get())
        self.app._start_task("Проверка PI", lambda cancel, progress: dict(
            compare_pi(result, *roles, **settings, cancel=cancel, progress=progress), origin=origin), self.accept_pi, "identification")

    def accept_pi(self, result):
        self.pi_result = result
        self.view.set("Регулирование")
        lines = []
        for run in result["runs"]:
            m = run["metrics"]
            lines.append(f"{run['name']}: Kp={run['gain']:.5g}; Ti={run['integral_time']:.5g}; ошибка={m['final_error']:.5g}; перерегулирование={m['overshoot_percent']:.3g}%; IAE={m['iae']:.5g}; установление={settling_text(m['settling_time'])}; насыщение={m['saturation_duration']:.3g} с. "
                         + ("" if m["reachable"] else "Задание недостижимо при η=0…1.")
                         + (" Выход вне диапазона 0…1; проверьте применимость модели." if m.get("output_out_of_range") else ""))
        self.summary.set("\n".join(lines))
        self.draw()
        self.app._set_status("Сравнение PI выполнено")

    def draw(self):
        if self.app.current_page == "identification" and self.view.get() == ABSORBER_VIEW:
            self.absorber_sensitivity.draw(); return
        if self.result is None or self.app.current_page != "identification":
            return
        app, result = self.app, self.result
        app.signal_mode_box.grid()
        app._remove_controller_axis()
        special = self.validation if self.view.get() == "Проверка" else self.pi_result if self.view.get() == "Регулирование" else None
        if special is not None:
            app._style_axis(app.disturbance_axis, "Время, с", "η, доля" if self.view.get() == "Регулирование" else "Вход, приведённые единицы")
            app._style_axis(app.response_axis, "Время, с", "Выход, доля" if self.view.get() == "Регулирование" else "Выход, приведённые единицы")
            if self.view.get() == "Регулирование":
                for run in special["runs"]:
                    app.disturbance_axis.plot(run["time"], run["control"], label=run["name"])
                    app.response_axis.plot(run["time"], run["output"], label=run["name"])
                app.response_axis.axhline(special["setpoint"], color="#64748B", linestyle="--", label="Задание")
                title = "Проверка PI на экспериментальной модели"
            else:
                app.disturbance_axis.plot(special["time"], special["input"], label="Проверочный вход")
                app.response_axis.plot(special["time"], special["output"], label="Независимый эксперимент")
                app.response_axis.plot(special["time"], special["model"], label="Модель с фиксированными K/T/L")
                title = "Независимая проверка модели"
            stale = special["origin"]["data_revision"] != self.generation or special["origin"]["result_revision"] != self.result_revision
            app.primary_chart_title.set("Управление" if self.view.get() == "Регулирование" else "Проверочный вход")
            app.primary_chart_subtitle.set(special["origin"]["source"])
            app.response_chart_title.set(title)
            p = special["model"] if self.view.get() == "Регулирование" else special
            caption = f"K={p['gain']:.5g}; T={p['time_constant']:.5g} с; L={p['delay']:.5g} с. "
            caption += "Снимок прежних данных; повторите проверку." if stale else "Симуляция; η = 0…1." if self.view.get() == "Регулирование" else "Без повторной подгонки."
            app.response_subtitle.set(caption)
            for axis, canvas in ((app.disturbance_axis, app.disturbance_canvas), (app.response_axis, app.response_canvas)):
                app._place_legend_above(axis)
                app._enable_legend_toggles(axis, canvas)
                canvas.draw_idle()
            app._charts_show_comparison = False
            return
        app._style_axis(app.disturbance_axis, "Время, с", "Вход, приведённые единицы")
        app.disturbance_axis.plot(result["time"], result["input"], label="Вход", color="#16A34A")
        app.primary_chart_title.set("Экспериментальный вход")
        app.primary_chart_subtitle.set(result["source"])
        residual = self.view.get() == "Остаток"
        app._style_axis(app.response_axis, "Время, с", "Остаток, приведённые единицы" if residual else "Выход, приведённые единицы")
        if residual:
            app.response_axis.plot(result["time"], result["residual"], label="Эксперимент − модель", color="#F59E0B")
            app.response_axis.axhline(0, color="#64748B", linewidth=.8)
        else:
            app.response_axis.plot(result["time"], result["output"], label="Эксперимент", color="#64748B")
            app.response_axis.plot(result["time"], result["model"], label="Модель", color="#2563EB")
        app.response_chart_title.set("Остаток идентификации" if residual else "Эксперимент и модель")
        app.response_subtitle.set(f"K={result['gain']:.5g}; T={result['time_constant']:.5g} с; L={result['delay']:.5g} с")
        for axis, canvas in ((app.disturbance_axis, app.disturbance_canvas), (app.response_axis, app.response_canvas)):
            app._place_legend_above(axis)
            app._enable_legend_toggles(axis, canvas)
            canvas.draw_idle()
        app._charts_show_comparison = False

    def apply(self):
        if self.result is not None and self.result_current:
            self.app.absorber_form.time_constant.set(f"{self.result['time_constant']:.12g}")
            self.app.absorber_form.delay.set(f"{self.result['delay']:.12g}")
            self.applied_origin = self.origin()
            self.app._show_page("controller")
            self.app._set_status("T и L применены. Выполните автоподбор и расчёт опыта.")

    def capture(self, *, validate=True, copy_results=True):
        result = None if self.result is None else {
            key: value.tolist() if isinstance(value, np.ndarray) else copy.deepcopy(value) if copy_results else value
            for key, value in self.result.items()}
        snapshot = dict(headers=list(self.headers), rows=copy.deepcopy(self.rows) if copy_results else self.rows,
            source=Path(self.source).name, columns=[v.get() for v in self.columns],
            units=[v.get() for v in self.units], delimiter=self.delimiter.get(),
            view=self.view.get(), result=result, result_current=self.result_current,
            experiment_id=self.experiment_id, data_revision=self.generation, result_revision=self.result_revision,
            metadata={k: v.get() for k, v in self.metadata.items()}, same_signals=self.same_signals.get(),
            pi_settings={k: v.get() for k, v in self.pi_settings.items()},
            validation=copy.deepcopy(self.validation) if copy_results else self.validation,
            pi_result=copy.deepcopy(self.pi_result) if copy_results else self.pi_result,
            absorber_sensitivity=self.absorber_sensitivity.capture(copy_results=copy_results),
            applied_origin=copy.deepcopy(self.applied_origin))
        return validate_experiment_state(snapshot) if validate else snapshot

    def restore(self, state):
        state = validate_experiment_state(state)
        self.result = None
        self.result_current = False
        self.validation = self.pi_result = self.applied_origin = None
        self.headers, self.rows, self.source = [], [], ""
        self.generation += 1
        for button in (self.apply_button, *self.export_buttons):
            button.configure(state="disabled")
        if state is None:
            from app.absorber_sensitivity import FIELD_DEFAULTS
            self.absorber_sensitivity.restore(dict(fields=FIELD_DEFAULTS, result=None, selected=0))
            self.experiment_id = uuid.uuid4().hex
            self.result_revision = 0
            for key, variable in self.metadata.items():
                variable.set("Не определён" if key in ("input_role", "output_role") else "")
            for variable, value in zip(self.units, ("с", "как в\u00a0файле", "как в\u00a0файле")):
                variable.set(value)
            for key, value in (("setpoint", "0.4"), ("horizon", "100"), ("gain", "1"), ("integral_time", "8")):
                self.pi_settings[key].set(value)
            self.same_signals.set(False)
            self.delimiter.set(";")
            self.view.set("Отклик")
            for box, variable in zip(self.boxes, self.columns):
                box.configure(values=())
                variable.set("")
            self.summary.set("Импортируйте CSV с одной постоянной ступенью.")
            return
        self.headers, self.rows, self.source = state["headers"], state["rows"], state["source"]
        for box, variable, value in zip(self.boxes, self.columns, state["columns"]):
            box.configure(values=self.headers)
            variable.set(value)
        for variable, value in zip(self.units, state["units"]):
            variable.set(value)
        self.delimiter.set(state["delimiter"])
        self.view.set(state["view"])
        for key, variable in self.metadata.items():
            variable.set(state.get("metadata", {}).get(key, "Не определён" if key in ("input_role", "output_role") else ""))
        for key, variable in self.pi_settings.items():
            variable.set(state.get("pi_settings", {}).get(key, variable.get()))
        self.same_signals.set(state.get("same_signals", False))
        if state["result"] is not None:
            result = copy.deepcopy(state["result"])
            for key in ("time", "input", "output", "model", "residual"):
                result[key] = np.asarray(result[key], dtype=float)
            self.accept(result)
            if not state["result_current"]:
                self.changed()
        else:
            self.summary.set(f"{self.source}: {len(self.rows)} строк. Проверьте выбор и оцените модель.")
        self.experiment_id = state.get("experiment_id", uuid.uuid4().hex)
        self.generation = state.get("data_revision", self.generation)
        self.result_revision = state.get("result_revision", self.result_revision)
        self.validation = state.get("validation")
        self.pi_result = state.get("pi_result")
        self.applied_origin = state.get("applied_origin")
        self.absorber_sensitivity.restore(state["absorber_sensitivity"])
        if self.view.get() == "Регулирование" and self.pi_result is not None:
            self.accept_pi(self.pi_result)
        elif self.view.get() == "Проверка" and self.validation is not None:
            self.accept_validation(self.validation)
        self.draw()

    def active_origin(self):
        if self.applied_origin is None:
            return None
        p = self.applied_origin["parameters"]
        try:
            matches = np.allclose([float(self.app.absorber_form.time_constant.get()), float(self.app.absorber_form.delay.get())],
                                  [p["time_constant"], p["delay"]], rtol=1e-10, atol=1e-12)
        except ValueError:
            return None
        return copy.deepcopy(self.applied_origin) if matches else None

    def export_protocol(self, fmt):
        if self.result is None:
            self.summary.set("Сначала выполните идентификацию.")
            return
        path = filedialog.asksaveasfilename(title="Полный экспериментальный протокол", defaultextension=f".{fmt}", initialfile=f"experiment.{fmt}")
        if path:
            from app.exporting import write_experiment_report
            try:
                write_experiment_report(path, self.capture(), fmt)
                self.app._set_status("Экспериментальный протокол сохранён")
            except (OSError, ValueError) as error:
                self.summary.set(str(error))

    def export_all_csv(self):
        if self.result is None:
            return
        path = filedialog.asksaveasfilename(title="Все результаты эксперимента", defaultextension=".csv", initialfile="experiment.csv")
        if path:
            from app.exporting import write_experiment_csv
            try:
                write_experiment_csv(path, self.capture())
                self.app._set_status("Все результаты экспортированы в CSV")
            except (OSError, ValueError) as error:
                self.summary.set(str(error))

    def export(self, fmt):
        if self.result is None:
            return
        path = filedialog.asksaveasfilename(title="Результат идентификации", defaultextension=f".{fmt}", initialfile=f"identification.{fmt}")
        if path:
            try:
                export_identification(path, self.result, fmt)
                self.app._set_status("Идентификация экспортирована")
            except OSError as error:
                self.summary.set(str(error))
