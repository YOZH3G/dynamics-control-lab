import copy
import importlib
import json
import shutil
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

APP_NAME = "Лаборатория динамики и управления"
APP_VERSION = "1.15.0"

from app.comparison import MAX_COMPARISON_RUNS
from app.model_contract import copy_data
from app.object_file import DESCRIBED, load_description
from app.background_tasks import BackgroundTask, CalculationCancelled
from app.session_store import MAX_FILE_OBJECTS, read_laboratory_session, restore_calculation, write_session
from app.settings_store import SettingsStore
from app.plotting import adaptive_legend
from app.simulation import RICH_ABSORBENT
from ui.model_result_page import ModelResultPage
from ui.absorber_workspace import AbsorberWorkspace
from app.physical_models import MODELS
from ui.chart_tools import SharedTimeCursor
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
    relax_field_widths,
)


class DynamicsControlApp(ttk.Frame):
    def __init__(self, root, scenario_path=None):
        super().__init__(root, style="App.TFrame")
        self.root = root
        self.absorber = AbsorberWorkspace(self, scenario_path)
        self.settings_store = SettingsStore(
            self.absorber.scenario_library.store.path.with_name("settings.json")
        )
        self.settings = self.settings_store.load()
        self.sidebar_collapsed = self.settings["sidebar_collapsed"]
        self.pages = {}
        self.model_results = {}
        self.nav_buttons = {}

        self.page_title = tk.StringVar(value="Возмущения")
        self.topbar_context = tk.StringVar(value="Свободный расчёт · без регулятора")
        self.response_subtitle = tk.StringVar()
        self.primary_chart_title = tk.StringVar(value="Возмущающее воздействие")
        self.primary_chart_subtitle = tk.StringVar(value="Изменение относительно базового уровня")
        self.response_chart_title = tk.StringVar(value="Кривая разгона")
        self.status_text = tk.StringVar(value="Готово к расчёту")
        self.controller_signal_axis = None
        self.current_page = self.settings["last_page"]
        self._charts_show_comparison = False
        self.text_tooltips = []
        self._task = None
        self._task_poll = None
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
        self.absorber.connect()
        if self.absorber.scenario_library.store.warning:
            self._set_status(self.absorber.scenario_library.store.warning, error=True)
        self.autosave_path = self.absorber.scenario_library.store.path.with_name("laboratory.autosave.json")
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
        self.absorber.build_route_button(topbar)
        self.selected_model = tk.StringVar(value="Абсорбер")
        self.model_box = ttk.Combobox(topbar, textvariable=self.selected_model, values=tuple(model.name for model in MODELS.all()),
                                      state="readonly", width=12)
        self.model_box.grid(row=0, column=2, sticky="e", padx=(12, 0))
        self.model_box.bind("<<ComboboxSelected>>", self._select_model)
        ttk.Button(topbar, text="Открыть объект…", command=self._open_object_file, style="Toolbar.TButton").grid(
            row=0, column=3, sticky="e", padx=(8, 0))
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

        self.page_host = page_host = ttk.Frame(inspector, style="App.TFrame")
        page_host.grid(row=1, column=0, sticky="nsew")
        page_host.columnconfigure(0, weight=1)
        page_host.rowconfigure(0, weight=1)
        self.page_contents = {}
        for key in (*(section for model in MODELS.all() for section in model.sections), "model_result"):
            page = ScrollablePage(page_host, BACKGROUND)
            page.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = page
            self.page_contents[key] = page.content

        self.absorber.build_sections(self.page_contents)
        self.model_result_page = ModelResultPage(self.page_contents["model_result"], self)
        self.model_result_page.grid(row=0, column=0, sticky="nsew")
        for page in self.pages.values():
            relax_field_widths(page.content)
            page.bind_mousewheel()
        self.object_pages = {}
        for model in MODELS.all():
            if model.page:
                self._add_object_page(model)

        actions = ttk.Frame(inspector, style="App.TFrame")
        actions.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        actions.columnconfigure((0, 1), weight=1)
        self.absorber.build_actions(actions)
        self.cancel_button = ttk.Button(actions, text="Отменить расчёт", command=self._cancel_task, state="disabled")
        self.cancel_button.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.task_progress = ttk.Progressbar(actions, maximum=100)
        self.task_progress.grid(row=3, column=1, sticky="ew", padx=(6, 0), pady=(6, 0))

        workspace = ttk.Frame(self.main_panes, style="App.TFrame", padding=(4, 16, 16, 12))
        self.workspace = workspace
        self.main_panes.add(workspace, weight=1)
        workspace.columnconfigure(0, weight=1)
        workspace.rowconfigure(1, weight=1)
        self.absorber._build_metric_strip(workspace)
        self.absorber.build_comparison_workspace(workspace)
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
        self.absorber.assignment_panel.update_route()

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
        if table_height != int(self.absorber.comparison_page.table["height"]):
            self.absorber.comparison_page.table.configure(height=table_height)
        canvas_height = max(140, int((event.height - 140 * scale) * 0.43))
        if canvas_height != int(self.absorber.comparison_workspace.canvas["height"]):
            self.absorber.comparison_workspace.canvas.configure(height=canvas_height)
        if event.height < 850 * scale and self.chart_mode == "both" and self.current_page != "comparison":
            self._set_chart_mode("response")

    def _upper_chart_label(self):
        if self.current_page == "identification" and self.absorber.identification_page.view.get() == "Ошибки на абсорбере":
            return "Ошибки K/T"
        if self.current_page == "model_result":
            return "Входы модели"
        elif self.current_page in self.object_pages:
            return self.object_pages[self.current_page].chart_label
        if self.current_page in ("comparison", "sensitivity", "tuning_map"):
            return {"comparison": "Сравнение", "sensitivity": "Чувствительность", "tuning_map": "Карта настроек"}[self.current_page]
        return "Сигналы регулятора" if self.absorber.absorber_form.controller_enabled.get() else "Воздействие"

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

    def _add_object_page(self, model):
        """Create the page of a registered object; a page of a replaced description is rebuilt.

        Each object page is also reachable as ``<entry_page>_page``.
        """
        key = model.entry_page
        if self.object_pages.pop(key, None) is not None:
            self.pages.pop(key).destroy()
        holder = ScrollablePage(self.page_host, BACKGROUND)
        holder.grid(row=0, column=0, sticky="nsew")
        self.pages[key] = holder
        self.page_contents[key] = holder.content
        module, name = model.page.split(":")
        page = getattr(importlib.import_module(module), name)(holder.content, self, model)
        page.grid(row=0, column=0, sticky="nsew")
        self.object_pages[key] = page
        setattr(self, f"{key}_page", page)
        relax_field_widths(holder.content)
        holder.bind_mousewheel()
        if getattr(self, "current_page", None) != key:
            holder.grid_remove()
        names = tuple(m.name for m in MODELS.all())
        self.model_box.configure(values=names, width=min(24, max(12, *map(len, names))))

    def _file_objects(self):
        """Descriptions of objects opened from files, saved inside the session."""
        return [DESCRIBED[m.id].data for m in MODELS.all() if "file" in m.capabilities]

    def _add_file_object(self, source):
        """Register an object described in a file (path or data); returns its model."""
        description = load_description(source, remember=False)
        current = DESCRIBED.get(description.id)
        if current is not None and current.data == description.data and description.id in {m.id for m in MODELS.all()}:
            return MODELS.get(description.id)
        model = description.model()
        opened = {m.id for m in MODELS.all() if "file" in m.capabilities}
        if model.id not in opened and len(opened) >= MAX_FILE_OBJECTS:
            raise ValueError(f"Можно открыть не больше {MAX_FILE_OBJECTS} объектов из файлов.")
        if model.entry_page in self.pages and model.entry_page not in self.object_pages:
            raise ValueError(f"Ключ «{model.id}» совпадает с разделом программы.")
        MODELS.add_file_model(model)
        DESCRIBED[description.id] = description
        self.model_results.pop(model.id, None)
        self._add_object_page(model)
        return model

    def _open_object_file(self):
        selected = filedialog.askopenfilename(parent=self.root, title="Открыть описание объекта",
                                              filetypes=(("Описание объекта JSON", "*.json"),))
        if not selected:
            return
        self._cancel_task(announce=False)
        try:
            model = self._add_file_object(selected)
        except ValueError as error:
            self._set_status(str(error), error=True)
            return
        self.selected_model.set(model.name)
        self._select_model()
        self._set_status(f"Открыт объект «{model.name}»")

    def _model_workspace(self):
        self._show_page(MODELS.named(self.selected_model.get()).entry_page)

    def _select_model(self, _event=None):
        self._cancel_task(announce=False)
        model = MODELS.named(self.selected_model.get())
        self._show_page(getattr(self, "_absorber_page", model.entry_page) if model.sections else model.entry_page)

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
            "model_result": "Результат модели",
            **{m.entry_page: f"{m.name}: опыт и регулирование" for m in MODELS.all() if m.page},
        }
        previous_page = self.current_page
        if previous_page in self.pages and previous_page != page:
            focus = self.root.focus_get()
            self._page_context[previous_page] = (self.pages[previous_page].canvas.yview()[0],
                focus if focus is not None and str(focus).startswith(str(self.pages[previous_page])) else None)
        self.current_page = page
        object_pages = {m.entry_page for m in MODELS.all() if not m.sections} - {"model_result"}
        if page in object_pages:
            if previous_page not in object_pages: self._absorber_context = self.topbar_context.get()
            self.topbar_context.set(next(m for m in MODELS.all() if m.entry_page == page).context)
        elif previous_page in object_pages:
            self.topbar_context.set(getattr(self,"_absorber_context","Абсорбер"))
            for axis in (self.disturbance_axis,self.response_axis):
                axis.figure.set_layout_engine("constrained")
        if page != "model_result":
            model = next(m for m in MODELS.all() if m.entry_page == page or page in m.sections)
            self.selected_model.set(model.name)
        model = MODELS.named(self.selected_model.get())
        self.sidebar_title.configure(text=model.sidebar)
        if page == "model_result":
            self.topbar_context.set(model.context)
        if page not in (*object_pages, "model_result"):
            self._absorber_page = page
        elif previous_page not in object_pages:
            self._cancel_task(announce=False)
        for widget in self.absorber.absorber_action_widgets:
            widget.grid_remove() if page == "model_result" or not model.sections else widget.grid()
        self.absorber.route_button.grid_remove() if page == "model_result" or not model.sections else self.absorber.route_button.grid()
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
            available = (page not in ("comparison", "sensitivity", "tuning_map", "identification", *object_pages, "model_result")
                         and self.absorber.last_calculation is not None and self.absorber.last_calculation["controller"] is not None)
            self.signal_mode_box.configure(state="readonly" if available else "disabled")
            self.signal_mode_box.grid_remove() if page in (*object_pages, "model_result") else self.signal_mode_box.grid()
        self.nav_labels["disturbances"] = "Параметры опыта" if model.sections else "Работа с моделью"
        for key, button in self.nav_buttons.items():
            visible = key in model.sections or key in ("disturbances", "model_result")
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
            self.absorber.comparison_workspace.grid()
            self.absorber.metric_strip.grid_remove()
            self.absorber.comparison_page.draw()
        else:
            self.absorber.comparison_workspace.grid_remove()
            self.absorber.metric_strip.grid()
        if page == "sensitivity":
            self.absorber.analysis_panel.draw_sensitivity()
        elif page == "tuning_map":
            self.absorber.analysis_panel.draw_map()
        elif page == "identification":
            self.absorber.metric_strip.grid_remove()
            self.absorber.analysis_panel.remove_map_colorbar()
            self.absorber.analysis_panel.clear_map_click_callback()
            self.absorber.identification_page.draw()
        elif page == "model_result":
            self.absorber.metric_strip.grid_remove()
            self.model_result_page.draw()
        elif page in self.object_pages:
            self.absorber.metric_strip.grid_remove()
            self.object_pages[page].draw()
        elif previous_page in ("identification", *object_pages, "model_result") and page != "comparison":
            self.absorber._draw_last_calculation()
        elif page != "comparison" and previous_page in ("comparison", "sensitivity", "tuning_map") and self._charts_show_comparison:
            self.absorber.analysis_panel.remove_map_colorbar()
            self.absorber.analysis_panel.clear_map_click_callback()
            self.absorber._draw_last_calculation()

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
            if MODELS.named(self.selected_model.get()).sections:
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
        if kind in self.object_pages:
            return [self.selected_model.get(), self.object_pages[kind].signature()]
        return self.absorber.task_signature(kind)

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
                self.absorber.identification_page.summary.set(str(error))
            if self._task_kind in self.object_pages and not isinstance(error, CalculationCancelled):
                self.object_pages[self._task_kind].summary.set(str(error))
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
            self.signal_mode_box.bind("<<ComboboxSelected>>", lambda _event: self.absorber._redraw_signal_mode())
        ttk.Button(header, text="Развернуть / вернуть", command=lambda: self._set_chart_mode(
            "both" if self.chart_mode != "both" else "input" if index == 0 else "response"),
            style="Toolbar.TButton").grid(row=2, column=0, sticky="w", pady=(4, 0))

        return axis, canvas, toolbar

    def _close_application(self):
        editor = self.absorber.scenario_library.editor
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
            path = write_session(selected, self.absorber.comparison_page.runs, self.absorber.comparison_page.counter, self._capture_laboratory())
        except (OSError, ValueError, TypeError) as error:
            self._set_status(f"Не удалось сохранить сеанс: {error}", error=True)
            return
        self._set_status(f"Учебный сеанс сохранён: {path.name}")

    def _open_comparison_session(self):
        if self.absorber.comparison_page.runs and not messagebox.askyesno(
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
        self.absorber.comparison_page.runs = runs
        self.absorber.comparison_page.counter = counter
        if laboratory is not None:
            self._restore_laboratory(laboratory, result)
            self._set_status(f"Лабораторная восстановлена: {len(runs)} закреплённых опытов")
            return
        selected_ids = tuple(run["id"] for run in runs)
        self.absorber.comparison_page.refresh_table(selected_ids)
        self.absorber.comparison_page.draw()
        self._set_status(f"Открыт учебный сеанс: {len(runs)} опытов")

    def _capture_laboratory(self, *, copy_results=True):
        return {
            **self.absorber.capture(copy_results=copy_results), "page": self.current_page,
            "selected_model": MODELS.named(self.selected_model.get()).id,
            "model_results": copy_data(self.model_results) if copy_results else self.model_results,
            **{key: page.capture(copy_results=copy_results) for key, page in self.object_pages.items()},
            "objects": copy_data(self._file_objects()),
        }

    def _restore_laboratory(self, laboratory, result):
        self._cancel_task(announce=False)
        self.absorber.restore(laboratory, result)
        for data in laboratory.get("objects", []):
            self._add_file_object(data)
        for key, page in self.object_pages.items():
            page.restore(laboratory.get(key))
        self.model_results = copy.deepcopy(laboratory.get("model_results", {}))
        for model in MODELS.all():
            page = self.absorber if model.sections else self.object_pages.get(model.entry_page)
            record = page.latest_result() if page is not None else None
            if record is not None and model.id not in self.model_results:
                self.model_results[model.id] = MODELS.result(model.id, record)
        self.selected_model.set(MODELS.get(laboratory["selected_model"]).name)
        self._show_page(laboratory["page"])
        self.absorber._mark_result_stale()
        self.absorber.calculate_button_text.set("Проверить прогноз" if self.absorber.assignment_panel.assignment_enabled.get()
                                       else "Пересчитать" if result is not None else "Рассчитать")

    def _autosave_state(self):
        return (self._capture_laboratory(copy_results=False),
                [(run["id"], run["name"]) for run in self.absorber.comparison_page.runs], self.absorber.comparison_page.counter)

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
                snapshot = (copy_data(state[0]), list(state[1]), state[2])
                write_session(self.autosave_path, self.absorber.comparison_page.runs, self.absorber.comparison_page.counter, snapshot[0])
                self._autosave_snapshot = snapshot
        except (OSError, ValueError, TypeError) as error:
            self._set_status(f"Не удалось автоматически сохранить лабораторную: {error}", error=True)

    def _autosave_tick(self):
        self._finish_autosave()
        if self._autosave_task is None:
            try:
                state = self._autosave_state()
                if state != self._autosave_snapshot:
                    snapshot = (copy_data(state[0]), list(state[1]), state[2])
                    runs = copy_data(self.absorber.comparison_page.runs)
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
            self.absorber.comparison_page.runs, self.absorber.comparison_page.counter = runs, counter
            if laboratory is not None:
                self._restore_laboratory(laboratory, result)
            else:
                self.absorber.comparison_page.refresh_table()
            if path != self.autosave_path:
                self._set_status("Лабораторная восстановлена из резервного автосохранения")
            else:
                self._set_status("Лабораторная восстановлена из автосохранения")
            return
        self._set_status(warning or "Не удалось восстановить лабораторную", error=True)

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

    def _remove_controller_axis(self):
        if self.controller_signal_axis is not None:
            self.controller_signal_axis.remove()
            self.controller_signal_axis = None

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
            for scenario in app.absorber.scenario_library.scenarios:
                app.absorber._apply_scenario_data(scenario)
                app.absorber._calculate()
                import time
                deadline = time.monotonic() + 30
                while app._task is not None:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Превышено время проверки расчёта.")
                    root.update()
                    time.sleep(0.01)
                if app.absorber.last_calculation is None:
                    raise RuntimeError(app.status_text.get())
            report["builtin_scenarios"] = len(app.absorber.scenario_library.scenarios)
            app.absorber.identification_page.load_path(Path(__file__).resolve().parent / "data" / "identification_step.csv")
            app.absorber.identification_page.metadata["input_role"].set("η, доля")
            app.absorber.identification_page.metadata["output_role"].set("Xна, доля")
            app.absorber.identification_page.metadata["conclusion"].set("Проверка экспериментального цикла")
            app.absorber.identification_page.calculate()
            deadline = time.monotonic() + 30
            while app._task is not None:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Превышено время проверки идентификации.")
                root.update()
                time.sleep(0.01)
            fitted = app.absorber.identification_page.result
            if fitted is None or abs(fitted["time_constant"] - 8) > .08 or abs(fitted["delay"] - 2) > .2:
                raise RuntimeError("Учебный CSV не восстановил эталонную динамику.")
            report["identification_verified"] = True
            page = app.absorber.identification_page
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
            app.absorber.absorber_form.chain = RICH_ABSORBENT
            app.absorber.absorber_form.controller_enabled.set(True); app.absorber.absorber_form.controller_type.set("PI")
            app.absorber.absorber_form.setpoint.set("30.5"); app.absorber.absorber_form.control_limit.set("20")
            app.absorber.absorber_form.component_enabled.set(False); app.absorber.absorber_form.flow_enabled.set(False)
            app.absorber.absorber_form.simulation_duration.set("100"); app.absorber.absorber_form.time_constant.set("8"); app.absorber.absorber_form.delay.set("0")
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
            app._show_page("heater")
            heater = app.heater_page
            heater.calculate(); wait_tank()
            heater.identify(); wait_tank()
            if heater.fit is None:
                raise RuntimeError(heater.summary.get())
            heater.tune()
            heater.calculate_pi(); wait_tank()
            if heater.result is None or heater.pi is None or heater.pi["metrics"]["status"] != "settled":
                raise RuntimeError(heater.summary.get())
            expected_heater = heater.capture()
            file_model = app._add_file_object(Path(__file__).resolve().parent / "data" / "heater_object.json")
            app._show_page(file_model.entry_page)
            described = app.object_pages[file_model.entry_page]
            described.calculate(); wait_tank()
            described.calculate_pi(); wait_tank()
            if described.result is None or described.pi is None or described.pi["metrics"]["status"] != "settled":
                raise RuntimeError(described.summary.get())
            expected_file_object = described.capture()
            from app.model_contract import read_result, write_result
            from app.model_output import write_model_report
            expected_models = copy.deepcopy(app.model_results)
            if set(expected_models) != {model.id for model in MODELS.all()}:
                raise RuntimeError("Не получены общие результаты всех моделей.")
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
            expected = app.absorber.last_calculation["final_response"].copy()
            app.absorber.assignment_panel.student_conclusion.set("Проверка восстановления релиза")
            app._save_autosave()
            if not app.autosave_path.exists():
                raise RuntimeError(app.status_text.get())
            app._close_application()
            root = None
            root = tk.Tk()
            root.withdraw()
            app = DynamicsControlApp(root, scenario_path)
            if (app.absorber.last_calculation is None
                    or not np.array_equal(expected, app.absorber.last_calculation["final_response"])
                    or app.absorber.assignment_panel.student_conclusion.get() != "Проверка восстановления релиза"):
                raise RuntimeError("Не удалось точно восстановить расчёт и пользовательские данные.")
            if app.absorber.identification_page.capture() != expected_experiment:
                raise RuntimeError("Эксперимент и проверка PI восстановлены неточно.")
            report["experiment_workflow_verified"] = True
            if app.current_page != "tank" or app.tank_page.capture() != expected_tank:
                raise RuntimeError("Не удалось точно восстановить работу с баком.")
            report["tank_workflow_verified"] = True
            if app.heater_page.capture() != expected_heater:
                raise RuntimeError("Не удалось точно восстановить работу с нагревателем.")
            report["heater_workflow_verified"] = True
            if app.object_pages[file_model.entry_page].capture() != expected_file_object:
                raise RuntimeError("Не удалось точно восстановить объект из файла.")
            report["file_object_verified"] = True
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
