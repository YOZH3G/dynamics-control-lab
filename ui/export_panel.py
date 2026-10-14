"""Report page: chart images, CSV points, calculation protocol and HTML/PDF reports."""

import tkinter as tk
from tkinter import filedialog, ttk

from app.exporting import build_protocol, save_graphs, write_csv, write_html_report, write_pdf_report, write_protocol


class ExportPanel:
    def __init__(self, app):
        self.app = app
        self.export_summary = tk.StringVar(value="Сначала выполните расчёт.")
        self.export_buttons = []

    def build(self, parent):
        card = self.app._card(parent, 0)
        ttk.Label(card, text="Материалы для отчёта", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        ttk.Label(
            card,
            textvariable=self.export_summary,
            style="Muted.TLabel",
            wraplength=330,
        ).grid(row=1, column=0, sticky="w", pady=(0, 14))
        actions = (
            ("Сохранить два графика PNG", self.export_graphs_png),
            ("Экспортировать точки CSV", self.export_csv),
            ("Сохранить отчёт HTML", self.save_html_report),
            ("Сохранить отчёт PDF", self.save_pdf_report),
            ("Копировать параметры и результаты", self.copy_protocol),
            ("Сохранить протокол TXT", self.save_protocol),
        )
        for row, (label, command) in enumerate(actions, start=2):
            button = ttk.Button(
                card,
                text=label,
                command=command,
                style="Primary.TButton" if row == 2 else "Secondary.TButton",
                state="disabled",
            )
            button.grid(row=row, column=0, sticky="ew", pady=(0, 8))
            self.export_buttons.append(button)

        note = self.app._card(parent, 1)
        ttk.Label(note, text="Состав файлов", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        ttk.Label(
            note,
            text=(
                "PNG сохраняет оба текущих графика. CSV содержит временную сетку, воздействие, "
                "целевые значения и рассчитанные отклики. Протокол TXT включает параметры и показатели."
            ),
            style="Body.TLabel",
            wraplength=330,
            justify="left",
        ).grid(row=1, column=0, sticky="w")

    def export_graphs_png(self):
        self.app.time_cursor.clear()
        selected = filedialog.asksaveasfilename(
            parent=self.app.root,
            title="Сохранить графики",
            defaultextension=".png",
            filetypes=(("PNG", "*.png"),),
            initialfile="dynamics_control_result.png",
        )
        if not selected:
            return
        try:
            signal_path, response_path = save_graphs(
                self.app.disturbance_axis.figure,
                self.app.response_axis.figure,
                selected,
            )
        except OSError as error:
            self.app._set_status(f"Не удалось сохранить PNG: {error}", error=True)
            return
        self.export_summary.set(f"Сохранены: {signal_path.name}, {response_path.name}.")
        self.app._set_status("Графики сохранены в PNG")

    def export_csv(self):
        selected = filedialog.asksaveasfilename(
            parent=self.app.root,
            title="Экспортировать точки",
            defaultextension=".csv",
            filetypes=(("CSV", "*.csv"),),
            initialfile="dynamics_control_result.csv",
        )
        if not selected:
            return
        try:
            path = write_csv(selected, self.app.last_calculation)
        except OSError as error:
            self.app._set_status(f"Не удалось сохранить CSV: {error}", error=True)
            return
        self.export_summary.set(f"Точки сохранены: {path.name}.")
        self.app._set_status("Точки экспортированы в CSV")

    def copy_protocol(self):
        protocol = self.build_protocol_text()
        self.app.root.clipboard_clear()
        self.app.root.clipboard_append(protocol)
        self.app.root.update_idletasks()
        self.export_summary.set("Параметры и результаты скопированы в буфер обмена.")
        self.app._set_status("Протокол скопирован")

    def save_protocol(self):
        selected = filedialog.asksaveasfilename(
            parent=self.app.root,
            title="Сохранить протокол",
            defaultextension=".txt",
            filetypes=(("Текстовый файл", "*.txt"),),
            initialfile="dynamics_control_protocol.txt",
        )
        if not selected:
            return
        try:
            path = write_protocol(selected, self.build_protocol_text())
        except OSError as error:
            self.app._set_status(f"Не удалось сохранить протокол: {error}", error=True)
            return
        self.export_summary.set(f"Протокол сохранён: {path.name}.")
        self.app._set_status("Протокол сохранён")

    def save_html_report(self):
        self.save_lab_report("html", write_html_report)

    def save_pdf_report(self):
        self.save_lab_report("pdf", write_pdf_report)

    def save_lab_report(self, format_name, writer):
        self.app.time_cursor.clear()
        selected = filedialog.asksaveasfilename(
            parent=self.app.root,
            title="Сохранить отчёт лабораторной работы",
            defaultextension=f".{format_name}",
            filetypes=((format_name.upper(), f"*.{format_name}"),),
            initialfile=f"dynamics_control_lab_report.{format_name}",
        )
        if not selected:
            return
        title = self.app.last_calculation["title"]
        try:
            self.app._draw_calculation_result(self.app.last_calculation)
            self.app.disturbance_canvas.draw()
            self.app.response_canvas.draw()
            self.app._mark_result_stale()
            path = writer(
                selected, self.app.last_calculation, title, self.app.last_calculation["lesson"],
                self.app.last_calculation["evaluation"], self.app.assignment_panel.student_name.get(),
                self.app.assignment_panel.student_conclusion.get(),
                (self.app.disturbance_axis.figure, self.app.response_axis.figure),
                prediction=self.app.last_calculation["prediction"],
                comparison_runs=self.app.comparison_page.runs,
            )
        except OSError as error:
            self.app._set_status(
                f"Не удалось сохранить {format_name.upper()}-отчёт: {error}",
                error=True,
            )
            return
        self.export_summary.set(f"{format_name.upper()}-отчёт сохранён: {path.name}.")
        self.app._set_status(f"{format_name.upper()}-отчёт сохранён")

    def build_protocol_text(self):
        title = self.app.last_calculation["title"]
        return build_protocol(self.app.last_calculation, title)
