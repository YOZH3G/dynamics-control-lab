"""Objects of control described in a JSON file: parameters, state equations, one input and a delayed output.

The description is data only. Formulas are checked by app.expressions and never
executed as code. Within each sample interval the input is held and the states
are integrated by the classical Runge–Kutta method; the measurement is the
output state delayed by a pure delay. PI uses the same update as the built-in
objects, on the delayed measurement.
"""

import json
import math
import re
from pathlib import Path

import numpy as np

from .background_tasks import check_cancelled
from .calculations import tune_controller_parameters
from .expressions import FUNCTIONS, Formula
from .identification import identify_step, validate_step_quality, validate_uncertainty
from .model_contract import Parameter, PhysicalModel, Signal
from .pi_control import pi_update

FORMAT = "dynamics-control-lab/object"
SUBSTEPS = 4
MAX_INTERVALS = 50000
_KEY = re.compile(r"[a-z][a-z0-9_]{0,30}")
_RESERVED = {"t", "commanded", "error", *FUNCTIONS}
VIEWS = ("Свободный отклик", "PI", "Идентификация")
# Descriptions loaded in this run, by model id; pages and session checks look them up here.
DESCRIBED = {}


def _text(data, key, where, limit=200):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{where}: нужен непустой текст «{key}» до {limit} символов.")
    return value


def _key(data, where, taken):
    key = data.get("key")
    if not isinstance(key, str) or not _KEY.fullmatch(key) or key in _RESERVED or key in taken:
        raise ValueError(f"{where}: ключ должен быть новым именем из строчных латинских букв, цифр и «_».")
    taken.add(key)
    return key


def _number(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{where}: нужно конечное число.")
    return float(value)


class ObjectDescription:
    def __init__(self, data):
        if not isinstance(data, dict) or data.get("format") != FORMAT or data.get("version") != 1:
            raise ValueError(f"Описание объекта: нужен формат «{FORMAT}» версии 1.")
        taken = set()
        self.data = data
        self.id = _key(dict(key=data.get("id")), "Описание объекта", set())
        self.name = _text(data, "name", "Описание объекта", 40)
        self.sidebar, self.context, self.hint = (_text(data, k, "Описание объекта", 300) for k in ("sidebar", "context", "hint"))
        raw = data.get("parameters")
        if not isinstance(raw, list) or not 1 <= len(raw) <= 40:
            raise ValueError("Описание объекта: нужен список из 1–40 параметров.")
        self.parameters = []
        for item in raw:
            where = "Параметр"
            if not isinstance(item, dict):
                raise ValueError(f"{where}: нужен объект JSON.")
            key = _key(item, where, taken)
            low = None if item.get("minimum") is None else _number(item["minimum"], f"{where} {key}: minimum")
            high = None if item.get("maximum") is None else _number(item["maximum"], f"{where} {key}: maximum")
            default = _number(item.get("default"), f"{where} {key}: default")
            strict = item.get("strict_minimum", False)
            if not isinstance(strict, bool) or (low is not None and (default < low or (strict and default == low))) \
                    or (high is not None and default > high):
                raise ValueError(f"{where} {key}: значение по умолчанию вне допустимого диапазона.")
            field = item.get("field", "")
            if not isinstance(field, str) or len(field) > 60:
                raise ValueError(f"{where} {key}: подпись поля — текст до 60 символов.")
            self.parameters.append(Parameter(key, _text(item, "label", where, 60), _text(item, "unit", where, 20),
                                             default, low, high, strict, field))
        names = {p.key for p in self.parameters}
        raw = data.get("states")
        if not isinstance(raw, list) or not 1 <= len(raw) <= 6:
            raise ValueError("Описание объекта: нужен список из 1–6 переменных состояния.")
        states = [(item if isinstance(item, dict) else {}) for item in raw]
        self.states = [_key(item, "Переменная состояния", taken) for item in states]
        self.state_info = {key: (_text(item, "label", key, 60), _text(item, "unit", key, 20)) for key, item in zip(self.states, states)}
        source = data.get("input")
        if not isinstance(source, dict):
            raise ValueError("Описание объекта: нужен вход «input».")
        self.input = _key(source, "Вход", taken)
        self.input_info = (_text(source, "label", "Вход", 60), _text(source, "unit", "Вход", 20))
        dynamic = names | set(self.states) | {self.input, "t"}
        self.initial = {key: Formula(item.get("initial"), names) for key, item in zip(self.states, states)}
        self.derivative = {key: Formula(item.get("derivative"), dynamic) for key, item in zip(self.states, states)}
        self.input_formulas = {k: Formula(source.get(k), names) for k in ("initial", "step", "step_time", "minimum", "maximum")}
        output = data.get("output")
        if not isinstance(output, dict) or output.get("state") not in self.states:
            raise ValueError("Описание объекта: выход «output» должен ссылаться на переменную состояния.")
        self.output_state = output["state"]
        self.measured = _key(output, "Выход", taken)
        self.measured_label = _text(output, "label", "Выход", 60)
        self.delay = Formula(output.get("delay", "0"), names)
        self.horizon, self.sample_time = (Formula(data.get(k), names) for k in ("horizon", "sample_time"))
        pi = data.get("pi", {})
        if not isinstance(pi, dict) or set(pi) - {"setpoint", "gain", "integral_time"}:
            raise ValueError("Описание объекта: «pi» может задавать только setpoint, gain и integral_time.")
        self.pi_defaults = {k: format(_number(v, f"PI: {k}"), "g") for k, v in pi.items()}

    @property
    def unit(self):
        return self.state_info[self.output_state][1]

    def values(self, values):
        """Validated parameter values from a mapping of numbers."""
        if not isinstance(values, dict) or set(values) != {p.key for p in self.parameters}:
            raise ValueError(f"{self.name}: нужны все параметры опыта.")
        p = {}
        for parameter in self.parameters:
            value = values[parameter.key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{self.name}: «{parameter.field_label}» должно быть конечным числом.")
            if ((parameter.minimum is not None and (value < parameter.minimum or (parameter.strict_minimum and value == parameter.minimum)))
                    or (parameter.maximum is not None and value > parameter.maximum)):
                raise ValueError(f"{self.name}: «{parameter.field_label}» вне допустимого диапазона.")
            p[parameter.key] = float(value)
        return p

    def simulate(self, values, *, controller=None, cancel=None, progress=None):
        p = self.values(values)
        horizon, dt, delay = self.horizon(p), self.sample_time(p), self.delay(p)
        u0, step, step_time, low, high = (self.input_formulas[k](p) for k in ("initial", "step", "step_time", "minimum", "maximum"))
        if horizon <= 0 or dt <= 0 or horizon / dt > MAX_INTERVALS:
            raise ValueError(f"{self.name}: нужны положительные горизонт и период, не больше {MAX_INTERVALS} интервалов.")
        if not 0 <= step_time < horizon or not 0 <= delay < horizon or not low <= u0 <= high:
            raise ValueError(f"{self.name}: ступень и запаздывание должны быть внутри горизонта, исходный вход — в пределах.")
        if controller is not None:
            kp, ti, setpoint = (_number(controller.get(k), "PI") for k in ("gain", "integral_time", "setpoint"))
            if kp < 0 or ti <= 0:
                raise ValueError("PI: нужны Kp ≥ 0 и Ti > 0.")
        state = np.array([self.initial[k](p) for k in self.states])
        grid = np.unique(np.r_[np.arange(0, horizon, dt), step_time, horizon])
        index = self.states.index(self.output_state)
        time, states, measured = [0.], [state.copy()], [float(state[index])]
        inputs, commanded, integral, saturation = [], [], 0., 0.
        for number, end in enumerate(grid[1:]):
            check_cancelled(cancel)
            now = time[-1]
            if controller is None:
                command = u0 + (step if now >= step_time else 0.)
                u = min(high, max(low, command))
            else:
                u, integral = pi_update(setpoint - measured[-1], kp, ti, end - now, u0, low, high, integral)
                command = u
            state = self._advance(state, u, now, end, p)
            inputs.append(float(u)); commanded.append(float(command))
            time.append(float(end)); states.append(state.copy())
            delayed = time[-1] - delay
            output = [s[index] for s in states]
            measured.append(float(output[0] if delayed <= 0 else np.interp(delayed, time, output)))
            if u in (low, high):
                saturation += end - now
            if progress and number % 200 == 0:
                progress((number + 1) / (len(grid) - 1))
        for series in (inputs, commanded):
            series.append(series[-1])
        trajectory = np.asarray(states)
        run = dict(model_id=self.id, model_version=1, parameters=p, time=time, measured=measured,
                   commanded=commanded, controller=None if controller is None else dict(gain=kp, integral_time=ti, setpoint=setpoint),
                   event=None, sample_time=dt, limits=[low, high])
        run[self.input] = inputs
        run.update({key: trajectory[:, i].tolist() for i, key in enumerate(self.states)})
        if controller is not None:
            run["error"] = (setpoint - np.asarray(measured)).tolist()
            run["metrics"] = self.metrics(run, saturation)
        if progress:
            progress(1.)
        return run

    def _advance(self, state, u, start, end, p):
        values = dict(p)
        values[self.input] = u

        def rate(t, x):
            values.update(zip(self.states, x), t=t)
            return np.array([self.derivative[k](values) for k in self.states])
        h = (end - start) / SUBSTEPS
        t = start
        for _ in range(SUBSTEPS):
            k1 = rate(t, state)
            k2 = rate(t + h / 2, state + h / 2 * k1)
            k3 = rate(t + h / 2, state + h / 2 * k2)
            k4 = rate(t + h, state + h * k3)
            state = state + h / 6 * (k1 + 2 * k2 + 2 * k3 + k4)
            t += h
        if not np.isfinite(state).all():
            raise ValueError(f"{self.name}: решение стало неконечным; уменьшите период расчёта.")
        return state

    def metrics(self, run, saturation):
        """Final error, IAE, overshoot and ±5% settling on the true output state."""
        time, output = np.asarray(run["time"]), np.asarray(run[self.output_state])
        setpoint, start = run["controller"]["setpoint"], output[0]
        error = setpoint - output
        span = abs(setpoint - start)
        outside = np.flatnonzero(abs(error) > max(.05 * span, 1e-8))
        at = int(outside[-1] + 1) if outside.size else 0
        settled = float(time[at]) if at < time.size - 1 else None
        overshoot = float(max(0., np.max(np.sign(setpoint - start) * (output - setpoint))) * 100 / span) if span > 1e-12 else 0.
        return dict(final_error=float(error[-1]), iae=float(np.trapezoid(abs(error), time)), overshoot_percent=overshoot,
                    settling_time=settled, saturation_duration=float(saturation),
                    status="settled" if settled is not None else "horizon")

    def validate_run(self, run):
        """A stored run must be exactly what the description computes for its own inputs."""
        if not isinstance(run, dict) or run.get("model_id") != self.id or run.get("model_version") != 1:
            raise ValueError(f"{self.name}: неизвестная версия описания.")
        try:
            expected = self.simulate(run["parameters"], controller=run.get("controller"))
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError(f"{self.name}: неполный результат расчёта.") from error
        for key in ("time", "measured", "commanded", self.input, *self.states):
            values = run.get(key)
            if not isinstance(values, list) or len(values) != len(expected[key]) or not np.allclose(values, expected[key], rtol=1e-9, atol=1e-9):
                raise ValueError(f"{self.name}: сигналы не соответствуют параметрам.")
        return run

    # Response loop: identification of the input → measurement channel and PI settings.

    def identify(self, run, noise, seed, *, cancel=None, progress=None):
        if run.get("controller") is not None or not self.input_formulas["step"](run["parameters"]):
            raise ValueError("Для идентификации нужен свободный отклик на ненулевую ступень входа.")
        if isinstance(noise, bool) or not isinstance(noise, (int, float)) or not math.isfinite(noise) or noise < 0 \
                or type(seed) is not int or not 0 <= seed <= 4294967295:
            raise ValueError("Нужны шум σ ≥ 0 и целое число генератора 0…4294967295.")
        output = np.asarray(run["measured"]) + np.random.default_rng(seed).normal(0, noise, len(run["time"]))
        fit = identify_step(np.asarray(run["time"]), np.asarray(run[self.input]), output, cancel=cancel, progress=progress)
        return dict({k: v.tolist() if isinstance(v, np.ndarray) else v for k, v in fit.items()},
                    parameters=run["parameters"], noise=float(noise), seed=seed)

    def validate_fit(self, fit):
        if not isinstance(fit, dict):
            raise ValueError(f"{self.name}: неверная локальная модель.")
        try:
            expected = self.identify(self.simulate(fit["parameters"]), fit["noise"], fit["seed"])
        except (KeyError, TypeError) as error:
            raise ValueError(f"{self.name}: неполная локальная модель.") from error
        for key in ("time", "input", "output", "model", "residual"):
            values = fit.get(key)
            if not isinstance(values, list) or len(values) != len(expected[key]) or not np.allclose(values, expected[key], rtol=1e-9, atol=1e-9):
                raise ValueError(f"{self.name}: локальная модель не соответствует эксперименту.")
        if any(not math.isclose(fit.get(k, math.nan), expected[k], rel_tol=1e-9, abs_tol=1e-12) for k in ("gain", "time_constant", "delay")):
            raise ValueError(f"{self.name}: K, T и L не соответствуют эксперименту.")
        validate_step_quality(fit)
        if "uncertainty" in fit:
            validate_uncertainty(fit["uncertainty"], len(fit["time"]))
        return fit

    @staticmethod
    def pi_settings(fit):
        settings = tune_controller_parameters("PI", fit["time_constant"], fit["delay"])
        return dict(gain=settings["proportional_gain"] / fit["gain"], integral_time=settings["integral_time"])

    # Page snapshot in a session.

    def field_defaults(self):
        fields = {p.key: format(p.default, "g") for p in self.parameters}
        fields.update(noise="0.05", seed="1", setpoint="", gain="1", integral_time="100", title=f"Опыт: {self.name}",
                      prediction="", conclusion="")
        fields.update(self.pi_defaults)
        return fields

    def validate_state(self, state):
        if state is None:
            return None
        fields = self.field_defaults()
        if (not isinstance(state, dict) or state.get("model_id") != self.id or not isinstance(state.get("fields"), dict)
                or set(state["fields"]) != set(fields) or any(not isinstance(v, str) for v in state["fields"].values())
                or state.get("view") not in VIEWS):
            raise ValueError(f"{self.name}: неверный снимок страницы.")
        revision = state.get("revision")
        if type(revision) is not int or revision < 0:
            raise ValueError(f"{self.name}: неверная версия формы.")
        for key in ("result", "pi", "fit"):
            record = state.get(key)
            if record is None:
                continue
            if not isinstance(record, dict) or type(record.get("revision")) is not int or not 0 <= record["revision"] <= revision:
                raise ValueError(f"{self.name}: неверное происхождение результата.")
            if key == "fit":
                self.validate_fit(record)
            else:
                self.validate_run(record)
                if (key == "pi") != (record.get("controller") is not None):
                    raise ValueError(f"{self.name}: опыт PI должен содержать регулятор, свободный отклик — нет.")
        return state

    # Registration.

    def model(self):
        signals = (Signal(self.input, *self.input_info, "input", required=True),
                   Signal("commanded", f"Команда: {self.input_info[0].lower()}", self.input_info[1], "input"),
                   *(Signal(k, *self.state_info[k], "output", required=k == self.output_state) for k in self.states),
                   Signal(self.measured, self.measured_label, self.unit, "output"),
                   Signal("error", "Ошибка", self.unit, "error"))
        return PhysicalModel(self.id, 1, self.name, tuple(self.parameters), signals, ((self.input, self.measured),),
                             ("dynamics", "PI", "identification"), (), self._run, self._project, self.id, self.sidebar,
                             self.context, self._validate_result, "ui.response_page:ResponsePage", self.validate_state)

    def _run(self, request, *, cancel=None, progress=None):
        return self.simulate(request["parameters"], controller=request.get("controller"), cancel=cancel, progress=progress)

    def _project(self, raw):
        keys = [self.input, "commanded", *self.states, self.measured]
        signals = {k: list(raw["measured" if k == self.measured else k]) for k in keys}
        if "error" in raw:
            signals["error"] = list(raw["error"])
        units = {self.input: self.input_info[1], "commanded": self.input_info[1], self.measured: self.unit,
                 **{k: self.state_info[k][1] for k in self.states}}
        if "error" in raw:
            units["error"] = self.unit
        return dict(time=list(raw["time"]), parameters=dict(raw["parameters"]), signals=signals, units=units, events=[],
                    metrics=dict(raw.get("metrics", {})), configuration=dict(controller=raw["controller"], sample_time=raw["sample_time"]),
                    limits=[dict(signal=self.input, value=raw["limits"][0], label="Минимум"),
                            dict(signal=self.input, value=raw["limits"][1], label="Максимум")])

    def _validate_result(self, result):
        """A common result is accepted only if the description recomputes the same signals."""
        signals = result["signals"]
        native = dict(model_id=self.id, model_version=1, parameters=result["parameters"], time=result["time"],
                      controller=result["configuration"].get("controller"), measured=signals.get(self.measured),
                      commanded=signals.get("commanded"), **{k: signals.get(k) for k in (self.input, *self.states)})
        self.validate_run(native)
        p = self.values(result["parameters"])
        bounds = [self.input_formulas[k](p) for k in ("minimum", "maximum")]
        if result.get("limits") != self._project(dict(native, limits=bounds, sample_time=0.))["limits"]:
            raise ValueError(f"{self.name}: ограничения не соответствуют описанию.")


def load_description(source):
    """Description from a dict or a JSON file; remembered for pages and session checks."""
    if isinstance(source, (str, Path)):
        try:
            source = json.loads(Path(source).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError("Не удалось прочитать описание объекта.") from error
    description = ObjectDescription(source)
    DESCRIBED[description.id] = description
    return description
