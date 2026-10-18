import csv
import base64
import html
import textwrap
from io import BytesIO
from pathlib import Path

import numpy as np
from matplotlib.figure import Figure

from .controller_extensions import EXTENSION_FIELDS
from .laboratory import format_protocol
from .simulation import LEAN_GAS
from .plotting import adaptive_legend


def write_experiment_report(path, state, format_name):
    """Export fit, independent validation and controller snapshots together."""
    from .identification import validate_experiment_state, quality_text, uncertainty_text
    state = validate_experiment_state(state)
    fit = state["result"]
    if fit is None:
        raise ValueError("Нет результата идентификации для протокола.")
    metadata = state.get("metadata", {})
    labels = dict(title="Название", description="Источник", input_role="Вход", output_role="Выход", regime="Рабочий режим",
                  comment="Комментарий", prediction="Прогноз", explanation="Объяснение остатка", conclusion="Вывод")
    sections = [("Эксперимент", [f"{labels.get(key, key)}: {value}" for key, value in metadata.items()]),
                ("Исходные данные", [f"Источник: {fit['source']}", f"Столбцы: {fit['columns']}",
                    f"Единицы: {fit['units']}", f"Версия данных подгонки: {fit.get('origin', {}).get('data_revision', 0)}",
                    "Результат актуален." if state["result_current"] else "Результат относится к прежнему выбору данных."]),
                ("Подгонка", [f"K={fit['gain']:.8g}; T={fit['time_constant']:.8g} с; L={fit['delay']:.8g} с",
                    f"RMSE всей записи={fit['rmse']:.8g}; относительная ошибка={fit['normalized_rmse']*100:.6g}%",
                    "Подгонка к одной ступени не подтверждает модель за пределами исходного режима.", quality_text(fit)]
                    + ([uncertainty_text(fit["uncertainty"])] if fit.get("uncertainty") else []))]
    figures = []
    figure = Figure(figsize=(9, 8))
    axes = figure.subplots(3, 1)
    for axis, label in zip(axes, ("Вход, приведённые единицы", "Выход, приведённые единицы", "Остаток")):
        axis.set_ylabel(label)
    axes[0].plot(fit["time"], fit["input"], label="Вход")
    axes[1].plot(fit["time"], fit["output"], label="Эксперимент")
    axes[1].plot(fit["time"], fit["model"], label="Подгонка")
    axes[2].plot(fit["time"], fit["residual"], label="Остаток")
    figures.append(figure)
    validation = state.get("validation")
    if validation is None:
        sections.append(("Независимая проверка", ["Оценена только подгонка к исходной записи."]))
    else:
        sections.append(("Независимая проверка", [f"Источник: {validation['source']}",
            f"K={validation['gain']:.8g}; T={validation['time_constant']:.8g}; L={validation['delay']:.8g}",
            f"RMSE всей записи={validation['rmse']:.8g}; ошибка={validation['normalized_rmse']*100:.6g}%",
            f"Версия идентификации: {validation['origin']['result_revision']}", validation["warning"],
            "Параметры зафиксированы до загрузки проверочной записи.", quality_text(validation)]))
        figure = Figure(figsize=(9, 8))
        axes = figure.subplots(3, 1)
        for axis, label in zip(axes, ("Вход, приведённые единицы", "Выход, приведённые единицы", "Остаток")):
            axis.set_ylabel(label)
        axes[0].plot(validation["time"], validation["input"], label="Проверочный вход")
        axes[1].plot(validation["time"], validation["output"], label="Проверочный эксперимент")
        axes[1].plot(validation["time"], validation["model"], label="Фиксированная модель")
        axes[2].plot(validation["time"], validation["residual"], label="Остаток проверки")
        figures.append(figure)
    control = state.get("pi_result")
    if control is not None:
        p = control["model"]
        lines = [f"Модель: K={p['gain']:.8g}; T={p['time_constant']:.8g} с; L={p['delay']:.8g} с", f"Исходная η={control['eta0']:.8g}; задание={control['setpoint']:.8g}",
                 f"Назначение сигналов: {control['input_role']} → {control['output_role']}",
                 f"Версия идентификации: {control['origin']['result_revision']}",
                 "Проверка выполнена в симуляции, оборудование не подключалось."]
        figure = Figure(figsize=(9, 8))
        axes = figure.subplots(3, 1)
        for axis, label in zip(axes, (control["output_role"], "Ошибка, доля", "η, доля")):
            axis.set_ylabel(label)
        for run in control["runs"]:
            m = run["metrics"]
            lines.append(f"{run['name']}: Kp={run['gain']:.8g}; Ti={run['integral_time']:.8g}; "
                         f"ошибка={m['final_error']:.8g}; IAE={m['iae']:.8g}; перерегулирование={m['overshoot_percent']:.6g}%; "
                         f"установление={_format_optional_number(m['settling_time'])} с; насыщение={m['saturation_duration']:.6g} с; "
                         f"задание {'достижимо' if m['reachable'] else 'недостижимо'}")
            if m.get("output_out_of_range"):
                lines.append("Выход модели вне диапазона 0…1: требуется проверить её применимость.")
            for axis, key in zip(axes, ("output", "error", "control")):
                axis.plot(run["time"], run[key], label=run["name"])
        axes[0].axhline(control["setpoint"], color="gray", linestyle="--", label="Задание")
        sections.append(("PI-регулирование", lines))
        figures.append(figure)
    for figure in figures:
        figure.subplots_adjust(left=.14, right=.98, bottom=.08, top=.97, hspace=.5)
        for axis in figure.axes:
            axis.set_xlabel("Время, с")
            axis.legend()
            axis.grid(alpha=.2)
    for key in ("validation", "pi_result"):
        record = state.get(key)
        if record is not None and (record["origin"]["data_revision"] != state.get("data_revision", 0)
                or record["origin"]["result_revision"] != state.get("result_revision", 0)):
            sections.append(("Снимок прежнего опыта", [f"{key}: исходные данные или идентификация изменились. Показаны сохранённые результаты."]))
    title = metadata.get("title") or "Эксперимент и проверка регулятора"
    block = state.get("absorber_sensitivity", {})
    if block.get("result") is not None:
        from .absorber_sensitivity_exporting import report_parts
        snapshot = dict(block["result"], exported_stale=(block.get("stale", False) or block["result"]["fields"] != block["fields"]
            or block["result"]["origin"]["data_revision"] != state.get("data_revision", 0)
            or block["result"]["origin"]["result_revision"] != state.get("result_revision", 0)))
        parts, error_figures = report_parts(snapshot, block["selected"])
        sections.extend(("Абсорбер: "+name, text.split("\n")) for name, text in parts)
        figures.extend(error_figures)
    if format_name == "pdf":
        return _write_pdf(path, [*_text_pages(title, [(name, "\n".join(lines)) for name, lines in sections]), *figures])
    if format_name != "html":
        raise ValueError("Поддерживаются HTML и PDF.")
    markup = "".join(f"<h2>{html.escape(name)}</h2><ul>" + "".join(f"<li>{html.escape(line)}</li>" for line in lines) + "</ul>" for name, lines in sections)
    document = f'<!doctype html><html lang="ru"><meta charset="utf-8"><title>{html.escape(title)}</title><style>body{{font:16px sans-serif;max-width:1000px;margin:32px auto}}img{{max-width:100%}}</style><h1>{html.escape(title)}</h1>{markup}'
    document += _figures_markup(figures, "Результаты экспериментальной работы") + "</html>"
    Path(path).write_text(document, encoding="utf-8")
    return Path(path)


def write_experiment_csv(path, state):
    from .identification import validate_experiment_state
    state = validate_experiment_state(state)
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(("Этап", "Время, с", "Вход", "Эксперимент", "Модель", "Остаток или ошибка", "Управление η"))
        for key, name in (("result", "Подгонка"), ("validation", "Независимая проверка")):
            record = state.get(key)
            if record is not None:
                writer.writerows((name, *values, "") for values in zip(*(record[k] for k in ("time", "input", "output", "model", "residual"))))
        if state.get("pi_result") is not None:
            for run in state["pi_result"]["runs"]:
                writer.writerows((run["name"], t, "", "", y, e, u) for t, y, e, u in zip(*(run[k] for k in ("time", "output", "error", "control"))))
        block = state.get("absorber_sensitivity", {})
        if block.get("result") is not None:
            from .tank_sensitivity_exporting import case_label
            result = block["result"]
            output = "xog" if result["request"]["chain"] == "lean_gas" else "xna"
            for case in result["controls"]+result["cases"]:
                run = case["run"]
                writer.writerows(("Абсорбер: "+case_label(case), t, "", "", y, e, u)
                    for t, y, e, u in zip(run["time"], *(run["signals"][key] for key in (output, "error", "control"))))
    return Path(path)


def save_graphs(signal_figure, response_figure, selected_path):
    base = Path(selected_path)
    stem = base.with_suffix("")
    signal_path = stem.with_name(f"{stem.name}_signals.png")
    response_path = stem.with_name(f"{stem.name}_response.png")
    signal_figure.savefig(signal_path, dpi=160, bbox_inches="tight")
    response_figure.savefig(response_path, dpi=160, bbox_inches="tight")
    return signal_path, response_path


def write_csv(selected_path, result):
    columns = [
        ("Время, с", result["time"]),
        ("Профиль воздействия", result["profile"]),
        ("Цель: совместное воздействие, доля", result["targets"]["Совместное воздействие"]),
    ]
    columns.extend(
        (f"Отклик: {label}, доля", values)
        for label, values in result["responses"].items()
    )
    columns.extend((f"Концентрация {key}, доля", values)
                   for key, values in result["phase_responses"].items())
    if result["controlled_response"] is not None:
        columns.extend((
            ("Регулируемый выход, доля", result["controlled_response"]),
            ("Ошибка e(t), доля", result["error"]),
            ("Степень извлечения η(t), доля", result["control"]),
        ))
    for key, label in (("measurement", "Измеренный выход, доля"),
                       ("measurement_error", "Измеренная ошибка, доля"),
                       ("commanded_control", "Командная η, доля"),
                       ("rate_limited", "Ограничение скорости η, 0/1")):
        if result.get(key) is not None:
            columns.append((label, result[key].astype(int) if key == "rate_limited" else result[key]))
    path = Path(selected_path)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file, delimiter=";")
        writer.writerow(label for label, _values in columns)
        writer.writerows(zip(*(values for _label, values in columns)))
    return path


def build_protocol(result, title):
    controller = result["controller"]
    parameters = [
        ("Цепь", "Обеднённый газ" if result["chain"] == LEAN_GAS else "Насыщенный абсорбент"),
        ("Возмущение состава", f"{result['component_fraction'] * 100:+.1f}%"),
        ("Возмущение расхода", f"{result['flow_fraction'] * 100:+.1f}%"),
        ("Вид воздействия", result["disturbance_type"]),
        ("Начало воздействия", f"{result['dynamics']['start_time']:g} с"),
        ("Длительность моделирования", f"{result['dynamics']['simulation_duration']:g} с"),
        ("Постоянная времени T", f"{result['dynamics']['time_constant']:g} с"),
        ("Запаздывание L", f"{result['dynamics']['delay']:g} с"),
        ("Режим", result["result_mode"]),
    ]
    from .identification import format_experiment_origin
    if result.get("experiment_origin") is not None:
        parameters.append(("Экспериментальная динамика", format_experiment_origin(result["experiment_origin"])))
    for key in ("gg", "ga", "xg", "xa", "eta"):
        value = result["model_values"][key]
        parameters.append((key, f"{value:g} кг/ч" if key in ("gg", "ga") else f"{value * 100:.8g}%"))
    if controller is not None:
        parameters.extend((
            ("K", f"{controller['controller_gain']:g}"),
            (
                "Ti",
                f"{controller['integral_time']:g} с"
                if "I" in controller["controller_type"]
                else "не используется",
            ),
            (
                "Td",
                f"{controller['derivative_time']:g} с"
                if "D" in controller["controller_type"]
                else "не используется",
            ),
            ("Предел Δη", f"{controller['control_limit'] * 100:g} п.п."),
            ("Задание", f"{controller['setpoint'] * 100:g}%"),
        ))

        parameters.extend((label, f"{controller.get(key, 0) * scale:g}")
                          for key, label, scale in EXTENSION_FIELDS)
        if controller.get("noise_std", 0) > 0:
            parameters.append(("Шаг источника шума", "0.1 с; линейная интерполяция"))

    metrics = result["metrics"]
    relative_deviation = metrics["relative_deviation"]
    settling_time = metrics["settling_time"]
    settling_duration = (
        None
        if settling_time is None
        else max(0.0, settling_time - result["response_start"])
    )
    results = [
        ("Базовое значение", f"{result["baseline"] * 100:.8g}%"),
        ("Суммарная доля", f"{_format_number(result['combined_fraction'])} ({result['combined_fraction'] * 100:+.1f}%)"),
        ("Расчётное значение", f"{result["calculated"] * 100:.8g}%"),
        ("В конце моделирования", f"{result["final_response"][-1] * 100:.8g}%"),
        ("Теоретический установившийся режим", f"{metrics["steady_state"] * 100:.8g}%"),
        ("Статус установления", metrics["settling_status"]),
        ("Максимальное отклонение", f"{metrics["maximum_deviation"] * 100:.8g} п.п."),
        (
            "Относительное отклонение",
            f"{relative_deviation:.2f}%" if relative_deviation is not None else "не определено",
        ),
        (
            "Длительность регулирования (±5%)"
            if controller is not None
            else "Длительность установления (±5%)",
            "не достигнуто" if settling_duration is None else f"{settling_duration:.1f} с",
        ),
        ("Теоретическая ошибка", f"{metrics['static_error'] * 100:+.8g} п.п."),
        ("Ошибка в конце опыта", f"{metrics['final_error'] * 100:+.8g} п.п."),
        ("Полуширина полосы ±5%", f"{metrics['settling_tolerance'] * 100:.8g} п.п."),
        ("IAE относительно задания", "неприменимо без регулятора" if metrics["iae"] is None
         else f"{metrics['iae'] * 100:.8g} п.п.·с"),
        ("Длительность насыщения η", "неприменимо без регулятора" if metrics["saturation_duration"] is None
         else f"{metrics['saturation_duration']:.8g} с"),
    ]
    if controller is not None:
        results.append(("Ограничение скорости η", f"{metrics.get('rate_limit_duration', 0):.8g} с"))
    balance = result["stationary_balance"]
    for key in ("j", "gog", "gna", "mass_residual", "component_residual"):
        results.append((key + " (полная амплитуда, исходная η)", f"{float(balance[key]):.8g} кг/ч"))
    if controller is not None:
        results.append(("Достижимость задания", "Да" if result["setpoint_reachable"] else "Нет: достигнут предел η"))
    return format_protocol(title, parameters, results)


def write_protocol(selected_path, protocol):
    path = Path(selected_path)
    path.write_text(protocol, encoding="utf-8")
    return path


def write_html_report(
    selected_path,
    result,
    title,
    lesson,
    evaluation,
    student_name,
    conclusion,
    figures,
    prediction=None,
    comparison_runs=(),
):
    images = _figures_markup(figures, "График расчёта")
    score = "Не оценивалось" if evaluation is None else f"{evaluation['score']} из {evaluation['total']}"
    questions = "".join(f"<li>{html.escape(question)}</li>" for question in lesson["questions"])
    prediction_markup = _prediction_markup(prediction)
    criteria_markup = _criteria_markup(evaluation)
    comparison_markup = _comparison_markup(comparison_runs)
    report = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>body{{font:16px Segoe UI,Arial,sans-serif;color:#1f2937;max-width:1040px;margin:32px auto;line-height:1.5}}h1,h2{{color:#10243e}}pre{{white-space:pre-wrap;background:#f4f6f8;padding:16px}}img{{max-width:100%;margin:18px 0;border:1px solid #d7dce2}}</style></head>
<body><h1>Отчёт по лабораторной работе</h1><p><b>Студент:</b> {html.escape(student_name or 'не указан')}</p>
<h2>{html.escape(title)}</h2><p>{html.escape(lesson['task'])}</p><p>{html.escape(lesson['guidance'])}</p>
<h2>Вопросы</h2><ul>{questions or '<li>Не заданы</li>'}</ul><h2>Оценка</h2><p>{score}</p>
{criteria_markup}<h2>Прогноз студента</h2>{prediction_markup}<h2>Сравнение опытов</h2>{comparison_markup}
<pre>{html.escape(build_protocol(result, title))}</pre><h2>Графики</h2>{images}
<h2>Вывод студента</h2><p>{html.escape(conclusion or 'не указан')}</p></body></html>"""
    path = Path(selected_path)
    path.write_text(report, encoding="utf-8")
    return path


def write_pdf_report(
    selected_path,
    result,
    title,
    lesson,
    evaluation,
    student_name,
    conclusion,
    figures,
    prediction=None,
    comparison_runs=(),
):
    score = "Не оценивалось" if evaluation is None else f"{evaluation['score']} из {evaluation['total']}"
    sections = (
        ("Студент", student_name or "не указан"),
        ("Сценарий", title),
        ("Задание", lesson["task"]),
        ("Методические указания", lesson["guidance"]),
        ("Оценка", score),
        ("Прогноз", _prediction_text(prediction)),
        ("Показатели расчёта", build_protocol(result, title)),
        ("Закреплённые опыты", _comparison_text(comparison_runs)),
        ("Вывод студента", conclusion or "не указан"),
    )
    return _write_pdf(selected_path, (*_text_pages("Отчёт по лабораторной работе", sections), *figures))


def write_comparison_html_report(selected_path, runs):
    if not runs:
        raise ValueError("Нет закреплённых опытов для отчёта.")
    figures = build_comparison_figures(runs)
    images = _figures_markup(figures, "График сравнения")
    report = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>Отчёт по сравнению опытов</title>
<style>body{{font:16px Segoe UI,Arial,sans-serif;color:#1f2937;max-width:1040px;margin:32px auto;line-height:1.5}}h1,h2{{color:#10243e}}table{{border-collapse:collapse;width:100%}}th,td{{padding:8px;border:1px solid #d7dce2;text-align:left}}th{{background:#f4f6f8}}img{{max-width:100%;margin:18px 0;border:1px solid #d7dce2}}</style></head>
<body><h1>Отчёт по сравнению закреплённых опытов</h1><p>Всего опытов: {len(runs)}</p>
{_comparison_markup(runs)}<h2>Графики</h2>{images}</body></html>"""
    path = Path(selected_path)
    path.write_text(report, encoding="utf-8")
    return path


def write_comparison_pdf_report(selected_path, runs):
    if not runs:
        raise ValueError("Нет закреплённых опытов для отчёта.")
    sections = (
        ("Количество закреплённых опытов", str(len(runs))),
        ("Сводные показатели", _comparison_text(runs)),
    )
    figures = build_comparison_figures(runs)
    return _write_pdf(
        selected_path,
        (*_text_pages("Отчёт по сравнению закреплённых опытов", sections), *figures),
    )


def build_comparison_figures(runs):
    if not runs:
        raise ValueError("Нет закреплённых опытов для отчёта.")
    colors = ("#2563EB", "#F59E0B", "#16A34A", "#7C3AED", "#DB2777", "#0891B2")
    response_figure = Figure(figsize=(10, 4.8), constrained_layout=True)
    response_axis = response_figure.add_subplot(111)
    for index, run in enumerate(runs):
        response_axis.plot(
            run["time"], np.asarray(run["response"]) * 100, color=colors[index % len(colors)],
            linewidth=2.2, label=run["name"],
        )
    response_axis.set_title("Сравнение переходных процессов")
    response_axis.set_xlabel("Время, с")
    response_axis.set_ylabel("Концентрация, %")
    response_axis.grid(color="#D7DCE2", linewidth=0.8)
    adaptive_legend(response_axis)

    metrics_figure = Figure(figsize=(10, 4.8), constrained_layout=True)
    metrics_axis = metrics_figure.add_subplot(111)
    durations = [0.0 if run["settling_duration"] is None else run["settling_duration"] for run in runs]
    positions = np.arange(len(runs))
    bars = metrics_axis.bar(positions, durations, color=[colors[index % len(colors)] for index in positions])
    metrics_axis.set_title("Длительность установления")
    metrics_axis.set_ylabel("Время, с")
    metrics_axis.set_xticks(positions, [run["name"] for run in runs], rotation=20, ha="right")
    metrics_axis.grid(axis="y", color="#D7DCE2", linewidth=0.8)
    for bar, run in zip(bars, runs, strict=True):
        label = "не достигнуто" if run["settling_duration"] is None else f"{run['settling_duration']:.1f} с"
        metrics_axis.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), label, ha="center", va="bottom")
    metrics_axis.margins(y=0.18)
    return response_figure, metrics_figure


def _figure_base64(figure):
    buffer = BytesIO()
    figure.savefig(buffer, format="png", dpi=140, bbox_inches="tight")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _figures_markup(figures, alt_text):
    return "".join(
        f'<img src="data:image/png;base64,{_figure_base64(figure)}" alt="{alt_text}">'
        for figure in figures
    )


def _write_pdf(selected_path, figures):
    from matplotlib.backends.backend_pdf import PdfPages

    path = Path(selected_path)
    with PdfPages(path) as pdf:
        for figure in figures:
            pdf.savefig(figure, bbox_inches="tight")
    return path


def _text_pages(title, sections):
    pages = []

    def new_page():
        figure = Figure(figsize=(8.27, 11.69))
        axis = figure.add_axes((0.08, 0.06, 0.84, 0.88))
        axis.set_axis_off()
        axis.text(0, 0.98, title, fontsize=18, fontweight="bold", va="top")
        pages.append(figure)
        return axis, 0.92

    axis, y = new_page()
    for heading, content in sections:
        if y < 0.10:
            axis, y = new_page()
        axis.text(0, y, heading, fontsize=11, fontweight="bold", va="top")
        y -= 0.025
        for line in _wrapped_lines(content):
            if y < 0.05:
                axis, y = new_page()
                axis.text(0, y, heading + " (продолжение)", fontsize=11,
                          fontweight="bold", va="top")
                y -= 0.025
            axis.text(0, y, line, fontsize=9, va="top", family="DejaVu Sans")
            y -= 0.018
        y -= 0.018
    return tuple(pages)


def _wrapped_lines(value):
    lines = []
    for paragraph in str(value).splitlines() or ("",):
        lines.extend(textwrap.wrap(paragraph, width=100) or [""])
    return lines


def _prediction_text(prediction):
    if prediction is None:
        return "Режим задания не использовался."
    labels = {
        "direction": "Направление изменения",
        "steady": "Установившееся значение",
        "fastest": "Самая быстрая кривая",
        "correction": "Действие регулятора",
    }
    return "\n".join(f"{labels[key]}: {_prediction_value(key, value)}"
                     for key, value in prediction.items() if key in labels)


def _comparison_text(runs):
    from .identification import format_experiment_origin
    if not runs:
        return "Закреплённые опыты отсутствуют."
    return "\n".join(
        f"{run['name']}: регулятор {run['controller_type']}; "
        f"максимальное отклонение {_format_number(run['maximum_deviation'] * 100) + ' п.п.'}; "
        f"установление {_format_optional_number(run['settling_duration'])} с; "
        f"{run.get('settling_status', 'нет данных о статусе')}.\n"
        f"Теория {_scaled_metric(run.get('steady_state'), '%')}; "
        f"конец {_scaled_metric(run.get('final_value'), '%')}; "
        f"ошибка: теория {_scaled_metric(run.get('static_error'), ' п.п.')}, "
        f"конец {_scaled_metric(run.get('final_error'), ' п.п.')}; "
        f"IAE {_scaled_metric(run.get('iae'), ' п.п.·с')}; "
        f"насыщение η {_scaled_metric(run.get('saturation_duration'), ' с', 1)}.\n"
        + format_experiment_origin(run.get("experiment_origin"))
        for run in runs
    )


def _prediction_value(key, value):
    return f"{float(value) * 100:.8g}%" if key == "steady" else str(value)


def _prediction_markup(prediction):
    if prediction is None:
        return "<p>Режим задания не использовался.</p>"
    labels = {
        "direction": "Направление изменения",
        "steady": "Установившееся значение",
        "fastest": "Самая быстрая кривая",
        "correction": "Действие регулятора",
    }
    items = "".join(
        f"<li><b>{labels[key]}:</b> {html.escape(_prediction_value(key, value))}</li>"
        for key, value in prediction.items() if key in labels
    )
    return f"<ul>{items or '<li>Не указан</li>'}</ul>"


def _criteria_markup(evaluation):
    if evaluation is None or not evaluation.get("criteria"):
        return ""
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(item['label']))}</td>"
        f"<td>{'Верно' if item['passed'] else 'Неверно'}</td>"
        f"<td>{item['points']} / {item['maximum']}</td>"
        "</tr>"
        for item in evaluation["criteria"]
    )
    return (
        "<table><thead><tr><th>Критерий</th><th>Результат</th><th>Баллы</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def _comparison_markup(runs):
    if not runs:
        return "<p>Закреплённые опыты отсутствуют.</p>"
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(run['name']))}</td>"
        f"<td>{html.escape(str(run['controller_type']))}</td>"
        f"<td>{_format_number(run['maximum_deviation'] * 100) + ' п.п.'}</td>"
        f"<td>{_format_optional_number(run['settling_duration'])}</td>"
        "</tr>"
        for run in runs
    )
    return (
        "<table><thead><tr><th>Опыт</th><th>Регулятор</th>"
        "<th>Максимальное отклонение</th><th>Установление, с</th>"
        f"</tr></thead><tbody>{rows}</tbody></table><pre>{html.escape(_comparison_text(runs))}</pre>"
    )


def _format_optional_number(value):
    return "—" if value is None else _format_number(value)


def _scaled_metric(value, unit, scale=100):
    return "неприменимо" if value is None else _format_number(value * scale) + unit


def _format_number(value):
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _format_signed_number(value):
    if abs(value) < 0.00005:
        return "0"
    return f"{value:+.4f}".rstrip("0").rstrip(".").replace("-", "−")
