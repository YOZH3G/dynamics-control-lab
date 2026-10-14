"""Portable JSON sessions for the comparison history."""

import copy
import json
import math
import os
from pathlib import Path
import shutil

import numpy as np
from .calculations import DEFAULT_MODEL_VALUES, IMPULSE, RAMP, RECTANGLE, STEP, absorption_balance
from .scenario_store import DISTURBANCE_TYPES, normalize_scenario
from .laboratory import CORRECTION_OPTIONS, DIRECTION_OPTIONS, FASTEST_OPTIONS, normalize_lesson
from .controller_extensions import EXTENSION_DEFAULTS, extensions_from_form
from .validation import parse_disturbance, parse_nonnegative_number, parse_percentage, parse_positive_number
from .identification import validate_experiment_state, validate_experiment_origin
from .tank_state import validate_tank_state
from .physical_models import MODELS, run_model
from .model_contract import copy_data


FORMAT_VERSION = 3


def write_session(path, runs, counter, laboratory=None):
    destination = Path(path)
    payload = {
        "version": FORMAT_VERSION,
        "comparison_counter": int(counter),
        "runs": [_serialize_run(run) for run in runs],
        "laboratory": laboratory,
    }
    _runs, _counter, payload["laboratory"] = _validate_payload(payload)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if destination.exists():
            try:
                read_laboratory_session(destination)
            except ValueError:
                pass
            else:
                backup = destination.with_suffix(destination.suffix + ".bak")
                backup_temporary = backup.with_suffix(backup.suffix + ".tmp")
                shutil.copyfile(destination, backup_temporary)
                backup_temporary.replace(backup)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def read_session(path):
    runs, counter, _laboratory = read_laboratory_session(path)
    return runs, counter


def read_laboratory_session(path):
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"Не удалось прочитать учебный сеанс: {error}") from error
    return _validate_payload(payload)


def _validate_payload(payload):
    if not isinstance(payload, dict) or payload.get("version") not in (2, FORMAT_VERSION):
        raise ValueError(f"Поддерживается формат учебного сеанса версии {FORMAT_VERSION}.")
    runs = payload.get("runs")
    if not isinstance(runs, list):
        raise ValueError("Поле runs должно содержать список опытов.")
    restored = [_deserialize_run(run) for run in runs]
    if len(restored) > 6 or len({run["id"] for run in restored}) != len(restored):
        raise ValueError("Сеанс: не больше шести опытов с уникальными идентификаторами.")
    counter = payload.get("comparison_counter", len(restored))
    if isinstance(counter, bool) or not isinstance(counter, int) or counter < len(restored):
        raise ValueError("Сеанс: счётчик опытов должен быть целым числом не меньше размера истории.")
    return restored, counter, validate_laboratory(payload.get("laboratory"))


def _serialize_run(run):
    serialized = copy_data(run)
    serialized["time"] = _serialize_signal(run.get("time"), "time")
    serialized["response"] = _serialize_signal(run.get("response"), "response")
    return serialized


def _deserialize_run(run):
    if not isinstance(run, dict) or run.get("model_version") != 2:
        raise ValueError("Несовместимая версия модели опыта; старые сеансы не поддерживаются.")
    values = run.get("model_values")
    if not isinstance(values, dict) or set(values) != set(DEFAULT_MODEL_VALUES):
        raise ValueError("Опыт должен содержать параметры согласованной модели.")
    values = {key: _finite_number(value, key) for key, value in values.items()}
    absorption_balance(**values)
    if not isinstance(run, dict):
        raise ValueError("Опыт должен быть объектом JSON.")
    required_text = ("id", "name", "chain", "controller_type")
    for key in required_text:
        if not isinstance(run.get(key), str) or not run[key].strip():
            raise ValueError(f"Опыт: поле {key} должно быть непустым текстом.")
    if run["chain"] not in ("lean_gas", "rich_absorbent") or run["controller_type"] not in ("—", "P", "PI", "PID", "PD"):
        raise ValueError("Опыт: неизвестная цепь или тип регулятора.")
    restored = copy_data(run)
    restored["experiment_origin"] = validate_experiment_origin(run.get("experiment_origin"))
    restored["model_values"] = values
    restored["time"] = _deserialize_signal(run.get("time"), "time")
    restored["response"] = _deserialize_signal(run.get("response"), "response")
    if restored["time"].shape != restored["response"].shape:
        raise ValueError("Опыт: массивы time и response должны иметь одинаковую длину.")
    if np.any(np.diff(restored["time"]) <= 0):
        raise ValueError("Время опыта должно строго возрастать.")
    if restored["time"][0] < 0 or np.any((restored["response"] < 0) | (restored["response"] > 1)):
        raise ValueError("Опыт: время неотрицательно, концентрация находится в диапазоне 0–1.")
    for key in ("time_constant", "delay", "maximum_deviation", "static_error"):
        restored[key] = _finite_number(run.get(key), key)
    if restored["time_constant"] <= 0 or restored["delay"] < 0 or restored["maximum_deviation"] < 0:
        raise ValueError("Опыт: T положительно, L и максимальное отклонение неотрицательны.")
    relative = run.get("relative_deviation")
    restored["relative_deviation"] = None if relative is None else _finite_number(relative, "relative_deviation")
    settling = run.get("settling_duration")
    restored["settling_duration"] = None if settling is None else _finite_number(settling, "settling_duration")
    for key in ("steady_state", "final_error", "iae", "saturation_duration"):
        value = run.get(key)
        restored[key] = None if value is None else _finite_number(value, key)
    for key in ("relative_deviation", "settling_duration", "iae", "saturation_duration"):
        if restored[key] is not None and restored[key] < 0:
            raise ValueError(f"Опыт: {key} не может быть отрицательным.")
    if restored["steady_state"] is not None and not 0 <= restored["steady_state"] <= 1:
        raise ValueError("Опыт: теоретическая концентрация должна быть в диапазоне 0–1.")
    restored["final_value"] = float(restored["response"][-1])
    status = run.get("settling_status", "Нет сохранённых данных об установлении")
    if not isinstance(status, str) or not status.strip():
        raise ValueError("Опыт: статус установления должен быть непустым текстом.")
    restored["settling_status"] = status
    input_state = run.get("input_state")
    if input_state is not None and not isinstance(input_state, dict):
        raise ValueError("Опыт: input_state должен быть объектом JSON.")
    if input_state is not None:
        validate_input_state(input_state)
    return restored


def validate_input_state(state, *, draft=False):
    """Validate the complete form snapshot before any widget is changed."""
    if not isinstance(state, dict) or state.get("model_version") != 2:
        raise ValueError("Снимок: нужны параметры модели версии 2.")
    for key in ("component_enabled", "flow_enabled", "controller_enabled"):
        if not isinstance(state.get(key), bool):
            raise ValueError(f"Снимок: {key} должно быть логическим значением.")
    units = state.get("disturbance_units", "fraction")
    if units not in ("fraction", "percent"):
        raise ValueError("Снимок: неизвестные единицы возмущения.")
    fields = ("component_value", "flow_value", "disturbance_type", "start_time", "simulation_duration",
              "effect_duration", "time_constant", "delay", "controller_type", "proportional_gain",
              "integral_time", "derivative_time", "control_limit", "setpoint")
    for key in fields:
        if not isinstance(state.get(key), str):
            raise ValueError(f"Снимок: {key} должно быть текстом поля.")
    if state["disturbance_type"] not in DISTURBANCE_TYPES or state["controller_type"] not in ("P", "PI", "PD", "PID"):
        raise ValueError("Снимок: неизвестный вид воздействия или регулятора.")
    if state.get("chain") not in ("lean_gas", "rich_absorbent"):
        raise ValueError("Снимок: неизвестная цепь управления.")
    values = state.get("model_values")
    if not isinstance(values, dict) or set(values) != set(DEFAULT_MODEL_VALUES):
        raise ValueError("Снимок: нужны все пять параметров модели.")
    absorption_balance(**{key: _finite_number(value, key) for key, value in values.items()})
    for key in EXTENSION_DEFAULTS:
        if key in state and not isinstance(state[key], str):
            raise ValueError(f"Снимок: {key} должно быть текстом поля.")
    if draft:
        return copy.deepcopy(state)
    scenario = {
        "name": "Сеанс", "description": "Сохранённые параметры", "chain": state.get("chain"),
        "model_values": state.get("model_values"), "disturbance_type": state["disturbance_type"],
        "component": parse_disturbance(state["component_value"], units) if state["component_enabled"] else 0,
        "flow": parse_disturbance(state["flow_value"], units) if state["flow_enabled"] else None,
        "start_time": parse_nonnegative_number(state["start_time"]),
        "simulation_duration": parse_positive_number(state["simulation_duration"]),
        "effect_duration": (1 if state["disturbance_type"] == "Ступенчатое"
                            else parse_positive_number(state["effect_duration"])),
        "time_constant": parse_positive_number(state["time_constant"]),
        "delay": parse_nonnegative_number(state["delay"]), "controller": None,
    }
    if state["controller_enabled"]:
        kind = state["controller_type"]
        scenario["controller"] = {
            **extensions_from_form(state, kind),
            "type": kind, "gain": parse_nonnegative_number(state["proportional_gain"]),
            "integral_time": parse_positive_number(state["integral_time"]) if "I" in kind else 1,
            "derivative_time": parse_nonnegative_number(state["derivative_time"]) if "D" in kind else 0,
            "control_limit": parse_percentage(state["control_limit"]),
            "setpoint": parse_percentage(state["setpoint"]),
        }
    return normalize_scenario(scenario)


def validate_laboratory(laboratory):
    if laboratory is None:
        return None
    if not isinstance(laboratory, dict):
        raise ValueError("Лабораторная должна быть объектом JSON.")
    restored = copy_data(laboratory)
    restored["tank"] = validate_tank_state(restored.get("tank"))
    restored.setdefault("selected_model", "absorber")
    model = MODELS.get(restored["selected_model"])
    results = restored.get("model_results", {})
    if not isinstance(results, dict):
        raise ValueError("Лабораторная: неверные результаты моделей.")
    restored["model_results"] = {key: MODELS.validate(value) for key, value in results.items()}
    if any(key != value["model_id"] for key, value in restored["model_results"].items()):
        raise ValueError("Лабораторная: идентификатор результата не соответствует модели.")
    if (restored.get("page") != "model_result"
            and (model.entry_page == "tank") != (restored.get("page") == "tank")):
        raise ValueError("Лабораторная: выбранная модель не соответствует разделу.")
    restored["identification"] = validate_experiment_state(restored.get("identification"))
    validate_input_state(restored.get("input_state"), draft=True)
    restored["scenario"] = normalize_scenario(restored.get("scenario"))
    restored["lesson"] = normalize_lesson(restored.get("lesson"))
    for key in ("learning_mode", "assignment_enabled"):
        if not isinstance(restored.get(key), bool):
            raise ValueError(f"Лабораторная: {key} должно быть логическим значением.")
    for key, low, high in (("variant", 1, 30), ("learning_step", 1, 6), ("attempts", 0, 20)):
        value = restored.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"Лабораторная: {key} вне диапазона {low}–{high}.")
    for key in ("student_name", "conclusion", "scenario_name", "active_scenario", "page"):
        if not isinstance(restored.get(key), str):
            raise ValueError(f"Лабораторная: {key} должно быть текстом.")
    for key, maximum in (("steady_tolerance_percent", 100), ("steady_absolute_tolerance", 1)):
        number = _finite_number(restored.get(key), key)
        if not 0 <= number <= maximum:
            raise ValueError(f"Лабораторная: {key} вне допустимого диапазона.")
    if restored["attempts"] > restored["lesson"]["attempt_limit"]:
        raise ValueError("Лабораторная: число попыток превышает лимит задания.")
    if not isinstance(restored.get("selected_runs"), list) or any(
            not isinstance(value, str) for value in restored["selected_runs"]):
        raise ValueError("Лабораторная: выбранные опыты должны быть списком идентификаторов.")
    if restored["page"] not in ("disturbances", "dynamics", "results", "comparison", "controller",
                                "sensitivity", "tuning_map", "scenarios", "export", "identification", "tank", "model_result"):
        raise ValueError("Лабораторная: неизвестный раздел.")
    draft = restored.get("prediction_fields")
    if not isinstance(draft, dict):
        raise ValueError("Лабораторная: нет полей прогноза.")
    for key, options in (("direction", DIRECTION_OPTIONS), ("fastest", FASTEST_OPTIONS),
                         ("correction", CORRECTION_OPTIONS)):
        if draft.get(key) not in ("", *options):
            raise ValueError(f"Лабораторная: неизвестный ответ {key}.")
    if not isinstance(draft.get("steady"), str):
        raise ValueError("Лабораторная: прогноз концентрации должен быть текстом поля.")
    restored["evaluation"] = _validate_evaluation(restored.get("evaluation"))
    calculation = restored.get("last_calculation")
    if calculation is not None:
        if not isinstance(calculation, dict) or not isinstance(calculation.get("title"), str):
            raise ValueError("Лабораторная: некорректный снимок расчёта.")
        validate_input_state(calculation.get("input_state"))
        validate_experiment_origin(calculation.get("experiment_origin"))
        calculation["lesson"] = normalize_lesson(calculation.get("lesson"))
        calculation["evaluation"] = _validate_evaluation(calculation.get("evaluation"))
        prediction = calculation.get("prediction")
        if prediction is not None:
            if not isinstance(prediction, dict):
                raise ValueError("Расчёт: прогноз должен быть объектом JSON.")
            for key, options in (("direction", DIRECTION_OPTIONS), ("fastest", FASTEST_OPTIONS),
                                 ("correction", CORRECTION_OPTIONS)):
                if prediction.get(key) not in options:
                    raise ValueError(f"Расчёт: неизвестный ответ {key}.")
            value = _finite_number(prediction.get("steady"), "steady")
            if not 0 <= value <= 1:
                raise ValueError("Расчёт: прогноз концентрации должен быть в диапазоне 0–1.")
        restored["last_calculation"] = {key: copy_data(calculation.get(key))
                                        for key in ("input_state", "title", "lesson", "prediction", "evaluation", "experiment_origin")}
    return restored


def _validate_evaluation(evaluation):
    if evaluation is None:
        return None
    if not isinstance(evaluation, dict):
        raise ValueError("Оценка должна быть объектом JSON.")
    score, total = (_finite_number(evaluation.get(key), key) for key in ("score", "total"))
    evaluation = copy.deepcopy(evaluation)
    evaluation.update(score=score, total=total)
    if not 0 <= score <= total:
        raise ValueError("Оценка: балл должен быть между нулём и максимумом.")
    lines, criteria = evaluation.get("lines"), evaluation.get("criteria")
    if not isinstance(lines, list) or any(not isinstance(line, str) for line in lines):
        raise ValueError("Оценка: обратная связь должна быть списком строк.")
    if not isinstance(criteria, list):
        raise ValueError("Оценка: критерии должны быть списком.")
    keys = []
    for criterion in criteria:
        if not isinstance(criterion, dict) or not isinstance(criterion.get("passed"), bool):
            raise ValueError("Оценка: некорректный критерий.")
        for key in ("key", "label", "answer"):
            if not isinstance(criterion.get(key), str):
                raise ValueError(f"Оценка: {key} должно быть текстом.")
        keys.append(criterion["key"])
        if criterion["key"] not in ("direction", "steady", "fastest", "correction", "controller_settings"):
            raise ValueError("Оценка: неизвестный критерий.")
        points, maximum = (_finite_number(criterion.get(key), key) for key in ("points", "maximum"))
        if not 0 <= points <= maximum or (not criterion["passed"] and points != 0):
            raise ValueError("Оценка: балл критерия вне диапазона.")
        criterion.update(points=points, maximum=maximum)
    if len(keys) != len(set(keys)):
        raise ValueError("Оценка: критерии не должны повторяться.")
    if (not math.isclose(sum(item["points"] for item in criteria), score, abs_tol=1e-9)
            or not math.isclose(sum(item["maximum"] for item in criteria), total, abs_tol=1e-9)):
        raise ValueError("Оценка: сумма критериев не совпадает с итогом.")
    return copy.deepcopy(evaluation)


def restore_calculation(snapshot):
    if snapshot is None:
        return None
    scenario = validate_input_state(snapshot["input_state"])
    kinds = {"Ступенчатое": STEP, "Импульсное": IMPULSE,
             "Временное прямоугольное": RECTANGLE, "Плавно нарастающее": RAMP}
    dynamics = {"kind": kinds[scenario["disturbance_type"]],
                **{key: scenario[key] for key in ("start_time", "simulation_duration", "effect_duration",
                                                "time_constant", "delay")}}
    controller = scenario["controller"]
    if controller is not None:
        controller = {**{key: scenario["controller"][key] for key in EXTENSION_DEFAULTS},
            "controller_type": controller["type"], "controller_gain": controller["gain"],
                      **{key: controller[key] for key in ("integral_time", "derivative_time", "control_limit", "setpoint")}}
    result = run_model("absorber", dict(chain=scenario["chain"], parameters=scenario["model_values"],
                      component=scenario["component"] or 0, flow=scenario["flow"] or 0,
                      dynamics=dynamics, controller=controller))
    result.update({key: copy_data(snapshot.get(key))
                   for key in ("input_state", "title", "lesson", "prediction", "evaluation", "experiment_origin")})
    result["disturbance_type"] = scenario["disturbance_type"]
    return result


def _serialize_signal(values, label):
    signal = np.asarray(values, dtype=float)
    _validate_signal(signal, label)
    return signal.tolist()


def _deserialize_signal(values, label):
    try:
        signal = np.asarray(values, dtype=float)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Опыт: {label} должен быть числовым сигналом.") from error
    _validate_signal(signal, label)
    return signal


def _validate_signal(signal, label):
    if signal.ndim != 1 or not signal.size or not np.isfinite(signal).all():
        raise ValueError(f"Опыт: {label} должен быть непустым конечным одномерным сигналом.")


def _finite_number(value, label):
    if isinstance(value, bool):
        raise ValueError(f"Опыт: {label} должно быть числом.")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Опыт: {label} должно быть числом.") from error
    if not math.isfinite(number):
        raise ValueError(f"Опыт: {label} должно быть конечным числом.")
    return number
