"""Pinned experiments: comparison table, actions, details and charts."""

import copy
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np

from app.comparison import MAX_COMPARISON_RUNS, build_comparison_run, write_comparison_csv
from app.exporting import write_comparison_html_report, write_comparison_pdf_report
from ui.theme import BACKGROUND, COMPARISON_COLORS, MUTED, TEXT
from ui.ui_helpers import plot_samples


class ComparisonPanel:
    def __init__(self, app):
        self.app = app
        self.runs = []
        self.details = tk.StringVar()
        self.counter = 0
        self.summary = tk.StringVar(
            value="Закрепите результаты нескольких расчётов для сравнения."
        )

    def build(self, parent):
        card = self.app._card(parent, 0)
        ttk.Label(card, text="Закреплённые опыты", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )
        ttk.Label(
            card,
            textvariable=self.summary,
            style="Muted.TLabel",
            wraplength=950,
        ).grid(row=1, column=0, sticky="w", pady=(0, 10))

        table_host = ttk.Frame(card, style="CardBody.TFrame")
        table_host.grid(row=2, column=0, sticky="nsew")
        table_host.columnconfigure(0, weight=1)
        table_host.rowconfigure(0, weight=1)
        columns = (
            "name",
            "T",
            "L",
            "controller",
            "deviation",
            "settling",
            "error",
        )
        self.table = ttk.Treeview(
            table_host,
            columns=columns,
            show="headings",
            selectmode="extended",
            height=6,
        )
        headings = {
            "name": "Опыт",
            "T": "T, с",
            "L": "L, с",
            "controller": "Регулятор",
            "deviation": "Δmax, п.п.",
            "settling": "tуст, с",
            "error": "eуст, п.п.",
        }
        widths = {
            "name": 190,
            "T": 45,
            "L": 45,
            "controller": 55,
            "deviation": 70,
            "settling": 70,
            "error": 70,
        }
        for column in columns:
            self.table.heading(column, text=headings[column])
            self.table.column(
                column,
                width=max(widths[column], int(self.app.root.tk.call("font", "measure", "TkDefaultFont", headings[column])) + 16),
                minwidth=max(widths[column], int(self.app.root.tk.call("font", "measure", "TkDefaultFont", headings[column])) + 16),
                anchor="w" if column == "name" else "center",
                stretch=column == "name",
            )
        self.table.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(
            table_host,
            orient="vertical",
            command=self.table.yview,
        )
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(
            table_host,
            orient="horizontal",
            command=self.table.xview,
        )
        horizontal.grid(row=1, column=0, sticky="ew")
        self.table.configure(
            yscrollcommand=vertical.set,
            xscrollcommand=horizontal.set,
        )
        self.table.bind("<<TreeviewSelect>>", lambda _event: self.draw())
        details = ttk.Frame(card)
        details.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        details.columnconfigure(0, weight=1)
        self.details_text = tk.Text(details, height=2, width=1, wrap="word", font=("Segoe UI", 9), state="disabled")
        self.details_text.grid(row=0, column=0, sticky="ew")
        detail_scroll = ttk.Scrollbar(details, command=self.details_text.yview)
        detail_scroll.grid(row=0, column=1, sticky="ns")
        self.details_text.configure(yscrollcommand=detail_scroll.set)
        self.details.trace_add("write", self.update_details)

        actions = ttk.Frame(card, style="CardBody.TFrame")
        actions.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        actions.columnconfigure((0, 1), weight=1)
        for index, (label, command) in enumerate((
            ("Показать все", self.select_all),
            ("Удалить выбранные", self.remove_selected),
            ("Очистить", self.clear),
            ("Вернуть параметры", self.restore_selected),
            ("Переименовать", self.rename),
            ("Сохранить сеанс", self.app._save_comparison_session),
            ("Открыть сеанс", self.app._open_comparison_session),
        )):
            ttk.Button(actions, text=label, command=command).grid(row=index // 4, column=index % 4,
                sticky="ew", padx=3, pady=3)
        actions.columnconfigure((0, 1, 2, 3), weight=1)
        export_menu = tk.Menu(actions, tearoff=False)
        for label, command in (("CSV выбранных", self.export_csv),
                               ("PNG выбранных", self.app.absorber.export_panel.export_graphs_png),
                               ("HTML всех опытов", self.save_html_report),
                               ("PDF всех опытов", self.save_pdf_report)):
            export_menu.add_command(label=label, command=command)
        ttk.Menubutton(actions, text="Экспорт", menu=export_menu).grid(row=1, column=3, sticky="ew", padx=3, pady=3)

    def add_current(self):
        if self.app.absorber.last_calculation is None:
            self.app._set_status("Сначала выполните расчёт", error=True)
            return
        if len(self.runs) >= MAX_COMPARISON_RUNS:
            self.app._set_status(
                f"Можно сравнивать не больше {MAX_COMPARISON_RUNS} опытов",
                error=True,
            )
            return
        self.counter += 1
        scenario_name = self.app.absorber.last_calculation["title"]
        name = f"Опыт {self.counter}: {scenario_name}"
        run = build_comparison_run(
            self.app.absorber.last_calculation,
            name,
            self.app.absorber.last_calculation["input_state"],
        )
        run["id"] = f"run-{self.counter}"
        self.runs.append(run)
        self.refresh_table(
            tuple(item["id"] for item in self.runs)
        )
        self.app._show_page("comparison")
        self.app._set_status(f"{name} добавлен к сравнению")

    def refresh_table(self, selected_ids=()):
        self.table.delete(*self.table.get_children())
        for index, run in enumerate(self.runs):
            self.table.tag_configure(run["id"], foreground=COMPARISON_COLORS[index % 6])
            settling = (
                "—"
                if run["settling_duration"] is None
                else f"{run['settling_duration']:.1f}"
            )
            self.table.insert(
                "",
                "end",
                iid=run["id"],
                tags=(run["id"],),
                values=(
                    "● " + run["name"],
                    self.app._format_number(run["time_constant"]),
                    self.app._format_number(run["delay"]),
                    run["controller_type"],
                    self.app._format_number(run["maximum_deviation"] * 100),
                    settling,
                    self.app._format_signed_number(run["static_error"] * 100),
                ),
            )
        for run_id in selected_ids:
            if self.table.exists(run_id):
                self.table.selection_add(run_id)
                self.table.see(run_id)
        count = len(self.runs)
        self.summary.set(
            f"Закреплено опытов: {count} из {MAX_COMPARISON_RUNS}."
            if count
            else "Закрепите результаты нескольких расчётов для сравнения."
        )

    def selected_runs(self):
        selected_ids = set(self.table.selection())
        if not selected_ids:
            return list(self.runs)
        return [run for run in self.runs if run["id"] in selected_ids]

    def select_all(self):
        children = self.table.get_children()
        self.table.selection_set(children)
        self.draw()

    def remove_selected(self):
        selected_ids = set(self.table.selection())
        if not selected_ids:
            self.app._set_status("Выберите опыты для удаления", error=True)
            return
        self.runs = [
            run for run in self.runs if run["id"] not in selected_ids
        ]
        self.refresh_table()
        self.draw()
        self.app._set_status("Выбранные опыты удалены из сравнения")

    def clear(self):
        if not self.runs:
            return
        if not messagebox.askyesno(
            "Очистить сравнение",
            "Удалить все закреплённые опыты из текущего сеанса?",
            parent=self.app.root,
        ):
            return
        self.runs.clear()
        self.refresh_table()
        self.draw()
        self.app._set_status("Сравнение очищено")

    def export_csv(self):
        runs = self.selected_runs()
        if not runs:
            self.app._set_status("Нет опытов для экспорта", error=True)
            return
        selected = filedialog.asksaveasfilename(
            parent=self.app.root,
            title="Экспортировать сравнение",
            defaultextension=".csv",
            filetypes=(("CSV", "*.csv"),),
            initialfile="dynamics_control_comparison.csv",
        )
        if not selected:
            return
        try:
            path = write_comparison_csv(selected, runs)
        except (OSError, ValueError) as error:
            self.app._set_status(f"Не удалось экспортировать сравнение: {error}", error=True)
            return
        self.app._set_status(f"Сравнение сохранено: {path.name}")

    def save_html_report(self):
        self.save_report("html")

    def save_pdf_report(self):
        self.save_report("pdf")

    def save_report(self, format_name):
        if not self.runs:
            self.app._set_status("Нет закреплённых опытов для отчёта", error=True)
            return
        selected = filedialog.asksaveasfilename(
            parent=self.app.root,
            title="Сохранить отчёт по сравнению",
            defaultextension=f".{format_name}",
            filetypes=((format_name.upper(), f"*.{format_name}"),),
            initialfile=f"dynamics_control_comparison_report.{format_name}",
        )
        if not selected:
            return
        writer = write_comparison_html_report if format_name == "html" else write_comparison_pdf_report
        try:
            path = writer(selected, self.runs)
        except (OSError, ValueError) as error:
            self.app._set_status(f"Не удалось сохранить отчёт сравнения: {error}", error=True)
            return
        self.app._set_status(f"Отчёт по всем закреплённым опытам сохранён: {path.name}")

    def rename(self):
        selected = self.selected_runs()
        if len(selected) != 1:
            self.app._set_status("Выберите один опыт для переименования", error=True)
            return
        run = selected[0]
        name = self.ask_name(run["name"])
        if name is None:
            return
        name = name.strip()
        if not name:
            self.app._set_status("Название опыта не должно быть пустым", error=True)
            return
        run["name"] = name
        self.refresh_table((run["id"],))
        self.draw()
        self.app._set_status("Название опыта изменено")

    def ask_name(self, initial_name):
        dialog = tk.Toplevel(self.app.root)
        dialog.title("Переименовать опыт")
        dialog.configure(background=BACKGROUND)
        dialog.resizable(False, False)
        dialog.transient(self.app.root)
        dialog.grab_set()
        dialog.geometry("520x165")

        name = tk.StringVar(value=initial_name)
        result = {"value": None}
        content = ttk.Frame(dialog, style="App.TFrame", padding=20)
        content.pack(fill="both", expand=True)
        ttk.Label(content, text="Название опыта", style="DialogLabel.TLabel").pack(anchor="w")
        entry = ttk.Entry(content, textvariable=name, width=56)
        entry.pack(fill="x", pady=(8, 16))

        actions = ttk.Frame(content, style="App.TFrame")
        actions.pack(fill="x")
        def apply_name():
            result["value"] = name.get()
            dialog.destroy()

        ttk.Button(actions, text="Переименовать", command=apply_name, style="Primary.TButton").pack(side="right")
        ttk.Button(actions, text="Отмена", command=dialog.destroy, style="Secondary.TButton").pack(
            side="right", padx=(0, 8)
        )
        dialog.bind("<Return>", lambda _event: apply_name())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.update_idletasks()
        x = self.app.root.winfo_rootx() + (self.app.root.winfo_width() - dialog.winfo_width()) // 2
        y = self.app.root.winfo_rooty() + (self.app.root.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
        entry.focus_set()
        entry.select_range(0, "end")
        self.app.root.wait_window(dialog)
        return result["value"]

    def restore_selected(self):
        selected = self.selected_runs()
        if len(selected) != 1:
            self.app._set_status("Выберите один опыт для возврата параметров", error=True)
            return
        run = selected[0]
        input_state = run.get("input_state")
        if input_state is None:
            self.app._set_status("В этом опыте нет сохранённых параметров формы", error=True)
            return
        try:
            self.app.absorber._restore_input_state(input_state)
            self.app.absorber.identification_page.applied_origin = copy.deepcopy(run.get("experiment_origin"))
        except (KeyError, TypeError, ValueError) as error:
            self.app._set_status(f"Не удалось вернуть параметры: {error}", error=True)
            return
        self.app.absorber.active_scenario.set(f"Восстановлен: {run['name']}.")
        self.app.topbar_context.set(f"Параметры опыта «{run['name']}» возвращены в форму")
        self.app._set_status("Параметры опыта возвращены — при необходимости измените их и рассчитайте")
        self.app._show_page("disturbances")


    def color(self, run):
        index = next(index for index, stored in enumerate(self.runs) if stored["id"] == run["id"])
        return COMPARISON_COLORS[index % len(COMPARISON_COLORS)]

    def update_details(self, *_):
        self.details_text.configure(state="normal")
        self.details_text.delete("1.0", "end")
        self.details_text.insert("1.0", self.details.get())
        self.details_text.configure(state="disabled")

    def draw(self):
        if not hasattr(self, "table") or self.app.current_page != "comparison":
            return
        runs = self.selected_runs()
        def metric(value, unit, scale=100):
            return "неприменимо" if value is None else self.app._format_number(value * scale) + unit
        from app.identification import format_experiment_origin
        self.details.set("\n\n".join(
            f"{run['name']}: {run.get('settling_status', 'нет данных')}.\n"
            f"Теория {metric(run.get('steady_state'), '%')}; конец {metric(run.get('final_value'), '%')}.\n"
            f"Ошибка: теория {metric(run['static_error'], ' п.п.')}; конец {metric(run.get('final_error'), ' п.п.')}.\n"
            f"IAE {metric(run.get('iae'), ' п.п.·с')}; насыщение η {metric(run.get('saturation_duration'), ' с', 1)}.\n"
            + format_experiment_origin(run.get("experiment_origin"))
            for run in runs))
        self.app._remove_controller_axis()
        self.app.primary_chart_title.set("Сравнение переходных процессов")
        self.app.primary_chart_subtitle.set("Закреплённые расчёты на одном графике")
        self.app._style_axis(self.app.disturbance_axis, "Время, с", "X, %")
        for index, run in enumerate(runs):
            self.app.disturbance_axis.plot(
                *plot_samples(run["time"], run["response"] * 100),
                color=self.color(run),
                linewidth=2.2,
                label=run["name"].split(":", 1)[0],
            )
        if runs:
            self.app._place_legend_above(self.app.disturbance_axis)
            self.app.disturbance_axis.margins(x=0.02, y=0.12)
        else:
            self.app.disturbance_axis.text(
                0.5,
                0.5,
                "Нет закреплённых опытов",
                transform=self.app.disturbance_axis.transAxes,
                ha="center",
                va="center",
                color=MUTED,
            )
        self.app._enable_legend_toggles(self.app.disturbance_axis, self.app.disturbance_canvas)
        self.app.disturbance_canvas.draw_idle()

        self.app.response_chart_title.set("Длительность установления")
        self.app.response_subtitle.set("Время после начала реакции объекта")
        self.app._style_axis(self.app.response_axis, "Опыт", "Время, с")
        values = [
            0.0 if run["settling_duration"] is None else run["settling_duration"]
            for run in runs
        ]
        positions = np.arange(len(runs))
        bars = self.app.response_axis.bar(
            positions,
            values,
            color=[self.color(run) for run in runs],
            alpha=0.85,
        )
        self.app.response_axis.set_xticks(positions)
        self.app.response_axis.set_xticklabels(
            [run["name"].split(":", 1)[0] for run in runs],
            rotation=20,
            ha="right",
        )
        for bar, run in zip(bars, runs, strict=True):
            label = "не достигнуто" if run["settling_duration"] is None else f"{run['settling_duration']:.1f} с"
            self.app.response_axis.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height(),
                label,
                ha="center",
                va="bottom",
                fontsize=8,
                color=TEXT,
            )
        self.app.response_axis.margins(y=0.18)
        self.app.response_canvas.draw_idle()
        self.app._charts_show_comparison = True
