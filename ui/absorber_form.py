"""Absorber experiment form: process chain, disturbance, dynamics and controller inputs."""

from decimal import Decimal
import tkinter as tk
from tkinter import ttk

from app.calculations import CONTROLLER_TYPES, DEFAULT_MODEL_VALUES, IMPULSE, RAMP, RECTANGLE, STEP, absorption_balance
from app.controller_extensions import EXTENSION_DEFAULTS, EXTENSION_FIELDS, extensions_from_form
from app.simulation import LEAN_GAS, RICH_ABSORBENT, tune_balanced_controller
from app.validation import parse_disturbance, parse_nonnegative_number, parse_percentage, parse_positive_number
from ui.model_dialog import ModelParametersDialog
from ui.theme import ACCENT, ACCENT_ACTIVE, BACKGROUND, BORDER, CARD_BACKGROUND, MUTED, SUCCESS, TEXT
from ui.ui_helpers import DisturbanceTooltip, FormulaPanel

DISTURBANCE_TYPES = {
    "Ступенчатое": STEP,
    "Импульсное": IMPULSE,
    "Временное прямоугольное": RECTANGLE,
    "Плавно нарастающее": RAMP,
}


class AbsorberForm:
    def __init__(self, app):
        self.app = app
        self.chain = LEAN_GAS
        self.model_values = DEFAULT_MODEL_VALUES.copy()
        self.model_dialog = None
        self.component_enabled = tk.BooleanVar(value=False)
        self.flow_enabled = tk.BooleanVar(value=False)
        self.disturbance_units = tk.StringVar(value="Доля")
        self._disturbance_units = "fraction"
        self._changing_units = False
        self.disturbance_units_hint = tk.StringVar(value="Доля: +0.10 = +10%, −0.10 = −10%. Диапазон: −0.99…9.99")
        self.component_value = tk.StringVar()
        self.flow_value = tk.StringVar()
        self.disturbance_type = tk.StringVar(value="Ступенчатое")
        self.dynamics_summary = tk.StringVar()
        self.start_time = tk.StringVar(value="10")
        self.simulation_duration = tk.StringVar(value="100")
        self.effect_duration = tk.StringVar(value="10")
        self.time_constant = tk.StringVar(value="10")
        self.delay = tk.StringVar(value="2")
        self.extension_values = {key: tk.StringVar(value="0") for key in EXTENSION_DEFAULTS}
        self.extension_entries = {}
        self.controller_enabled = tk.BooleanVar(value=False)
        self.controller_type = tk.StringVar(value="PI")
        self.proportional_gain = tk.StringVar(value="2")
        self.integral_time = tk.StringVar(value="20")
        self.derivative_time = tk.StringVar(value="1")
        self.control_limit = tk.StringVar(value="100")
        self.setpoint = tk.StringVar(value=app._format_number((1 / 6 * 100)))
        self.component_error = tk.StringVar()
        self.flow_error = tk.StringVar()
        self.dynamics_error = tk.StringVar()
        self.controller_error = tk.StringVar()
        self.tuning_summary = tk.StringVar(value="Автоподбор ещё не выполнялся.")
        self.controller_settings_title = tk.StringVar(value="Настройки PI-регулятора")
        self.controller_formula = tk.StringVar()
        self.controller_on_text = tk.StringVar(value="С PI-регулятором")
        self.component_label = tk.StringVar()
        self.component_symbol = tk.StringVar()
        self.flow_label = tk.StringVar()
        self.flow_symbol = tk.StringVar()

    def build_chain_card(self, parent):
        card = self.app._card(parent, 0)
        ttk.Label(card, text="Цепь управления", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 14))
        ttk.Button(
            card,
            text="Параметры модели",
            command=self.open_model_parameters,
            style="Toolbar.TButton",
        ).grid(row=0, column=1, sticky="e", pady=(0, 14))

        self.lean_button = ttk.Button(card, text="Обеднённый газ", command=lambda: self.select_chain(LEAN_GAS), style="Segment.TButton")
        self.lean_button.grid(row=1, column=0, sticky="ew")
        self.rich_button = ttk.Button(card, text="Насыщенный абсорбент", command=lambda: self.select_chain(RICH_ABSORBENT), style="Segment.TButton")
        self.rich_button.grid(row=1, column=1, sticky="ew", padx=(4, 0))
        card.columnconfigure((0, 1), weight=1)

    def open_model_parameters(self):
        if self.model_dialog is not None and self.model_dialog.winfo_exists():
            self.model_dialog.lift()
            self.model_dialog.focus_force()
            return

        def apply_values(values):
            self.model_values = values
            self.setpoint.set(self.app._format_number(self.current_baseline() * 100))
            self.app._mark_result_stale()
            self.draw_control_diagram()
            if self.app.last_calculation is None:
                self.app._draw_static_charts()
            self.app._set_status("Параметры модели обновлены")

        self.model_dialog = ModelParametersDialog(
            self.app.root,
            self.model_values,
            DEFAULT_MODEL_VALUES,
            apply_values,
            lambda: setattr(self, "model_dialog", None),
            self.app._format_number,
            BACKGROUND,
        )

    def build_disturbance_card(self, parent):
        card = self.app._card(parent, 1)
        ttk.Label(card, text="Возмущающие воздействия", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 14))

        self.component_check = ttk.Checkbutton(
            card,
            textvariable=self.component_label,
            variable=self.component_enabled,
            command=self.update_input_states,
        )
        self.component_check.grid(row=1, column=0, sticky="w")
        ttk.Label(card, textvariable=self.component_symbol, style="Body.TLabel").grid(row=1, column=1, padx=(8, 8))
        self.component_entry = ttk.Entry(card, textvariable=self.component_value, width=8, state="disabled")
        self.component_entry.grid(row=1, column=2, sticky="ew")
        ttk.Label(card, textvariable=self.component_error, style="Error.TLabel", wraplength=300).grid(row=2, column=0, columnspan=3, sticky="w", pady=(3, 8))

        self.flow_check = ttk.Checkbutton(
            card,
            textvariable=self.flow_label,
            variable=self.flow_enabled,
            command=self.update_input_states,
        )
        self.flow_check.grid(row=3, column=0, sticky="w")
        ttk.Label(card, textvariable=self.flow_symbol, style="Body.TLabel").grid(row=3, column=1, padx=(8, 8))
        self.flow_entry = ttk.Entry(card, textvariable=self.flow_value, width=8, state="disabled")
        self.flow_entry.grid(row=3, column=2, sticky="ew")
        ttk.Label(card, textvariable=self.flow_error, style="Error.TLabel", wraplength=300).grid(row=4, column=0, columnspan=3, sticky="w", pady=(3, 8))

        ttk.Label(card, textvariable=self.disturbance_units_hint, style="Muted.TLabel", wraplength=320).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(2, 14)
        )

        units_box = ttk.Combobox(card, textvariable=self.disturbance_units, values=("Доля", "%"),
                                state="readonly", width=8)
        units_box.grid(row=6, column=0, columnspan=3, sticky="w", pady=(0, 12))
        units_box.bind("<<ComboboxSelected>>", self.switch_disturbance_units)
        self.app._attach_tooltip(units_box, "Единицы возмущения: доля или относительные проценты")
        ttk.Button(
            card,
            text="Параметры динамики  →",
            command=lambda: self.app._show_page("dynamics"),
            style="Secondary.TButton",
        ).grid(row=7, column=0, columnspan=3, sticky="ew")
        ttk.Label(card, textvariable=self.dynamics_summary, style="Muted.TLabel", wraplength=320).grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(6, 0)
        )
        card.columnconfigure(0, weight=1)

    def build_control_diagram(self, parent):
        card = self.app._card(parent, 2, padding=(14, 12))
        ttk.Label(card, text="Структурная схема", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )
        ttk.Label(
            card,
            text="Включённые возмущения подсвечиваются синим.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(0, 6))
        self.control_diagram = tk.Canvas(
            card,
            width=340,
            height=180,
            background=CARD_BACKGROUND,
            borderwidth=0,
            highlightthickness=0,
        )
        self.control_diagram.grid(row=2, column=0, sticky="ew")
        self.component_value.trace_add("write", lambda *_: self.draw_control_diagram())
        self.flow_value.trace_add("write", lambda *_: self.draw_control_diagram())
        self.draw_control_diagram()

    def build_dynamics_card(self, parent):
        card = self.app._card(parent, 0)
        ttk.Label(card, text="Параметры времени и формы", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 14)
        )

        ttk.Label(card, text="Вид воздействия", style="Body.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Label(card, text="Начало, с", style="Body.TLabel").grid(row=1, column=1, sticky="w", padx=(10, 0))
        self.disturbance_type_box = ttk.Combobox(
            card,
            textvariable=self.disturbance_type,
            values=tuple(DISTURBANCE_TYPES),
            state="readonly",
            width=23,
        )
        self.disturbance_type_box.grid(row=2, column=0, sticky="ew", pady=(3, 12))
        self.disturbance_type_box.bind("<<ComboboxSelected>>", self.update_disturbance_type)
        self.disturbance_tooltip = DisturbanceTooltip(
            self.disturbance_type_box,
            self.disturbance_type.get,
        )
        self.start_time_entry = ttk.Entry(card, textvariable=self.start_time)
        self.start_time_entry.grid(row=2, column=1, sticky="ew", padx=(10, 0), pady=(3, 12))

        ttk.Label(card, text="Длительность моделирования, с", style="Body.TLabel", wraplength=170).grid(row=3, column=0, sticky="w")
        ttk.Label(card, text="Длительность воздействия, с", style="Body.TLabel", wraplength=170).grid(row=3, column=1, sticky="w", padx=(10, 0))
        self.simulation_duration_entry = ttk.Entry(card, textvariable=self.simulation_duration)
        self.simulation_duration_entry.grid(row=4, column=0, sticky="ew", pady=(3, 12))
        self.effect_duration_entry = ttk.Entry(card, textvariable=self.effect_duration)
        self.effect_duration_entry.grid(row=4, column=1, sticky="ew", padx=(10, 0), pady=(3, 12))

        time_constant_label = ttk.Label(
            card,
            text="Постоянная времени T, с",
            style="Body.TLabel",
        )
        time_constant_label.grid(row=5, column=0, sticky="w")
        delay_label = ttk.Label(card, text="Запаздывание L, с", style="Body.TLabel")
        delay_label.grid(row=5, column=1, sticky="w", padx=(10, 0))
        self.app._attach_tooltip(
            time_constant_label,
            "T характеризует инерционность объекта: примерно за T секунд отклик проходит 63% изменения.",
        )
        self.app._attach_tooltip(
            delay_label,
            "L — чистое запаздывание между воздействием и началом реакции объекта.",
        )
        self.time_constant_entry = ttk.Entry(card, textvariable=self.time_constant)
        self.time_constant_entry.grid(row=6, column=0, sticky="ew", pady=(3, 0))
        self.delay_entry = ttk.Entry(card, textvariable=self.delay)
        self.delay_entry.grid(row=6, column=1, sticky="ew", padx=(10, 0), pady=(3, 0))

        self.dynamics_error_label = ttk.Label(
            card,
            textvariable=self.dynamics_error,
            style="Error.TLabel",
            wraplength=330,
        )
        self.dynamics_error_label.grid(row=7, column=0, columnspan=2, sticky="w", pady=(6, 0))
        card.columnconfigure((0, 1), weight=1)
        for variable in (
            self.disturbance_type,
            self.start_time,
            self.simulation_duration,
            self.time_constant,
            self.delay,
        ):
            variable.trace_add("write", self.update_dynamics_summary)
        self.update_disturbance_type()

    def build_controller_card(self, parent):
        card = self.app._card(parent, 0, padding=(14, 10))
        ttk.Label(card, text="Режим управления", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10)
        )
        self.controller_off_button = ttk.Button(
            card,
            text="Без регулятора",
            command=lambda: self.set_controller_mode(False),
            style="SelectedSegment.TButton",
        )
        self.controller_off_button.grid(row=1, column=0, sticky="ew")
        self.controller_on_button = ttk.Button(
            card,
            textvariable=self.controller_on_text,
            command=lambda: self.set_controller_mode(True),
            style="Segment.TButton",
        )
        self.controller_on_button.grid(row=1, column=1, sticky="ew", padx=(4, 0))
        card.columnconfigure((0, 1), weight=1)

        parameters = self.app._card(parent, 1, padding=(14, 10))
        ttk.Label(parameters, textvariable=self.controller_settings_title, style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )
        ttk.Label(parameters, text="Тип регулятора", style="Body.TLabel").grid(
            row=1, column=0, sticky="w", pady=3
        )
        self.controller_type_box = ttk.Combobox(
            parameters,
            textvariable=self.controller_type,
            values=CONTROLLER_TYPES,
            state="readonly",
            width=10,
        )
        self.controller_type_box.grid(row=1, column=1, sticky="e", pady=3, padx=(10, 0))
        self.controller_type_box.bind("<<ComboboxSelected>>", self.update_controller_type)
        fields = (
            ("Коэффициент регулятора K", self.proportional_gain, "proportional_gain_entry"),
            ("Время интегрирования Ti, с", self.integral_time, "integral_time_entry"),
            ("Время дифференцирования Td, с", self.derivative_time, "derivative_time_entry"),
            ("Предел Δη, п.п.", self.control_limit, "control_limit_entry"),
            ("Задание концентрации, %", self.setpoint, "setpoint_entry"),
        )
        self.controller_entries = []
        controller_help = {
            "Коэффициент регулятора K": "K определяет силу реакции регулятора на текущую ошибку.",
            "Время интегрирования Ti, с": "Ti задаёт скорость накопления интегральной составляющей: меньше Ti — сильнее интегральное действие.",
            "Время дифференцирования Td, с": "Td определяет влияние скорости изменения выхода; большое Td повышает чувствительность к шуму.",
            "Предел Δη, п.п.": "Максимальное изменение η от исходного значения в процентных пунктах; η остаётся от 0 до 100%.",
            "Задание концентрации, %": "Значение выхода, к которому регулятор должен вернуть объект.",
        }
        for row, (label, variable, attribute) in enumerate(fields, start=2):
            field_label = ttk.Label(parameters, text=label, style="Body.TLabel")
            field_label.grid(row=row, column=0, sticky="w", pady=3)
            self.app._attach_tooltip(field_label, controller_help[label])
            entry = ttk.Entry(parameters, textvariable=variable, width=12, state="disabled")
            entry.grid(row=row, column=1, sticky="e", pady=3, padx=(10, 0))
            setattr(self, attribute, entry)
            self.controller_entries.append(entry)
        self.auto_tune_button = ttk.Button(
            parameters,
            text="Подобрать автоматически",
            command=self.auto_tune_controller,
            style="Secondary.TButton",
        )
        self.auto_tune_button.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        ttk.Label(
            parameters,
            textvariable=self.tuning_summary,
            style="Muted.TLabel",
            wraplength=330,
        ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(3, 0))
        ttk.Label(
            parameters,
            textvariable=self.controller_error,
            style="Error.TLabel",
            wraplength=330,
        ).grid(row=9, column=0, columnspan=2, sticky="w", pady=(5, 0))
        parameters.columnconfigure(0, weight=1)

        extensions = self.app._card(parent, 2, padding=(14, 10))
        extensions.columnconfigure(0, weight=1)
        ttk.Label(extensions, text="Датчик и механизм", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        for row, (key, label, scale) in enumerate(EXTENSION_FIELDS, 1):
            ttk.Label(extensions, text=label, style="Body.TLabel").grid(row=row, column=0, sticky="w", pady=3)
            entry = ttk.Entry(extensions, textvariable=self.extension_values[key], width=12, state="disabled")
            entry.grid(row=row, column=1, sticky="e", padx=(10, 0), pady=3)
            self.extension_entries[key] = entry
            self.controller_entries.append(entry)
        ttk.Label(extensions, text="Ноль отключает шум, фильтр, инерцию или предел скорости. Начальное число повторяет шум.",
                  style="Muted.TLabel", wraplength=330).grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.extension_values["noise_std"].trace_add("write", lambda *_: self.update_controller_type())
        explanation = self.app._card(parent, 3, padding=(14, 10))
        ttk.Label(explanation, text="Учебная модель", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )
        self.formula_panel = FormulaPanel(explanation, CARD_BACKGROUND)
        self.formula_panel.grid(row=1, column=0, sticky="ew")
        ttk.Label(
            explanation,
            textvariable=self.controller_formula,
            style="Body.TLabel",
            justify="left",
            wraplength=330,
        ).grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.update_controller_type()

    def select_chain(self, chain):
        self.chain = chain
        self.lean_button.configure(style="SelectedSegment.TButton" if chain == LEAN_GAS else "Segment.TButton")
        self.rich_button.configure(style="SelectedSegment.TButton" if chain == RICH_ABSORBENT else "Segment.TButton")

        if chain == LEAN_GAS:
            self.component_label.set("Состав исходного газа")
            self.component_symbol.set("Xг")
            self.flow_label.set("Расход газовой смеси")
            self.flow_symbol.set("Gг")
            self.app.response_subtitle.set("Концентрация обеднённого газа")
        else:
            self.component_label.set("Исходный состав абсорбента")
            self.component_symbol.set("Xа")
            self.flow_label.set("Расход абсорбента")
            self.flow_symbol.set("Gа")
            self.app.response_subtitle.set("Концентрация насыщенного абсорбента")

        self.setpoint.set(self.app._format_number(self.current_baseline() * 100))
        self.draw_control_diagram()
        self.app._mark_result_stale()
        if self.app.last_calculation is None:
            self.app._draw_static_charts()

    def current_baseline(self):
        return float(absorption_balance(**self.model_values)["xog" if self.chain == LEAN_GAS else "xna"])

    def update_controller_type(self, _event=None):
        self.app._refresh_chart_modes()
        controller_type = self.controller_type.get()
        self.controller_settings_title.set(f"Настройки {controller_type}-регулятора")
        self.controller_on_text.set(f"С {controller_type}-регулятором")
        details = []
        if "I" in controller_type:
            details.append("Интегратор защищён от насыщения.")
        if "D" in controller_type:
            details.append("Производная берётся по выходу без скачка от задания.")
        details.append("η = clip(η₀ + s·v): s = −1 для газа, +1 для жидкости. Предел Δη — в п.п.")
        self.controller_formula.set(
            f"{' '.join(details)}\n\n"
            "Выше показаны правила автоподбора. Для PID/PD при наличии "
            "запаздывания целевая динамика ускоряется вдвое."
        )
        if hasattr(self, "formula_panel"):
            self.formula_panel.set_controller_type(controller_type)
        self.auto_tune_button.configure(state="normal")
        self.tuning_summary.set(f"Автоподбор {controller_type} готов к запуску.")
        self.update_controller_entry_states()
        if hasattr(self.app, "disturbance_axis") and _event is not None:
            self.app._mark_result_stale()
            self.app._set_status("Тип регулятора изменён — выполните расчёт")

    def update_controller_entry_states(self):
        enabled = self.controller_enabled.get()
        controller_type = self.controller_type.get()
        states = {
            self.proportional_gain_entry: enabled,
            self.integral_time_entry: enabled and "I" in controller_type,
            self.derivative_time_entry: enabled and "D" in controller_type,
            self.control_limit_entry: enabled,
            self.setpoint_entry: enabled,
        }
        for key, entry in self.extension_entries.items():
            active = enabled and (key != "derivative_filter_time" or "D" in controller_type)
            if key == "noise_seed":
                try:
                    active = enabled and float(self.extension_values["noise_std"].get().replace(",", ".")) > 0
                except ValueError:
                    active = False
            states[entry] = active
        for entry, active in states.items():
            entry.configure(state="normal" if active else "disabled")

    def set_controller_mode(self, enabled):
        self.controller_enabled.set(enabled)
        if self.app.last_calculation is None:
            self.app.settling_time_label.set("Длительность регулирования (±5%)" if enabled
                                         else "Длительность установления (±5%)")
        self.controller_off_button.configure(
            style="Segment.TButton" if enabled else "SelectedSegment.TButton"
        )
        self.controller_on_button.configure(
            style="SelectedSegment.TButton" if enabled else "Segment.TButton"
        )
        self.update_controller_entry_states()
        self.controller_error.set("")
        if hasattr(self.app, "disturbance_axis"):
            self.app._mark_result_stale()
            if self.app.last_calculation is None:
                self.app._draw_static_charts()
            self.app._set_status("Режим управления изменён — выполните расчёт")

    def auto_tune_controller(self):
        fields = (
            ("Постоянная времени T", self.time_constant, self.time_constant_entry, parse_positive_number),
            ("Запаздывание L", self.delay, self.delay_entry, parse_nonnegative_number),
        )
        parsed = {}
        self.dynamics_error.set("")
        for _label, _variable, entry, _parser in fields:
            entry.configure(style="TEntry")

        for label, variable, entry, parser in fields:
            try:
                parsed[label] = parser(variable.get())
            except ValueError as error:
                entry.configure(style="Error.TEntry")
                self.dynamics_error.set(f"{label}: {error}")
                self.app._show_page("dynamics")
                self.app._set_status("Исправьте параметры динамики", error=True)
                return

        controller_type = self.controller_type.get()
        try:
            tuning = tune_balanced_controller(
                self.chain, self.model_values, controller_type,
                parsed["Постоянная времени T"],
                parsed["Запаздывание L"],
            )
        except ValueError as error:
            self.controller_error.set(str(error))
            self.app._set_status(str(error), error=True)
            return
        self.set_controller_mode(True)
        self.proportional_gain.set(self.app._format_number(tuning["proportional_gain"]))
        if tuning["integral_time"] is not None:
            self.integral_time.set(self.app._format_number(tuning["integral_time"]))
        if tuning["derivative_time"] is not None:
            self.derivative_time.set(self.app._format_number(tuning["derivative_time"]))
        tuned_parameters = [
            f"K={self.app._format_number(tuning['proportional_gain'])}",
        ]
        if tuning["integral_time"] is not None:
            tuned_parameters.append(f"Ti={self.app._format_number(tuning['integral_time'])} с")
        if tuning["derivative_time"] is not None:
            tuned_parameters.append(f"Td={self.app._format_number(tuning['derivative_time'])} с")
        self.tuning_summary.set(
            f"Для T={self.app._format_number(parsed['Постоянная времени T'])} с, "
            f"L={self.app._format_number(parsed['Запаздывание L'])} с: "
            f"λ={self.app._format_number(tuning['closed_loop_time'])} с, "
            f"{', '.join(tuned_parameters)}."
        )
        self.app._show_page("controller")
        self.app._set_status(f"Настройки {controller_type}-регулятора подобраны — выполните расчёт")

    def draw_control_diagram(self):
        if not hasattr(self, "control_diagram"):
            return

        canvas = self.control_diagram
        canvas.delete("all")
        active_fill = "#DBEAFE"
        inactive_fill = "#F3F4F6"
        component_symbol = "Xг" if self.chain == LEAN_GAS else "Xа"
        flow_symbol = "Gг" if self.chain == LEAN_GAS else "Gа"
        output_symbol = "Xог" if self.chain == LEAN_GAS else "Xна"

        def disturbance_box(y, symbol, enabled, value):
            fill = active_fill if enabled else inactive_fill
            outline = ACCENT if enabled else BORDER
            canvas.create_rectangle(8, y, 112, y + 40, fill=fill, outline=outline, width=2)
            canvas.create_text(
                60,
                y + 20,
                text=self.diagram_disturbance_text(symbol, enabled, value, self._disturbance_units),
                fill=ACCENT_ACTIVE if enabled else MUTED,
                font=("Segoe UI", 9, "bold" if enabled else "normal"),
                width=94,
            )

        disturbance_box(10, component_symbol, self.component_enabled.get(), self.component_value.get())
        disturbance_box(106, flow_symbol, self.flow_enabled.get(), self.flow_value.get())
        canvas.create_line(112, 30, 148, 66, fill=MUTED, width=2, arrow=tk.LAST)
        canvas.create_line(112, 126, 148, 90, fill=MUTED, width=2, arrow=tk.LAST)
        canvas.create_rectangle(148, 48, 282, 106, fill="#EAF2FF", outline=ACCENT, width=2)
        canvas.create_text(215, 77, text="Объект управления", fill=TEXT, font=("Segoe UI", 10, "bold"), width=120)
        canvas.create_line(215, 106, 215, 134, fill=MUTED, width=2, arrow=tk.LAST)
        canvas.create_rectangle(148, 136, 282, 174, fill="#ECFDF3", outline=SUCCESS, width=2)
        canvas.create_text(215, 155, text=f"Выходная концентрация {output_symbol}", fill=TEXT, font=("Segoe UI", 9, "bold"), width=124)

    @staticmethod
    def diagram_disturbance_text(symbol, enabled, value, units="fraction"):
        if not enabled:
            return f"{symbol}  выключено"
        try:
            return f"{symbol}  {parse_disturbance(value, units) * 100:+.1f}%"
        except ValueError:
            return f"{symbol}  активно"

    def update_input_states(self):
        self.set_entry_state(self.component_entry, self.component_enabled.get(), self.component_value)
        self.set_entry_state(self.flow_entry, self.flow_enabled.get(), self.flow_value)
        enabled = self.component_enabled.get() or self.flow_enabled.get()
        self.app.calculate_button.configure(state="normal" if enabled else "disabled")
        self.app.calculation_hint.set("" if enabled else "Включите хотя бы одно возмущение")
        self.clear_errors()
        self.draw_control_diagram()

    def update_dynamics_summary(self, *_):
        self.dynamics_summary.set(
            f"{self.disturbance_type.get()} · "
            f"t₀={self.start_time.get() or '—'} с · "
            f"T={self.time_constant.get() or '—'} с · "
            f"L={self.delay.get() or '—'} с"
        )

    def update_disturbance_type(self, _event=None):
        state = "disabled" if DISTURBANCE_TYPES[self.disturbance_type.get()] == STEP else "normal"
        self.effect_duration_entry.configure(state=state)
        self.update_dynamics_summary()

    @staticmethod
    def set_entry_state(entry, enabled, value):
        if enabled:
            entry.configure(state="normal")
        else:
            value.set("")
            entry.configure(state="disabled")

    def clear_errors(self):
        self.component_error.set("")
        self.flow_error.set("")
        self.dynamics_error.set("")
        self.controller_error.set("")
        self.component_entry.configure(style="TEntry")
        self.flow_entry.configure(style="TEntry")
        for entry in (
            self.start_time_entry,
            self.simulation_duration_entry,
            self.effect_duration_entry,
            self.time_constant_entry,
            self.delay_entry,
        ):
            entry.configure(style="TEntry")
        for entry in self.controller_entries:
            entry.configure(style="TEntry")

    def read_fraction(self, enabled, value, error_variable, entry):
        if not enabled:
            return 0.0
        try:
            return parse_disturbance(value.get(), self._disturbance_units)
        except ValueError as error:
            error_variable.set(str(error))
            entry.configure(style="Error.TEntry")
            self.app._show_page("disturbances")
            raise

    def read_dynamic_parameters(self):
        fields = (
            ("Начало воздействия", self.start_time, self.start_time_entry, parse_nonnegative_number),
            ("Длительность моделирования", self.simulation_duration, self.simulation_duration_entry, parse_positive_number),
            ("Постоянная времени T", self.time_constant, self.time_constant_entry, parse_positive_number),
            ("Запаздывание L", self.delay, self.delay_entry, parse_nonnegative_number),
        )
        parsed = {}
        for label, variable, entry, parser in fields:
            try:
                parsed[label] = parser(variable.get())
            except ValueError as error:
                entry.configure(style="Error.TEntry")
                self.dynamics_error.set(f"{label}: {error}")
                self.app._show_page("dynamics")
                raise

        kind = DISTURBANCE_TYPES[self.disturbance_type.get()]
        if kind == STEP:
            effect_duration = 1.0
        else:
            try:
                effect_duration = parse_positive_number(self.effect_duration.get())
            except ValueError as error:
                self.effect_duration_entry.configure(style="Error.TEntry")
                self.dynamics_error.set(f"Длительность воздействия: {error}")
                self.app._show_page("dynamics")
                raise

        start_time = parsed["Начало воздействия"]
        simulation_duration = parsed["Длительность моделирования"]
        if start_time >= simulation_duration:
            self.start_time_entry.configure(style="Error.TEntry")
            self.simulation_duration_entry.configure(style="Error.TEntry")
            self.dynamics_error.set("Начало воздействия должно быть раньше окончания моделирования.")
            self.app._show_page("dynamics")
            raise ValueError(self.dynamics_error.get())

        return {
            "kind": kind,
            "start_time": start_time,
            "simulation_duration": simulation_duration,
            "effect_duration": effect_duration,
            "time_constant": parsed["Постоянная времени T"],
            "delay": parsed["Запаздывание L"],
        }

    def read_controller_parameters(self):
        if not self.controller_enabled.get():
            return None

        controller_type = self.controller_type.get()
        fields = [
            ("Коэффициент K", self.proportional_gain, self.proportional_gain_entry, parse_nonnegative_number),
            ("Предел Δη, п.п.", self.control_limit, self.control_limit_entry, parse_percentage),
            ("Задание концентрации, %", self.setpoint, self.setpoint_entry, parse_percentage),
        ]
        if "I" in controller_type:
            fields.append(("Время интегрирования Ti", self.integral_time, self.integral_time_entry, parse_positive_number))
        if "D" in controller_type:
            fields.append(("Время дифференцирования Td", self.derivative_time, self.derivative_time_entry, parse_nonnegative_number))
        parsed = {}
        for label, variable, entry, parser in fields:
            try:
                parsed[label] = parser(variable.get())
            except ValueError as error:
                entry.configure(style="Error.TEntry")
                self.controller_error.set(f"{label}: {error}")
                self.app._show_page("controller")
                raise

        try:
            extensions = extensions_from_form({key: variable.get() for key, variable in self.extension_values.items()}, controller_type)
        except ValueError as problem:
            self.controller_error.set(str(problem))
            self.app._show_page("controller")
            raise
        return {
            **extensions,
            "controller_type": controller_type,
            "controller_gain": parsed["Коэффициент K"],
            "integral_time": parsed.get("Время интегрирования Ti", 1.0),
            "derivative_time": parsed.get("Время дифференцирования Td", 0.0),
            "control_limit": parsed["Предел Δη, п.п."],
            "setpoint": parsed["Задание концентрации, %"],
        }

    def set_disturbance_units(self, units):
        self._disturbance_units = units
        self.disturbance_units.set("%" if units == "percent" else "Доля")
        self.disturbance_units_hint.set("Проценты: +10 = +10%, −10 = −10%. Диапазон: −99…999%."
                                        if units == "percent" else "Доля: +0.10 = +10%, −0.10 = −10%. Диапазон: −0.99…9.99")

    def switch_disturbance_units(self, _event=None):
        units = "percent" if self.disturbance_units.get() == "%" else "fraction"
        if units == self._disturbance_units:
            return
        converted = []
        for variable, error, entry in ((self.component_value, self.component_error, self.component_entry),
                                       (self.flow_value, self.flow_error, self.flow_entry)):
            if not variable.get().strip():
                converted.append("")
                continue
            try:
                value = parse_disturbance(variable.get(), self._disturbance_units)
            except ValueError as problem:
                self.disturbance_units.set("%" if self._disturbance_units == "percent" else "Доля")
                error.set(str(problem))
                entry.configure(style="Error.TEntry")
                return
            converted.append(str(Decimal(variable.get().strip().replace(",", ".")).scaleb(2 if units == "percent" else -2)))
        sensitivity = None
        if self.app.analysis_panel.sensitivity_parameter.get() in ("Возмущение состава", "Возмущение расхода"):
            try:
                self.app.analysis_panel.parse_sensitivity_values()
            except ValueError as problem:
                self.disturbance_units.set("%" if self._disturbance_units == "percent" else "Доля")
                self.app.analysis_panel.sensitivity_summary.set(str(problem))
                return
            sensitivity = "; ".join(str(Decimal(item.strip().replace(",", ".")).scaleb(
                2 if units == "percent" else -2)) for item in self.app.analysis_panel.sensitivity_values.get().replace("\n", ";").split(";"))
        self._changing_units = True
        try:
            self.set_disturbance_units(units)
            if sensitivity is not None:
                self.app.analysis_panel.sensitivity_values.set(sensitivity)
            for variable, value in zip((self.component_value, self.flow_value), converted):
                variable.set(value)
        finally:
            self._changing_units = False
        self.app._mark_result_stale()
        self.draw_control_diagram()
