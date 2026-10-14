"""Validate tank snapshots before any session changes the live interface."""

import copy
import numpy as np

from .tank import DEFAULTS, MODEL_ID, MODEL_VERSION, SIGNAL_UNITS, validate_inputs


FIELD_DEFAULTS = {k: str(v) for k, v in DEFAULTS.items()}
FIELD_DEFAULTS.update(mode="Равновесие", setpoint="1.4", gain="0.015", integral_time="200",
                      noise="0", seed="42", title="Опыт с баком", prediction="", conclusion="")
VIEWS = ("Свободный отклик", "Идентификация", "Остаток", "Проверка", "PI", "Ошибки идентификации")


def signals(record, names, minimum=2):
    arrays = []
    if not isinstance(record, dict):
        raise ValueError("Бак: результат должен быть объектом.")
    for name in names:
        value = record.get(name)
        if not isinstance(value, list) or any(isinstance(v, bool) or not isinstance(v, (int,float)) for v in value):
            raise ValueError("Бак: сигналы должны быть числовыми списками.")
        a = np.asarray(value, dtype=float)
        if a.ndim != 1 or not minimum <= a.size <= 200000 or not np.isfinite(a).all():
            raise ValueError("Бак: неверный размер или неконечные сигналы.")
        arrays.append(a)
    if len({len(a) for a in arrays}) != 1 or np.any(np.diff(arrays[0]) <= 0):
        raise ValueError("Бак: нужны согласованные сигналы и возрастающее время.")
    return arrays


def finite_number(value, name, minimum=None):
    if isinstance(value,bool) or not isinstance(value,(float,int)) or not np.isfinite(value) or (minimum is not None and value < minimum):
        raise ValueError(f"Бак: недопустимое число {name}.")
    return float(value)


def validate_run(run):
    if not isinstance(run,dict) or run.get("model_id") != MODEL_ID or run.get("model_version") != MODEL_VERSION:
        raise ValueError("Бак: неизвестная версия физической модели.")
    p = validate_inputs(run.get("parameters"))
    if not {"event","controller","baseline_pump","sample_time"} <= run.keys():
        raise ValueError("Бак: неполный результат расчёта.")
    if finite_number(run["sample_time"],"период",0) != p["sample_time"]:
        raise ValueError("Бак: период не соответствует параметрам.")
    if finite_number(run["baseline_pump"],"базовая подача",0) > p["pump_limit"]:
        raise ValueError("Бак: базовая подача превышает предел насоса.")
    time, h, q, command, d = signals(run,("time","level","pump","commanded","withdrawal"))
    if (np.any((h < 0) | (h > p["height"])) or np.any((q < 0) | (q > p["pump_limit"]))
            or np.any(command < 0) or np.any(d < 0) or time[0] != 0 or time[-1] > p["horizon"]+1e-8):
        raise ValueError("Бак: сигнал вне физических границ.")
    if run.get("units") != SIGNAL_UNITS:
        raise ValueError("Бак: неверные единицы результата.")
    event = run.get("event")
    if event is not None:
        if not isinstance(event,dict) or event.get("kind") not in ("empty","full"):
            raise ValueError("Бак: неизвестное физическое событие.")
        if event.get("time") != time[-1] or event.get("level") != h[-1] or not np.isclose(h[-1],0 if event["kind"] == "empty" else p["height"],atol=1e-10):
            raise ValueError("Бак: событие не совпадает с концом опыта.")
    controller = run.get("controller")
    if controller is not None:
        if not isinstance(controller,dict):
            raise ValueError("Бак: неверные настройки PI.")
        finite_number(controller.get("gain"),"Kp",0)
        ti = finite_number(controller.get("integral_time"),"Ti",0)
        sp = finite_number(controller.get("setpoint"),"задание",0)
        if ti == 0 or not 0 < sp < p["height"]:
            raise ValueError("Бак: неверное Ti или задание.")
        _, error = signals(run,("time","error"))
        if not np.allclose(error,sp-h,rtol=1e-10,atol=1e-10):
            raise ValueError("Бак: ошибка PI не соответствует уровню.")
        m = run.get("metrics")
        if not isinstance(m,dict) or not isinstance(m.get("reachable"),bool) or m.get("status") not in ("physical_stop","unreachable","settled","horizon"):
            raise ValueError("Бак: неверные показатели PI.")
        finite_number(m.get("final_error"),"ошибка")
        for key in ("iae","overshoot_percent","saturation_duration"):
            finite_number(m.get(key),key,0)
        if m.get("settling_time") is not None:
            if finite_number(m["settling_time"],"установление",0) > time[-1] or event or not m["reachable"]:
                raise ValueError("Бак: неверное время установления.")
    if "prediction" in run:
        signals(run,("time","prediction"))


def validate_origin(origin):
    if (not isinstance(origin,dict) or origin.get("model_id") != MODEL_ID
            or origin.get("model_version") != MODEL_VERSION
            or not isinstance(origin.get("source"),str)
            or not isinstance(origin.get("columns"),list) or len(origin["columns"]) != 3
            or any(not isinstance(c,str) or not c for c in origin["columns"])
            or len(set(origin["columns"])) != 3
            or not isinstance(origin.get("units"),list) or len(origin["units"]) != 3
            or origin["units"][0] not in ("с","мин")
            or origin["units"][1] not in ("м³/с","л/с")
            or origin["units"][2] not in ("м","см")
            or not isinstance(origin.get("fields"),dict) or set(origin["fields"]) != set(FIELD_DEFAULTS)
            or any(not isinstance(v,str) for v in origin["fields"].values())
            or isinstance(origin.get("revision"),bool) or not isinstance(origin.get("revision"),int)
            or origin["revision"] < 0):
        raise ValueError("Бак: неполный источник результата.")


def validate_tank_state(state):
    if state is None:
        return None
    if not isinstance(state,dict) or state.get("model_id") != MODEL_ID or state.get("model_version") != MODEL_VERSION:
        raise ValueError("Бак: неизвестная версия сеанса модели.")
    state = copy.deepcopy(state)
    if not {"result","fit","validation","pi"} <= state.keys():
        raise ValueError("Бак: неполный снимок сеанса.")
    fields = state.get("fields")
    if not isinstance(fields,dict) or set(fields) != set(FIELD_DEFAULTS) or any(not isinstance(v,str) for v in fields.values()):
        raise ValueError("Бак: неверные поля формы.")
    if fields["mode"] not in ("Равновесие","Заданный уровень") or state.get("view") not in VIEWS:
        raise ValueError("Бак: неизвестный режим страницы.")
    revision = state.get("revision")
    if isinstance(revision,bool) or not isinstance(revision,int) or revision < 0:
        raise ValueError("Бак: неверная версия исходных данных.")
    source = state.get("source")
    if not isinstance(source,str):
        raise ValueError("Бак: неверный источник данных.")
    headers, rows = state.get("headers"),state.get("rows")
    if (not isinstance(headers,list) or any(not isinstance(h,str) or not h for h in headers)
            or len(set(headers)) != len(headers) or (headers and len(headers)<3)
            or not isinstance(rows,list) or len(rows)>200000 or any(not isinstance(row,list) or len(row)!=len(headers)
                or any(not isinstance(v,str) for v in row) for row in rows)):
        raise ValueError("Бак: неверные исходные строки CSV.")
    if (not isinstance(state.get("columns"),list) or len(state["columns"]) != 3
            or any(c and c not in headers for c in state["columns"])):
        raise ValueError("Бак: неверные столбцы CSV.")
    if state.get("units") not in (["с","м³/с","м"],["мин","м³/с","м"],["с","л/с","м"],["мин","л/с","м"],
                                  ["с","м³/с","см"],["мин","м³/с","см"],["с","л/с","см"],["мин","л/с","см"]):
        raise ValueError("Бак: неверные единицы CSV.")
    if state.get("delimiter") not in (";",",","табуляция") or not isinstance(state.get("same_signals"),bool):
        raise ValueError("Бак: неверные настройки импорта.")
    for key in ("result","fit","validation","pi"):
        record = state.get(key)
        if record is None:
            continue
        if not isinstance(record,dict) or not isinstance(record.get("revision"),int) or isinstance(record["revision"],bool) or not 0 <= record["revision"] <= revision:
            raise ValueError("Бак: неверное происхождение результата.")
        if key == "result":
            validate_run(record)
        elif key in ("fit","validation"):
            signals(record,("time","input","output","model","residual"),17)
            from .experiment_control import model_parameters
            k, t, delay = model_parameters(record)
            if k <= 0 or not np.allclose(np.asarray(record["output"])-record["model"],record["residual"],atol=1e-10):
                raise ValueError("Бак: некорректная локальная модель.")
            for name in ("baseline","step_time","rmse","normalized_rmse"):
                finite_number(record.get(name),name,0)
            if not isinstance(record.get("source"),str) or not isinstance(record.get("origin"),dict):
                raise ValueError("Бак: нет источника идентификации.")
            validate_origin(record["origin"])
            from .identification import validate_step_quality, validate_uncertainty
            validate_step_quality(record)
            if "uncertainty" in record:
                if key != "fit":
                    raise ValueError("Бак: интервалы относятся только к подгонке.")
                validate_uncertainty(record["uncertainty"], len(record["time"]))
        else:
            configuration=record.get("configuration")
            if (not isinstance(configuration,dict) or set(configuration) != {"gain","integral_time","setpoint"}
                    or any(not isinstance(v,str) for v in configuration.values())):
                raise ValueError("Бак: отсутствует снимок настроек сравнения.")
            cases = record.get("cases")
            if not isinstance(cases,list) or not 1 <= len(cases) <= 8:
                raise ValueError("Бак: неверное число сравнений PI.")
            for case in cases:
                validate_run(case)
                if not isinstance(case.get("name"),str):
                    raise ValueError("Бак: нет названия опыта PI.")
                if case["controller"] is None:
                    raise ValueError("Бак: в сравнении отсутствует регулятор PI.")
            if not isinstance(record.get("origin"),dict):
                raise ValueError("Бак: нет источника настроек PI.")
            validate_origin(record["origin"])
    from .tank_sensitivity import FIELD_DEFAULTS as SENSITIVITY_DEFAULTS, validate_result
    state.setdefault("sensitivity_fields", dict(SENSITIVITY_DEFAULTS))
    state.setdefault("sensitivity", None)
    state.setdefault("selected_sensitivity", 0)
    sensitivity_fields = state["sensitivity_fields"]
    if (not isinstance(sensitivity_fields, dict) or set(sensitivity_fields) != set(SENSITIVITY_DEFAULTS)
            or any(not isinstance(v, str) for v in sensitivity_fields.values())):
        raise ValueError("Бак: неверные поля ошибок идентификации.")
    selected = state["selected_sensitivity"]
    result = state["sensitivity"]
    if type(selected) is not int or selected < 0 or (result is None and selected != 0):
        raise ValueError("Бак: неверный выбранный опыт чувствительности.")
    if result is not None:
        validate_result(result)
        if type(result.get("revision")) is not int or not 0 <= result["revision"] <= revision or selected >= len(result["cases"]):
            raise ValueError("Бак: неверный снимок ошибок идентификации.")
        validate_origin(result.get("origin"))
        if result["origin"]["revision"] != result["revision"]:
            raise ValueError("Бак: источник не соответствует версии опыта ошибок.")
        configuration = result.get("configuration")
        from .tank_sensitivity import parse_fields
        if (not isinstance(configuration, dict) or set(configuration) != {"fields", "controller"}
                or not isinstance(configuration["controller"], dict) or set(configuration["controller"]) != {"gain", "integral_time", "setpoint"}
                or any(not isinstance(v, str) for v in configuration["controller"].values())):
            raise ValueError("Бак: неполная конфигурация ошибок идентификации.")
        grid, requirements = parse_fields(configuration["fields"])
        if grid != result["grid"] or requirements != result["requirements"]:
            raise ValueError("Бак: сетка не соответствует конфигурации.")
        try:
            controller = {k: float(v.replace(",", ".")) for k, v in configuration["controller"].items()}
        except ValueError as error:
            raise ValueError("Бак: неверный исходный PI в снимке.") from error
        if controller != result["controls"][0]["run"]["controller"]:
            raise ValueError("Бак: исходный PI не соответствует конфигурации.")
    return state
