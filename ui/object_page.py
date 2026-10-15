"""Common base of object pages: a form built from the model registry, revisions and background tasks.

A page shows one registered object. Fields that define the experiment raise the
revision, so earlier results are marked as snapshots of previous settings; other
fields only redraw. Calculations run as background tasks of the page's kind.
"""

import tkinter as tk
from tkinter import ttk

from app.identification import bootstrap_intervals
from app.physical_models import MODELS

WRAP = 320


class ObjectPage(ttk.Frame):
    chart_label = ""
    # Genitive name in status messages, e.g. "Интервалы K, T и L бака рассчитаны".
    object_name = ""
    fit_missing = "Сначала оцените актуальную локальную модель."

    def __init__(self, parent, app, model_id, field_defaults, views, summary):
        super().__init__(parent, style="CardBody.TFrame", padding=12)
        self.app = app
        self.model = MODELS.get(model_id)
        self.kind = self.model.entry_page
        self.fields = {k: tk.StringVar(value=v) for k, v in field_defaults.items()}
        self.view = tk.StringVar(value=views[0])
        self.summary = tk.StringVar(value=summary)
        self.fit = None
        self.revision = 0
        self.restoring = False
        self.row = 0
        self.columnconfigure(1, weight=1)

    # Layout: each helper places its widgets on the next free rows.

    def heading(self, title, hint):
        ttk.Label(self, text=title, style="CardTitle.TLabel").grid(row=self.row, column=0, columnspan=2, sticky="w")
        ttk.Label(self, text=hint, style="Hint.TLabel", wraplength=WRAP).grid(
            row=self.row + 1, column=0, columnspan=2, sticky="ew", pady=8)
        self.row += 2

    def hint(self, text, pady=8):
        ttk.Label(self, text=text, style="Hint.TLabel", wraplength=WRAP).grid(
            row=self.row, column=0, columnspan=2, sticky="ew", pady=pady)
        self.row += 1

    def add_fields(self, spec, *, width=14, pady=2):
        """Label and input per item: a parameter key, (key, label) or (key, label, choices) for a list."""
        parameters = {p.key: p for p in self.model.parameters}
        widgets = {}
        for item in spec:
            key, label, choices = (item, parameters[item].field_label, None) if isinstance(item, str) else (*item, None)[:3]
            ttk.Label(self, text=label, style="Body.TLabel").grid(row=self.row, column=0, sticky="w", pady=pady)
            if choices:
                widget = ttk.Combobox(self, textvariable=self.fields[key], values=choices, state="readonly", width=15)
            else:
                widget = ttk.Entry(self, textvariable=self.fields[key], **({} if width is None else dict(width=width)))
            widget.grid(row=self.row, column=1, sticky="ew")
            widgets[key] = widget
            self.row += 1
        return widgets

    def add_button(self, text, command, pady=3):
        button = ttk.Button(self, text=text, command=command)
        button.grid(row=self.row, column=0, columnspan=2, sticky="ew", pady=pady)
        self.row += 1
        return button

    def add_view_list(self, views):
        ttk.Combobox(self, textvariable=self.view, values=views, state="readonly").grid(
            row=self.row, column=0, columnspan=2, sticky="ew")
        self.row += 1

    def add_summary(self):
        ttk.Label(self, textvariable=self.summary, style="Body.TLabel", wraplength=WRAP).grid(
            row=self.row, column=0, columnspan=2, sticky="ew", pady=8)
        self.row += 1

    def trace(self, keys, callback):
        for key in keys:
            self.fields[key].trace_add("write", callback)

    # Revisions and tasks.

    def changed(self, *_):
        """An experiment field changed: earlier results become snapshots of previous settings."""
        if self.restoring:
            return
        self.revision += 1
        self.cancel_own_task()
        self.draw()

    def settings_changed(self, *_):
        if self.restoring:
            return
        self.cancel_own_task()
        self.draw()

    def cancel_own_task(self):
        if getattr(self.app, "_task", None) is not None and self.app._task_kind == self.kind:
            self.app._cancel_task()

    def perform(self, name, calculate, accept):
        self.app._start_task(name, calculate, accept, self.kind)

    def fail(self, error):
        self.summary.set(str(error))
        self.app._set_status(str(error), error=True)

    def current(self, record):
        return record is not None and record["revision"] == self.revision

    def signature(self):
        return (self.revision, tuple(v.get() for v in self.fields.values()))

    def fit_current(self):
        """The fit belongs to the current experiment and, for the response loop, to the current noise and seed."""
        fit = self.fit
        if not self.current(fit):
            return False
        if "noise" not in fit:
            return True
        try:
            return (float(self.fields["noise"].get().replace(",", ".")) == fit["noise"]
                    and int(self.fields["seed"].get()) == fit["seed"])
        except ValueError:
            return False

    # Identification shared by object pages.

    def fit_text(self, fit):
        raise NotImplementedError

    def estimate_uncertainty(self):
        if not self.fit_current():
            self.fail(self.fit_missing); return
        fit = self.fit

        def accept(record):
            if self.fit is not fit or not self.fit_current():
                return
            fit["uncertainty"] = record
            self.draw()
            self.summary.set(self.fit_text(fit))
            self.app._set_status(f"Интервалы K, T и L {self.object_name} рассчитаны")
        self.perform(f"Неопределённость K, T, L {self.object_name}",
                     lambda cancel, progress: bootstrap_intervals(fit, cancel=cancel, progress=progress), accept)

    # Response loop: free response → K, T, L with sensor noise → PI settings. A page enables it by
    # setting identify_response(run, noise, seed, *, cancel, progress) and pi_settings(fit), and
    # provides the fields noise, seed, gain and integral_time and the attribute result. Noise and seed
    # only affect the fit: changing them should call settings_changed, not changed.
    identify_response = None
    pi_settings = None

    def identify(self):
        if not self.current(self.result):
            self.fail("Сначала рассчитайте свободный отклик при текущих параметрах."); return
        try:
            noise, seed = float(self.fields["noise"].get().replace(",", ".")), int(self.fields["seed"].get())
        except ValueError:
            self.fail("Нужны шум σ ≥ 0 и целое число генератора 0…4294967295."); return
        run, revision, identify = self.result, self.revision, self.identify_response

        def accept(fit):
            self.fit = fit
            self.view.set("Идентификация")
            self.draw()
            self.app._set_status(f"Идентификация {self.object_name} выполнена")
        self.perform(f"Идентификация {self.object_name}", lambda cancel, progress: dict(
            identify(run, noise, seed, cancel=cancel, progress=progress), revision=revision), accept)

    def tune(self):
        if not self.fit_current():
            self.fail(self.fit_missing); return
        settings = self.pi_settings(self.fit)
        self.fields["gain"].set(format(settings["gain"], ".4g"))
        self.fields["integral_time"].set(format(settings["integral_time"], ".4g"))
        self.app._set_status("Kp и Ti рассчитаны по модели; проверьте их кнопкой «Рассчитать с PI»")
