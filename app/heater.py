"""Stirred tank with an electric heater: energy balance, delayed sensor and PI on power.

M·c·dT/dt = W·c·(Tin − T) + P − UA·(T − Tenv). With inputs held over a step the
temperature relaxes exactly towards T∞ = (W·c·Tin + P + UA·Tenv) / (W·c + UA)
with time constant M·c / (W·c + UA). Water boils at 100 °C, which ends a run.
"""

import math

import numpy as np

from .background_tasks import check_cancelled
from .calculations import tune_controller_parameters
from .identification import identify_step, validate_step_quality, validate_uncertainty
from .pi_control import pi_update

MODEL_ID = "heated_tank"
MODEL_VERSION = 1
BOILING = 100.
DEFAULTS = dict(mass=50., heat_capacity=4.19, flow=.1, inlet_temperature=15., loss_coefficient=.05,
                ambient_temperature=20., power_limit=20., power=12., step_time=60., power_step=2.,
                inlet_step=0., flow_step=0., sensor_delay=5., horizon=2400., sample_time=1.)
POSITIVE = ("mass", "heat_capacity", "power_limit", "horizon", "sample_time")
NONNEGATIVE = ("flow", "loss_coefficient", "power", "step_time", "sensor_delay")
SIGNAL_UNITS = dict(time="с", temperature="°C", measured="°C", power="кВт", commanded="кВт",
                    inlet="°C", flow="кг/с")


def validate_inputs(values):
    if not isinstance(values, dict) or set(values) != set(DEFAULTS):
        raise ValueError("Нагреватель: нужны все параметры опыта.")
    if any(isinstance(v, bool) for v in values.values()):
        raise ValueError("Нагреватель: параметры должны быть числами.")
    try:
        p = {k: float(v) for k, v in values.items()}
    except (TypeError, ValueError) as error:
        raise ValueError("Нагреватель: параметры должны быть числами.") from error
    if not all(math.isfinite(v) for v in p.values()):
        raise ValueError("Нагреватель: параметры должны быть конечными.")
    if any(p[k] <= 0 for k in POSITIVE) or any(p[k] < 0 for k in NONNEGATIVE):
        raise ValueError("M, c, Pmax, горизонт и период должны быть положительными; W, UA, P0, ступень и запаздывание — неотрицательными.")
    if p["flow"] + p["flow_step"] < 0 or p["power"] + p["power_step"] < 0:
        raise ValueError("Расход и мощность после ступени не могут быть отрицательными.")
    if p["flow"] * p["heat_capacity"] + p["loss_coefficient"] <= 0 or (p["flow"] + p["flow_step"]) * p["heat_capacity"] + p["loss_coefficient"] <= 0:
        raise ValueError("Нужен отвод тепла: расход W или теплопотери UA должны быть больше нуля.")
    if p["power"] > p["power_limit"]:
        raise ValueError("Исходная мощность превышает предел нагревателя.")
    if not 0 <= p["step_time"] < p["horizon"] or p["sensor_delay"] >= p["horizon"]:
        raise ValueError("Ступень и запаздывание датчика должны быть внутри горизонта опыта.")
    if p["horizon"] / p["sample_time"] > 50000:
        raise ValueError("Нагреватель: допускается не больше 50000 интервалов.")
    if not 0 < equilibrium_temperature(p, p["power"]) < BOILING:
        raise ValueError("Исходное равновесие должно быть между 0 и 100 °C.")
    return p


def _removal(p, flow):
    return flow * p["heat_capacity"] + p["loss_coefficient"]


def equilibrium_temperature(p, power, flow=None, inlet=None):
    flow = p["flow"] if flow is None else flow
    inlet = p["inlet_temperature"] if inlet is None else inlet
    return (flow * p["heat_capacity"] * inlet + power + p["loss_coefficient"] * p["ambient_temperature"]) / _removal(p, flow)


def equilibrium_power(p, temperature):
    """Power holding ``temperature`` with the initial flow and inlet temperature."""
    return _removal(p, p["flow"]) * temperature - p["flow"] * p["heat_capacity"] * p["inlet_temperature"] \
        - p["loss_coefficient"] * p["ambient_temperature"]


def time_constant(p, flow=None):
    return p["mass"] * p["heat_capacity"] / _removal(p, p["flow"] if flow is None else flow)


def advance(temperature, power, flow, inlet, duration, p):
    """Exact held-input step; returns (temperature, elapsed, boiled)."""
    removal = _removal(p, flow)
    target = (flow * p["heat_capacity"] * inlet + power + p["loss_coefficient"] * p["ambient_temperature"]) / removal
    rate = removal / (p["mass"] * p["heat_capacity"])
    if target > BOILING and temperature < BOILING:
        boil_time = math.log((target - temperature) / (target - BOILING)) / rate
        if boil_time <= duration:
            return BOILING, boil_time, True
    return target + (temperature - target) * math.exp(-rate * duration), duration, False


def simulate(values, *, controller=None, cancel=None, progress=None):
    p = validate_inputs(values)
    if controller is not None:
        kp, ti, setpoint = (float(controller[k]) for k in ("gain", "integral_time", "setpoint"))
        if not np.isfinite([kp, ti, setpoint]).all() or kp < 0 or ti <= 0 or not 0 < setpoint < BOILING:
            raise ValueError("PI: нужны Kp ≥ 0, Ti > 0 и задание от 0 до 100 °C.")
    start = equilibrium_temperature(p, p["power"])
    grid = np.unique(np.r_[np.arange(0, p["horizon"], p["sample_time"]), p["step_time"], p["horizon"]])
    time, temperature, measured = [0.], [start], [start]
    power, commanded, inlet, flow = [], [], [], []
    integral, event, saturation = 0., None, 0.
    for index, end in enumerate(grid[1:]):
        check_cancelled(cancel)
        now = time[-1]
        dt = float(end - now)
        changed = now >= p["step_time"]
        w = p["flow"] + (p["flow_step"] if changed else 0.)
        t_in = p["inlet_temperature"] + (p["inlet_step"] if changed else 0.)
        if controller is None:
            command = p["power"] + (p["power_step"] if changed else 0.)
            q = min(p["power_limit"], max(0., command))
        else:
            q, integral = pi_update(setpoint - measured[-1], kp, ti, dt, p["power"], 0., p["power_limit"], integral)
            command = q
        value, elapsed, boiled = advance(temperature[-1], q, w, t_in, dt, p)
        power.append(float(q)); commanded.append(float(command)); inlet.append(float(t_in)); flow.append(float(w))
        time.append(now + elapsed); temperature.append(float(value))
        delayed = time[-1] - p["sensor_delay"]
        measured.append(float(start if delayed <= 0 else np.interp(delayed, time, temperature)))
        if q in (0., p["power_limit"]):
            saturation += elapsed
        if boiled:
            event = dict(kind="boiling", time=time[-1], temperature=BOILING)
            break
        if progress and index % 200 == 0:
            progress((index + 1) / (len(grid) - 1))
    for series in (power, commanded, inlet, flow):
        series.append(series[-1])
    result = dict(model_id=MODEL_ID, model_version=MODEL_VERSION, parameters=p, units=SIGNAL_UNITS.copy(),
                  time=time, temperature=temperature, measured=measured, power=power, commanded=commanded,
                  inlet=inlet, flow=flow, event=event, controller=None if controller is None else
                  dict(gain=kp, integral_time=ti, setpoint=setpoint), sample_time=p["sample_time"],
                  initial_temperature=start, method="held-input exact energy balance; sampled PI on delayed measurement")
    if controller is not None:
        result["error"] = (setpoint - np.asarray(measured)).tolist()
        result["metrics"] = pi_metrics(p, result, saturation)
    check_cancelled(cancel)
    if progress:
        progress(1.)
    return result


def pi_metrics(p, run, saturation):
    """Final error, IAE, overshoot and ±5% settling on the true temperature."""
    time, temperature = np.asarray(run["time"]), np.asarray(run["temperature"])
    setpoint, start = run["controller"]["setpoint"], run["initial_temperature"]
    error = setpoint - temperature
    span = abs(setpoint - start)
    outside = np.flatnonzero(abs(error) > max(.05 * span, 1e-8))
    at = int(outside[-1] + 1) if outside.size else 0
    settled = float(time[at]) if at < time.size and time[-1] - time[at] >= time_constant(p) else None
    w_end = p["flow"] + p["flow_step"]
    reachable = 0 <= _removal(p, w_end) * setpoint - w_end * p["heat_capacity"] * (p["inlet_temperature"] + p["inlet_step"]) \
        - p["loss_coefficient"] * p["ambient_temperature"] <= p["power_limit"]
    if run["event"] or not reachable:
        settled = None
    overshoot = float(max(0., np.max(np.sign(setpoint - start) * (temperature - setpoint)))) * 100 / span if span > 1e-12 else 0.
    status = "physical_stop" if run["event"] else "unreachable" if not reachable else "settled" if settled is not None else "horizon"
    return dict(final_error=float(error[-1]), iae=float(np.trapezoid(abs(error), time)), overshoot_percent=overshoot,
                settling_time=settled, saturation_duration=float(saturation), reachable=bool(reachable), status=status)


def validate_run(run):
    """A stored run must be exactly what the model computes for its own inputs."""
    if not isinstance(run, dict) or run.get("model_id") != MODEL_ID or run.get("model_version") != MODEL_VERSION:
        raise ValueError("Нагреватель: неизвестная версия физической модели.")
    try:
        expected = simulate(run["parameters"], controller=run.get("controller"))
    except (KeyError, TypeError) as error:
        raise ValueError("Нагреватель: неполный результат расчёта.") from error
    for key in ("time", "temperature", "measured", "power", "commanded", "inlet", "flow"):
        values = run.get(key)
        if (not isinstance(values, list) or len(values) != len(expected[key])
                or not np.allclose(values, expected[key], rtol=1e-9, atol=1e-9)):
            raise ValueError("Нагреватель: сигналы не соответствуют параметрам модели.")
    if run.get("event") != expected["event"] or run.get("units") != SIGNAL_UNITS:
        raise ValueError("Нагреватель: событие или единицы не соответствуют расчёту.")
    return run


def identification_data(run, noise, seed):
    """Step experiment from a free response: power in, delayed measurement plus sensor noise out."""
    p = run["parameters"]
    if run["controller"] is not None or run["event"] or p["inlet_step"] or p["flow_step"] or not p["power_step"]:
        raise ValueError("Для идентификации нужен свободный отклик без закипания на ступень одной мощности: "
                         "ступени Tвх и расхода равны нулю.")
    if isinstance(noise, bool) or not isinstance(noise, (int, float)) or not math.isfinite(noise) or noise < 0 \
            or type(seed) is not int or not 0 <= seed <= 4294967295:
        raise ValueError("Нужны шум σ ≥ 0 и целое число генератора 0…4294967295.")
    output = np.asarray(run["measured"]) + np.random.default_rng(seed).normal(0, noise, len(run["time"]))
    return np.asarray(run["time"]), np.asarray(run["power"]), output


def identify(run, noise, seed, *, cancel=None, progress=None):
    """K, T and L of the power → measured temperature channel, with the true physical values for comparison."""
    fit = identify_step(*identification_data(run, noise, seed), cancel=cancel, progress=progress)
    if fit["gain"] <= 0:
        raise ValueError("Нужен положительный канал: мощность → температура.")
    p = run["parameters"]
    return dict({k: v.tolist() if isinstance(v, np.ndarray) else v for k, v in fit.items()}, parameters=p,
                noise=float(noise), seed=seed, physical=dict(gain=1 / _removal(p, p["flow"]), time_constant=time_constant(p),
                                                             delay=p["sensor_delay"]))


def validate_fit(fit):
    """A stored fit must be exactly what identification gives for its own experiment."""
    if not isinstance(fit, dict):
        raise ValueError("Нагреватель: неверная локальная модель.")
    try:
        expected = identify(simulate(fit["parameters"]), fit["noise"], fit["seed"])
    except (KeyError, TypeError) as error:
        raise ValueError("Нагреватель: неполная локальная модель.") from error
    for key in ("time", "input", "output", "model", "residual"):
        values = fit.get(key)
        if not isinstance(values, list) or len(values) != len(expected[key]) or not np.allclose(values, expected[key], rtol=1e-9, atol=1e-9):
            raise ValueError("Нагреватель: локальная модель не соответствует эксперименту.")
    if any(not math.isclose(fit.get(k, math.nan), expected[k], rel_tol=1e-9, abs_tol=1e-12)
           for k in ("gain", "time_constant", "delay")) or fit.get("physical") != expected["physical"]:
        raise ValueError("Нагреватель: K, T и L не соответствуют эксперименту.")
    validate_step_quality(fit)
    if "uncertainty" in fit:
        validate_uncertainty(fit["uncertainty"], len(fit["time"]))
    return fit


def pi_tuning(fit):
    """PI settings from the identified K, T and L by the project's first-order tuning rule."""
    settings = tune_controller_parameters("PI", fit["time_constant"], fit["delay"])
    return dict(gain=settings["proportional_gain"] / fit["gain"], integral_time=settings["integral_time"])


FIELD_DEFAULTS = {k: format(v, "g") for k, v in DEFAULTS.items()}
FIELD_DEFAULTS.update(setpoint="45", gain="3", integral_time="300", title="Опыт с нагревателем", prediction="", conclusion="",
                      noise="0.05", seed="1")
# Sessions saved by 1.13.0 have no identification fields; they load with the defaults.
ADDED_FIELDS = ("noise", "seed")
VIEWS = ("Свободный отклик", "PI", "Идентификация")


def validate_state(state):
    """Check a heater page snapshot before a session replaces the live page."""
    if state is None:
        return None
    if isinstance(state, dict) and isinstance(state.get("fields"), dict) and set(state["fields"]) == set(FIELD_DEFAULTS) - set(ADDED_FIELDS):
        state = dict(state, fields={**{k: FIELD_DEFAULTS[k] for k in ADDED_FIELDS}, **state["fields"]})
    if (not isinstance(state, dict) or state.get("model_id") != MODEL_ID or state.get("model_version") != MODEL_VERSION
            or not isinstance(state.get("fields"), dict) or set(state["fields"]) != set(FIELD_DEFAULTS)
            or any(not isinstance(v, str) for v in state["fields"].values()) or state.get("view") not in VIEWS):
        raise ValueError("Нагреватель: неверный снимок страницы.")
    revision = state.get("revision")
    if type(revision) is not int or revision < 0:
        raise ValueError("Нагреватель: неверная версия формы.")
    for key in ("result", "pi", "fit"):
        record = state.get(key)
        if record is None:
            continue
        if not isinstance(record, dict) or type(record.get("revision")) is not int or not 0 <= record["revision"] <= revision:
            raise ValueError("Нагреватель: неверное происхождение результата.")
        if key == "fit":
            validate_fit(record)
            continue
        validate_run(record)
        if (key == "pi") != (record.get("controller") is not None):
            raise ValueError("Нагреватель: опыт PI должен содержать регулятор, свободный отклик — нет.")
    return state
