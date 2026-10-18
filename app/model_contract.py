"""Trusted built-in models and portable results, independent of Tkinter."""

import copy
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .background_tasks import check_cancelled

_SCALARS = (int, float, str, bool, type(None))


def copy_data(value):
    """Deep copy of JSON-like data; lists of scalars are copied in C."""
    kind = type(value)
    if kind is dict:
        return {key: copy_data(item) for key, item in value.items()}
    if kind is list:
        if set(map(type, value)) <= set(_SCALARS):
            return value[:]
        return [copy_data(item) for item in value]
    if kind in _SCALARS:
        return value
    return copy.deepcopy(value)


@dataclass(frozen=True)
class Parameter:
    key: str
    label: str
    unit: str
    default: float
    minimum: float | None = None
    maximum: float | None = None
    strict_minimum: bool = False
    # Label of the input field when it differs from "label, unit" (usually adds the symbol).
    field: str = ""

    @property
    def field_label(self):
        return self.field or f"{self.label}, {self.unit}"


@dataclass(frozen=True)
class Signal:
    key: str
    label: str
    unit: str
    role: str
    display_unit: str | None = None
    scale: float = 1.
    required: bool = False
    minimum: float | None = None
    maximum: float | str | None = None


@dataclass(frozen=True)
class PhysicalModel:
    id: str
    version: int
    name: str
    parameters: tuple
    signals: tuple
    channels: tuple
    capabilities: tuple
    events: tuple
    calculate: object
    project: object
    entry_page: str
    sidebar: str
    context: str
    validate_result: object = None
    # Object page in the interface, "module:Class"; empty means only the common result page.
    page: str = ""
    # Checks the page snapshot stored in a session under ``entry_page``; required with ``page``.
    validate_state: object = None
    # Sidebar sections of a workspace with several pages, the entry page among them; empty for one object page.
    sections: tuple = ()


class ModelRegistry:
    def __init__(self, models=()):
        self._models = {}
        for model in models:
            self.register(model)

    def register(self, model):
        if (not isinstance(model, PhysicalModel) or not model.id or model.id in self._models
                or type(model.version) is not int or model.version < 1
                or any(m.name == model.name for m in self.all())
                or not callable(model.calculate) or not callable(model.project)):
            raise ValueError("Неверное или повторное описание физической модели.")
        for items in (model.parameters, model.signals):
            if len({item.key for item in items}) != len(items):
                raise ValueError("Имена параметров и сигналов модели должны быть уникальными.")
        keys = {signal.key for signal in model.signals}
        if any(a not in keys or b not in keys for a, b in model.channels):
            raise ValueError("Канал управления должен ссылаться на сигналы модели.")
        if model.page and (model.page.count(":") != 1 or not callable(model.validate_state)
                           or any(m.entry_page == model.entry_page for m in self.all())):
            raise ValueError("Страница объекта требует проверки снимка и собственного раздела.")
        self._models[model.id] = model

    def add_file_model(self, model):
        """Add an object described in a file, or replace an earlier description with the same key."""
        old = self._models.get(model.id)
        if old is not None and "file" not in old.capabilities:
            raise ValueError(f"Ключ «{model.id}» занят встроенной моделью.")
        if "file" not in model.capabilities:
            raise ValueError("Заменять можно только объекты, описанные в файле.")
        if old is not None:
            del self._models[model.id]
        try:
            self.register(model)
        except ValueError:
            if old is not None:
                self._models[old.id] = old
            raise
        return old

    def extended(self, models):
        """Copy of the registry with the given file-described models added or replaced."""
        registry = ModelRegistry(self.all())
        for model in models:
            registry.add_file_model(model)
        return registry

    def get(self, model_id):
        try:
            return self._models[model_id]
        except (KeyError, TypeError) as error:
            raise ValueError("Неизвестная физическая модель.") from error

    def all(self):
        return tuple(self._models.values())

    def named(self, name):
        for model in self.all():
            if model.name == name:
                return model
        raise ValueError("Неизвестное название физической модели.")

    def run(self, model_id, request, *, cancel=None, progress=None):
        model = self.get(model_id)
        check_cancelled(cancel)
        raw = model.calculate(copy.deepcopy(request), cancel=cancel, progress=progress)
        # Verify the common representation while keeping legacy solver types intact.
        self.result(model_id, raw)
        check_cancelled(cancel)
        return raw

    def result(self, model_id, raw):
        model = self.get(model_id)
        result = model.project(raw)
        result.update(format_version=1, model_id=model.id, model_version=model.version)
        return self.validate(result)

    def validate(self, result):
        try:
            return self._validate(result)
        except (OverflowError, TypeError) as error:
            raise ValueError("Результат: неверные числовые данные.") from error

    def _validate(self, result):
        if not isinstance(result, dict) or type(result.get("format_version")) is not int or result["format_version"] != 1:
            raise ValueError("Неверный формат результата модели.")
        model = self.get(result.get("model_id"))
        if type(result.get("model_version")) is not int or result["model_version"] != model.version:
            raise ValueError("Несовместимая версия расчёта модели.")
        def vector(values):
            if (not isinstance(values, list) or not set(map(type, values)) <= {int, float}
                    or not 2 <= len(values) <= 200000):
                raise ValueError("Результат: нужны конечные числовые сигналы.")
            a = np.asarray(values, dtype=float)
            if a.ndim != 1 or not np.isfinite(a).all():
                raise ValueError("Результат: неверный числовой сигнал.")
            return a
        time = vector(result.get("time"))
        if time[0] < 0 or np.any(np.diff(time) <= 0):
            raise ValueError("Результат: время должно строго возрастать.")
        signals = result.get("signals")
        definitions = {s.key: s for s in model.signals}
        if not isinstance(signals, dict) or not signals or not set(signals) <= definitions.keys():
            raise ValueError("Результат: неизвестные или отсутствующие сигналы.")
        if not {s.key for s in model.signals if s.required} <= signals.keys():
            raise ValueError("Результат: отсутствуют обязательные сигналы модели.")
        for values in signals.values():
            if len(vector(values)) != len(time):
                raise ValueError("Результат: длины сигналов не совпадают.")
        expected_units = {key: definitions[key].unit for key in signals}
        if result.get("units") != expected_units:
            raise ValueError("Результат: единицы не соответствуют модели.")
        parameters = result.get("parameters")
        if not isinstance(parameters, dict) or set(parameters) != {p.key for p in model.parameters}:
            raise ValueError("Результат: неполные параметры модели.")
        for p in model.parameters:
            value = parameters[p.key]
            if (type(value) not in (int, float) or not np.isfinite(value)
                    or (p.minimum is not None and (value < p.minimum or (p.strict_minimum and value == p.minimum)))
                    or (p.maximum is not None and value > p.maximum)):
                raise ValueError(f"Результат: неверный параметр {p.key}.")
        for key, values in signals.items():
            spec = definitions[key]
            a = np.asarray(values)
            maximum = parameters[spec.maximum] if isinstance(spec.maximum, str) else spec.maximum
            if ((spec.minimum is not None and np.any(a < spec.minimum))
                    or (maximum is not None and np.any(a > maximum))):
                raise ValueError("Результат: сигнал вне физических границ.")
        limits = result.get("limits", [])
        if not isinstance(limits, list):
            raise ValueError("Результат: неверные ограничения графиков.")
        for limit in limits:
            if (not isinstance(limit, dict) or limit.get("signal") not in signals
                    or type(limit.get("value")) not in (int, float) or not np.isfinite(limit["value"])
                    or not isinstance(limit.get("label"), str)):
                raise ValueError("Результат: неверное ограничение графика.")
        events = result.get("events")
        if not isinstance(events, list):
            raise ValueError("Результат: события должны быть списком.")
        for event in events:
            if (not isinstance(event, dict) or event.get("kind") not in model.events
                    or type(event.get("time")) not in (int, float)
                    or not time[0] <= event["time"] <= time[-1]):
                raise ValueError("Результат: неверное физическое событие.")
        try:
            # Time and signals are already checked as finite numbers above.
            json.dumps({key: value for key, value in result.items() if key not in ("time", "signals")}, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("Результат должен содержать только конечные данные JSON.") from error
        if model.validate_result is not None:
            model.validate_result(result)
        return copy_data(result)


def write_result(path, result, registry):
    data = registry.validate(result)
    destination = Path(path)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def read_result(path, registry):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("Не удалось прочитать результат модели.") from error
    return registry.validate(data)
