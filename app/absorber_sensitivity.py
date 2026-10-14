"""Retune PI from erroneous estimates and test the unchanged absorber balance."""

import copy
import itertools

import numpy as np

from .background_tasks import check_cancelled
from .calculations import absorption_balance, disturbance_profile, tune_controller_parameters
from .controller_extensions import normalize_extensions
from .experiment_control import model_parameters
from .physical_models import MODELS, run_model
from .simulation import LEAN_GAS, RICH_ABSORBENT, disturbed_inputs, eta_gain, simulation_time_grid
from .tank_sensitivity import REQUIREMENTS, finite, parse_fields, validate_grid  # noqa: F401 (parse_fields re-exported)


FIELD_DEFAULTS = dict(k_errors="-20;0;20", t_errors="-20;0;20", l_errors="0;1",
                     final_error="0.001", iae="1", overshoot_percent="10",
                     settling_time="80", saturation_duration="20")
REASONS = dict(unreachable="Задание недостижимо", final_error="Конечная ошибка",
               iae="Интегральная ошибка IAE", overshoot_percent="Перерегулирование",
               settling_time="Установление", saturation_duration="Насыщение η")


def estimate(local, offsets):
    k, t, delay = model_parameters(local)
    result = dict(gain=k*(1+offsets[0]/100), time_constant=t*(1+offsets[1]/100), delay=delay+offsets[2])
    model_parameters(result)
    return result


def tuning(local, controller):
    k, t, delay = model_parameters(local)
    settings = tune_controller_parameters("PI", t, delay)
    return dict(controller, controller_gain=settings["proportional_gain"]/abs(k), integral_time=settings["integral_time"])


def validate_request(request, local):
    if not isinstance(request, dict) or request.get("chain") not in (LEAN_GAS, RICH_ABSORBENT):
        raise ValueError("Выберите канал концентрации абсорбера.")
    if (any(not isinstance(request.get(key), dict) for key in ("parameters", "controller", "dynamics"))
            or not {"component", "flow"}.issubset(request)
            or not {"controller_gain", "integral_time", "derivative_time", "setpoint", "control_limit", "controller_type"}.issubset(request["controller"])
            or not {"kind", "start_time", "effect_duration", "simulation_duration", "time_constant", "delay"}.issubset(request["dynamics"])):
        raise ValueError("Нужны полные параметры объекта, динамики, возмущений и PI.")
    p = request["parameters"]
    if set(p) != {"gg", "ga", "xg", "xa", "eta"}:
        raise ValueError("Нужны все пять параметров абсорбера.")
    p = {key: finite(value) for key, value in p.items()}
    absorption_balance(**p)
    if not isinstance(local, dict) or set(local) != {"gain", "time_constant", "delay"}:
        raise ValueError("Нужны номинальные K/T/L.")
    for value in local.values(): finite(value)
    k, _, _ = model_parameters(local)
    actual = eta_gain(request["chain"], p)
    if abs(actual) <= 1e-12 or k*actual <= 0:
        raise ValueError("Знак K должен совпадать с каналом η → концентрация; нечувствительный канал не допускается.")
    c = request["controller"]
    if c.get("controller_type") != "PI":
        raise ValueError("Для опыта ошибок идентификации нужен PI.")
    c = dict(normalize_extensions(c), **{key: finite(c[key]) for key in
             ("controller_gain", "integral_time", "derivative_time", "setpoint", "control_limit")}, controller_type="PI")
    if c["controller_gain"] < 0 or c["integral_time"] <= 0 or c["derivative_time"] != 0 or not 0 <= c["setpoint"] <= 1 or not 0 < c["control_limit"] <= 1:
        raise ValueError("Нужны Kp ≥ 0, Ti > 0, задание 0…1 и предел Δη в (0;1].")
    d = copy.deepcopy(request["dynamics"])
    if d.get("kind") not in ("step", "impulse", "rectangle", "ramp"):
        raise ValueError("Неизвестный профиль возмущения.")
    for key in ("start_time", "effect_duration", "simulation_duration", "time_constant", "delay"):
        d[key] = finite(d[key])
    if not 0 <= d["start_time"] < d["simulation_duration"]:
        raise ValueError("Начало возмущения должно быть внутри горизонта.")
    simulation_time_grid(d, c)
    component, flow = (finite(request[key]) for key in ("component", "flow"))
    for fraction in (0., 1.):
        inputs = disturbed_inputs(request["chain"], p, component*fraction, flow*fraction)
        for eta in (max(0., p["eta"]-c["control_limit"]), min(1., p["eta"]+c["control_limit"])):
            absorption_balance(**dict(inputs, eta=eta))
    output = "xog" if request["chain"] == LEAN_GAS else "xna"
    if abs(c["setpoint"]-float(absorption_balance(**p)[output])) <= 1e-12:
        raise ValueError("Задание должно отличаться от исходной концентрации для оценки перерегулирования.")
    return dict(chain=request["chain"], parameters=p, component=component, flow=flow, dynamics=d, controller=c)


def metrics(run, request):
    time = np.asarray(run["time"])
    output = "xog" if request["chain"] == LEAN_GAS else "xna"
    y, u = (np.asarray(run["signals"][key]) for key in (output, "control"))
    p, c, d = (request[key] for key in ("parameters", "controller", "dynamics"))
    baseline = float(absorption_balance(**p)[output])
    error = c["setpoint"]-y
    amplitude = abs(c["setpoint"]-baseline)
    stationary = d["start_time"]+d["delay"]+(0. if d["kind"] == "step" else d["effect_duration"])
    outside = np.flatnonzero((abs(error) > max(.05*amplitude, 1e-12)) | (time < stationary))
    index = int(outside[-1]+1) if outside.size else 0
    settled = float(time[index]) if index < time.size and time[-1]-time[index] >= d["time_constant"] else None
    terminal = 1. if d["kind"] in ("step", "ramp") else 0.
    inputs = disturbed_inputs(request["chain"], p, request["component"]*terminal, request["flow"]*terminal)
    low, high = max(0., p["eta"]-c["control_limit"]), min(1., p["eta"]+c["control_limit"])
    bounds = [float(absorption_balance(**dict(inputs, eta=eta))[output]) for eta in (low, high)]
    reachable = min(bounds) <= c["setpoint"] <= max(bounds)
    return dict(final_error=float(error[-1]), iae=float(np.trapezoid(abs(error), time)),
                overshoot_percent=float(max(0., np.max(np.sign(c["setpoint"]-baseline)*(y-c["setpoint"]))))/amplitude*100,
                settling_time=settled if reachable else None, reachable=bool(reachable),
                saturation_duration=float(np.dot(np.isclose(u[:-1], low, atol=1e-8, rtol=0)
                    | np.isclose(u[:-1], high, atol=1e-8, rtol=0), np.diff(time))))


def assess(values, requirements):
    reasons = [] if values["reachable"] else ["unreachable"]
    for key in REQUIREMENTS:
        value = values[key]
        if value is None or (abs(value) if key == "final_error" else value) > requirements[key]:
            reasons.append(key)
    return dict(passed=not reasons, reasons=reasons)


def plan(request, local, grid, requirements):
    request = validate_request(request, local)
    validate_grid(grid, requirements)
    offsets = list(itertools.product(*(grid[key] for key in ("k", "t", "l"))))
    estimates = [estimate(local, offset) for offset in offsets]
    settings = [request["controller"], tuning(local, request["controller"]), *[tuning(e, request["controller"]) for e in estimates]]
    if sum(len(simulation_time_grid(request["dynamics"], dict(c, process_gain=abs(eta_gain(request["chain"], request["parameters"]))))) for c in settings) > 300000:
        raise ValueError("Опыт слишком велик: допускается 300000 суммарных точек.")
    return request, offsets, estimates, settings


def calculate(request, local, grid, requirements, *, cancel=None, progress=None):
    request, offsets, estimates, settings = plan(request, local, grid, requirements)
    controls, cases = [], []
    for index, c in enumerate(settings):
        check_cancelled(cancel)
        trial = dict(request, controller=c)
        run = MODELS.result("absorber", run_model("absorber", trial, cancel=cancel,
                progress=None if progress is None else lambda v: progress((index+v)/len(settings))))
        values = metrics(run, trial)
        case = dict(run=run, metrics=values, assessment=assess(values, requirements))
        if index < 2:
            case["name"] = ("Исходный PI", "PI по номинальной оценке")[index]
            controls.append(case)
        else:
            case.update(offsets=list(offsets[index-2]), estimate=estimates[index-2]); cases.append(case)
    return dict(format_version=1, model_id="absorber", model_version=2, request=request,
                local_model={key: float(local[key]) for key in ("gain", "time_constant", "delay")},
                grid=copy.deepcopy(grid), requirements=dict(requirements), controls=controls, cases=cases)


def validate_result(result):
    try:
        if (not isinstance(result, dict) or type(result.get("format_version")) is not int or result["format_version"] != 1
                or result.get("model_id") != "absorber" or type(result.get("model_version")) is not int or result["model_version"] != 2):
            raise ValueError("Ошибки идентификации: неизвестный формат абсорбера.")
        request, offsets, estimates, settings = plan(result["request"], result["local_model"], result["grid"], result["requirements"])
        if len(result["controls"]) != 2 or len(result["cases"]) != len(offsets):
            raise ValueError("Ошибки идентификации: неполная сетка.")
        for index, case in enumerate(result["controls"]+result["cases"]):
            run = MODELS.validate(case["run"])
            c = dict(settings[index], process_gain=abs(eta_gain(request["chain"], request["parameters"])))
            expected = dict(chain=request["chain"], dynamics=request["dynamics"], controller=c)
            if run["parameters"] != request["parameters"] or run["configuration"] != expected:
                raise ValueError("В опытах должны совпадать физические параметры, канал и условия; меняются только настройки PI.")
            time = simulation_time_grid(request["dynamics"], c)
            if len(time) != len(run["time"]) or not np.allclose(time, run["time"], rtol=1e-10, atol=1e-10):
                raise ValueError("Ошибки идентификации: неверная сетка времени или неполный горизонт.")
            for key in ("component", "flow"):
                expected_signal = request[key]*disturbance_profile(time, request["dynamics"]["kind"], request["dynamics"]["start_time"], request["dynamics"]["effect_duration"])
                if not np.allclose(run["signals"][key], expected_signal, rtol=1e-10, atol=1e-10):
                    raise ValueError("Ошибки идентификации: изменено возмущение.")
            if index < 2:
                if case.get("name") != ("Исходный PI", "PI по номинальной оценке")[index]:
                    raise ValueError("Ошибки идентификации: неверный контроль.")
            elif case.get("offsets") != list(offsets[index-2]) or case.get("estimate") != estimates[index-2]:
                raise ValueError("Ошибки идентификации: оценка не соответствует отклонениям.")
            output = "xog" if request["chain"] == LEAN_GAS else "xna"
            if not np.allclose(run["signals"]["error"], c["setpoint"]-np.asarray(run["signals"][output]), rtol=1e-10, atol=1e-10):
                raise ValueError("Ошибки идентификации: ошибка не соответствует выходу.")
            low, high = max(0., request["parameters"]["eta"]-c["control_limit"]), min(1., request["parameters"]["eta"]+c["control_limit"])
            if np.any(np.asarray(run["signals"]["control"]) < low-1e-12) or np.any(np.asarray(run["signals"]["control"]) > high+1e-12):
                raise ValueError("Ошибки идентификации: η выходит за пределы механизма.")
            values = metrics(run, dict(request, controller=settings[index]))
            if case["metrics"].keys() != values.keys():
                raise ValueError("Ошибки идентификации: неполные показатели.")
            for key, value in values.items():
                actual = case["metrics"][key]
                if (value is None or type(value) is bool):
                    if actual != value or type(actual) is not type(value): raise ValueError("Неверный признак или время установления.")
                elif not np.isclose(finite(actual), value, rtol=1e-9, atol=1e-9):
                    raise ValueError("Ошибки идентификации: показатели не соответствуют сигналам.")
            if case["assessment"] != assess(values, result["requirements"]):
                raise ValueError("Ошибки идентификации: неверная оценка требований.")
        return result
    except (KeyError, TypeError, IndexError, AttributeError, OverflowError) as error:
        raise ValueError("Ошибки идентификации: повреждённый результат абсорбера.") from error
