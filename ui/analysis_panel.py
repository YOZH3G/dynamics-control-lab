"""Absorber exploration: sensitivity families and the controller tuning map."""

import tkinter as tk
from tkinter import ttk

import numpy as np
from matplotlib.colors import ListedColormap

from app.controller_extensions import extensions_from_form
from app.exploration import MAP_CATEGORIES, SENSITIVITY_PARAMETERS, controller_setting_map, sensitivity_runs
from app.validation import parse_disturbance, parse_nonnegative_number, parse_percentage, parse_positive_number, parse_value_list
from ui.theme import ACCENT, BORDER, MUTED, TEXT
from ui.ui_helpers import plot_samples


class AnalysisPanel:
    def __init__(self, app):
        self.app = app
        self.sensitivity_parameter = tk.StringVar(value="Постоянная времени T")
        self.sensitivity_values = tk.StringVar(value="5; 10; 15; 20")
        self.sensitivity_summary = tk.StringVar(
            value="Выберите параметр, введите от 2 до 6 значений и постройте семейство кривых."
        )
        self.sensitivity_data = None
        self.map_controller_type = tk.StringVar(value="PI")
        self.map_gain_min = tk.StringVar(value="0.2")
        self.map_gain_max = tk.StringVar(value="4")
        self.map_integral_min = tk.StringVar(value="2")
        self.map_integral_max = tk.StringVar(value="30")
        self.map_derivative_time = tk.StringVar(value="1")
        self.map_grid_size = tk.StringVar(value="9")
        self.map_summary = tk.StringVar(value="Постройте карту настроек P-, PI- или PID-регулятора.")
        self.map_selection_summary = tk.StringVar(value="Выберите ячейку карты, чтобы увидеть переходный процесс.")
        self.map_data = None
        self.map_selection = None
        self.map_click_callback = None
        self.map_colorbar = None

    def build_sensitivity_card(self, parent):
        card = self.app._card(parent, 0)
        ttk.Label(card, text="Семейство переходных процессов", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )
        ttk.Label(
            card,
            text=(
                "Меняйте один параметр при неизменных остальных условиях. "
                "Каждая кривая показывает отклик для отдельного значения."
            ),
            style="Body.TLabel", wraplength=330, justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 12))
        ttk.Label(card, text="Параметр", style="Body.TLabel").grid(row=2, column=0, sticky="w")
        ttk.Label(card, text="Значения через ;", style="Body.TLabel").grid(
            row=2, column=1, sticky="w", padx=(8, 0)
        )
        self.sensitivity_parameter_box = ttk.Combobox(
            card, textvariable=self.sensitivity_parameter, values=SENSITIVITY_PARAMETERS,
            state="readonly", width=20,
        )
        self.sensitivity_parameter_box.grid(row=3, column=0, sticky="ew", pady=(4, 12))
        self.sensitivity_values_entry = ttk.Entry(card, textvariable=self.sensitivity_values, width=20)
        self.sensitivity_values_entry.grid(row=3, column=1, sticky="ew", padx=(8, 0), pady=(4, 12))
        ttk.Button(
            card, text="Построить семейство", command=self.calculate_sensitivity, style="Primary.TButton"
        ).grid(row=4, column=0, columnspan=2, sticky="ew")
        ttk.Label(
            card, textvariable=self.sensitivity_summary, style="Muted.TLabel", wraplength=330, justify="left"
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))
        card.columnconfigure((0, 1), weight=1)

        note = self.app._card(parent, 1, padding=(14, 12))
        ttk.Label(note, text="Как читать график", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            note,
            text=(
                "Большая T замедляет реакцию, большая L сдвигает её вправо. "
                "Изменение доли возмущения показывает влияние силы нарушения режима."
            ),
            style="Body.TLabel", wraplength=330, justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))

    def build_map_card(self, parent):
        card = self.app._card(parent, 0)
        ttk.Label(card, text="Упрощённая карта настроек", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )
        ttk.Label(
            card,
            text=(
                "P отображается как ряд по Kp. Для PI и PID строится плоскость Kp × Ti; "
                "в PID выбранное Td остаётся фиксированным. "
                "Щёлкните по ячейке, затем примените выбранные настройки."
            ),
            style="Body.TLabel", wraplength=330, justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 12))
        ttk.Label(card, text="Тип", style="Body.TLabel").grid(row=2, column=0, sticky="w")
        ttk.Label(card, text="Размер сетки", style="Body.TLabel").grid(row=2, column=1, sticky="w", padx=(8, 0))
        self.map_type_box = ttk.Combobox(card, textvariable=self.map_controller_type, values=("P", "PI", "PID"), state="readonly")
        self.map_type_box.grid(row=3, column=0, sticky="ew", pady=(4, 10))
        self.map_grid_size_entry = ttk.Entry(card, textvariable=self.map_grid_size, width=12)
        self.map_grid_size_entry.grid(row=3, column=1, sticky="ew", padx=(8, 0), pady=(4, 10))
        self.map_range_entries(card, 4, "Kp", self.map_gain_min, self.map_gain_max)
        self.map_integral_widgets = self.map_range_entries(
            card, 6, "Ti, с", self.map_integral_min, self.map_integral_max
        )
        ttk.Label(card, text="Td, с (для PID)", style="Body.TLabel").grid(row=8, column=0, sticky="w")
        self.map_derivative_entry = ttk.Entry(card, textvariable=self.map_derivative_time, width=12)
        self.map_derivative_entry.grid(row=8, column=1, sticky="ew", padx=(8, 0))
        ttk.Button(card, text="Построить карту", command=self.calculate_map, style="Primary.TButton").grid(
            row=9, column=0, columnspan=2, sticky="ew", pady=(10, 0)
        )
        ttk.Label(card, textvariable=self.map_summary, style="Muted.TLabel", wraplength=330, justify="left").grid(
            row=10, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )
        card.columnconfigure((0, 1), weight=1)

        selection = self.app._card(parent, 1, padding=(14, 12))
        ttk.Label(selection, text="Выбранная точка", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            selection, textvariable=self.map_selection_summary, style="Body.TLabel", wraplength=330, justify="left"
        ).grid(row=1, column=0, sticky="w", pady=(6, 10))
        self.apply_map_selection_button = ttk.Button(
            selection, text="Применить и открыть переходный процесс",
            command=self.apply_map_selection, style="Secondary.TButton", state="disabled",
        )
        self.apply_map_selection_button.grid(row=2, column=0, sticky="ew")
        self.map_type_box.bind("<<ComboboxSelected>>", self.update_map_input_state)
        self.update_map_input_state()

    @staticmethod
    def map_range_entries(parent, row, label, minimum, maximum):
        ttk.Label(parent, text=f"{label}: от", style="Body.TLabel").grid(row=row, column=0, sticky="w")
        ttk.Label(parent, text="до", style="Body.TLabel").grid(row=row, column=1, sticky="w", padx=(8, 0))
        minimum_entry = ttk.Entry(parent, textvariable=minimum, width=12)
        minimum_entry.grid(row=row + 1, column=0, sticky="ew", pady=(4, 10))
        maximum_entry = ttk.Entry(parent, textvariable=maximum, width=12)
        maximum_entry.grid(row=row + 1, column=1, sticky="ew", padx=(8, 0), pady=(4, 10))
        return minimum_entry, maximum_entry

    def update_map_input_state(self, _event=None):
        controller_type = self.map_controller_type.get()
        state = "normal" if controller_type in ("PI", "PID") else "disabled"
        for widget in self.map_integral_widgets:
            widget.configure(state=state)
        self.map_derivative_entry.configure(state="normal" if controller_type == "PID" else "disabled")

    def read_inputs(self):
        if not (self.app.absorber_form.component_enabled.get() or self.app.absorber_form.flow_enabled.get()):
            raise ValueError("Включите хотя бы одно возмущение.")
        component_fraction = self.app.absorber_form.read_fraction(
            self.app.absorber_form.component_enabled.get(), self.app.absorber_form.component_value, self.app.absorber_form.component_error, self.app.absorber_form.component_entry
        )
        flow_fraction = self.app.absorber_form.read_fraction(
            self.app.absorber_form.flow_enabled.get(), self.app.absorber_form.flow_value, self.app.absorber_form.flow_error, self.app.absorber_form.flow_entry
        )
        return component_fraction, flow_fraction, self.app.absorber_form.read_dynamic_parameters()

    def calculate_sensitivity(self):
        self.app.absorber_form.clear_errors()
        try:
            component_fraction, flow_fraction, dynamics = self.read_inputs()
            controller = self.app.absorber_form.read_controller_parameters()
            values = self.parse_sensitivity_values()
        except ValueError as error:
            self.sensitivity_summary.set(str(error))
            self.app._set_status("Исправьте параметры анализа чувствительности", error=True)
            return
        chain, model = self.app.absorber_form.chain, self.app.absorber_form.model_values.copy()
        parameter = self.sensitivity_parameter.get()
        self.app._start_task("Анализ чувствительности", lambda cancel, progress: sensitivity_runs(
            chain, model, component_fraction, flow_fraction, dynamics, controller, parameter, values,
            cancel=cancel, progress=progress), lambda runs: self.apply_sensitivity(runs, parameter), "sensitivity")

    def apply_sensitivity(self, runs, parameter):
        self.sensitivity_data = {
            "parameter": parameter,
            "runs": runs,
        }
        self.sensitivity_summary.set(
            f"Построено кривых: {len(runs)}. Все остальные параметры взяты из текущего расчёта."
        )
        self.draw_sensitivity()
        self.app._set_status("Анализ чувствительности построен")

    def parse_sensitivity_values(self):
        parameter = self.sensitivity_parameter.get()
        parser = (
            parse_positive_number if parameter == "Постоянная времени T"
            else parse_nonnegative_number if parameter == "Запаздывание L"
            else lambda value: parse_disturbance(value, self.app.absorber_form._disturbance_units)
        )
        self.sensitivity_values_entry.configure(style="TEntry")
        try:
            values = parse_value_list(self.sensitivity_values.get(), parser)
        except ValueError as error:
            self.sensitivity_values_entry.configure(style="Error.TEntry")
            raise ValueError(f"Значения параметра: {error}") from error
        return values

    def calculate_map(self):
        self.app.absorber_form.clear_errors()
        try:
            component_fraction, flow_fraction, dynamics = self.read_inputs()
            grid_size = self.parse_grid_size()
            gain_min = parse_nonnegative_number(self.map_gain_min.get())
            gain_max = parse_positive_number(self.map_gain_max.get())
            if gain_max <= gain_min:
                raise ValueError("Верхняя граница Kp должна быть больше нижней.")
            controller_type = self.map_controller_type.get()
            gains = tuple(np.linspace(gain_min, gain_max, grid_size))
            if controller_type in ("PI", "PID"):
                integral_min = parse_positive_number(self.map_integral_min.get())
                integral_max = parse_positive_number(self.map_integral_max.get())
                if integral_max <= integral_min:
                    raise ValueError("Верхняя граница Ti должна быть больше нижней.")
                integral_times = tuple(np.linspace(integral_min, integral_max, grid_size))
            else:
                integral_times = ()
            derivative_time = (
                parse_nonnegative_number(self.map_derivative_time.get())
                if controller_type == "PID" else 0.0
            )
            control_limit = parse_percentage(self.app.absorber_form.control_limit.get())
            setpoint = parse_percentage(self.app.absorber_form.setpoint.get())
            extensions = extensions_from_form({key: value.get() for key, value in self.app.absorber_form.extension_values.items()}, controller_type)
        except ValueError as error:
            self.map_summary.set(str(error))
            self.app._set_status("Исправьте параметры карты настроек", error=True)
            return
        chain, model = self.app.absorber_form.chain, self.app.absorber_form.model_values.copy()
        self.app._start_task("Карта настроек", lambda cancel, progress: controller_setting_map(
            chain, model, component_fraction, flow_fraction, dynamics,
            controller_type, gains, integral_times, control_limit, setpoint,
            derivative_time=derivative_time, extensions=extensions, cancel=cancel, progress=progress),
            lambda result: self.apply_map(result, controller_type, derivative_time), "map")

    def apply_map(self, map_data, controller_type, derivative_time):
        self.map_data = map_data
        self.map_selection = None
        self.apply_map_selection_button.configure(state="disabled")
        self.map_selection_summary.set("Щёлкните по ячейке карты, чтобы выбрать настройки и увидеть отклик.")
        self.map_summary.set(
            f"Построена карта {controller_type}: {map_data['categories'].shape[1]} × {map_data['categories'].shape[0]} точек"
            + (f", Td={derivative_time:g} с." if controller_type == "PID" else ".")
        )
        self.draw_map()
        self.app._set_status("Карта настроек построена")

    def parse_grid_size(self):
        try:
            grid_size = int(self.map_grid_size.get())
        except ValueError as error:
            raise ValueError("Размер сетки должен быть целым числом.") from error
        if not 3 <= grid_size <= 15:
            raise ValueError("Размер сетки должен быть в диапазоне 3…15.")
        return grid_size

    def draw_sensitivity(self):
        if self.app.current_page != "sensitivity" or self.sensitivity_data is None:
            return
        self.app._remove_controller_axis()
        self.remove_map_colorbar()
        self.clear_map_click_callback()
        parameter = self.sensitivity_data["parameter"]
        runs = self.sensitivity_data["runs"]
        colors = ("#2563EB", "#F59E0B", "#16A34A", "#7C3AED", "#DB2777", "#0891B2")
        self.app.primary_chart_title.set("Анализ чувствительности")
        self.app.primary_chart_subtitle.set(f"Семейство кривых по параметру: {parameter}")
        self.app._style_axis(self.app.disturbance_axis, "Время, с", "X, %")
        for index, run in enumerate(runs):
            result = run["result"]
            value = run["value"]
            suffix = "%" if parameter.startswith("Возмущение") else " с"
            shown_value = value * 100 if parameter.startswith("Возмущение") else value
            self.app.disturbance_axis.plot(
                *plot_samples(result["time"], result["final_response"] * 100), color=colors[index], linewidth=2.2,
                label=f"{shown_value:g}{suffix}",
            )
        self.app.disturbance_axis.margins(x=0.02, y=0.12)
        self.app._place_legend_above(self.app.disturbance_axis)
        self.app.disturbance_canvas.draw_idle()

        self.app.response_chart_title.set("Максимальное отклонение")
        self.app.response_subtitle.set("Для каждого значения анализируемого параметра")
        self.app._style_axis(self.app.response_axis, parameter, "Отклонение")
        values = [run["value"] * 100 if parameter.startswith("Возмущение") else run["value"] for run in runs]
        deviations = [run["result"]["metrics"]["maximum_deviation"] for run in runs]
        self.app.response_axis.plot(values, deviations, color=ACCENT, marker="o", linewidth=2.2)
        self.app.response_axis.grid(color=BORDER, linewidth=0.8)
        self.app.response_canvas.draw_idle()
        self.app._charts_show_comparison = True

    def draw_map(self):
        if self.app.current_page != "tuning_map" or self.map_data is None:
            return
        self.app._remove_controller_axis()
        self.remove_map_colorbar()
        data = self.map_data
        categories = data["categories"]
        self.app.primary_chart_title.set(f"Карта настроек {data['controller_type']}-регулятора")
        self.app.primary_chart_subtitle.set("Щёлкните по ячейке для просмотра соответствующего переходного процесса")
        self.app._style_axis(self.app.disturbance_axis, "Kp", "Ti, с" if data["controller_type"] in ("PI", "PID") else "")
        image = self.app.disturbance_axis.imshow(
            categories, origin="lower", aspect="auto",
            cmap=ListedColormap(("#BBF7D0", "#FDE68A", "#FCA5A5", "#BFDBFE")),
            vmin=-0.5, vmax=len(MAP_CATEGORIES) - 0.5,
        )
        self.app.disturbance_axis.set_xticks(range(len(data["gains"])), [f"{value:.2g}" for value in data["gains"]])
        if data["controller_type"] in ("PI", "PID"):
            self.app.disturbance_axis.set_yticks(
                range(len(data["integral_times"])), [f"{value:.2g}" for value in data["integral_times"]]
            )
        else:
            self.app.disturbance_axis.set_yticks((0,), ("Ti не используется",))
        if self.map_selection is not None:
            row, column = self.map_selection
            self.app.disturbance_axis.scatter((column,), (row,), s=500, facecolors="none", edgecolors=TEXT, linewidths=2)
        self.map_colorbar = self.app.disturbance_axis.figure.colorbar(
            image, ax=self.app.disturbance_axis, ticks=range(len(MAP_CATEGORIES)), pad=0.02
        )
        self.map_colorbar.ax.set_yticklabels(
            ("Установилось\nза время опыта", "Затухающие\nколебания",
             "Незатухающие\nколебания", "Установление\nне подтверждено"), fontsize=8,
        )
        self.ensure_map_click_callback()
        self.app.disturbance_canvas.draw_idle()
        self.draw_map_selection_response()
        self.app._charts_show_comparison = True

    def draw_map_selection_response(self):
        self.app.response_chart_title.set("Переходный процесс выбранной точки")
        self.app.response_subtitle.set("Выберите ячейку карты")
        self.app._style_axis(self.app.response_axis, "Время, с", "X, %")
        if self.map_selection is None:
            self.app.response_axis.text(0.5, 0.5, "Ячейка не выбрана", transform=self.app.response_axis.transAxes, ha="center", va="center", color=MUTED)
        else:
            row, column = self.map_selection
            result = self.map_data["results"][row][column]
            self.app.response_axis.plot(*plot_samples(result["time"], result["final_response"] * 100), color=ACCENT, linewidth=2.4, label="Выбранная настройка")
            self.app.response_axis.axhline(result["controller"]["setpoint"] * 100, color=MUTED, linestyle="--", label="Задание")
            self.app._place_legend_above(self.app.response_axis)
            self.app.response_subtitle.set(self.map_selection_summary.get())
        self.app.response_canvas.draw_idle()

    def ensure_map_click_callback(self):
        if self.map_click_callback is None:
            self.map_click_callback = self.app.disturbance_canvas.mpl_connect("button_press_event", self.select_map_cell)

    def clear_map_click_callback(self):
        if self.map_click_callback is not None:
            self.app.disturbance_canvas.mpl_disconnect(self.map_click_callback)
            self.map_click_callback = None

    def remove_map_colorbar(self):
        if self.map_colorbar is not None:
            self.map_colorbar.remove()
            self.map_colorbar = None

    def select_map_cell(self, event):
        if (
            self.app.current_page != "tuning_map"
            or self.map_data is None
            or event.inaxes is not self.app.disturbance_axis
            or event.xdata is None
            or event.ydata is None
        ):
            return
        column = int(round(event.xdata))
        row = int(round(event.ydata))
        categories = self.map_data["categories"]
        if not (0 <= row < categories.shape[0] and 0 <= column < categories.shape[1]):
            return
        self.map_selection = (row, column)
        gain = self.map_data["gains"][column]
        integral_time = self.map_data["integral_times"][row]
        category = MAP_CATEGORIES[categories[row, column]]
        settings = f"Kp={gain:.3g}"
        if integral_time is not None:
            settings += f", Ti={integral_time:.3g} с"
        if self.map_data["derivative_time"] is not None:
            settings += f", Td={self.map_data['derivative_time']:.3g} с"
        assessment = self.map_data["assessments"][row][column]
        self.map_selection_summary.set(f"{settings}. Категория: {category}.\n{assessment['explanation']}")
        self.apply_map_selection_button.configure(state="normal")
        self.draw_map()

    def apply_map_selection(self):
        if self.map_data is None or self.map_selection is None:
            return
        row, column = self.map_selection
        self.app.absorber_form.controller_type.set(self.map_data["controller_type"])
        self.app.absorber_form.set_controller_mode(True)
        self.app.absorber_form.proportional_gain.set(self.app._format_number(self.map_data["gains"][column]))
        integral_time = self.map_data["integral_times"][row]
        if integral_time is not None:
            self.app.absorber_form.integral_time.set(self.app._format_number(integral_time))
        if self.map_data["derivative_time"] is not None:
            self.app.absorber_form.derivative_time.set(self.app._format_number(self.map_data["derivative_time"]))
        self.app.absorber_form.update_controller_type()
        self.app._show_page("controller")
        self.app._calculate()
