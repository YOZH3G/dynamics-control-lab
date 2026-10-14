"""Scenario library: built-in and teacher scenarios, variants and the editor."""

import tkinter as tk
from tkinter import ttk

from app.laboratory import VARIANT_COUNT, builtin_variant
from ui.scenario_editor import ScenarioEditorDialog
from ui.theme import BACKGROUND


class ScenarioLibrary:
    def __init__(self, app, store):
        self.app = app
        self.editor = None
        self.store = store
        self.scenarios = self.store.scenarios
        self.by_name = {scenario["name"]: scenario for scenario in self.scenarios}
        self.selected = tk.StringVar(value=self.scenarios[0]["name"])
        self.variant = tk.StringVar(value="1")
        self.description = tk.StringVar(value=self.scenarios[0]["description"])
        self.teacher_mode = tk.BooleanVar(value=False)
        self.storage_status = tk.StringVar(
            value=self.storage_text()
        )

    def build(self, parent):
        scenario_card = self.app._card(parent, 0)
        ttk.Label(scenario_card, text="Готовый сценарий", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 10)
        )
        self.box = ttk.Combobox(
            scenario_card,
            textvariable=self.selected,
            values=tuple(scenario["name"] for scenario in self.scenarios),
            state="readonly",
        )
        self.box.grid(row=1, column=0, sticky="ew")
        self.box.bind("<<ComboboxSelected>>", self.update_description)
        ttk.Label(scenario_card, text="Вариант задания", style="Body.TLabel").grid(
            row=2, column=0, sticky="w", pady=(10, 4)
        )
        self.variant_box = ttk.Combobox(
            scenario_card,
            textvariable=self.variant,
            values=tuple(str(number) for number in range(1, VARIANT_COUNT + 1)),
            state="readonly",
            width=8,
        )
        self.variant_box.grid(row=3, column=0, sticky="w")
        self.variant_box.bind("<<ComboboxSelected>>", self.update_description)
        ttk.Label(
            scenario_card,
            textvariable=self.description,
            style="Muted.TLabel",
            wraplength=330,
        ).grid(row=4, column=0, sticky="w", pady=(8, 12))
        ttk.Button(
            scenario_card,
            text="Применить сценарий",
            command=self.apply,
            style="Primary.TButton",
        ).grid(row=5, column=0, sticky="ew")
        ttk.Label(
            scenario_card,
            textvariable=self.app.active_scenario,
            style="Muted.TLabel",
            wraplength=330,
        ).grid(row=6, column=0, sticky="w", pady=(8, 0))

        ttk.Separator(scenario_card).grid(row=7, column=0, sticky="ew", pady=14)
        ttk.Checkbutton(
            scenario_card,
            text="Режим преподавателя",
            variable=self.teacher_mode,
            command=self.toggle_teacher_mode,
        ).grid(row=8, column=0, sticky="w")
        self.editor_button = ttk.Button(
            scenario_card,
            text="Открыть редактор сценариев",
            command=self.open_editor,
            style="Secondary.TButton",
        )
        self.editor_button.grid(row=9, column=0, sticky="ew", pady=(10, 0))
        self.storage_label = ttk.Label(
            scenario_card,
            textvariable=self.storage_status,
            style="Muted.TLabel",
            wraplength=330,
        )
        self.storage_label.grid(row=10, column=0, sticky="w", pady=(8, 0))
        self.restore_button = ttk.Button(
            scenario_card,
            text="Восстановить резервную копию",
            command=self.restore_backup,
            style="Secondary.TButton",
        )
        self.restore_button.grid(row=11, column=0, sticky="ew", pady=(8, 0))
        self.editor_button.grid_remove()
        self.storage_label.grid_remove()
        self.restore_button.grid_remove()

    def update_description(self, _event=None):
        scenario = self.by_name[self.selected.get()]
        if self.store.is_builtin(scenario["name"]):
            self.variant_box.configure(state="readonly")
            number = int(self.variant.get())
            self.description.set(
                f"{builtin_variant(scenario, number)['description']} Вариант {number} из {VARIANT_COUNT}."
            )
        else:
            self.variant.set("1")
            self.variant_box.configure(state="disabled")
            self.description.set(scenario["description"])

    def apply(self):
        scenario = self.by_name[self.selected.get()]
        number = int(self.variant.get())
        if self.store.is_builtin(scenario["name"]):
            self.app._apply_scenario_data(builtin_variant(scenario, number), number)
        else:
            self.app._apply_scenario_data(scenario)

    def toggle_teacher_mode(self):
        if self.teacher_mode.get():
            self.editor_button.grid()
            self.storage_label.grid()
            if self.store.recovery_available:
                self.restore_button.grid()
            self.app._set_status("Режим преподавателя включён")
        else:
            if self.editor is not None and self.editor.winfo_exists():
                if not self.editor.request_close():
                    self.teacher_mode.set(True)
                    return
            self.editor_button.grid_remove()
            self.storage_label.grid_remove()
            self.restore_button.grid_remove()
            self.app._set_status("Режим преподавателя выключен")

    def open_editor(self):
        if self.editor is not None and self.editor.winfo_exists():
            self.editor.lift()
            self.editor.focus_force()
            return
        self.editor = ScenarioEditorDialog(
            self.app.root,
            self.store,
            self.refresh,
            self.preview,
            lambda: setattr(self, "editor", None),
            BACKGROUND,
        )

    def refresh(self, selected_name=None, status=None):
        self.scenarios = self.store.scenarios
        self.by_name = {scenario["name"]: scenario for scenario in self.scenarios}
        names = tuple(self.by_name)
        self.box.configure(values=names)
        if selected_name not in self.by_name:
            selected_name = names[0]
        self.selected.set(selected_name)
        self.update_description()
        self.storage_status.set(
            self.storage_text()
        )
        if not self.store.recovery_available:
            self.restore_button.grid_remove()
        if status:
            self.app._set_status(status)

    def restore_backup(self):
        try:
            count = self.store.restore_backup()
        except (OSError, ValueError) as error:
            self.app._set_status(f"Не удалось восстановить сценарии: {error}", error=True)
            return
        self.refresh(status=f"Восстановлено пользовательских сценариев: {count}")

    def storage_text(self):
        prefix = f"{len(self.store.user_scenarios)} пользовательских сценариев."
        path_text = f"Файл: {self.store.path}"
        if self.store.warning:
            return f"{self.store.warning}\n{path_text}"
        return f"{prefix}\n{path_text}"

    def preview(self, scenario):
        if scenario["name"] in self.by_name:
            self.selected.set(scenario["name"])
            self.update_description()
        else:
            self.description.set(scenario["description"])
        self.app.assignment_enabled.set(False)
        self.app._update_assignment_states()
        self.app._apply_scenario_data(scenario)
        self.app._calculate()
