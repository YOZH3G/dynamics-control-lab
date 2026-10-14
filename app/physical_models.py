"""Adapters preserve the two existing solvers and their native result layouts."""

import numpy as np

from .calculations import DEFAULT_MODEL_VALUES
from .tank import DEFAULTS
from . import heater
from .model_contract import ModelRegistry, Parameter, PhysicalModel, Signal


def _absorber_run(request, *, cancel=None, progress=None):
    from .simulation import run_simulation
    return run_simulation(request["chain"], request["parameters"], request["component"], request["flow"],
                          request["dynamics"], request.get("controller"),
                          cancel=cancel, progress=progress)


def _tank_run(request, *, cancel=None, progress=None):
    from .tank import simulate
    return simulate(request["parameters"], controller=request.get("controller"),
                    local_model=request.get("local_model"), cancel=cancel, progress=progress)


def _heater_run(request, *, cancel=None, progress=None):
    return heater.simulate(request["parameters"], controller=request.get("controller"), cancel=cancel, progress=progress)


def _result(raw, parameters, signals, definitions, events=()):
    return dict(time=np.asarray(raw["time"]).tolist(), parameters=dict(parameters),
                signals={k: np.asarray(v).tolist() for k, v in signals.items()},
                units={s.key: s.unit for s in definitions if s.key in signals},
                events=list(events), metrics=dict(raw.get("metrics", {})),
                configuration={k: raw[k] for k in ("chain", "dynamics", "controller", "method", "baseline_pump", "sample_time", "name", "local_model",
                                                    "initial_temperature") if k in raw})


ABSORBER_SIGNALS = (
    Signal("component", "Состав", "доля", "input", "%", 100.),
    Signal("flow", "Расход", "доля", "input", "%", 100.),
    Signal("xog", "Обеднённый газ", "доля", "output", "%", 100., True, 0., 1.),
    Signal("xna", "Насыщенный абсорбент", "доля", "output", "%", 100., True, 0., 1.),
    Signal("control", "Степень извлечения η", "доля", "input", "%", 100., minimum=0., maximum=1.),
    Signal("commanded_control", "Команда η", "доля", "input", "%", 100.),
    Signal("measurement", "Измеренный выход", "доля", "output", "%", 100.),
    Signal("error", "Ошибка", "доля", "error", "п.п.", 100.),
)
TANK_SIGNALS = (
    Signal("pump", "Фактическая подача", "м³/с", "input", required=True, minimum=0., maximum="pump_limit"),
    Signal("commanded", "Заданная подача", "м³/с", "input"),
    Signal("withdrawal", "Дополнительный отбор", "м³/с", "input"),
    Signal("level", "Нелинейный бак", "м", "output", required=True, minimum=0., maximum="height"),
    Signal("prediction", "Локальный прогноз", "м", "output"),
    Signal("error", "Ошибка уровня", "м", "error"),
)


HEATER_SIGNALS = (
    Signal("power", "Мощность нагревателя", "кВт", "input", required=True, minimum=0., maximum="power_limit"),
    Signal("commanded", "Команда мощности", "кВт", "input"),
    Signal("inlet", "Температура на входе", "°C", "input"),
    Signal("flow", "Расход", "кг/с", "input", minimum=0.),
    Signal("temperature", "Температура в ёмкости", "°C", "output", required=True, maximum=heater.BOILING),
    Signal("measured", "Измеренная температура", "°C", "output"),
    Signal("error", "Ошибка температуры", "К", "error"),
)


def _absorber_result(raw):
    signals = {k: v for k, v in raw["phase_responses"].items()}
    signals.update(component=raw["component_fraction"] * raw["profile"],
                   flow=raw["flow_fraction"] * raw["profile"])
    signals.update({k: raw[k] for k in ("control", "commanded_control", "measurement", "error")
                    if raw.get(k) is not None})
    return _result(raw, raw["model_values"], signals, ABSORBER_SIGNALS)


def _tank_result(raw):
    result = _result(raw, raw["parameters"], {s.key: raw[s.key] for s in TANK_SIGNALS if s.key in raw},
                     TANK_SIGNALS, () if raw["event"] is None else (raw["event"],))
    result["limits"] = [dict(signal="pump", value=raw["parameters"]["pump_limit"], label="Qmax"),
                        dict(signal="level", value=raw["parameters"]["height"], label="Hmax")]
    return result


def _heater_result(raw):
    result = _result(raw, raw["parameters"], {s.key: raw[s.key] for s in HEATER_SIGNALS if s.key in raw},
                     HEATER_SIGNALS, () if raw["event"] is None else (raw["event"],))
    result["limits"] = [dict(signal="power", value=raw["parameters"]["power_limit"], label="Pmax"),
                        dict(signal="temperature", value=heater.BOILING, label="Кипение")]
    return result


def _validate_heater_result(result):
    configuration = result.get("configuration")
    if not isinstance(configuration, dict) or len(result["events"]) > 1:
        raise ValueError("Результат: неверная конфигурация нагревателя.")
    native = {key: result["signals"].get(key) for key in ("temperature", "measured", "power", "commanded", "inlet", "flow")}
    native.update(model_id=heater.MODEL_ID, model_version=heater.MODEL_VERSION, time=result["time"],
                  parameters=result["parameters"], units=heater.SIGNAL_UNITS.copy(),
                  event=result["events"][0] if result["events"] else None, controller=configuration.get("controller"))
    heater.validate_run(native)
    expected = [dict(signal="power", value=result["parameters"]["power_limit"], label="Pmax"),
                dict(signal="temperature", value=heater.BOILING, label="Кипение")]
    if result.get("limits") != expected:
        raise ValueError("Результат: ограничения не соответствуют нагревателю.")


def _validate_absorber_result(result):
    from .calculations import absorption_balance
    absorption_balance(**result["parameters"])
    configuration = result.get("configuration")
    if not isinstance(configuration, dict) or configuration.get("chain") not in ("lean_gas", "rich_absorbent"):
        raise ValueError("Результат: неизвестный выход абсорбера.")


def _validate_tank_result(result):
    from .tank_state import validate_run
    from .tank import SIGNAL_UNITS
    configuration = result.get("configuration")
    if not isinstance(configuration, dict) or len(result["events"]) > 1:
        raise ValueError("Результат: неверная конфигурация бака.")
    native = dict(result["signals"], model_id="level_tank", model_version=1,
                  time=result["time"], parameters=result["parameters"], units=SIGNAL_UNITS.copy(),
                  event=result["events"][0] if result["events"] else None,
                  controller=configuration.get("controller"), metrics=result.get("metrics"),
                  sample_time=configuration.get("sample_time"), baseline_pump=configuration.get("baseline_pump"))
    validate_run(native)
    expected_limits = [dict(signal="pump", value=result["parameters"]["pump_limit"], label="Qmax"),
                       dict(signal="level", value=result["parameters"]["height"], label="Hmax")]
    if result.get("limits") != expected_limits:
        raise ValueError("Результат: ограничения не соответствуют физическому баку.")
    if "prediction" in result["signals"]:
        local = configuration.get("local_model")
        if not isinstance(local, dict) or set(local) != {"gain", "time_constant", "delay"}:
            raise ValueError("Результат: отсутствуют параметры локального прогноза.")
        if any(type(value) not in (int, float) or not np.isfinite(float(value)) for value in local.values()):
            raise ValueError("Результат: неверные параметры локального прогноза.")
        if local["gain"] <= 0 or local["time_constant"] <= 0 or local["delay"] < 0:
            raise ValueError("Результат: неверные K/T/L локального прогноза.")


ABSORBER_PARAMETERS = tuple(Parameter(key, label, unit, DEFAULT_MODEL_VALUES[key], low, high, key in ("gg", "ga"))
    for key, label, unit, low, high in (
        ("gg", "Расход газа", "кг/ч", 0., None), ("xg", "Концентрация газа", "доля", 0., 1.),
        ("ga", "Расход абсорбента", "кг/ч", 0., None), ("xa", "Концентрация абсорбента", "доля", 0., 1.),
        ("eta", "Степень извлечения", "доля", 0., 1.)))
TANK_PARAMETERS = tuple(Parameter(key, label, unit, DEFAULTS[key], low, None, key in ("area", "coefficient", "height", "pump_limit", "horizon", "sample_time"))
    for key, label, unit, low in (
        ("area", "Площадь", "м²", 0.), ("coefficient", "Коэффициент слива", "м^(5/2)/с", 0.),
        ("height", "Высота", "м", 0.), ("pump_limit", "Предел насоса", "м³/с", 0.),
        ("initial_level", "Исходный уровень", "м", 0.), ("pump", "Подача", "м³/с", 0.),
        ("withdrawal", "Отбор", "м³/с", 0.), ("step_time", "Время ступени", "с", 0.),
        ("pump_step", "Ступень подачи", "м³/с", None), ("withdrawal_step", "Ступень отбора", "м³/с", None),
        ("horizon", "Горизонт", "с", 0.), ("sample_time", "Период расчёта", "с", 0.)))

HEATER_PARAMETERS = tuple(Parameter(key, label, unit, heater.DEFAULTS[key], low, None, key in heater.POSITIVE)
    for key, label, unit, low in (
        ("mass", "Масса жидкости", "кг", 0.), ("heat_capacity", "Теплоёмкость", "кДж/(кг·К)", 0.),
        ("flow", "Расход", "кг/с", 0.), ("inlet_temperature", "Температура на входе", "°C", None),
        ("loss_coefficient", "Теплопотери UA", "кВт/К", 0.), ("ambient_temperature", "Температура среды", "°C", None),
        ("power_limit", "Предел мощности", "кВт", 0.), ("power", "Исходная мощность", "кВт", 0.),
        ("step_time", "Время ступени", "с", 0.), ("power_step", "Ступень мощности", "кВт", None),
        ("inlet_step", "Ступень температуры входа", "°C", None), ("flow_step", "Ступень расхода", "кг/с", None),
        ("sensor_delay", "Запаздывание датчика", "с", 0.), ("horizon", "Горизонт", "с", 0.),
        ("sample_time", "Период расчёта", "с", 0.)))

MODELS = ModelRegistry((
    PhysicalModel("absorber", 2, "Абсорбер", ABSORBER_PARAMETERS, ABSORBER_SIGNALS,
                  (("control", "xog"), ("control", "xna")),
                  ("balance", "dynamics", "P", "PI", "PD", "PID", "identification"), (),
                  _absorber_run, _absorber_result, "disturbances", "АБСОРБЦИЯ", "Абсорбер", _validate_absorber_result),
    PhysicalModel("level_tank", 1, "Бак", TANK_PARAMETERS, TANK_SIGNALS,
                  (("pump", "level"),), ("balance", "dynamics", "PI", "identification"), ("empty", "full"),
                  _tank_run, _tank_result, "tank", "БАК",
                  "Бак: объёмный баланс, локальная модель и PI; уровень в метрах", _validate_tank_result),
    PhysicalModel(heater.MODEL_ID, heater.MODEL_VERSION, "Нагреватель", HEATER_PARAMETERS, HEATER_SIGNALS,
                  (("power", "temperature"),), ("balance", "dynamics", "PI", "identification"), ("boiling",),
                  _heater_run, _heater_result, "heater", "НАГРЕВ",
                  "Ёмкость с нагревателем: энергетический баланс, датчик с запаздыванием и PI; температура в °C",
                  _validate_heater_result),
))


def run_model(model_id, request, *, cancel=None, progress=None):
    return MODELS.run(model_id, request, cancel=cancel, progress=progress)
