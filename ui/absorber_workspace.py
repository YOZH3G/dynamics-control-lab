"""Absorber workspace: its sidebar sections, experiment, assignment, comparison and analysis.

The main window owns the layout, the shared charts, background tasks and the session file; the
workspace fills the absorber's sections, calculates its experiment and draws it on the shared charts.
Its panels keep a reference to the window and reach the workspace as ``app.absorber``.
"""

import copy
from decimal import Decimal
import tkinter as tk
from tkinter import ttk

import numpy as np

from app.calculations import DEFAULT_MODEL_VALUES, absorption_balance
from app.controller_extensions import EXTENSION_DEFAULTS, EXTENSION_FIELDS
from app.physical_models import MODELS, run_model
from app.scenario_store import ScenarioStore
from app.session_store import validate_input_state
from app.simulation import LEAN_GAS, RICH_ABSORBENT
from app.validation import parse_positive_number
from ui.absorber_form import AbsorberForm
from ui.analysis_panel import AnalysisPanel
from ui.assignment_panel import AssignmentPanel
from ui.comparison_panel import ComparisonPanel
from ui.export_panel import ExportPanel
from ui.identification_page import IdentificationPage
from ui.scenario_library import ScenarioLibrary
from ui.theme import ACCENT, BACKGROUND, BORDER, MUTED, TEXT
from ui.ui_helpers import ScrollablePage, plot_samples

CURVE_STYLES = {
    "Исходный режим": ("#667085", "--"),
    "Только состав": ("#F59E0B", "-"),
    "Только расход": ("#16A34A", "-"),
    "Совместное воздействие": (ACCENT, "-"),
}


class AbsorberWorkspace:
    def __init__(self, app, scenario_path=None):
        self.app = app
        # The panels created here look the workspace up as app.absorber.
        app.absorber = self
        self.absorber_form = AbsorberForm(app)
        self.scenario_library = ScenarioLibrary(
            app, ScenarioStore(scenario_path) if scenario_path is not None else ScenarioStore()
        )
        self.calculate_button_text = tk.StringVar(value="Рассчитать")
        self.baseline_result = tk.StringVar(value="—")
        self.disturbance_result = tk.StringVar(value="—")
        self.calculated_result = tk.StringVar(value="—")
        self.final_result = tk.StringVar(value="—")
        self.result_mode = tk.StringVar(value="Без регулятора")
        self.settling_time_label = tk.StringVar(value="Длительность установления (±5%)")
        self.calculation_steps = tk.StringVar(value="Выполните расчёт, чтобы увидеть происхождение результата.")
        self.transition_values = {
            key: tk.StringVar(value="—")
            for key in (
                "initial",
                "steady",
                "maximum_deviation",
                "relative_deviation",
                "time_constant",
                "settling_time",
                "settling_moment",
                "static_error",
                "final_error",
                "settling_status",
                "iae",
                "saturation_duration",
            )
        }
        self.active_scenario = tk.StringVar(value="Сценарий ещё не применён.")
        self.applied_scenario = copy.deepcopy(self.scenario_library.scenarios[0])
        self.last_calculation = None
        self.last_prediction = None
        self.analysis_panel = AnalysisPanel(app)
        self.export_panel = ExportPanel(app)
        self.comparison_page = ComparisonPanel(app)
        self.assignment_panel = AssignmentPanel(app)
        self._undo_clear = None

    def connect(self):
        """Initial chain and change tracking, once the window and the sections are built."""
        self.assignment_panel.update_summary()
        self.assignment_panel.update_route()
        self.absorber_form.select_chain(LEAN_GAS)
        for variable in (self.absorber_form.component_enabled, self.absorber_form.flow_enabled, self.absorber_form.component_value, self.absorber_form.flow_value,
                         self.absorber_form.disturbance_type, self.absorber_form.start_time, self.absorber_form.simulation_duration, self.absorber_form.effect_duration,
                         self.absorber_form.time_constant, self.absorber_form.delay, self.absorber_form.controller_enabled, self.absorber_form.controller_type,
                         self.absorber_form.proportional_gain, self.absorber_form.integral_time, self.absorber_form.derivative_time, self.absorber_form.control_limit, self.absorber_form.setpoint, *self.absorber_form.extension_values.values()):
            variable.trace_add("write", self._mark_result_stale)
        for variable in (self.assignment_panel.assignment_enabled, self.assignment_panel.predicted_direction, self.assignment_panel.predicted_steady,
                         self.assignment_panel.predicted_fastest, self.assignment_panel.predicted_correction, self.analysis_panel.sensitivity_parameter,
                         *self.absorber_form.extension_values.values(), self.analysis_panel.sensitivity_values, self.analysis_panel.map_controller_type, self.analysis_panel.map_gain_min,
                         self.analysis_panel.map_gain_max, self.analysis_panel.map_integral_min, self.analysis_panel.map_integral_max,
                         self.analysis_panel.map_derivative_time, self.analysis_panel.map_grid_size):
            variable.trace_add("write", self._invalidate_task)

    def build_route_button(self, topbar):
        self.route_stage = tk.StringVar(value="Учебный маршрут")
        self.route_button = ttk.Button(topbar, textvariable=self.route_stage,
                                       command=self.assignment_panel.advance_step, style="Toolbar.TButton")
        self.route_button.grid(row=0, column=1, sticky="e")

    def build_sections(self, contents):
        self.absorber_form.build_chain_card(contents["disturbances"])
        self.absorber_form.build_disturbance_card(contents["disturbances"])
        self.absorber_form.build_control_diagram(contents["disturbances"])
        self.absorber_form.build_dynamics_card(contents["dynamics"])
        self._build_result_card(contents["results"])
        ttk.Label(contents["comparison"], text="Выберите опыты в широкой таблице над графиками. Пустой выбор показывает все опыты. Отчёты включают все закреплённые опыты.", wraplength=330, style="Body.TLabel").grid(row=0, column=0, sticky="w")
        self.absorber_form.build_controller_card(contents["controller"])
        self.analysis_panel.build_sensitivity_card(contents["sensitivity"])
        self.analysis_panel.build_map_card(contents["tuning_map"])
        self._build_scenarios_card(contents["scenarios"])
        self.export_panel.build(contents["export"])
        self.identification_page = IdentificationPage(contents["identification"], self.app)
        self.identification_page.grid(row=0, column=0, sticky="nsew")

    def build_actions(self, actions):
        """Experiment buttons above the window's cancel button; the window hides them on other models' pages."""
        self.calculate_button = ttk.Button(
            actions,
            textvariable=self.calculate_button_text,
            command=self._calculate,
            style="Primary.TButton",
            state="disabled",
        )
        self.calculate_button.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        ttk.Button(actions, text="Очистить опыт", command=self._reset, style="Secondary.TButton").grid(row=0, column=1, sticky="ew", padx=(5, 0))
        ttk.Button(actions, text="Исходные настройки", command=self._restore_experiment_defaults).grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.undo_clear_button = ttk.Button(actions, text="Отменить очистку", command=self._undo_clear_experiment, state="disabled")
        self.undo_clear_button.grid(row=1, column=1, sticky="ew", pady=(6, 0))
        self.calculation_hint = tk.StringVar(value="Включите хотя бы одно возмущение")
        ttk.Label(actions, textvariable=self.calculation_hint, style="Error.TLabel", wraplength=310).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.absorber_action_widgets = list(actions.winfo_children())

    def build_comparison_workspace(self, workspace):
        """Wide table of pinned runs above the charts, shown on the comparison section."""
        self.comparison_workspace = ScrollablePage(workspace, background=BACKGROUND)
        self.comparison_workspace.grid(row=2, column=0, sticky="ew")
        self.comparison_workspace.columnconfigure(0, weight=1)
        self.comparison_page.build(self.comparison_workspace.content)
        self.comparison_workspace.bind_mousewheel()
        self.comparison_workspace.canvas.configure(height=260)
        self.comparison_workspace.grid_remove()

    def task_signature(self, kind):
        steady_prediction = self.assignment_panel.predicted_steady.get()
        try:
            steady_prediction = float(steady_prediction.replace(",", "."))
        except ValueError:
            pass
        signature = [self._normalized_input_state(self._capture_input_state()),
                     copy.deepcopy(self.assignment_panel.current_lesson), self.assignment_panel.assignment_tolerance_percent,
                     self.assignment_panel.assignment_absolute_tolerance, self.assignment_panel.assignment_attempts,
                     self.assignment_panel.assignment_enabled.get(), self.assignment_panel.predicted_direction.get(),
                     steady_prediction, self.assignment_panel.predicted_fastest.get(), self.assignment_panel.predicted_correction.get()]
        if kind == "sensitivity":
            try:
                values = self.analysis_panel.parse_sensitivity_values()
            except ValueError:
                values = self.analysis_panel.sensitivity_values.get()
            signature.extend((self.analysis_panel.sensitivity_parameter.get(), values))
        elif kind == "map":
            signature.extend(variable.get() for variable in (self.analysis_panel.map_controller_type, self.analysis_panel.map_gain_min,
                             self.analysis_panel.map_gain_max, self.analysis_panel.map_integral_min, self.analysis_panel.map_integral_max,
                             self.analysis_panel.map_derivative_time, self.analysis_panel.map_grid_size, *self.absorber_form.extension_values.values()))
        if kind == "identification":
            signature.append(self.identification_page.signature())
        return signature

    def capture(self, *, copy_results=True):
        """Absorber part of the laboratory snapshot saved in the session."""
        calculation = self.last_calculation
        snapshot = None if calculation is None else {
            key: copy.deepcopy(calculation[key])
            for key in ("input_state", "title", "lesson", "prediction", "evaluation")
        }
        if snapshot is not None:
            snapshot["experiment_origin"] = copy.deepcopy(calculation.get("experiment_origin"))
        return {
            "input_state": self._capture_input_state(), "scenario": copy.deepcopy(self.applied_scenario),
            "lesson": copy.deepcopy(self.assignment_panel.current_lesson), "scenario_name": self.scenario_library.selected.get(),
            "variant": int(self.scenario_library.variant.get()), "active_scenario": self.active_scenario.get(),
            "learning_mode": self.assignment_panel.learning_mode.get(), "learning_step": self.assignment_panel.learning_step,
            "assignment_enabled": self.assignment_panel.assignment_enabled.get(), "attempts": self.assignment_panel.assignment_attempts,
            "steady_tolerance_percent": self.assignment_panel.assignment_tolerance_percent,
            "steady_absolute_tolerance": self.assignment_panel.assignment_absolute_tolerance,
            "prediction_fields": {"direction": self.assignment_panel.predicted_direction.get(), "steady": self.assignment_panel.predicted_steady.get(),
                                  "fastest": self.assignment_panel.predicted_fastest.get(), "correction": self.assignment_panel.predicted_correction.get()},
            "evaluation": copy.deepcopy(self.assignment_panel.assignment_evaluation), "student_name": self.assignment_panel.student_name.get(),
            "conclusion": self.assignment_panel.student_conclusion.get(),
            "selected_runs": list(self.comparison_page.table.selection()), "last_calculation": snapshot,
            "identification": self.identification_page.capture(validate=False, copy_results=copy_results),
        }

    def restore(self, laboratory, result):
        scenario = laboratory["scenario"]
        if scenario["name"] not in self.scenario_library.by_name:
            self.scenario_library.scenarios = (*self.scenario_library.scenarios, copy.deepcopy(scenario))
            self.scenario_library.by_name[scenario["name"]] = self.scenario_library.scenarios[-1]
            self.scenario_library.box.configure(values=tuple(self.scenario_library.by_name))
        self.applied_scenario = copy.deepcopy(scenario)
        selected_name = laboratory["scenario_name"]
        self.scenario_library.selected.set(selected_name if selected_name in self.scenario_library.by_name else scenario["name"])
        self.scenario_library.variant.set(str(laboratory["variant"]))
        self.scenario_library.update_description()
        self.assignment_panel.current_lesson = laboratory["lesson"]
        self.assignment_panel.assignment_tolerance_percent = laboratory["steady_tolerance_percent"]
        self.assignment_panel.assignment_absolute_tolerance = laboratory["steady_absolute_tolerance"]
        self.assignment_panel.learning_mode.set(laboratory["learning_mode"])
        self.assignment_panel.learning_step = laboratory["learning_step"]
        self.assignment_panel.assignment_enabled.set(laboratory["assignment_enabled"])
        self.assignment_panel.update_states()
        self.assignment_panel.assignment_attempts = laboratory["attempts"]
        self.assignment_panel.assignment_evaluation = laboratory["evaluation"]
        self.assignment_panel.student_name.set(laboratory["student_name"])
        self.assignment_panel.student_conclusion.set(laboratory["conclusion"])
        for key, variable in (("direction", self.assignment_panel.predicted_direction), ("steady", self.assignment_panel.predicted_steady),
                              ("fastest", self.assignment_panel.predicted_fastest), ("correction", self.assignment_panel.predicted_correction)):
            variable.set(laboratory["prediction_fields"][key])
        self.last_calculation = result
        self.last_prediction = None if result is None else copy.deepcopy(result["prediction"])
        if result is None:
            self._clear_result_values()
        else:
            self._restore_input_state(result["input_state"], preserve_result=True)
            self.baseline_result.set(self.app._format_number(result["baseline"] * 100) + "%")
            self.disturbance_result.set(f"{self.app._format_number(result['combined_fraction'])} ({result['combined_fraction'] * 100:+.1f}%)")
            self.calculated_result.set(self.app._format_number(result["calculated"] * 100) + "%")
            self.final_result.set(self.app._format_number(result["final_response"][-1] * 100) + "%")
            self.result_mode.set(result["result_mode"])
            self._update_calculation_steps(result["component_fraction"], result["flow_fraction"],
                                           result["combined_fraction"], result["baseline"], result["calculated"])
            self._update_transition_metrics(result["metrics"], result["dynamics"], result["response_start"])
            self._draw_calculation_result(result)
            self.add_comparison_button.configure(state="normal")
            for button in self.export_panel.export_buttons:
                button.configure(state="normal")
        self._restore_input_state(laboratory["input_state"], draft=True, preserve_result=True)
        if result is None:
            self._draw_static_charts()
        self.active_scenario.set(laboratory["active_scenario"])
        self.assignment_panel.update_summary()
        self.assignment_panel.update_route()
        evaluation = self.assignment_panel.assignment_evaluation
        if evaluation is not None:
            self.assignment_panel.assignment_feedback.set(f"Результат: {evaluation['score']} из {evaluation['total']}.\n"
                                         + "\n".join(evaluation["lines"])
                                         + f"\nОсталось попыток: {self.assignment_panel.current_lesson['attempt_limit'] - self.assignment_panel.assignment_attempts}.")
        self.comparison_page.refresh_table(laboratory["selected_runs"])
        self.identification_page.restore(laboratory.get("identification"))

    def latest_result(self):
        """Native run shown on the common result page after a session is restored."""
        return self.last_calculation

    def _build_result_card(self, parent):
        card = self.app._card(parent, 0)
        ttk.Label(card, text="Результат", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        rows = (
            ("Базовое значение", self.baseline_result),
            ("Суммарная доля", self.disturbance_result),
            ("Расчётное значение", self.calculated_result),
            ("Режим", self.result_mode),
            ("В конце моделирования", self.final_result),
        )
        for row, (label, variable) in enumerate(rows, start=1):
            ttk.Label(card, text=label, style="Body.TLabel").grid(row=row, column=0, sticky="w", pady=6)
            ttk.Label(card, textvariable=variable, style="ResultValue.TLabel").grid(row=row, column=1, sticky="e", pady=6)
        card.columnconfigure(0, weight=1)

        calculation = self.app._card(parent, 1)
        ttk.Label(calculation, text="Ход расчёта", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 10)
        )
        ttk.Label(
            calculation,
            textvariable=self.calculation_steps,
            style="Body.TLabel",
            justify="left",
            wraplength=330,
        ).grid(row=1, column=0, sticky="w")

        metrics = self.app._card(parent, 2)
        ttk.Label(metrics, text="Показатели переходного процесса", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )
        metric_rows = (
            ("Начальное значение", "initial"),
            ("Теоретический установившийся режим", "steady"),
            ("Максимальное отклонение", "maximum_deviation"),
            ("Относительное отклонение", "relative_deviation"),
            ("Постоянная времени T", "time_constant"),
            (self.settling_time_label, "settling_time"),
            ("Момент установления на графике", "settling_moment"),
            ("Теоретическая ошибка eуст", "static_error"),
            ("Ошибка в конце опыта", "final_error"),
            ("Статус установления", "settling_status"),
            ("IAE относительно задания", "iae"),
            ("Длительность насыщения η", "saturation_duration"),
        )
        metric_help = {
            "maximum_deviation": "Наибольшее расстояние выхода от начального значения за время моделирования.",
            "relative_deviation": "Максимальное отклонение, выраженное в процентах от начального значения.",
            "time_constant": "Параметр инерционности объекта, заданный перед расчётом.",
            "settling_time": "Время после начала реакции до входа в полосу ±5%; после завершения воздействия нахождение в полосе подтверждается ещё в течение T.",
            "settling_moment": "Абсолютная координата момента установления на оси времени графика.",
            "static_error": "Разность между заданием (базой без регулятора) и теоретическим режимом; достижение этого режима не гарантируется.",
            "final_error": "Разность между заданием (базой без регулятора) и последней точкой выхода, в процентных пунктах.",
            "iae": "Интеграл модуля ошибки относительно задания за весь опыт, в п.п.·с. Применим только с регулятором.",
            "saturation_duration": "Суммарное время на нижнем или верхнем допустимом пределе η, в секундах.",
        }
        for row, (label, key) in enumerate(metric_rows, start=1):
            label_options = {"textvariable": label} if isinstance(label, tk.StringVar) else {"text": label}
            metric_label = ttk.Label(metrics, style="Body.TLabel", **label_options)
            metric_label.configure(wraplength=200)
            metric_label.grid(row=row, column=0, sticky="w", pady=3)
            if key in metric_help:
                self.app._attach_tooltip(metric_label, metric_help[key])
            # Long values such as "неприменимо без регулятора" wrap instead of widening the sidebar.
            ttk.Label(metrics, textvariable=self.transition_values[key], style="ResultValue.TLabel", wraplength=140,
                      justify="right").grid(row=row, column=1, sticky="e", pady=3, padx=(8, 0))
        metrics.columnconfigure(0, weight=1)

        comparison_action = self.app._card(parent, 3, padding=(14, 12))
        ttk.Label(
            comparison_action,
            text="Сравнение опытов",
            style="CardTitle.TLabel",
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Label(
            comparison_action,
            text="Закрепите текущий расчёт, затем измените параметры и повторите опыт.",
            style="Muted.TLabel",
            wraplength=330,
        ).grid(row=1, column=0, sticky="w", pady=(0, 10))
        self.add_comparison_button = ttk.Button(
            comparison_action,
            text="Добавить текущий расчёт",
            command=self.comparison_page.add_current,
            style="Primary.TButton",
            state="disabled",
        )
        self.add_comparison_button.grid(row=2, column=0, sticky="ew")

    def _build_scenarios_card(self, parent):
        self.scenario_library.build(parent)
        self.assignment_panel.build(parent)

    def _build_metric_strip(self, parent):
        metrics = ttk.Frame(parent, style="App.TFrame")
        self.metric_strip = metrics
        metrics.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        metrics.columnconfigure((0, 1, 2), weight=1)
        for column, (label, variable) in enumerate((
            ("Базовое значение", self.baseline_result),
            ("Суммарная доля", self.disturbance_result),
            ("Расчётное значение", self.calculated_result),
        )):
            card = ttk.Frame(metrics, style="Card.TFrame", padding=(8, 10))
            card.grid(row=0, column=column, sticky="ew", padx=(0, 6) if column < 2 else 0)
            ttk.Label(card, text=label, style="MetricTitle.TLabel", wraplength=160).grid(row=0, column=0, sticky="w")
            ttk.Label(card, textvariable=variable, style="MetricValue.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 0))

    def _apply_scenario_data(self, scenario, variant_number=None):
        self.app._cancel_task(announce=False)
        self._undo_clear = None
        self.undo_clear_button.configure(state="disabled")
        self.app._page_context.clear()
        for page in self.app.pages.values():
            page.scroll_to_top()
        self.applied_scenario = copy.deepcopy(scenario)
        self.absorber_form.model_values = scenario["model_values"].copy()
        self.assignment_panel.current_lesson = scenario["lesson"]
        self.assignment_panel.assignment_attempts = 0
        self.assignment_panel.assignment_evaluation = None
        self.assignment_panel.learning_step = 1
        self.assignment_panel.update_summary()
        self.assignment_panel.update_route()
        self.absorber_form.select_chain(scenario["chain"])
        self.absorber_form.component_enabled.set(scenario["component"] is not None)
        self.absorber_form.flow_enabled.set(scenario["flow"] is not None)
        self.absorber_form.component_value.set(
            "" if scenario["component"] is None else str(Decimal(str(scenario["component"])).scaleb(2 if self.absorber_form._disturbance_units == "percent" else 0))
        )
        self.absorber_form.flow_value.set(
            "" if scenario["flow"] is None else str(Decimal(str(scenario["flow"])).scaleb(2 if self.absorber_form._disturbance_units == "percent" else 0))
        )
        self.absorber_form.disturbance_type.set(scenario["disturbance_type"])
        self.absorber_form.start_time.set(self.app._format_number(scenario["start_time"]))
        self.absorber_form.simulation_duration.set(self.app._format_number(scenario["simulation_duration"]))
        self.absorber_form.effect_duration.set(self.app._format_number(scenario["effect_duration"]))
        self.absorber_form.time_constant.set(self.app._format_number(scenario["time_constant"]))
        self.absorber_form.delay.set(self.app._format_number(scenario["delay"]))
        self.absorber_form.update_disturbance_type()

        controller = scenario["controller"]
        for key, label, scale in EXTENSION_FIELDS:
            self.absorber_form.extension_values[key].set(format((controller or {}).get(key, 0) * scale, ".15g"))
        if controller is None:
            self.absorber_form.set_controller_mode(False)
        else:
            self.absorber_form.controller_type.set(controller["type"])
            self.absorber_form.update_controller_type()
            self.absorber_form.set_controller_mode(True)
            self.absorber_form.proportional_gain.set(self.app._format_number(controller["gain"]))
            self.absorber_form.integral_time.set(self.app._format_number(controller["integral_time"]))
            self.absorber_form.derivative_time.set(self.app._format_number(controller["derivative_time"]))
            self.absorber_form.control_limit.set(self.app._format_number(controller["control_limit"] * 100))
            setpoint = controller.get("setpoint")
            self.absorber_form.setpoint.set(
                self.app._format_number(100 * (self.absorber_form.current_baseline() if setpoint is None else setpoint))
            )

        self.assignment_panel.assignment_tolerance_percent = scenario.get("steady_tolerance_percent", 5.0)
        self.assignment_panel.assignment_absolute_tolerance = scenario.get("steady_absolute_tolerance", 1e-6)
        self.absorber_form.update_input_states()
        self._clear_result_values()
        self.assignment_panel.predicted_direction.set("")
        self.assignment_panel.predicted_steady.set("")
        self.assignment_panel.predicted_fastest.set("")
        self.assignment_panel.predicted_correction.set("")
        self.assignment_panel.assignment_feedback.set("Заполните прогноз и нажмите «Рассчитать».")
        scenario_title = scenario["name"]
        if variant_number is not None:
            scenario_title += f" — вариант {variant_number}"
        self.active_scenario.set(f"Применён: {scenario_title}.")
        self.app.topbar_context.set(
            f"{scenario_title} · "
            f"{'без регулятора' if controller is None else controller['type'] + '-регулятор'}"
        )
        self._draw_static_charts()
        self.app._show_page("scenarios")
        self.app._set_status("Сценарий применён — выполните расчёт")

    @staticmethod
    def _normalized_input_state(state):
        result = dict(state)
        for key in EXTENSION_DEFAULTS:
            result.setdefault(key, 0)
        for key, value in result.items():
            if isinstance(value, str):
                try:
                    result[key] = float(value.replace(",", "."))
                except ValueError:
                    pass
        units = result.pop("disturbance_units", "fraction")
        if units == "percent":
            for key in ("component_value", "flow_value"):
                if isinstance(result[key], (int, float)):
                    result[key] = float(Decimal(state[key].replace(",", ".")).scaleb(-2))
        if not result["component_enabled"]:
            result["component_value"] = None
        if not result["flow_enabled"]:
            result["flow_value"] = None
        if result["disturbance_type"] == "Ступенчатое":
            result["effect_duration"] = None
        if not result["controller_enabled"]:
            for key in ("controller_type", "proportional_gain", "integral_time", "derivative_time", "control_limit", "setpoint", *EXTENSION_DEFAULTS):
                result[key] = None
        else:
            if "I" not in result["controller_type"]:
                result["integral_time"] = None
            if "D" not in result["controller_type"]:
                result["derivative_time"] = None
                result["derivative_filter_time"] = None
            if result["noise_std"] == 0:
                result["noise_seed"] = None
        return result

    def _mark_result_stale(self, *_):
        if self.absorber_form._changing_units:
            return
        self._invalidate_task()
        if self.last_calculation is None or "input_state" not in self.last_calculation:
            if hasattr(self, "identification_page") and self.app.current_page == "identification" and self.identification_page.view.get() == "Ошибки на абсорбере":
                self.identification_page.draw()
            return
        stale = self._normalized_input_state(self._capture_input_state()) != self._normalized_input_state(self.last_calculation["input_state"])
        self.export_panel.export_summary.set("Параметры изменены — пересчитайте опыт. Экспорт относится к последнему расчёту."
                                if stale else "Расчёт готов к экспорту.")
        chain_name = "обеднённый газ" if self.last_calculation["chain"] == LEAN_GAS else "насыщенный абсорбент"
        self.app.response_subtitle.set(f"Параметры изменены — последний расчёт: {chain_name}" if stale
                                   else f"Последний расчёт: {chain_name}")
        if hasattr(self, "identification_page") and self.app.current_page == "identification" and self.identification_page.view.get() == "Ошибки на абсорбере":
            self.identification_page.draw()

    def _capture_input_state(self):
        return {
            "chain": self.absorber_form.chain,
            "model_values": self.absorber_form.model_values.copy(),
            "model_version": 2,
            "component_enabled": self.absorber_form.component_enabled.get(),
            "flow_enabled": self.absorber_form.flow_enabled.get(),
            "disturbance_units": self.absorber_form._disturbance_units,
            "component_value": self.absorber_form.component_value.get(),
            "flow_value": self.absorber_form.flow_value.get(),
            "disturbance_type": self.absorber_form.disturbance_type.get(),
            "start_time": self.absorber_form.start_time.get(),
            "simulation_duration": self.absorber_form.simulation_duration.get(),
            "effect_duration": self.absorber_form.effect_duration.get(),
            "time_constant": self.absorber_form.time_constant.get(),
            "delay": self.absorber_form.delay.get(),
            **{key: value.get() for key, value in self.absorber_form.extension_values.items()},
            "controller_enabled": self.absorber_form.controller_enabled.get(),
            "controller_type": self.absorber_form.controller_type.get(),
            "proportional_gain": self.absorber_form.proportional_gain.get(),
            "integral_time": self.absorber_form.integral_time.get(),
            "derivative_time": self.absorber_form.derivative_time.get(),
            "control_limit": self.absorber_form.control_limit.get(),
            "setpoint": self.absorber_form.setpoint.get(),
        }

    def _restore_input_state(self, state, *, draft=False, preserve_result=False):
        self.app._cancel_task(announce=False)
        validate_input_state(state, draft=draft)
        if state["chain"] not in (LEAN_GAS, RICH_ABSORBENT):
            raise ValueError("неизвестная цепь управления")
        if state.get("model_version") != 2:
            raise ValueError("Несовместимая версия модели; старые сеансы не поддерживаются.")
        model_values = state.get("model_values", {})
        if not isinstance(model_values, dict):
            raise ValueError("некорректные параметры математической модели")
        restored_model = DEFAULT_MODEL_VALUES.copy()
        for key in restored_model:
            restored_model[key] = float(model_values.get(key, restored_model[key]))
        absorption_balance(**restored_model)
        self.absorber_form.model_values = restored_model
        self.absorber_form.set_disturbance_units(state.get("disturbance_units", "fraction"))
        self.absorber_form.select_chain(state["chain"])
        for key, variable in self.absorber_form.extension_values.items():
            variable.set(state.get(key, "0"))
        for key, variable in (
            ("component_enabled", self.absorber_form.component_enabled),
            ("flow_enabled", self.absorber_form.flow_enabled),
            ("component_value", self.absorber_form.component_value),
            ("flow_value", self.absorber_form.flow_value),
            ("disturbance_type", self.absorber_form.disturbance_type),
            ("start_time", self.absorber_form.start_time),
            ("simulation_duration", self.absorber_form.simulation_duration),
            ("effect_duration", self.absorber_form.effect_duration),
            ("time_constant", self.absorber_form.time_constant),
            ("delay", self.absorber_form.delay),
            ("controller_type", self.absorber_form.controller_type),
            ("proportional_gain", self.absorber_form.proportional_gain),
            ("integral_time", self.absorber_form.integral_time),
            ("derivative_time", self.absorber_form.derivative_time),
            ("control_limit", self.absorber_form.control_limit),
            ("setpoint", self.absorber_form.setpoint),
        ):
            variable.set(state[key])
        self.absorber_form.set_controller_mode(bool(state["controller_enabled"]))
        self.absorber_form.update_controller_type()
        self.absorber_form.update_disturbance_type()
        self.absorber_form.update_input_states()
        self.absorber_form.draw_control_diagram()
        if not preserve_result:
            self._clear_result_values()
            self._draw_static_charts()

    def _active_scenario_title(self, include_prefix=False):
        title = self.active_scenario.get()
        if not title.startswith("Применён:"):
            return "Свободный расчёт"
        if include_prefix:
            return title
        return title.removeprefix("Применён: ").removesuffix(".")

    def _calculate(self):
        self.absorber_form.clear_errors()
        try:
            prediction = self.assignment_panel.read_prediction()
            component_fraction = self.absorber_form.read_fraction(
                self.absorber_form.component_enabled.get(), self.absorber_form.component_value, self.absorber_form.component_error, self.absorber_form.component_entry
            )
            flow_fraction = self.absorber_form.read_fraction(
                self.absorber_form.flow_enabled.get(), self.absorber_form.flow_value, self.absorber_form.flow_error, self.absorber_form.flow_entry
            )
            dynamics = self.absorber_form.read_dynamic_parameters()
            controller = self.absorber_form.read_controller_parameters()
        except ValueError:
            self.app._set_status("Исправьте значение", error=True)
            return

        state = self._capture_input_state()
        metadata = {"disturbance_type": self.absorber_form.disturbance_type.get(), "input_state": state,
                    "title": self._active_scenario_title(), "lesson": copy.deepcopy(self.assignment_panel.current_lesson),
                    "experiment_origin": self.identification_page.active_origin()}
        self.app._start_task("Расчёт опыта", lambda cancel, progress: run_model("absorber",
            dict(chain=state["chain"], parameters=state["model_values"], component=component_fraction,
                 flow=flow_fraction, dynamics=dynamics, controller=controller), cancel=cancel, progress=progress),
            lambda result: self._apply_calculation(result, prediction, metadata))

    def _apply_calculation(self, result, prediction, metadata):
        self.app.model_results["absorber"] = MODELS.result("absorber", result)
        self._undo_clear = None
        self.undo_clear_button.configure(state="disabled")
        result.update(metadata)
        component_fraction, flow_fraction = result["component_fraction"], result["flow_fraction"]
        dynamics = result["dynamics"]
        self.baseline_result.set(self.app._format_number(result["baseline"] * 100) + "%")
        self.disturbance_result.set(
            f"{self.app._format_number(result['combined_fraction'])} "
            f"({result['combined_fraction'] * 100:+.1f}%)"
        )
        self.calculated_result.set(self.app._format_number(result["calculated"] * 100) + "%")
        self._update_calculation_steps(
            component_fraction,
            flow_fraction,
            result["combined_fraction"],
            result["baseline"],
            result["calculated"],
        )
        self._draw_calculation_result(result)

        self.result_mode.set(result["result_mode"])
        self.final_result.set(self.app._format_number(result["final_response"][-1] * 100) + "%")
        self._update_transition_metrics(
            result["metrics"],
            dynamics,
            result["response_start"],
        )
        self.last_calculation = result
        self.last_prediction = prediction
        result["prediction"] = copy.deepcopy(prediction)
        self.add_comparison_button.configure(state="normal")
        for button in self.export_panel.export_buttons:
            button.configure(state="normal")
        self.export_panel.export_summary.set("Расчёт готов к экспорту." if result["setpoint_reachable"] is not False
                                else "Задание недостижимо при допустимых η; показано насыщение регулятора.")
        self.assignment_panel.evaluate(prediction, result["prediction_outcome"])
        result["evaluation"] = copy.deepcopy(self.assignment_panel.assignment_evaluation) if prediction is not None else None
        settling_time = result["metrics"]["settling_time"]
        settling_summary = (
            "не установилось"
            if settling_time is None
            else f"установление {max(0.0, settling_time - result['response_start']):.1f} с"
        )
        active_name = self._active_scenario_title()
        self.app.topbar_context.set(
            f"{active_name} · {result['result_mode'].lower()} · "
            f"отклонение {result['metrics']['maximum_deviation']:.4g} · {settling_summary}"
        )
        self.calculate_button_text.set("Пересчитать")
        if self.assignment_panel.learning_mode.get():
            self.app._show_page("scenarios" if prediction is not None else "results")
        self.app._set_status("Расчёт выполнен")

    def _update_calculation_steps(
        self,
        component_fraction,
        flow_fraction,
        combined_fraction,
        baseline,
        calculated,
    ):
        from app.simulation import disturbed_inputs
        values = disturbed_inputs(self.absorber_form.chain, self.absorber_form.model_values, component_fraction, flow_fraction)
        balance = absorption_balance(**values)
        self.calculation_steps.set(
            f"J = η · Gг · Xг = {float(balance['j']):.6g} кг/ч\n"
            f"Gог = Gг − J = {float(balance['gog']):.6g} кг/ч\n"
            f"Gна = Gа + J = {float(balance['gna']):.6g} кг/ч\n"
            f"Xог = (Gг · Xг − J) / Gог = {float(balance['xog']) * 100:.6g}%\n"
            f"Xна = (Gа · Xа + J) / Gна = {float(balance['xna']) * 100:.6g}%\n"
            f"Баланс стационарного режима: масса {float(balance['mass_residual']):.3g} кг/ч; "
            f"компонент {float(balance['component_residual']):.3g} кг/ч."
        )

    def _update_transition_metrics(self, metrics, dynamics, response_start):
        self.settling_time_label.set("Длительность установления (±5%)" if metrics["iae"] is None
                                     else "Длительность регулирования (±5%)")
        settling_time = metrics["settling_time"]
        if settling_time is None:
            settling_text = "не достигнуто"
            settling_moment_text = "не достигнут"
        else:
            settling_text = f"{max(0.0, settling_time - response_start):.1f} с"
            settling_moment_text = f"t = {settling_time:.1f} с"

        relative_deviation = metrics["relative_deviation"]
        self.transition_values["initial"].set(self.app._format_number(metrics["initial_value"] * 100) + "%")
        self.transition_values["steady"].set(self.app._format_number(metrics["steady_state"] * 100) + "%")
        self.transition_values["maximum_deviation"].set(self.app._format_number(metrics["maximum_deviation"] * 100) + " п.п.")
        self.transition_values["relative_deviation"].set(
            f"{relative_deviation:.2f}%" if relative_deviation is not None else "не определено"
        )
        self.transition_values["time_constant"].set(f"{self.app._format_number(dynamics['time_constant'])} с")
        self.transition_values["settling_time"].set(settling_text)
        self.transition_values["settling_moment"].set(settling_moment_text)
        self.transition_values["static_error"].set(self.app._format_signed_number(metrics["static_error"] * 100) + " п.п.")
        self.transition_values["final_error"].set(self.app._format_signed_number(metrics["final_error"] * 100) + " п.п.")
        self.transition_values["settling_status"].set(metrics["settling_status"])
        self.transition_values["iae"].set("неприменимо без регулятора" if metrics["iae"] is None
                                          else self.app._format_number(metrics["iae"] * 100) + " п.п.·с")
        self.transition_values["saturation_duration"].set("неприменимо без регулятора" if metrics["saturation_duration"] is None
                                                          else self.app._format_number(metrics["saturation_duration"]) + " с")

    def _reset(self):
        self.app._cancel_task(announce=False)
        self._undo_clear = (self.app._capture_laboratory(), self.last_calculation)
        self.undo_clear_button.configure(state="normal")
        self.absorber_form.component_enabled.set(False)
        self.absorber_form.flow_enabled.set(False)
        self.absorber_form.component_value.set("")
        self.absorber_form.flow_value.set("")
        self.absorber_form.component_entry.configure(state="disabled")
        self.absorber_form.flow_entry.configure(state="disabled")
        self.calculate_button.configure(state="disabled")
        self.absorber_form.clear_errors()
        self._clear_result_values()
        self.absorber_form.draw_control_diagram()
        self._draw_static_charts()
        self.calculation_hint.set("Включите хотя бы одно возмущение")
        self.app._set_status("Опыт очищен; динамика, регулятор и история сравнения сохранены")

    def _restore_experiment_defaults(self):
        self.app._cancel_task(announce=False)
        caption = self.active_scenario.get()
        if self.applied_scenario["controller"] is None:
            self.absorber_form.controller_type.set("PI")
            self.absorber_form.proportional_gain.set("2")
            self.absorber_form.integral_time.set("20")
            self.absorber_form.derivative_time.set("1")
            self.absorber_form.control_limit.set("100")
            self.absorber_form.update_controller_type()
        self._apply_scenario_data(copy.deepcopy(self.applied_scenario))
        self.active_scenario.set(caption)
        self.app._set_status("Исходные настройки применённого сценария восстановлены")

    def _undo_clear_experiment(self):
        if self._undo_clear is None:
            return
        laboratory, result = self._undo_clear
        self._undo_clear = None
        self.app._restore_laboratory(laboratory, result)
        self.undo_clear_button.configure(state="disabled")
        self.app._set_status("Очистка отменена; опыт восстановлен")

    def _clear_result_values(self):
        self.last_calculation = None
        self.last_prediction = None
        if hasattr(self, "add_comparison_button"):
            self.add_comparison_button.configure(state="disabled")
        if hasattr(self.export_panel, "export_buttons"):
            for button in self.export_panel.export_buttons:
                button.configure(state="disabled")
        if hasattr(self.export_panel, "export_summary"):
            self.export_panel.export_summary.set("Сначала выполните расчёт.")
        self.baseline_result.set("—")
        self.disturbance_result.set("—")
        self.calculated_result.set("—")
        self.final_result.set("—")
        self.result_mode.set(
            f"С {self.absorber_form.controller_type.get()}-регулятором"
            if self.absorber_form.controller_enabled.get()
            else "Без регулятора"
        )
        self.calculation_steps.set("Выполните расчёт, чтобы увидеть происхождение результата.")
        if hasattr(self, "calculate_button_text"):
            self.calculate_button_text.set(
                "Проверить прогноз" if self.assignment_panel.assignment_enabled.get() else "Рассчитать"
            )
        for variable in self.transition_values.values():
            variable.set("—")

    def _draw_static_charts(self):
        self.analysis_panel.remove_map_colorbar()
        self.analysis_panel.clear_map_click_callback()
        baseline = self.absorber_form.current_baseline()
        simulation_duration = self._safe_simulation_duration()
        time = np.linspace(0.0, simulation_duration, 501)
        self._draw_disturbance(time, np.zeros_like(time), 0.0, 0.0)
        self.app.response_chart_title.set("Кривая разгона")
        self.app._style_axis(self.app.response_axis, "Время, с", "X, %")
        self.app.response_axis.plot(
            [0, simulation_duration],
            [baseline * 100, baseline * 100],
            color=CURVE_STYLES["Исходный режим"][0],
            linestyle=CURVE_STYLES["Исходный режим"][1],
            linewidth=2,
            label="Исходный режим",
        )
        self.app._place_legend_above(self.app.response_axis)
        self.app.response_canvas.draw_idle()
        self.app._charts_show_comparison = False

    def _draw_last_calculation(self):
        self.analysis_panel.remove_map_colorbar()
        self.analysis_panel.clear_map_click_callback()
        if self.last_calculation is None:
            self._draw_static_charts()
        else:
            self._draw_calculation_result(self.last_calculation)

    def _draw_calculation_result(self, result):
        dynamics = result["dynamics"]
        controller = result["controller"]
        self.app.signal_mode_box.configure(state="readonly" if controller is not None and self.app.current_page not in (
            "comparison", "sensitivity", "tuning_map", "identification") else "disabled")
        if controller is None:
            self._draw_disturbance(
                result["time"],
                result["profile"],
                result["component_fraction"],
                result["flow_fraction"],
                dynamics,
            )
            self._draw_response(
                result["time"],
                result["responses"],
                dynamics,
                result["metrics"],
            )
        else:
            self._draw_controller_signals(
                result["time"],
                result["error"],
                result["control"],
                dynamics,
                result.get("commanded_control") if any(controller.get(key, 0) > 0
                    for key in ("actuator_time_constant", "actuator_rate_limit")) else None,
            )
            self._draw_control_comparison(
                result["time"],
                result["responses"]["Совместное воздействие"],
                result["controlled_response"],
                controller["setpoint"],
                controller["controller_type"],
                dynamics,
                result["metrics"],
                result.get("measurement") if controller.get("noise_std", 0) > 0 else None,
            )
        self.app._charts_show_comparison = False

    def _draw_disturbance(
        self,
        time,
        profile,
        component_fraction,
        flow_fraction,
        dynamics=None,
    ):
        self.app._remove_controller_axis()
        self.app.primary_chart_title.set("Возмущающее воздействие")
        self.app.primary_chart_subtitle.set("Изменение относительно базового уровня")
        component_signal = component_fraction * profile
        flow_signal = flow_fraction * profile
        combined_signal = (1 + component_signal) * (1 + flow_signal) - 1

        self.app._style_axis(self.app.disturbance_axis, "Время, с", "Относительное изменение")
        self.app.disturbance_axis.axhline(0.0, color=CURVE_STYLES["Исходный режим"][0], linestyle="--", linewidth=1.5, label="Исходный режим")
        self.app.disturbance_axis.plot(*plot_samples(time, component_signal), color=CURVE_STYLES["Только состав"][0], linewidth=2, label="Состав")
        self.app.disturbance_axis.plot(*plot_samples(time, flow_signal), color=CURVE_STYLES["Только расход"][0], linewidth=2, label="Расход")
        self.app.disturbance_axis.plot(*plot_samples(time, combined_signal), color=CURVE_STYLES["Совместное воздействие"][0], linewidth=2.4, label="Совместно")
        self._annotate_timing(self.app.disturbance_axis, dynamics)
        self.app.disturbance_axis.margins(x=0.02, y=0.15)
        self.app._place_legend_above(self.app.disturbance_axis)
        self.app._enable_legend_toggles(self.app.disturbance_axis, self.app.disturbance_canvas)
        self.app.disturbance_canvas.draw_idle()

    def _draw_response(self, time, responses, dynamics=None, metrics=None):
        self.app.response_chart_title.set("Кривая разгона")
        self.app._style_axis(self.app.response_axis, "Время, с", "X, %")
        for label, response in responses.items():
            color, linestyle = CURVE_STYLES[label]
            self.app.response_axis.plot(
                *plot_samples(time, response * 100),
                color=color,
                linestyle=linestyle,
                linewidth=2.4 if label == "Совместное воздействие" else 1.8,
                label=label,
            )
        self._annotate_transition(self.app.response_axis, dynamics, metrics)
        self.app.response_axis.margins(x=0.02, y=0.12)
        self.app._place_legend_above(self.app.response_axis)
        self.app._enable_legend_toggles(self.app.response_axis, self.app.response_canvas)
        self.app.response_canvas.draw_idle()

    def _redraw_signal_mode(self):
        if self.last_calculation is not None and self.last_calculation["controller"] is not None and not self.app._charts_show_comparison and self.app.current_page not in ("identification", *self.app.object_pages, "model_result"):
            self._draw_calculation_result(self.last_calculation)

    def _draw_controller_signals(self, time, error, control, dynamics=None, commanded=None):
        self.app._remove_controller_axis()
        self.app.primary_chart_title.set("Ошибка и управляющее воздействие")
        self.app.primary_chart_subtitle.set("Сигналы замкнутой системы")
        self.app._style_axis(self.app.disturbance_axis, "Время, с", "Ошибка e(t), п.п.")
        error_line = self.app.disturbance_axis.plot(
            *plot_samples(time, error * 100),
            color="#F59E0B",
            linewidth=2,
            label="Ошибка e(t), п.п.",
        )[0]
        self.app.disturbance_axis.axhline(0.0, color=MUTED, linestyle="--", linewidth=1)
        self._annotate_timing(self.app.disturbance_axis, dynamics)

        mode = self.app.controller_signal_mode.get()
        if mode == "Только ошибка":
            self.app._place_legend_above(self.app.disturbance_axis)
            self.app._enable_legend_toggles(self.app.disturbance_axis, self.app.disturbance_canvas)
            self.app.disturbance_canvas.draw_idle()
            return
        if mode == "Только управление":
            self.app._style_axis(self.app.disturbance_axis, "Время, с", "η, %")
            self.app.controller_signal_axis = None
            control_axis = self.app.disturbance_axis
        else:
            self.app.controller_signal_axis = self.app.disturbance_axis.twinx()
            control_axis = self.app.controller_signal_axis
        control_line = control_axis.plot(
            *plot_samples(time, control * 100),
            color="#16A34A",
            linewidth=2,
            label="Степень извлечения η(t), %",
        )[0]
        lines = [control_line] if mode == "Только управление" else [error_line, control_line]
        if commanded is not None:
            control_line.set_label("Фактическая η, %")
            lines.append(control_axis.plot(*plot_samples(time, commanded * 100),
                         color="#7C3AED", linestyle="--", linewidth=1.5, label="Командная η, %")[0])
        control_axis.set_ylabel("η, %", color=TEXT)
        control_axis.tick_params(colors=TEXT)
        control_axis.spines["top"].set_visible(False)
        control_axis.spines["right"].set_color(BORDER)
        self.app.disturbance_axis.margins(x=0.02, y=0.15)
        self.app._place_legend_above(
            self.app.disturbance_axis,
            lines,
            [line.get_label() for line in lines],
        )
        self.app._enable_legend_toggles(self.app.disturbance_axis, self.app.disturbance_canvas)
        self.app.disturbance_canvas.draw_idle()

    def _draw_control_comparison(
        self,
        time,
        open_response,
        controlled_response,
        setpoint,
        controller_type,
        dynamics=None,
        metrics=None,
        measurement=None,
    ):
        self.app.response_chart_title.set(f"Без регулятора / {controller_type}-регулятор")
        self.app._style_axis(self.app.response_axis, "Время, с", "X, %")
        self.app.response_axis.axhline(
            setpoint * 100,
            color=MUTED,
            linestyle="--",
            linewidth=1.5,
            label="Задание",
        )
        self.app.response_axis.plot(
            *plot_samples(time, open_response * 100),
            color="#F59E0B",
            linewidth=2,
            label="Без регулятора",
        )
        self.app.response_axis.plot(
            *plot_samples(time, controlled_response * 100),
            color=ACCENT,
            linewidth=2.4,
            label=f"{controller_type}-регулятор",
        )
        if measurement is not None:
            self.app.response_axis.plot(*plot_samples(time, measurement * 100), color="#64748B",
                                    linewidth=0.6, alpha=0.55, label="Измеренный выход")
        self._annotate_transition(self.app.response_axis, dynamics, metrics)
        self.app.response_axis.margins(x=0.02, y=0.12)
        self.app._place_legend_above(self.app.response_axis)
        self.app._enable_legend_toggles(self.app.response_axis, self.app.response_canvas)
        self.app.response_canvas.draw_idle()

    def _annotate_transition(self, axis, dynamics, metrics):
        self._annotate_timing(
            axis,
            dynamics,
            None if metrics is None else metrics.get("settling_time"),
        )
        if metrics is None:
            return
        tolerance = metrics["settling_tolerance"]
        if tolerance > 1e-12:
            axis.axhspan(
                100 * (metrics["steady_state"] - tolerance),
                100 * (metrics["steady_state"] + tolerance),
                color="#16A34A",
                alpha=0.08,
                label="Полоса ±5%",
            )

    @staticmethod
    def _annotate_timing(axis, dynamics, settling_time=None):
        if dynamics is None:
            return
        markers = [(dynamics["start_time"], "t₀", "#7C3AED")]
        delayed_start = dynamics["start_time"] + dynamics["delay"]
        if dynamics["delay"] > 0:
            markers.append((delayed_start, "t₀ + L", "#DB2777"))
        if settling_time is not None:
            markers.append((settling_time, "tуст", "#16A34A"))
        for index, (position, label, color) in enumerate(markers):
            axis.axvline(position, color=color, linestyle=":", linewidth=1.2, alpha=0.9)
            axis.text(
                position,
                0.98 - index * 0.09,
                label,
                color=color,
                fontsize=8,
                ha="left",
                va="top",
                transform=axis.get_xaxis_transform(),
            )

    def _safe_simulation_duration(self):
        try:
            return parse_positive_number(self.absorber_form.simulation_duration.get())
        except ValueError:
            return 100.0

    def _invalidate_task(self, *_):
        if self.absorber_form._changing_units:
            return
        if self.app._task is not None and self.app._task_source != self.app._task_signature(self.app._task_kind):
            self.app._cancel_task()

