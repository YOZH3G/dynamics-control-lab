"""Teaching assignment: lesson text, learning route, prediction and its assessment."""

import tkinter as tk
from tkinter import ttk

from app.laboratory import CORRECTION_OPTIONS, DIRECTION_OPTIONS, FASTEST_OPTIONS, evaluate_prediction
from app.validation import parse_percentage


class AssignmentPanel:
    def __init__(self, app):
        self.app = app
        self.assignment_tolerance_percent = 5.0
        self.assignment_absolute_tolerance = 1e-6
        self.current_lesson = app.scenario_library.scenarios[0]["lesson"]
        self.assignment_attempts = 0
        self.assignment_evaluation = None
        self.learning_mode = tk.BooleanVar(value=False)
        self.learning_step = 1
        self.lesson_summary = tk.StringVar()
        self.learning_route = tk.StringVar()
        self.student_name = tk.StringVar()
        self.student_conclusion = tk.StringVar()
        self.assignment_enabled = tk.BooleanVar(value=False)
        self.predicted_direction = tk.StringVar()
        self.predicted_steady = tk.StringVar()
        self.predicted_fastest = tk.StringVar()
        self.predicted_correction = tk.StringVar()
        self.assignment_feedback = tk.StringVar(
            value="Включите режим задания и заполните прогноз до расчёта."
        )

    def build(self, parent):
        lesson_card = self.app._card(parent, 1, padding=(14, 12))
        ttk.Label(lesson_card, text="Учебное задание", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        ttk.Label(
            lesson_card, textvariable=self.lesson_summary, style="Body.TLabel",
            justify="left", wraplength=330,
        ).grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(
            lesson_card, text="Пошаговый учебный режим", variable=self.learning_mode,
            command=self.update_route,
        ).grid(row=2, column=0, sticky="w", pady=(10, 4))
        ttk.Label(lesson_card, textvariable=self.learning_route, style="Muted.TLabel", wraplength=330).grid(
            row=3, column=0, sticky="w"
        )
        ttk.Button(
            lesson_card, text="Следующий шаг", command=self.advance_step,
            style="Secondary.TButton",
        ).grid(row=4, column=0, sticky="ew", pady=(8, 0))

        assignment = self.app._card(parent, 2, padding=(14, 12))
        ttk.Checkbutton(
            assignment,
            text="Режим задания: сначала сделать прогноз",
            variable=self.assignment_enabled,
            command=self.update_states,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        prediction_fields = (
            ("Направление изменения выхода", self.predicted_direction, DIRECTION_OPTIONS, "direction_prediction"),
            ("Какая реакция завершится быстрее", self.predicted_fastest, FASTEST_OPTIONS, "fastest_prediction"),
            ("Устранит ли регулятор отклонение", self.predicted_correction, CORRECTION_OPTIONS, "correction_prediction"),
        )
        self.prediction_widgets = []
        row = 1
        for label, variable, values, attribute in prediction_fields:
            ttk.Label(assignment, text=label, style="Body.TLabel", wraplength=160).grid(
                row=row, column=0, sticky="w", pady=4
            )
            widget = ttk.Combobox(
                assignment,
                textvariable=variable,
                values=values,
                state="disabled",
                width=20,
            )
            widget.grid(row=row, column=1, sticky="e", padx=(8, 0), pady=4)
            setattr(self, attribute, widget)
            self.prediction_widgets.append(widget)
            row += 1
        ttk.Label(assignment, text="Ожидаемое установившееся значение, %", style="Body.TLabel", wraplength=170).grid(
            row=row, column=0, sticky="w", pady=4
        )
        self.steady_prediction = ttk.Entry(
            assignment,
            textvariable=self.predicted_steady,
            state="disabled",
            width=22,
        )
        self.steady_prediction.grid(row=row, column=1, sticky="e", padx=(8, 0), pady=4)
        self.prediction_widgets.append(self.steady_prediction)
        row += 1
        ttk.Label(assignment, text="Студент", style="Body.TLabel").grid(
            row=row, column=0, sticky="w", pady=4
        )
        self.student_name_entry = ttk.Entry(assignment, textvariable=self.student_name, state="disabled", width=22)
        self.student_name_entry.grid(row=row, column=1, sticky="e", padx=(8, 0), pady=4)
        self.prediction_widgets.append(self.student_name_entry)
        row += 1
        ttk.Label(assignment, text="Краткий вывод", style="Body.TLabel").grid(
            row=row, column=0, sticky="w", pady=4
        )
        self.student_conclusion_entry = ttk.Entry(assignment, textvariable=self.student_conclusion, state="disabled", width=22)
        self.student_conclusion_entry.grid(row=row, column=1, sticky="e", padx=(8, 0), pady=4)
        self.prediction_widgets.append(self.student_conclusion_entry)
        assignment.columnconfigure(0, weight=1)

        feedback = self.app._card(parent, 3, padding=(14, 12))
        ttk.Label(feedback, text="Проверка прогноза", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        ttk.Label(
            feedback,
            textvariable=self.assignment_feedback,
            style="Body.TLabel",
            justify="left",
            wraplength=330,
        ).grid(row=1, column=0, sticky="w")


    def update_states(self):
        enabled = self.assignment_enabled.get()
        for widget in self.prediction_widgets:
            widget.configure(state="normal" if enabled else "disabled")
        for widget in (
            self.direction_prediction,
            self.fastest_prediction,
            self.correction_prediction,
        ):
            widget.configure(state="readonly" if enabled else "disabled")
        self.assignment_feedback.set(
            "Заполните прогноз и нажмите «Рассчитать»."
            if enabled
            else "Включите режим задания и заполните прогноз до расчёта."
        )
        self.app.calculate_button_text.set("Проверить прогноз" if enabled else "Рассчитать")

    def update_summary(self):
        lesson = self.current_lesson
        questions = lesson["questions"]
        question_text = "" if not questions else "\nВопросы:\n" + "\n".join(f"• {item}" for item in questions)
        self.lesson_summary.set(
            f"{lesson['task']}\n\n{lesson['guidance']}"
            f"{question_text}\n\nПопыток для прогноза: {lesson['attempt_limit']}."
        )

    def update_route(self):
        steps = ("Задание", "Параметры опыта", "Регулятор", "Результаты", "Сравнение", "Отчёт")
        if not self.learning_mode.get():
            self.learning_route.set("Свободный доступ ко всем разделам. Включите режим для маршрута.")
            if hasattr(self.app, "route_stage"):
                self.app.route_stage.set("Учебный маршрут")
                self.app.route_button.configure(state="normal")
            return
        index = self.learning_step - 1
        self.learning_route.set(" → ".join(f"[{i + 1}] {text}" if i == index else text
                                          for i, text in enumerate(steps)))
        if hasattr(self.app, "route_stage"):
            next_step = " → " + steps[index + 1] if index < 5 else ""
            self.app.route_stage.set(f"{self.learning_step}/6: {steps[index]}{next_step}")
            self.app.route_button.configure(state="normal" if index < 5 else "disabled")

    def advance_step(self):
        if not self.learning_mode.get():
            self.learning_mode.set(True)
            self.learning_step = 1
        else:
            self.learning_step = min(6, self.learning_step + 1)
        self.update_route()
        self.app._show_page(("scenarios", "disturbances", "controller", "results", "comparison", "export")[self.learning_step - 1])

    def read_prediction(self):
        if not self.assignment_enabled.get():
            return None
        if self.assignment_attempts >= self.current_lesson["attempt_limit"]:
            self.assignment_feedback.set("Лимит попыток для этого задания исчерпан.")
            self.app._show_page("scenarios")
            raise ValueError("Лимит попыток исчерпан.")
        self.steady_prediction.configure(style="TEntry")
        if not all((
            self.predicted_direction.get(),
            self.predicted_fastest.get(),
            self.predicted_correction.get(),
            self.predicted_steady.get().strip(),
        )):
            self.assignment_feedback.set("Заполните все четыре поля прогноза до расчёта.")
            self.app._show_page("scenarios")
            raise ValueError("Прогноз заполнен не полностью.")
        try:
            steady = parse_percentage(self.predicted_steady.get())
        except ValueError as error:
            self.steady_prediction.configure(style="Error.TEntry")
            self.assignment_feedback.set(f"Установившееся значение: {error}")
            self.app._show_page("scenarios")
            raise
        return {
            "direction": self.predicted_direction.get(),
            "steady": steady,
            "fastest": self.predicted_fastest.get(),
            "correction": self.predicted_correction.get(),
        }

    def evaluate(self, prediction, outcome):
        if prediction is None:
            return
        evaluation = evaluate_prediction(
            prediction,
            outcome,
            self.assignment_tolerance_percent,
            lesson=self.current_lesson,
            controller=self.app.last_calculation["controller"] if self.app.last_calculation is not None else None,
            steady_absolute_tolerance=self.assignment_absolute_tolerance,
        )
        self.assignment_attempts += 1
        self.assignment_evaluation = evaluation
        remaining = self.current_lesson["attempt_limit"] - self.assignment_attempts
        self.assignment_feedback.set(
            f"Результат: {evaluation['score']} из {evaluation['total']}.\n"
            + "\n".join(evaluation["lines"])
            + f"\nОсталось попыток: {remaining}."
        )
        if self.learning_mode.get():
            self.learning_step = max(self.learning_step, 4)
            self.update_route()
