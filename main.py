import copy
from decimal import Decimal
import json
import shutil
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

APP_NAME = "Лаборатория динамики и управления"
APP_VERSION = "1.12.2"

from app.calculations import (
    DEFAULT_MODEL_VALUES,
    absorption_balance,
)
from app.comparison import MAX_COMPARISON_RUNS
from app.background_tasks import BackgroundTask, CalculationCancelled
from app.scenario_store import ScenarioStore
from app.session_store import read_laboratory_session, restore_calculation, validate_input_state, write_session
from app.settings_store import SettingsStore
from app.plotting import adaptive_legend
from app.simulation import LEAN_GAS, RICH_ABSORBENT
from app.controller_extensions import EXTENSION_DEFAULTS, EXTENSION_FIELDS
from app.validation import parse_positive_number
from ui.identification_page import IdentificationPage
from ui.tank_page import TankPage
from ui.model_result_page import ModelResultPage
from app.physical_models import MODELS, run_model
from ui.chart_tools import SharedTimeCursor
from ui.absorber_form import AbsorberForm
from ui.analysis_panel import AnalysisPanel
from ui.assignment_panel import AssignmentPanel
from ui.export_panel import ExportPanel
from ui.comparison_panel import ComparisonPanel
from ui.scenario_library import ScenarioLibrary
from ui.theme import (
    ACCENT,
    ACCENT_ACTIVE,
    BACKGROUND,
    BORDER,
    CARD_BACKGROUND,
    ERROR,
    MUTED,
    SIDEBAR,
    SIDEBAR_MUTED,
    SUCCESS,
    TEXT,
)
from ui.window_geometry import fit_geometry, work_area
from ui.ui_helpers import (
    ScrollablePage,
    TextTooltip,
    create_icon,
    plot_samples,
)



CURVE_STYLES = {
    "Исходный режим": ("#667085", "--"),
    "Только состав": ("#F59E0B", "-"),
    "Только расход": ("#16A34A", "-"),
    "Совместное воздействие": (ACCENT, "-"),
}




class DynamicsControlApp(ttk.Frame):
    def __init__(self, root, scenario_path=None):
        super().__init__(root, style="App.TFrame")
        self.root = root
        self.absorber_form = AbsorberForm(self)
        self.scenario_library = ScenarioLibrary(
            self, ScenarioStore(scenario_path) if scenario_path is not None else ScenarioStore()
        )
        self.settings_store = SettingsStore(
            self.scenario_library.store.path.with_name("settings.json")
        )
        self.settings = self.settings_store.load()
        self.sidebar_collapsed = self.settings["sidebar_collapsed"]
        self.pages = {}
        self.model_results = {}
        self.nav_buttons = {}

        self.page_title = tk.StringVar(value="Возмущения")
        self.topbar_context = tk.StringVar(value="Свободный расчёт · без регулятора")
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
        self.response_subtitle = tk.StringVar()
        self.primary_chart_title = tk.StringVar(value="Возмущающее воздействие")
        self.primary_chart_subtitle = tk.StringVar(value="Изменение относительно базового уровня")
        self.response_chart_title = tk.StringVar(value="Кривая разгона")
        self.status_text = tk.StringVar(value="Готово к расчёту")
        self.active_scenario = tk.StringVar(value="Сценарий ещё не применён.")
        self.applied_scenario = copy.deepcopy(self.scenario_library.scenarios[0])
        self.last_calculation = None
        self.last_prediction = None
        self.controller_signal_axis = None
        self.analysis_panel = AnalysisPanel(self)
        self.export_panel = ExportPanel(self)
        self.comparison_page = ComparisonPanel(self)
        self.assignment_panel = AssignmentPanel(self)
        self.current_page = self.settings["last_page"]
        self._charts_show_comparison = False
        self.text_tooltips = []
        self._task = None
        self._task_poll = None
        self._undo_clear = None
        self.chart_mode = self.settings["chart_mode"]
        self.chart_mode_label = tk.StringVar()
        self.chart_cards = []
        self.chart_readouts = []
        self.controller_signal_mode = tk.StringVar(value="Ошибка и управление")
        self._page_context = {}
        self._layout_jobs = []
        self._layout_drag_pane = None

        self._configure_window()
        self._configure_styles()
        self._build_layout()
        self.time_cursor = SharedTimeCursor(((self.disturbance_canvas, self.disturbance_toolbar, self.chart_readouts[0]),
                                              (self.response_canvas, self.response_toolbar, self.chart_readouts[1])))
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
        if self.scenario_library.store.warning:
            self._set_status(self.scenario_library.store.warning, error=True)
        self.autosave_path = self.scenario_library.store.path.with_name("laboratory.autosave.json")
        self._autosave_snapshot = None
        self._autosave_task = None
        self._restore_autosave()
        self._show_page(self.current_page)
        self._autosave_job = self.root.after(5000, self._autosave_tick)

    def _configure_window(self):
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        area = work_area(self.root)
        self.root.geometry(fit_geometry(self.settings["geometry"], area))
        self.root.minsize(min(900, area[2] - area[0] - 16), min(540, area[3] - area[1] - 48))
        self.root.configure(background=BACKGROUND)
        self.root.protocol("WM_DELETE_WINDOW", self._close_application)
        self.pack(fill="both", expand=True)

    def _configure_styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")

        style.configure("App.TFrame", background=BACKGROUND)
        style.configure("Card.TFrame", background=CARD_BACKGROUND, relief="solid", borderwidth=1)
        style.configure("CardBody.TFrame", background=CARD_BACKGROUND)
        style.configure("Header.TLabel", background=BACKGROUND, foreground=TEXT, font=("Segoe UI", 17, "bold"))
        style.configure("CardTitle.TLabel", background=CARD_BACKGROUND, foreground=TEXT, font=("Segoe UI", 12, "bold"))
        style.configure("DialogLabel.TLabel", background=BACKGROUND, foreground=TEXT, font=("Segoe UI", 10, "bold"))
        style.configure("Body.TLabel", background=CARD_BACKGROUND, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=CARD_BACKGROUND, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Error.TLabel", background=CARD_BACKGROUND, foreground=ERROR, font=("Segoe UI", 8))
        style.configure("Dirty.TEntry", fieldbackground="#FFFAEB", bordercolor="#F79009")
        style.configure("Dirty.TCombobox", fieldbackground="#FFFAEB", bordercolor="#F79009")
        style.configure("ResultValue.TLabel", background=CARD_BACKGROUND, foreground=TEXT, font=("Segoe UI", 10, "bold"))
        style.configure("Status.TLabel", background=BACKGROUND, foreground=TEXT, font=("Segoe UI", 9))
        style.configure("StatusDot.TLabel", background=BACKGROUND, foreground=SUCCESS, font=("Segoe UI", 14))
        style.configure("Topbar.TFrame", background="#FFFFFF")
        style.configure("TopbarTitle.TLabel", background="#FFFFFF", foreground=TEXT, font=("Segoe UI", 16, "bold"))
        style.configure("TopbarMeta.TLabel", background="#FFFFFF", foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Sidebar.TFrame", background=SIDEBAR)
        style.configure("SidebarTitle.TLabel", background=SIDEBAR, foreground="#FFFFFF", font=("Segoe UI", 14, "bold"))
        style.configure("SidebarMeta.TLabel", background=SIDEBAR, foreground=SIDEBAR_MUTED, font=("Segoe UI", 9))
        style.configure("SidebarNav.TButton", background=SIDEBAR, foreground="#E5ECF5", borderwidth=0, padding=(18, 9), font=("Segoe UI", 10), anchor="w")
        style.map("SidebarNav.TButton", background=[("active", "#193553")])
        style.configure("SidebarToggle.TButton", background=SIDEBAR, foreground="#E5ECF5", borderwidth=0, padding=(12, 10), font=("Segoe UI", 10), anchor="center")
        style.map("SidebarToggle.TButton", background=[("active", "#193553")])
        style.configure("SelectedSidebarNav.TButton", background=ACCENT, foreground="#FFFFFF", borderwidth=0, padding=(18, 9), font=("Segoe UI", 10, "bold"), anchor="w")
        style.map("SelectedSidebarNav.TButton", background=[("active", ACCENT_ACTIVE)])
        style.configure("SectionHeader.TLabel", background=BACKGROUND, foreground=TEXT, font=("Segoe UI", 15, "bold"))
        style.configure("MetricTitle.TLabel", background=CARD_BACKGROUND, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("MetricValue.TLabel", background=CARD_BACKGROUND, foreground=TEXT, font=("Segoe UI", 18, "bold"))

        style.configure("TEntry", padding=7, fieldbackground="#FFFFFF", bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        style.configure("Error.TEntry", padding=7, fieldbackground="#FFF6F5", bordercolor=ERROR, lightcolor=ERROR, darkcolor=ERROR)
        style.configure("TCheckbutton", background=CARD_BACKGROUND, foreground=TEXT, font=("Segoe UI", 10))
        style.map("TCheckbutton", background=[("active", CARD_BACKGROUND)])

        style.configure("Primary.TButton", background=ACCENT, foreground="#FFFFFF", borderwidth=0, padding=(14, 10), font=("Segoe UI", 10, "bold"))
        style.map("Primary.TButton", background=[("active", ACCENT_ACTIVE), ("disabled", "#AEBBD0")], foreground=[("disabled", "#F2F4F7")])
        style.configure("Secondary.TButton", background="#FFFFFF", foreground=TEXT, bordercolor=BORDER, padding=(12, 9), font=("Segoe UI", 10))
        style.map("Secondary.TButton", background=[("active", "#EEF2F6")])
        style.configure("Segment.TButton", background="#FFFFFF", foreground=TEXT, bordercolor=BORDER, padding=(12, 10), font=("Segoe UI", 10))
        style.configure("SelectedSegment.TButton", background=ACCENT, foreground="#FFFFFF", bordercolor=ACCENT, padding=(12, 10), font=("Segoe UI", 10))
        style.map("SelectedSegment.TButton", background=[("active", ACCENT_ACTIVE)])
        style.configure("Toolbar.TButton", background="#FFFFFF", foreground=TEXT, bordercolor=BORDER, padding=(6, 4), font=("Segoe UI", 8))
        style.map("Toolbar.TButton", background=[("active", "#EEF2F6")])

    def _build_layout(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        topbar = ttk.Frame(self, style="Topbar.TFrame", padding=(22, 12))
        topbar.grid(row=0, column=0, sticky="ew")
        topbar.columnconfigure(0, weight=1)
        ttk.Label(topbar, text=APP_NAME, style="TopbarTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.route_stage = tk.StringVar(value="Учебный маршрут")
        self.route_button = ttk.Button(topbar, textvariable=self.route_stage,
                                       command=self.assignment_panel.advance_step, style="Toolbar.TButton")
        self.route_button.grid(row=0, column=1, sticky="e")
        self.selected_model = tk.StringVar(value="Абсорбер")
        model_box = ttk.Combobox(topbar, textvariable=self.selected_model, values=tuple(model.name for model in MODELS.all()),
                                state="readonly", width=12)
        model_box.grid(row=0, column=2, sticky="e", padx=(12, 0))
        model_box.bind("<<ComboboxSelected>>", self._select_model)
        ttk.Label(
            topbar,
            textvariable=self.topbar_context,
            style="TopbarMeta.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(3, 0))

        self.body = ttk.Frame(self, style="App.TFrame")
        self.body.grid(row=1, column=0, sticky="nsew")
        self.body.columnconfigure(0, minsize=220)
        self.body.columnconfigure(1, weight=1)
        self.body.rowconfigure(0, weight=1)

        self.sidebar = ttk.Frame(self.body, style="Sidebar.TFrame", padding=(12, 20))
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.columnconfigure(0, weight=1)
        self.sidebar_title = ttk.Label(
            self.sidebar,
            text="АБСОРБЦИЯ",
            style="SidebarTitle.TLabel",
        )
        self.sidebar_title.grid(row=0, column=0, sticky="w", padx=8, pady=(0, 4))
        self.sidebar_meta = ttk.Label(
            self.sidebar,
            text="Моделирование контуров",
            style="SidebarMeta.TLabel",
        )
        self.sidebar_meta.grid(row=1, column=0, sticky="w", padx=8, pady=(0, 24))

        active_navigation = (
            ("scenarios", "Задание"),
            ("disturbances", "Параметры опыта"),
            ("dynamics", "Динамика"),
            ("controller", "Регулятор"),
            ("results", "Результаты"),
            ("model_result", "Результат модели"),
            ("comparison", "Сравнение"),
            ("export", "Отчёт"),
            ("sensitivity", "Чувствительность"),
            ("tuning_map", "Карта настроек"),
            ("identification", "Идентификация"),
        )
        self.nav_icons = {
            key: create_icon(self.root, "sensitivity" if key == "identification" else "results" if key == "model_result" else key)
            for key, _label in active_navigation
        }
        self.nav_labels = dict(active_navigation)
        self.research_label = ttk.Label(self.sidebar, text="Исследование", style="SidebarMeta.TLabel")
        research_row = 2 + next(i for i, (key, _label) in enumerate(active_navigation) if key == "sensitivity")
        self.research_label.grid(row=research_row, column=0, sticky="w", padx=8, pady=(8, 2))
        for row, (key, label) in enumerate(active_navigation, start=2):
            if row >= research_row:
                row += 1
            button = ttk.Button(
                self.sidebar,
                text=label,
                image=self.nav_icons[key],
                compound="left",
                command=self._model_workspace if key == "disturbances" else lambda page=key: self._show_page(page),
                style="SidebarNav.TButton",
            )
            button.grid(row=row, column=0, sticky="ew", pady=2)
            self.nav_buttons[key] = button
            self._attach_tooltip(button, "Открыть рабочую страницу выбранной модели" if key == "disturbances" else label)

        separator_row = 3 + len(active_navigation)
        ttk.Separator(self.sidebar).grid(
            row=separator_row,
            column=0,
            sticky="ew",
            padx=8,
            pady=16,
        )
        self.sidebar_toggle = ttk.Button(
            self.sidebar,
            command=self._toggle_sidebar,
            style="SidebarToggle.TButton",
        )
        self.sidebar_toggle.grid(row=separator_row + 1, column=0, sticky="ew", pady=2)

        self.main_panes = ttk.Panedwindow(self.body, orient="horizontal")
        self.main_panes.grid(row=0, column=1, sticky="nsew")
        inspector = ttk.Frame(self.main_panes, style="App.TFrame", padding=(16, 16, 12, 12))
        self.inspector = inspector
        self.main_panes.add(inspector, weight=0)
        inspector.columnconfigure(0, weight=1)
        inspector.rowconfigure(1, weight=1)
        ttk.Label(inspector, textvariable=self.page_title, style="SectionHeader.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 12))

        page_host = ttk.Frame(inspector, style="App.TFrame")
        page_host.grid(row=1, column=0, sticky="nsew")
        page_host.columnconfigure(0, weight=1)
        page_host.rowconfigure(0, weight=1)
        self.page_contents = {}
        for key in (
            "disturbances",
            "dynamics",
            "results",
            "comparison",
            "controller",
            "sensitivity",
            "tuning_map",
            "identification",
            "tank",
            "model_result",
            "scenarios",
            "export",
        ):
            page = ScrollablePage(page_host, BACKGROUND)
            page.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = page
            self.page_contents[key] = page.content

        self.absorber_form.build_chain_card(self.page_contents["disturbances"])
        self.absorber_form.build_disturbance_card(self.page_contents["disturbances"])
        self.absorber_form.build_control_diagram(self.page_contents["disturbances"])
        self.absorber_form.build_dynamics_card(self.page_contents["dynamics"])
        self._build_result_card(self.page_contents["results"])
        ttk.Label(self.page_contents["comparison"], text="Выберите опыты в широкой таблице над графиками. Пустой выбор показывает все опыты. Отчёты включают все закреплённые опыты.", wraplength=330, style="Body.TLabel").grid(row=0, column=0, sticky="w")
        self.absorber_form.build_controller_card(self.page_contents["controller"])
        self.analysis_panel.build_sensitivity_card(self.page_contents["sensitivity"])
        self.analysis_panel.build_map_card(self.page_contents["tuning_map"])
        self._build_scenarios_card(self.page_contents["scenarios"])
        self.export_panel.build(self.page_contents["export"])
        self.identification_page = IdentificationPage(self.page_contents["identification"], self)
        self.identification_page.grid(row=0, column=0, sticky="nsew")
        self.tank_page = TankPage(self.page_contents["tank"], self)
        self.tank_page.grid(row=0, column=0, sticky="nsew")
        self.model_result_page = ModelResultPage(self.page_contents["model_result"], self)
        self.model_result_page.grid(row=0, column=0, sticky="nsew")
        for page in self.pages.values():
            page.bind_mousewheel()

        actions = ttk.Frame(inspector, style="App.TFrame")
        actions.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        actions.columnconfigure((0, 1), weight=1)
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
        self.cancel_button = ttk.Button(actions, text="Отменить расчёт", command=self._cancel_task, state="disabled")
        self.cancel_button.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.task_progress = ttk.Progressbar(actions, maximum=100)
        self.task_progress.grid(row=3, column=1, sticky="ew", padx=(6, 0), pady=(6, 0))

        workspace = ttk.Frame(self.main_panes, style="App.TFrame", padding=(4, 16, 16, 12))
        self.workspace = workspace
        self.main_panes.add(workspace, weight=1)
        workspace.columnconfigure(0, weight=1)
        workspace.rowconfigure(1, weight=1)
        self._build_metric_strip(workspace)
        self.comparison_workspace = ScrollablePage(workspace, background=BACKGROUND)
        self.comparison_workspace.grid(row=2, column=0, sticky="ew")
        self.comparison_workspace.columnconfigure(0, weight=1)
        self.comparison_page.build(self.comparison_workspace.content)
        self.comparison_workspace.bind_mousewheel()
        self.comparison_workspace.canvas.configure(height=260)
        self.comparison_workspace.grid_remove()
        chart_actions = ttk.Frame(workspace, style="App.TFrame")
        chart_actions.grid(row=1, column=0, sticky="ew", pady=(4, 4))
        workspace.rowconfigure(1, weight=0)
        workspace.rowconfigure(2, weight=0)
        workspace.rowconfigure(3, weight=1)
        self.chart_mode_box = ttk.Combobox(chart_actions, textvariable=self.chart_mode_label,
                                         state="readonly", width=22)
        self.chart_mode_box.pack(side="left")
        self.chart_mode_box.bind("<<ComboboxSelected>>", self._choose_chart_mode)
        ttk.Button(chart_actions, text="Исходная компоновка", command=self._reset_layout).pack(side="right")
        self.chart_panes = ttk.Panedwindow(workspace, orient="vertical")
        self.chart_panes.grid(row=3, column=0, sticky="nsew")

        self.disturbance_axis, self.disturbance_canvas, self.disturbance_toolbar = self._build_chart_card(
            self.chart_panes,
            row=1,
            title_variable=self.primary_chart_title,
            subtitle_variable=self.primary_chart_subtitle,
        )
        self.response_axis, self.response_canvas, self.response_toolbar = self._build_chart_card(
            self.chart_panes,
            row=2,
            title_variable=self.response_chart_title,
            subtitle_variable=self.response_subtitle,
        )

        status = ttk.Frame(self, style="App.TFrame", padding=(18, 6, 18, 8))
        status.grid(row=2, column=0, sticky="ew")
        status.columnconfigure(1, weight=1)
        self.status_dot = ttk.Label(status, text="●", style="StatusDot.TLabel")
        self.status_dot.grid(row=0, column=0, sticky="w")
        ttk.Label(status, textvariable=self.status_text, style="Status.TLabel").grid(row=0, column=1, sticky="w", padx=(5, 0))
        ttk.Label(status, text=f"v{APP_VERSION} · Python 3.14", style="Status.TLabel").grid(row=0, column=2, sticky="e")
        self._apply_sidebar_state()
        initial_page = self.current_page if self.current_page in self.pages else "disturbances"
        self._show_page(initial_page)
        self._set_chart_mode(self.chart_mode)
        self._schedule_layout(self._restore_layout)
        self.root.bind("<Configure>", self._adapt_window, add="+")
        for pane in (self.main_panes, self.chart_panes):
            pane.bind("<ButtonPress-1>", self._begin_layout_drag, add="+")
            pane.bind("<B1-Motion>", self._move_layout_drag, add="+")
            pane.bind("<ButtonRelease-1>", self._end_layout_drag, add="+")
            pane.bind("<Unmap>", self._end_layout_drag, add="+")
        self.root.bind("<ButtonRelease-1>", self._end_layout_drag, add="+")
        self.assignment_panel.update_route()

    def _begin_layout_drag(self, event):
        pane = event.widget
        if pane.identify(event.x, event.y) not in (0, "0"):
            return
        self._layout_drag_pane = pane
        horizontal = pane is self.main_panes
        self._layout_drag_position = pane.sashpos(0)
        self._layout_drag_offset = (event.x if horizontal else event.y) - self._layout_drag_position
        self._layout_drag_marker = tk.Frame(pane, background=ACCENT)
        self._place_layout_marker()
        self.time_cursor.clear()
        return "break"

    def _place_layout_marker(self):
        if self._layout_drag_pane is self.main_panes:
            self._layout_drag_marker.place(x=self._layout_drag_position, y=0, width=3, relheight=1)
        else:
            self._layout_drag_marker.place(x=0, y=self._layout_drag_position, height=3, relwidth=1)
        self._layout_drag_marker.lift()

    def _move_layout_drag(self, event):
        pane = self._layout_drag_pane
        if pane is None:
            return
        horizontal = pane is self.main_panes
        size = pane.winfo_width() if horizontal else pane.winfo_height()
        self._layout_drag_position = max(0, min(size-5,
            (event.x if horizontal else event.y) - self._layout_drag_offset))
        self._place_layout_marker()
        return "break"

    def _end_layout_drag(self, _event=None):
        pane = self._layout_drag_pane
        if pane is None:
            return
        self._layout_drag_marker.destroy()
        pane.sashpos(0, self._layout_drag_position)
        self._layout_drag_pane = None
        return "break"

    def _adapt_window(self, event):
        if event.widget != self.root:
            return
        scale = float(self.root.tk.call("tk", "scaling")) / (96 / 72)
        size = (event.width, event.height, scale)
        if size == getattr(self, "_adapt_size", None):
            return
        self._adapt_size = size
        if event.width < 1240 * scale and not self.sidebar_collapsed:
            self.sidebar_collapsed = True
            self._apply_sidebar_state()
        nav_padding = max(2, min(9, int((event.height / scale - 560) / 28)))
        if nav_padding != getattr(self, "_nav_padding", None):
            self._nav_padding = nav_padding
            for style_name in ("SidebarNav.TButton", "SelectedSidebarNav.TButton"):
                ttk.Style(self.root).configure(style_name, padding=(18, nav_padding))
        line_height = int(self.root.tk.call("font", "metrics", "TkDefaultFont", "-linespace")) + 4
        if line_height != getattr(self, "_table_row_height", None):
            self._table_row_height = line_height
            ttk.Style(self.root).configure("Treeview", rowheight=line_height)
        table_height = max(3, min(6, int((event.height - 500 * scale) / line_height)))
        if table_height != int(self.comparison_page.table["height"]):
            self.comparison_page.table.configure(height=table_height)
        canvas_height = max(140, int((event.height - 140 * scale) * 0.43))
        if canvas_height != int(self.comparison_workspace.canvas["height"]):
            self.comparison_workspace.canvas.configure(height=canvas_height)
        if event.height < 850 * scale and self.chart_mode == "both" and self.current_page != "comparison":
            self._set_chart_mode("response")

    def _upper_chart_label(self):
        if self.current_page == "identification" and self.identification_page.view.get() == "Ошибки на абсорбере":
            return "Ошибки K/T"
        if self.current_page == "model_result":
            return "Входы модели"
        elif self.current_page == "tank":
            return "Подача и отбор"
        if self.current_page in ("comparison", "sensitivity", "tuning_map"):
            return {"comparison": "Сравнение", "sensitivity": "Чувствительность", "tuning_map": "Карта настроек"}[self.current_page]
        return "Сигналы регулятора" if self.absorber_form.controller_enabled.get() else "Воздействие"

    def _choose_chart_mode(self, _event=None):
        label = self.chart_mode_label.get()
        self._set_chart_mode("both" if label == "Оба" else "response" if label == "Отклик" else "input")

    def _set_chart_mode(self, mode):
        if len(self.chart_panes.panes()) == 2:
            height = self.chart_panes.winfo_height()
            if height > 1:
                self.settings["chart_split"] = min(0.85, max(0.15, self.chart_panes.sashpos(0) / height))
        self.chart_mode = mode
        for card in self.chart_panes.panes():
            self.chart_panes.forget(card)
        for index in ((0, 1) if mode == "both" else (0,) if mode == "input" else (1,)):
            self.chart_panes.add(self.chart_cards[index], weight=1)
        self._refresh_chart_modes()
        self.chart_mode_label.set("Оба" if mode == "both" else "Отклик" if mode == "response" else self._upper_chart_label())
        if mode == "both":
            self._schedule_layout(self._restore_chart_split, delay=10)

    def _refresh_chart_modes(self):
        if hasattr(self, "chart_mode_box"):
            self.chart_mode_box.configure(values=("Оба", self._upper_chart_label(), "Отклик"))
            self.chart_mode_label.set("Оба" if self.chart_mode == "both" else "Отклик" if self.chart_mode == "response" else self._upper_chart_label())

    def _schedule_layout(self, callback, delay=0):
        def apply():
            self._layout_jobs.remove(job)
            callback()
        job = self.root.after(delay, apply) if delay else self.root.after_idle(apply)
        self._layout_jobs.append(job)

    def _restore_chart_split(self):
        if self._layout_drag_pane is not None:
            return
        if len(self.chart_panes.panes()) == 2:
            self.chart_panes.sashpos(0, int(self.chart_panes.winfo_height() * self.settings["chart_split"]))

    def _ensure_chart_split(self, _event=None):
        if len(self.chart_panes.panes()) == 2 and self.chart_panes.winfo_height() > 100 and self.chart_panes.sashpos(0) == 0:
            self._schedule_layout(self._restore_chart_split, delay=10)

    def _restore_layout(self):
        if self._layout_drag_pane is not None:
            return
        if len(self.main_panes.panes()) == 2:
            self.main_panes.sashpos(0, min(self.settings["inspector_width"], max(300, self.main_panes.winfo_width() - 320)))
        self._restore_chart_split()

    def _reset_layout(self):
        self.settings["inspector_width"], self.settings["chart_split"] = 410, 0.5
        self._set_chart_mode("both")
        self.settings["chart_split"] = 0.5
        self._restore_layout()

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

    def _model_workspace(self):
        self._show_page(MODELS.named(self.selected_model.get()).entry_page)

    def _select_model(self, _event=None):
        self._cancel_task(announce=False)
        model = MODELS.named(self.selected_model.get())
        self._show_page(getattr(self, "_absorber_page", model.entry_page) if model.id == "absorber" else model.entry_page)

    def _show_page(self, page):
        titles = {
            "disturbances": "Параметры опыта",
            "dynamics": "Динамика объекта",
            "results": "Результаты расчёта",
            "comparison": "Сравнение опытов",
            "controller": "Регулятор",
            "sensitivity": "Анализ чувствительности",
            "tuning_map": "Карта настроек регулятора",
            "scenarios": "Задание",
            "export": "Отчёт",
            "identification": "Идентификация по CSV",
            "tank": "Бак: опыт и регулирование",
            "model_result": "Результат модели",
        }
        previous_page = self.current_page
        if previous_page in self.pages and previous_page != page:
            focus = self.root.focus_get()
            self._page_context[previous_page] = (self.pages[previous_page].canvas.yview()[0],
                focus if focus is not None and str(focus).startswith(str(self.pages[previous_page])) else None)
        self.current_page = page
        if page == "tank":
            if previous_page != "tank": self._absorber_context = self.topbar_context.get()
            self.topbar_context.set("Бак: объёмный баланс, локальная модель и PI; уровень в метрах")
        elif previous_page == "tank":
            self.topbar_context.set(getattr(self,"_absorber_context","Абсорбер"))
            for axis in (self.disturbance_axis,self.response_axis):
                axis.figure.set_layout_engine("constrained")
        if page != "model_result":
            model = next((m for m in MODELS.all() if m.entry_page == page), MODELS.get("absorber"))
            self.selected_model.set(model.name)
        model = MODELS.named(self.selected_model.get())
        self.sidebar_title.configure(text=model.sidebar)
        if page == "model_result":
            self.topbar_context.set(model.context)
        if page not in ("tank", "model_result"):
            self._absorber_page = page
        elif previous_page != "tank":
            self._cancel_task(announce=False)
        for widget in self.absorber_action_widgets:
            widget.grid_remove() if page == "model_result" or model.id != "absorber" else widget.grid()
        self.route_button.grid_remove() if page == "model_result" or model.id != "absorber" else self.route_button.grid()
        for key, widget in self.pages.items():
            if key != page:
                widget.grid_remove()
        self.pages[page].grid()
        self.pages[page].tkraise()
        if page in self._page_context:
            scroll, focus = self._page_context[page]
            self.pages[page].canvas.yview_moveto(scroll)
            if focus is not None and focus.winfo_exists():
                self._schedule_layout(lambda: focus.focus_set()
                                      if self.current_page == page and focus.winfo_exists() else None)
        self.page_title.set(titles[page])
        if hasattr(self, "signal_mode_box"):
            available = (page not in ("comparison", "sensitivity", "tuning_map", "identification", "tank", "model_result")
                         and self.last_calculation is not None and self.last_calculation["controller"] is not None)
            self.signal_mode_box.configure(state="readonly" if available else "disabled")
            self.signal_mode_box.grid_remove() if page in ("tank", "model_result") else self.signal_mode_box.grid()
        self.nav_labels["disturbances"] = "Параметры опыта" if model.id == "absorber" else "Работа с моделью"
        for key, button in self.nav_buttons.items():
            visible = model.id == "absorber" or key in ("disturbances", "model_result")
            button.grid() if visible else button.grid_remove()
            selected = key == page or (key == "disturbances" and page == model.entry_page)
            button.configure(style="SelectedSidebarNav.TButton" if selected else "SidebarNav.TButton")
        self._apply_sidebar_state()
        if previous_page == "comparison" and page != "comparison":
            if str(self.inspector) not in self.main_panes.panes():
                self.main_panes.insert(0, self.inspector, weight=0)
                self._schedule_layout(self._restore_layout)
            self._set_chart_mode(getattr(self, "_comparison_chart_mode", "both"))
        if page == "comparison":
            if str(self.inspector) in self.main_panes.panes():
                self.settings["inspector_width"] = self.main_panes.sashpos(0)
                self.main_panes.forget(self.inspector)
            if previous_page != "comparison" or not hasattr(self, "_comparison_chart_mode"):
                self._comparison_chart_mode = self.chart_mode
                self._set_chart_mode("input")
            self.comparison_workspace.grid()
            self.metric_strip.grid_remove()
            self.comparison_page.draw()
        else:
            self.comparison_workspace.grid_remove()
            self.metric_strip.grid()
        if page == "sensitivity":
            self.analysis_panel.draw_sensitivity()
        elif page == "tuning_map":
            self.analysis_panel.draw_map()
        elif page == "identification":
            self.metric_strip.grid_remove()
            self.analysis_panel.remove_map_colorbar()
            self.analysis_panel.clear_map_click_callback()
            self.identification_page.draw()
        elif page == "model_result":
            self.metric_strip.grid_remove()
            self.model_result_page.draw()
        elif page == "tank":
            self.metric_strip.grid_remove()
            self.tank_page.draw()
        elif previous_page in ("identification", "tank", "model_result") and page != "comparison":
            self._draw_last_calculation()
        elif page != "comparison" and previous_page in ("comparison", "sensitivity", "tuning_map") and self._charts_show_comparison:
            self.analysis_panel.remove_map_colorbar()
            self.analysis_panel.clear_map_click_callback()
            self._draw_last_calculation()

        self._refresh_chart_modes()

    def _toggle_sidebar(self):
        self.sidebar_collapsed = not self.sidebar_collapsed
        self._apply_sidebar_state()

    def _apply_sidebar_state(self):
        if self.sidebar_collapsed:
            self.sidebar_title.grid_remove()
            self.sidebar_meta.grid_remove()
            self.research_label.grid_remove()
            self.sidebar.configure(padding=(6, 20))
            self.body.columnconfigure(0, minsize=64)
        else:
            self.sidebar_title.grid()
            self.sidebar_meta.grid()
            if MODELS.named(self.selected_model.get()).id == "absorber":
                self.research_label.grid()
            else:
                self.research_label.grid_remove()
            self.sidebar.configure(padding=(12, 20))
            self.body.columnconfigure(0, minsize=220)
        for key, button in self.nav_buttons.items():
            button.configure(
                text="" if self.sidebar_collapsed else self.nav_labels[key],
                compound="center" if self.sidebar_collapsed else "left",
                width=3 if self.sidebar_collapsed else 18,
            )
        self.sidebar_toggle.configure(
            text="»" if self.sidebar_collapsed else "«  Свернуть",
            width=3 if self.sidebar_collapsed else 18,
        )

    def _card(self, parent, row, padding=(18, 16)):
        card = ttk.Frame(parent, style="Card.TFrame", padding=padding)
        card.grid(row=row, column=0, sticky="ew", pady=(0, 14))
        card.columnconfigure(0, weight=1)
        return card

    def _attach_tooltip(self, widget, text):
        self.text_tooltips.append(TextTooltip(widget, text))

    def _task_signature(self, kind):
        if kind == "tank":
            return [self.selected_model.get(), self.tank_page.signature()]
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

    def _start_task(self, name, calculate, apply, kind="calculation"):
        self._cancel_task(announce=False)
        self._task_kind = kind
        self._task_source = self._task_signature(kind)
        self._task_apply = apply
        self._task_name = name
        self._task = BackgroundTask(calculate)
        self.cancel_button.configure(state="normal")
        self.task_progress.configure(value=0)
        self._set_status(f"{name}: 0%")
        self._task_poll = self.root.after(50, self._poll_task)

    def _poll_task(self):
        self._task_poll = None
        task = self._task
        if task is None:
            return
        if self._task_source != self._task_signature(self._task_kind):
            self._cancel_task()
            return
        outcome = task.poll()
        if outcome is None:
            self.task_progress.configure(value=task.progress * 100)
            self._set_status(f"{self._task_name}: {task.progress:.0%}")
            self._task_poll = self.root.after(100, self._poll_task)
            return
        self._task = None
        self.cancel_button.configure(state="disabled")
        value, error = outcome
        if error is not None:
            self.task_progress.configure(value=0)
            if self._task_kind == "identification" and not isinstance(error, CalculationCancelled):
                self.identification_page.summary.set(str(error))
            if self._task_kind == "tank" and not isinstance(error, CalculationCancelled):
                self.tank_page.summary.set(str(error))
            self._set_status("Расчёт отменён" if isinstance(error, CalculationCancelled) else str(error),
                             error=not isinstance(error, CalculationCancelled))
            return
        self.task_progress.configure(value=100)
        self._task_apply(value)

    def _cancel_task(self, announce=True):
        if self._task is None:
            return
        self._task.cancel.set()
        self._task = None
        if self._task_poll is not None:
            self.root.after_cancel(self._task_poll)
            self._task_poll = None
        self.cancel_button.configure(state="disabled")
        self.task_progress.configure(value=0)
        if announce:
            self._set_status("Расчёт отменён; последний результат сохранён")

    def _invalidate_task(self, *_):
        if self.absorber_form._changing_units:
            return
        if self._task is not None and self._task_source != self._task_signature(self._task_kind):
            self._cancel_task()

    def _build_result_card(self, parent):
        card = self._card(parent, 0)
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

        calculation = self._card(parent, 1)
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

        metrics = self._card(parent, 2)
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
                self._attach_tooltip(metric_label, metric_help[key])
            ttk.Label(metrics, textvariable=self.transition_values[key], style="ResultValue.TLabel").grid(
                row=row, column=1, sticky="e", pady=3, padx=(8, 0)
            )
        metrics.columnconfigure(0, weight=1)

        comparison_action = self._card(parent, 3, padding=(14, 12))
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

    def _build_chart_card(
        self,
        parent,
        row,
        title=None,
        subtitle=None,
        title_variable=None,
        subtitle_variable=None,
    ):
        card = ttk.Frame(parent, style="Card.TFrame", padding=(16, 12))
        self.chart_cards.append(card)
        parent.add(card, weight=1)
        card.columnconfigure(0, weight=1)
        card.rowconfigure(1, weight=1)

        header = ttk.Frame(card, style="CardBody.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        header.columnconfigure(0, weight=1)
        if title_variable is not None:
            ttk.Label(header, textvariable=title_variable, style="CardTitle.TLabel", wraplength=700).grid(row=0, column=0, columnspan=2, sticky="w")
        else:
            ttk.Label(header, text=title, style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        if subtitle_variable is not None:
            ttk.Label(header, textvariable=subtitle_variable, style="Muted.TLabel", wraplength=700).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))
        else:
            ttk.Label(header, text=subtitle, style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 0))

        plot_host = ttk.Frame(card, style="CardBody.TFrame")
        plot_host.grid(row=1, column=0, sticky="nsew")
        plot_host.columnconfigure(0, weight=1)
        plot_host.rowconfigure(0, weight=1)

        figure = Figure(figsize=(7, 3), dpi=100, facecolor=CARD_BACKGROUND, constrained_layout=True)
        axis = figure.add_subplot(111)
        canvas = FigureCanvasTkAgg(figure, master=plot_host)
        canvas.mpl_connect("draw_event", self._ensure_chart_split)
        canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")

        hidden_toolbar_host = ttk.Frame(card, style="CardBody.TFrame")
        toolbar = NavigationToolbar2Tk(canvas, hidden_toolbar_host, pack_toolbar=False)
        toolbar.update()

        tools = ttk.Frame(header, style="CardBody.TFrame")
        tools.grid(row=2, column=1, sticky="e")
        tool_buttons = {}
        def update_tools(_event=None):
            for key, button in tool_buttons.items():
                active = key == "Увеличение" and "zoom" in str(toolbar.mode) or key == "Перемещение" and "pan" in str(toolbar.mode)
                button.configure(style="SelectedSegment.TButton" if active else "Toolbar.TButton")
        def tool(command):
            if hasattr(self, "time_cursor"):
                self.time_cursor.clear()
            command()
            update_tools()
        for column, (label, command) in enumerate((
            ("Сброс", toolbar.home), ("Увеличение", toolbar.zoom),
            ("Перемещение", toolbar.pan), ("Сохранить PNG", toolbar.save_figure),
        )):
            button = ttk.Button(tools, text=label, command=lambda command=command: tool(command),
                                style="Toolbar.TButton")
            button.grid(row=0, column=column, padx=(4, 0))
            tool_buttons[label] = button
        toolbar._visible_buttons = tool_buttons
        canvas.mpl_connect("key_release_event", update_tools)
        readout = tk.StringVar()
        self.chart_readouts.append(readout)
        ttk.Label(card, textvariable=readout, style="Hint.TLabel", wraplength=950).grid(
            row=2, column=0, sticky="ew", padx=12, pady=3)
        index = len(self.chart_cards) - 1
        if index == 0:
            self.signal_mode_box = ttk.Combobox(header, textvariable=self.controller_signal_mode,
                values=("Ошибка и управление", "Только ошибка", "Только управление"), state="readonly", width=22)
            self.signal_mode_box.grid(row=3, column=0, columnspan=2, sticky="w", pady=4)
            self.signal_mode_box.bind("<<ComboboxSelected>>", lambda _event: self._redraw_signal_mode())
        ttk.Button(header, text="Развернуть / вернуть", command=lambda: self._set_chart_mode(
            "both" if self.chart_mode != "both" else "input" if index == 0 else "response"),
            style="Toolbar.TButton").grid(row=2, column=0, sticky="w", pady=(4, 0))

        return axis, canvas, toolbar

    def _apply_scenario_data(self, scenario, variant_number=None):
        self._cancel_task(announce=False)
        self._undo_clear = None
        self.undo_clear_button.configure(state="disabled")
        self._page_context.clear()
        for page in self.pages.values():
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
        self.absorber_form.start_time.set(self._format_number(scenario["start_time"]))
        self.absorber_form.simulation_duration.set(self._format_number(scenario["simulation_duration"]))
        self.absorber_form.effect_duration.set(self._format_number(scenario["effect_duration"]))
        self.absorber_form.time_constant.set(self._format_number(scenario["time_constant"]))
        self.absorber_form.delay.set(self._format_number(scenario["delay"]))
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
            self.absorber_form.proportional_gain.set(self._format_number(controller["gain"]))
            self.absorber_form.integral_time.set(self._format_number(controller["integral_time"]))
            self.absorber_form.derivative_time.set(self._format_number(controller["derivative_time"]))
            self.absorber_form.control_limit.set(self._format_number(controller["control_limit"] * 100))
            setpoint = controller.get("setpoint")
            self.absorber_form.setpoint.set(
                self._format_number(100 * (self.absorber_form.current_baseline() if setpoint is None else setpoint))
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
        self.topbar_context.set(
            f"{scenario_title} · "
            f"{'без регулятора' if controller is None else controller['type'] + '-регулятор'}"
        )
        self._draw_static_charts()
        self._show_page("scenarios")
        self._set_status("Сценарий применён — выполните расчёт")

    def _close_application(self):
        editor = self.scenario_library.editor
        if editor is not None and editor.winfo_exists():
            if not editor.request_close():
                return
        self._cancel_task(announce=False)
        for job in self._layout_jobs:
            self.root.after_cancel(job)
        self._layout_jobs.clear()
        for canvas in (self.disturbance_canvas, self.response_canvas):
            if canvas._idle_draw_id is not None:
                canvas.get_tk_widget().after_cancel(canvas._idle_draw_id)
                canvas._idle_draw_id = None
        self._save_autosave()
        if hasattr(self, "_autosave_job"):
            self.root.after_cancel(self._autosave_job)
        try:
            self.settings_store.save({
                "geometry": self.root.geometry(),
                "last_page": self.current_page,
                "sidebar_collapsed": self.sidebar_collapsed,
                "chart_mode": getattr(self, "_comparison_chart_mode", self.chart_mode) if self.current_page == "comparison" else self.chart_mode,
                "inspector_width": self.main_panes.sashpos(0) if len(self.main_panes.panes()) == 2 else self.settings["inspector_width"],
                "chart_split": self.chart_panes.sashpos(0) / max(1, self.chart_panes.winfo_height())
                    if len(self.chart_panes.panes()) == 2 else self.settings["chart_split"],
            })
        except OSError:
            pass
        self.root.destroy()

    def _save_comparison_session(self):
        selected = filedialog.asksaveasfilename(
            parent=self.root,
            title="Сохранить учебный сеанс",
            defaultextension=".json",
            filetypes=(("Учебный сеанс JSON", "*.json"),),
            initialfile="dynamics_control_session.json",
        )
        if not selected:
            return
        try:
            self._finish_autosave(wait=True)
            path = write_session(selected, self.comparison_page.runs, self.comparison_page.counter, self._capture_laboratory())
        except (OSError, ValueError, TypeError) as error:
            self._set_status(f"Не удалось сохранить сеанс: {error}", error=True)
            return
        self._set_status(f"Учебный сеанс сохранён: {path.name}")

    def _open_comparison_session(self):
        if self.comparison_page.runs and not messagebox.askyesno(
            "Открыть учебный сеанс",
            "Текущая история опытов будет заменена. Продолжить?",
            parent=self.root,
        ):
            return
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="Открыть учебный сеанс",
            filetypes=(("Учебный сеанс JSON", "*.json"),),
        )
        if not selected:
            return
        try:
            runs, counter, laboratory = read_laboratory_session(selected)
            result = restore_calculation(laboratory["last_calculation"]) if laboratory is not None else None
        except (OSError, ValueError) as error:
            self._set_status(str(error), error=True)
            return
        if len(runs) > MAX_COMPARISON_RUNS:
            self._set_status(
                f"В сеансе {len(runs)} опытов; поддерживается не больше {MAX_COMPARISON_RUNS}",
                error=True,
            )
            return
        self.comparison_page.runs = runs
        self.comparison_page.counter = counter
        if laboratory is not None:
            self._restore_laboratory(laboratory, result)
            self._set_status(f"Лабораторная восстановлена: {len(runs)} закреплённых опытов")
            return
        selected_ids = tuple(run["id"] for run in runs)
        self.comparison_page.refresh_table(selected_ids)
        self.comparison_page.draw()
        self._set_status(f"Открыт учебный сеанс: {len(runs)} опытов")

    def _capture_laboratory(self, *, copy_results=True):
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
            "conclusion": self.assignment_panel.student_conclusion.get(), "page": self.current_page,
            "selected_runs": list(self.comparison_page.table.selection()), "last_calculation": snapshot,
            "identification": self.identification_page.capture(validate=False, copy_results=copy_results),
            "selected_model": MODELS.named(self.selected_model.get()).id,
            "model_results": copy.deepcopy(self.model_results) if copy_results else self.model_results,
            "tank": self.tank_page.capture(copy_results=copy_results),
        }

    def _restore_laboratory(self, laboratory, result):
        self._cancel_task(announce=False)
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
            self.baseline_result.set(self._format_number(result["baseline"] * 100) + "%")
            self.disturbance_result.set(f"{self._format_number(result['combined_fraction'])} ({result['combined_fraction'] * 100:+.1f}%)")
            self.calculated_result.set(self._format_number(result["calculated"] * 100) + "%")
            self.final_result.set(self._format_number(result["final_response"][-1] * 100) + "%")
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
        self.tank_page.restore(laboratory.get("tank"))
        self.model_results = copy.deepcopy(laboratory.get("model_results", {}))
        if result is not None and "absorber" not in self.model_results:
            self.model_results["absorber"] = MODELS.result("absorber", result)
        if self.tank_page.result is not None and "level_tank" not in self.model_results:
            self.model_results["level_tank"] = MODELS.result("level_tank", self.tank_page.result)
        self.selected_model.set(MODELS.get(laboratory["selected_model"]).name)
        self._show_page(laboratory["page"])
        self._mark_result_stale()
        self.calculate_button_text.set("Проверить прогноз" if self.assignment_panel.assignment_enabled.get()
                                       else "Пересчитать" if result is not None else "Рассчитать")

    def _autosave_state(self):
        return (self._capture_laboratory(copy_results=False),
                [(run["id"], run["name"]) for run in self.comparison_page.runs], self.comparison_page.counter)

    def _finish_autosave(self, *, wait=False):
        task = self._autosave_task
        if task is None:
            return
        if wait:
            task.thread.join()
        outcome = task.poll()
        if outcome is not None:
            self._autosave_task = None
            snapshot, error = outcome
            if error is None:
                self._autosave_snapshot = snapshot
            else:
                self._set_status(f"Не удалось автоматически сохранить лабораторную: {error}", error=True)

    def _save_autosave(self):
        # Explicit saves and shutdown wait for the writer before saving newer data.
        self._finish_autosave(wait=True)
        try:
            state = self._autosave_state()
            if state != self._autosave_snapshot:
                snapshot = copy.deepcopy(state)
                write_session(self.autosave_path, self.comparison_page.runs, self.comparison_page.counter, snapshot[0])
                self._autosave_snapshot = snapshot
        except (OSError, ValueError, TypeError) as error:
            self._set_status(f"Не удалось автоматически сохранить лабораторную: {error}", error=True)

    def _autosave_tick(self):
        self._finish_autosave()
        if self._autosave_task is None:
            try:
                state = self._autosave_state()
                if state != self._autosave_snapshot:
                    snapshot, runs = copy.deepcopy(state), copy.deepcopy(self.comparison_page.runs)
                    def save(cancel, progress):
                        write_session(self.autosave_path, runs, snapshot[2], snapshot[0])
                        return snapshot
                    self._autosave_task = BackgroundTask(save)
            except (OSError, ValueError, TypeError) as error:
                self._set_status(f"Не удалось автоматически сохранить лабораторную: {error}", error=True)
        self._autosave_job = self.root.after(5000, self._autosave_tick)

    def _restore_autosave(self):
        backup = self.autosave_path.with_suffix(".json.bak")
        if not self.autosave_path.exists() and not backup.exists():
            return
        warning = None
        for path in (self.autosave_path, backup):
            try:
                runs, counter, laboratory = read_laboratory_session(path)
                result = restore_calculation(laboratory["last_calculation"]) if laboratory is not None else None
            except (OSError, ValueError) as error:
                warning = f"Автосохранение повреждено: {error}"
                if path == self.autosave_path:
                    try:
                        shutil.copyfile(path, path.with_suffix(".json.corrupt"))
                    except OSError:
                        pass
                continue
            self.comparison_page.runs, self.comparison_page.counter = runs, counter
            if laboratory is not None:
                self._restore_laboratory(laboratory, result)
            else:
                self.comparison_page.refresh_table()
            if path != self.autosave_path:
                self._set_status("Лабораторная восстановлена из резервного автосохранения")
            else:
                self._set_status("Лабораторная восстановлена из автосохранения")
            return
        self._set_status(warning or "Не удалось восстановить лабораторную", error=True)

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
            if hasattr(self, "identification_page") and self.current_page == "identification" and self.identification_page.view.get() == "Ошибки на абсорбере":
                self.identification_page.draw()
            return
        stale = self._normalized_input_state(self._capture_input_state()) != self._normalized_input_state(self.last_calculation["input_state"])
        self.export_panel.export_summary.set("Параметры изменены — пересчитайте опыт. Экспорт относится к последнему расчёту."
                                if stale else "Расчёт готов к экспорту.")
        chain_name = "обеднённый газ" if self.last_calculation["chain"] == LEAN_GAS else "насыщенный абсорбент"
        self.response_subtitle.set(f"Параметры изменены — последний расчёт: {chain_name}" if stale
                                   else f"Последний расчёт: {chain_name}")
        if hasattr(self, "identification_page") and self.current_page == "identification" and self.identification_page.view.get() == "Ошибки на абсорбере":
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
        self._cancel_task(announce=False)
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
            self._set_status("Исправьте значение", error=True)
            return

        state = self._capture_input_state()
        metadata = {"disturbance_type": self.absorber_form.disturbance_type.get(), "input_state": state,
                    "title": self._active_scenario_title(), "lesson": copy.deepcopy(self.assignment_panel.current_lesson),
                    "experiment_origin": self.identification_page.active_origin()}
        self._start_task("Расчёт опыта", lambda cancel, progress: run_model("absorber",
            dict(chain=state["chain"], parameters=state["model_values"], component=component_fraction,
                 flow=flow_fraction, dynamics=dynamics, controller=controller), cancel=cancel, progress=progress),
            lambda result: self._apply_calculation(result, prediction, metadata))

    def _apply_calculation(self, result, prediction, metadata):
        self.model_results["absorber"] = MODELS.result("absorber", result)
        self._undo_clear = None
        self.undo_clear_button.configure(state="disabled")
        result.update(metadata)
        component_fraction, flow_fraction = result["component_fraction"], result["flow_fraction"]
        dynamics = result["dynamics"]
        self.baseline_result.set(self._format_number(result["baseline"] * 100) + "%")
        self.disturbance_result.set(
            f"{self._format_number(result['combined_fraction'])} "
            f"({result['combined_fraction'] * 100:+.1f}%)"
        )
        self.calculated_result.set(self._format_number(result["calculated"] * 100) + "%")
        self._update_calculation_steps(
            component_fraction,
            flow_fraction,
            result["combined_fraction"],
            result["baseline"],
            result["calculated"],
        )
        self._draw_calculation_result(result)

        self.result_mode.set(result["result_mode"])
        self.final_result.set(self._format_number(result["final_response"][-1] * 100) + "%")
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
        self.topbar_context.set(
            f"{active_name} · {result['result_mode'].lower()} · "
            f"отклонение {result['metrics']['maximum_deviation']:.4g} · {settling_summary}"
        )
        self.calculate_button_text.set("Пересчитать")
        if self.assignment_panel.learning_mode.get():
            self._show_page("scenarios" if prediction is not None else "results")
        self._set_status("Расчёт выполнен")

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
        self.transition_values["initial"].set(self._format_number(metrics["initial_value"] * 100) + "%")
        self.transition_values["steady"].set(self._format_number(metrics["steady_state"] * 100) + "%")
        self.transition_values["maximum_deviation"].set(self._format_number(metrics["maximum_deviation"] * 100) + " п.п.")
        self.transition_values["relative_deviation"].set(
            f"{relative_deviation:.2f}%" if relative_deviation is not None else "не определено"
        )
        self.transition_values["time_constant"].set(f"{self._format_number(dynamics['time_constant'])} с")
        self.transition_values["settling_time"].set(settling_text)
        self.transition_values["settling_moment"].set(settling_moment_text)
        self.transition_values["static_error"].set(self._format_signed_number(metrics["static_error"] * 100) + " п.п.")
        self.transition_values["final_error"].set(self._format_signed_number(metrics["final_error"] * 100) + " п.п.")
        self.transition_values["settling_status"].set(metrics["settling_status"])
        self.transition_values["iae"].set("не применимо без регулятора" if metrics["iae"] is None
                                          else self._format_number(metrics["iae"] * 100) + " п.п.·с")
        self.transition_values["saturation_duration"].set("не применимо без регулятора" if metrics["saturation_duration"] is None
                                                          else self._format_number(metrics["saturation_duration"]) + " с")

    def _reset(self):
        self._cancel_task(announce=False)
        self._undo_clear = (self._capture_laboratory(), self.last_calculation)
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
        self._set_status("Опыт очищен; динамика, регулятор и история сравнения сохранены")

    def _restore_experiment_defaults(self):
        self._cancel_task(announce=False)
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
        self._set_status("Исходные настройки применённого сценария восстановлены")

    def _undo_clear_experiment(self):
        if self._undo_clear is None:
            return
        laboratory, result = self._undo_clear
        self._undo_clear = None
        self._restore_laboratory(laboratory, result)
        self.undo_clear_button.configure(state="disabled")
        self._set_status("Очистка отменена; опыт восстановлен")

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
        self.response_chart_title.set("Кривая разгона")
        self._style_axis(self.response_axis, "Время, с", "X, %")
        self.response_axis.plot(
            [0, simulation_duration],
            [baseline * 100, baseline * 100],
            color=CURVE_STYLES["Исходный режим"][0],
            linestyle=CURVE_STYLES["Исходный режим"][1],
            linewidth=2,
            label="Исходный режим",
        )
        self._place_legend_above(self.response_axis)
        self.response_canvas.draw_idle()
        self._charts_show_comparison = False

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
        self.signal_mode_box.configure(state="readonly" if controller is not None and self.current_page not in (
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
        self._charts_show_comparison = False

    def _draw_disturbance(
        self,
        time,
        profile,
        component_fraction,
        flow_fraction,
        dynamics=None,
    ):
        self._remove_controller_axis()
        self.primary_chart_title.set("Возмущающее воздействие")
        self.primary_chart_subtitle.set("Изменение относительно базового уровня")
        component_signal = component_fraction * profile
        flow_signal = flow_fraction * profile
        combined_signal = (1 + component_signal) * (1 + flow_signal) - 1

        self._style_axis(self.disturbance_axis, "Время, с", "Относительное изменение")
        self.disturbance_axis.axhline(0.0, color=CURVE_STYLES["Исходный режим"][0], linestyle="--", linewidth=1.5, label="Исходный режим")
        self.disturbance_axis.plot(*plot_samples(time, component_signal), color=CURVE_STYLES["Только состав"][0], linewidth=2, label="Состав")
        self.disturbance_axis.plot(*plot_samples(time, flow_signal), color=CURVE_STYLES["Только расход"][0], linewidth=2, label="Расход")
        self.disturbance_axis.plot(*plot_samples(time, combined_signal), color=CURVE_STYLES["Совместное воздействие"][0], linewidth=2.4, label="Совместно")
        self._annotate_timing(self.disturbance_axis, dynamics)
        self.disturbance_axis.margins(x=0.02, y=0.15)
        self._place_legend_above(self.disturbance_axis)
        self._enable_legend_toggles(self.disturbance_axis, self.disturbance_canvas)
        self.disturbance_canvas.draw_idle()

    def _draw_response(self, time, responses, dynamics=None, metrics=None):
        self.response_chart_title.set("Кривая разгона")
        self._style_axis(self.response_axis, "Время, с", "X, %")
        for label, response in responses.items():
            color, linestyle = CURVE_STYLES[label]
            self.response_axis.plot(
                *plot_samples(time, response * 100),
                color=color,
                linestyle=linestyle,
                linewidth=2.4 if label == "Совместное воздействие" else 1.8,
                label=label,
            )
        self._annotate_transition(self.response_axis, dynamics, metrics)
        self.response_axis.margins(x=0.02, y=0.12)
        self._place_legend_above(self.response_axis)
        self._enable_legend_toggles(self.response_axis, self.response_canvas)
        self.response_canvas.draw_idle()

    def _redraw_signal_mode(self):
        if self.last_calculation is not None and self.last_calculation["controller"] is not None and not self._charts_show_comparison and self.current_page not in ("identification", "tank", "model_result"):
            self._draw_calculation_result(self.last_calculation)

    def _draw_controller_signals(self, time, error, control, dynamics=None, commanded=None):
        self._remove_controller_axis()
        self.primary_chart_title.set("Ошибка и управляющее воздействие")
        self.primary_chart_subtitle.set("Сигналы замкнутой системы")
        self._style_axis(self.disturbance_axis, "Время, с", "Ошибка e(t), п.п.")
        error_line = self.disturbance_axis.plot(
            *plot_samples(time, error * 100),
            color="#F59E0B",
            linewidth=2,
            label="Ошибка e(t), п.п.",
        )[0]
        self.disturbance_axis.axhline(0.0, color=MUTED, linestyle="--", linewidth=1)
        self._annotate_timing(self.disturbance_axis, dynamics)

        mode = self.controller_signal_mode.get()
        if mode == "Только ошибка":
            self._place_legend_above(self.disturbance_axis)
            self._enable_legend_toggles(self.disturbance_axis, self.disturbance_canvas)
            self.disturbance_canvas.draw_idle()
            return
        if mode == "Только управление":
            self._style_axis(self.disturbance_axis, "Время, с", "η, %")
            self.controller_signal_axis = None
            control_axis = self.disturbance_axis
        else:
            self.controller_signal_axis = self.disturbance_axis.twinx()
            control_axis = self.controller_signal_axis
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
        self.disturbance_axis.margins(x=0.02, y=0.15)
        self._place_legend_above(
            self.disturbance_axis,
            lines,
            [line.get_label() for line in lines],
        )
        self._enable_legend_toggles(self.disturbance_axis, self.disturbance_canvas)
        self.disturbance_canvas.draw_idle()

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
        self.response_chart_title.set(f"Без регулятора / {controller_type}-регулятор")
        self._style_axis(self.response_axis, "Время, с", "X, %")
        self.response_axis.axhline(
            setpoint * 100,
            color=MUTED,
            linestyle="--",
            linewidth=1.5,
            label="Задание",
        )
        self.response_axis.plot(
            *plot_samples(time, open_response * 100),
            color="#F59E0B",
            linewidth=2,
            label="Без регулятора",
        )
        self.response_axis.plot(
            *plot_samples(time, controlled_response * 100),
            color=ACCENT,
            linewidth=2.4,
            label=f"{controller_type}-регулятор",
        )
        if measurement is not None:
            self.response_axis.plot(*plot_samples(time, measurement * 100), color="#64748B",
                                    linewidth=0.6, alpha=0.55, label="Измеренный выход")
        self._annotate_transition(self.response_axis, dynamics, metrics)
        self.response_axis.margins(x=0.02, y=0.12)
        self._place_legend_above(self.response_axis)
        self._enable_legend_toggles(self.response_axis, self.response_canvas)
        self.response_canvas.draw_idle()

    @staticmethod
    def _place_legend_above(axis, handles=None, labels=None):
        legend = adaptive_legend(axis, handles, labels)
        canvas = axis.figure.canvas
        old = getattr(canvas, "_legend_resize_callback", None)
        if old is not None:
            canvas.mpl_disconnect(old)
        if legend is None:
            return

        width = (axis.figure.bbox.width, axis.figure.dpi)
        def resized(_event):
            if (axis.figure.bbox.width, axis.figure.dpi) == width:
                return
            DynamicsControlApp._place_legend_above(axis, handles, labels)
            DynamicsControlApp._enable_legend_toggles(axis, canvas)
            canvas.draw_idle()

        canvas._legend_resize_callback = canvas.mpl_connect("resize_event", resized)

    @staticmethod
    def _enable_legend_toggles(axis, canvas):
        for attribute in ("_legend_toggle_callback", "_legend_key_callback"):
            callback = getattr(canvas, attribute, None)
            if callback is not None:
                canvas.mpl_disconnect(callback)
        legend = axis.get_legend()
        if legend is None:
            return
        artists_by_label = {
            artist.get_label(): artist
            for chart in axis.figure.axes
            for artist in (*chart.lines, *chart.patches, *chart.collections)
            if artist.get_label() and not artist.get_label().startswith("_")
        }
        visibility = getattr(canvas, "_curve_visibility", {})
        canvas._curve_visibility = visibility
        entries = []
        toggle_map = {}
        labels = getattr(legend, "_source_labels", tuple(text.get_text() for text in legend.get_texts()))
        for sample, text, label in zip(legend.legend_handles, legend.get_texts(), labels, strict=True):
            artist = artists_by_label.get(label)
            if artist is None:
                continue
            artist.set_visible(visibility.get(label, artist.get_visible()))
            sample.set_picker(5)
            text.set_picker(True)
            entry = (artist, sample, text, label)
            entries.append(entry)
            toggle_map[sample] = toggle_map[text] = entry
            for item in (sample, text):
                item.set_alpha(1.0 if artist.get_visible() else 0.25)

        def change(entry):
            artist, sample, text, label = entry
            visible = not artist.get_visible()
            artist.set_visible(visible)
            visibility[label] = visible
            sample.set_alpha(1.0 if visible else 0.25)
            text.set_alpha(1.0 if visible else 0.25)
            canvas.draw_idle()

        def toggle(event):
            if event.artist in toggle_map:
                change(toggle_map[event.artist])

        def keyboard(event):
            if event.key in tuple(str(index + 1) for index in range(len(entries))):
                change(entries[int(event.key) - 1])

        canvas._legend_toggle_callback = canvas.mpl_connect("pick_event", toggle)
        canvas._legend_key_callback = canvas.mpl_connect("key_press_event", keyboard)
        if hasattr(canvas, "get_tk_widget") and not getattr(canvas, "_legend_focus_bound", False):
            widget = canvas.get_tk_widget()
            widget.configure(takefocus=True)
            widget.bind("<Button-1>", lambda _event: widget.focus_set(), add="+")
            canvas._legend_focus_bound = True

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

    def _remove_controller_axis(self):
        if self.controller_signal_axis is not None:
            self.controller_signal_axis.remove()
            self.controller_signal_axis = None

    def _safe_simulation_duration(self):
        try:
            return parse_positive_number(self.absorber_form.simulation_duration.get())
        except ValueError:
            return 100.0

    @staticmethod
    def _style_axis(axis, x_label, y_label):
        axis.clear()
        axis.set_facecolor(CARD_BACKGROUND)
        axis.set_xlabel(x_label, color=TEXT)
        axis.set_ylabel(y_label, color=TEXT)
        axis.grid(True, color="#DDE2E8", linewidth=0.8)
        axis.tick_params(colors=TEXT)
        axis.spines["top"].set_visible(False)
        axis.spines["right"].set_visible(False)
        axis.spines["left"].set_color(BORDER)
        axis.spines["bottom"].set_color(BORDER)

    def _set_status(self, message, error=False):
        self.status_text.set(message)
        self.status_dot.configure(foreground=ERROR if error else SUCCESS)

    @staticmethod
    def _format_number(value):
        return f"{value:.4f}".rstrip("0").rstrip(".")

    @staticmethod
    def _format_signed_number(value):
        if abs(value) < 0.00005:
            return "0"
        return f"{value:+.4f}".rstrip("0").rstrip(".").replace("-", "−")

    @staticmethod
    def _factor_expression(fraction):
        sign = "+" if fraction >= 0 else "−"
        return f"1 {sign} {abs(fraction):.2f}"


def _run_release_check(output_path):
    from pathlib import Path
    import tempfile
    report = {"version": APP_VERSION, "frozen": bool(getattr(sys, "frozen", False)), "status": "failed"}
    root = None
    try:
        with tempfile.TemporaryDirectory(prefix="dynamics-control-release-") as directory:
            scenario_path = Path(directory) / "scenarios.json"
            root = tk.Tk()
            root.withdraw()
            app = DynamicsControlApp(root, scenario_path)
            for scenario in app.scenario_library.scenarios:
                app._apply_scenario_data(scenario)
                app._calculate()
                import time
                deadline = time.monotonic() + 30
                while app._task is not None:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Превышено время проверки расчёта.")
                    root.update()
                    time.sleep(0.01)
                if app.last_calculation is None:
                    raise RuntimeError(app.status_text.get())
            report["builtin_scenarios"] = len(app.scenario_library.scenarios)
            app.identification_page.load_path(Path(__file__).resolve().parent / "data" / "identification_step.csv")
            app.identification_page.metadata["input_role"].set("η, доля")
            app.identification_page.metadata["output_role"].set("Xна, доля")
            app.identification_page.metadata["conclusion"].set("Проверка экспериментального цикла")
            app.identification_page.calculate()
            deadline = time.monotonic() + 30
            while app._task is not None:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Превышено время проверки идентификации.")
                root.update()
                time.sleep(0.01)
            fitted = app.identification_page.result
            if fitted is None or abs(fitted["time_constant"] - 8) > .08 or abs(fitted["delay"] - 2) > .2:
                raise RuntimeError("Учебный CSV не восстановил эталонную динамику.")
            report["identification_verified"] = True
            page = app.identification_page
            page.check_pi()
            deadline = time.monotonic() + 30
            while app._task is not None:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Превышено время проверки PI.")
                root.update()
                time.sleep(.01)
            if page.pi_result is None:
                raise RuntimeError(page.summary.get())
            from app.experiment_control import validate_step
            validation_time = np.linspace(0, 120, 601)
            validation_input = np.where(validation_time < 15, .2, .7)
            validation_output = .3 + fitted["gain"]*.5*-np.expm1(-np.maximum(validation_time-15-fitted["delay"], 0)/fitted["time_constant"])
            page.accept_validation(dict(validate_step(validation_time, validation_input, validation_output, fitted),
                                        source="release-validation.csv", origin=page.origin()))
            if (not page.validation["quality"]["error_acceptable"]
                    or "RMSE отклика" not in page.summary.get()):
                raise RuntimeError("Показатели качества независимой проверки не подтверждены.")
            no_response_time = np.r_[np.linspace(0, 9999, 101), np.linspace(10000, 10080, 401)]
            no_response = validate_step(no_response_time, np.where(no_response_time < 10000, .3, .5),
                                        np.full_like(no_response_time, .2), dict(gain=.4, time_constant=8, delay=2))
            if no_response["quality"]["error_acceptable"] or not no_response["warning"]:
                raise RuntimeError("Длинный исходный участок скрыл отсутствие отклика.")
            report["identification_quality_verified"] = True
            app.absorber_form.chain = RICH_ABSORBENT
            app.absorber_form.controller_enabled.set(True); app.absorber_form.controller_type.set("PI")
            app.absorber_form.setpoint.set("30.5"); app.absorber_form.control_limit.set("20")
            app.absorber_form.component_enabled.set(False); app.absorber_form.flow_enabled.set(False)
            app.absorber_form.simulation_duration.set("100"); app.absorber_form.time_constant.set("8"); app.absorber_form.delay.set("0")
            for key, value in dict(k_errors="0;20", t_errors="0", l_errors="0").items():
                page.absorber_sensitivity.fields[key].set(value)
            page.absorber_sensitivity.start()
            deadline = time.monotonic()+30
            while app._task is not None:
                if time.monotonic() >= deadline: raise RuntimeError("Превышено время проверки ошибок оценки абсорбера.")
                root.update(); time.sleep(.01)
            errors = page.absorber_sensitivity.result
            if errors is None or len(errors["cases"]) != 2:
                raise RuntimeError(page.summary.get())
            from app.absorber_sensitivity_exporting import write_report as write_absorber_errors
            for extension in ("csv", "html", "pdf"):
                write_absorber_errors(Path(directory)/f"absorber-errors.{extension}", errors, 1, extension)
            report["absorber_identification_errors_verified"] = True
            from app.exporting import write_experiment_report
            write_experiment_report(Path(directory)/"experiment.html", page.capture(), "html")
            expected_experiment = page.capture()
            app._show_page("tank")
            tank = app.tank_page
            def wait_tank():
                deadline = time.monotonic()+30
                while app._task is not None:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Превышено время проверки бака.")
                    root.update(); time.sleep(.01)
            tank.generate(); wait_tank()
            tank.identify(); wait_tank()
            tank.check_pi(); wait_tank()
            for key, value in dict(k_errors="0;20", t_errors="0", l_errors="0").items():
                tank.sensitivity.fields[key].set(value)
            tank.sensitivity.start(); wait_tank()
            if tank.sensitivity.result is None or len(tank.sensitivity.result["cases"]) != 2:
                raise RuntimeError(tank.summary.get())
            from app.tank_sensitivity_exporting import write_report
            for extension in ("csv", "html", "pdf"):
                write_report(Path(directory)/f"tank-errors.{extension}", tank.sensitivity.result, 1, extension)
            report["identification_errors_verified"] = True
            if tank.fit is None or tank.pi is None or len(tank.pi["cases"]) != 8:
                raise RuntimeError(tank.summary.get())
            from app.tank import DEFAULTS, simulate
            independent = simulate(dict(DEFAULTS,step_time=10.,pump_step=.0005))
            tank.validation = dict(validate_step(independent["time"],independent["pump"],independent["level"],tank.fit),
                                   source="release-tank-validation.csv",origin=tank.origin(),revision=tank.revision)
            from app.tank_exporting import write_tank_report
            expected_tank = tank.capture()
            for extension in ("csv","html","pdf"):
                write_tank_report(Path(directory)/f"tank.{extension}",expected_tank,extension)
            from app.model_contract import read_result, write_result
            from app.model_output import write_model_report
            expected_models = copy.deepcopy(app.model_results)
            if set(expected_models) != {model.id for model in MODELS.all()}:
                raise RuntimeError("Не получены общие результаты обеих моделей.")
            for model_id, common in expected_models.items():
                app.selected_model.set(MODELS.get(model_id).name)
                app._show_page("model_result")
                path = Path(directory)/f"{model_id}-result.json"
                write_result(path, common, MODELS)
                if read_result(path, MODELS) != common:
                    raise RuntimeError("Общий результат восстановлен неточно.")
                for extension in ("csv", "html", "pdf"):
                    write_model_report(Path(directory)/f"{model_id}-result.{extension}", common, extension, MODELS)
            app._show_page("tank")
            expected = app.last_calculation["final_response"].copy()
            app.assignment_panel.student_conclusion.set("Проверка восстановления релиза")
            app._save_autosave()
            if not app.autosave_path.exists():
                raise RuntimeError(app.status_text.get())
            app._close_application()
            root = None
            root = tk.Tk()
            root.withdraw()
            app = DynamicsControlApp(root, scenario_path)
            if (app.last_calculation is None
                    or not np.array_equal(expected, app.last_calculation["final_response"])
                    or app.assignment_panel.student_conclusion.get() != "Проверка восстановления релиза"):
                raise RuntimeError("Не удалось точно восстановить расчёт и пользовательские данные.")
            if app.identification_page.capture() != expected_experiment:
                raise RuntimeError("Эксперимент и проверка PI восстановлены неточно.")
            report["experiment_workflow_verified"] = True
            if app.current_page != "tank" or app.tank_page.capture() != expected_tank:
                raise RuntimeError("Не удалось точно восстановить работу с баком.")
            report["tank_workflow_verified"] = True
            if app.model_results != expected_models:
                raise RuntimeError("Общие результаты моделей восстановлены неточно.")
            report["model_contract_verified"] = True
            app._close_application()
            root = None
            report.update(status="passed", gui_started=True, autosave_restored=True)
    except Exception as error:
        report["error"] = str(error)
    finally:
        if root is not None:
            root.destroy()
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if report["status"] == "passed" else 1


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--performance-check":
        from app.performance import run_check
        raise SystemExit(run_check(sys.argv[2], DynamicsControlApp, APP_VERSION))
    if len(sys.argv) == 3 and sys.argv[1] == "--smoke-test":
        raise SystemExit(_run_release_check(sys.argv[2]))
    root = tk.Tk()
    DynamicsControlApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
