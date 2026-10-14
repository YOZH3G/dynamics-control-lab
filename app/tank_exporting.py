"""Tank reports use the validated snapshots and the existing report renderer."""

import csv
import html
from pathlib import Path

from matplotlib.figure import Figure

from .exporting import _text_pages, _write_pdf, _figures_markup
from .tank_state import validate_tank_state
from .identification import quality_text, uncertainty_text


STATUS_TEXT = dict(physical_stop="физическая остановка",unreachable="задание недостижимо",
                   settled="установление подтверждено",horizon="установление не подтверждено за горизонт")
EVENT_TEXT = dict(empty="опустошение",full="достижение верхней границы")
PARAMETER_TEXT = dict(area="Площадь A, м²",coefficient="Коэффициент c, м^(5/2)/с",height="Высота Hmax, м",
    pump_limit="Предел Qmax, м³/с",initial_level="Исходный h0, м",pump="Подача Q0, м³/с",
    withdrawal="Отбор d0, м³/с",step_time="Время ступени, с",pump_step="Ступень подачи, м³/с",
    withdrawal_step="Ступень отбора, м³/с",horizon="Горизонт, с",sample_time="Период расчёта, с")


def write_tank_report(path,state,format_name):
    state=validate_tank_state(state)
    title=state["fields"]["title"] or "Работа с баком"
    sections=[("Физическая модель",f"Бак; версия расчёта {state['model_version']}. A·dh/dt=Q−c√h−d.\n"
               "Идеальный насос; границы останавливают опыт; перелив и работа сухого бака не моделируются."),
              ("Учебная работа",f"Прогноз: {state['fields']['prediction']}\nВывод: {state['fields']['conclusion']}")]
    series=[]
    if state["result"] is not None: series.append(("Свободный отклик",state["result"],"level","pump"))
    for key,label in (("fit","Идентификация"),("validation","Независимая проверка")):
        r=state[key]
        if r is not None:
            origin=r["origin"]
            sections.append((label,f"Источник: {r['source']}; версия данных {r['revision']}.\n"
                f"K={r['gain']:.8g} с/м²; T={r['time_constant']:.8g} с; L={r['delay']:.8g} с; RMSE всей записи={r['rmse']:.8g} м.\n"
                f"Исходные единицы: {origin['units']}; столбцы: {origin['columns']}.\n"
                + ("Локальная подгонка; не заменяет нелинейную физическую модель." if key=="fit" else "K/T/L фиксированы до загрузки независимой записи.") + "\n" + quality_text(r)
                + ("\n" + uncertainty_text(r["uncertainty"]) if key == "fit" and r.get("uncertainty") else "")))
            series.append((label,r,"output","input"))
    if state["pi"] is not None:
        sections.append(("Сравнение PI","Kp/Ti фиксированы для каждого регулятора во всех режимах; равновесные базовые подачи различны.\n"
             "PI управляет нелинейным баком. Локальный прогноз показан для номинала по фактической подаче; возмущение отбора в этом прогнозе не учтено.\n"
             "Проверенные режимы не доказывают устойчивость во всём диапазоне."))
        for r in state["pi"]["cases"]: series.append((r["name"],r,"level","pump"))
    if not series: raise ValueError("Сначала рассчитайте бак или идентифицируйте эксперимент.")
    if format_name=="csv":
        if state["sensitivity"] is not None:
            from .tank_sensitivity_exporting import case_label
            for case in [*state["sensitivity"]["controls"], *state["sensitivity"]["cases"]]:
                series.append(("Ошибки оценки: "+case_label(case),case["run"],"level","pump"))
        with open(path,"w",encoding="utf-8-sig",newline="") as stream:
            writer=csv.writer(stream,delimiter=";")
            writer.writerow(("Опыт","Время, с","Подача, м³/с","Уровень, м","Модель или прогноз, м","Отбор, м³/с"))
            for label,r,y,u in series:
                model=r.get("model",r.get("prediction",[""]*len(r["time"])))
                withdrawal=r.get("withdrawal",[""]*len(r["time"]))
                for i,t in enumerate(r["time"]):
                    writer.writerow((label,t,r[u][i],r[y][i],model[i],withdrawal[i]))
        return Path(path)
    figures=[]
    for label,r,y,u in series:
        extra=[f"Версия данных: {r.get('revision',state['pi']['revision'] if state['pi'] else 0)}."]
        if "parameters" in r:
            extra.extend(f"{PARAMETER_TEXT[key]}: {value}" for key,value in r["parameters"].items())
            extra.append(f"Базовая подача: {r['baseline_pump']:.8g} м³/с; период: {r['sample_time']:.8g} с.")
            if r["event"]: extra.append(f"Физическая остановка: {EVENT_TEXT[r['event']['kind']]}; {r['event']['time']:.8g} с.")
        if r.get("controller"):
            m=r["metrics"]; settings=r["controller"]
            extra.append(f"Kp={settings['gain']:.8g} м²/с; Ti={settings['integral_time']:.8g} с; задание={settings['setpoint']:.8g} м.")
            extra.append(f"Ошибка={m['final_error']:.8g} м; IAE={m['iae']:.8g} м·с; перерегулирование={m['overshoot_percent']:.6g}%; "
                         f"установление={m['settling_time'] if m['settling_time'] is not None else 'не достигнуто'} с; насыщение={m['saturation_duration']:.8g} с; {STATUS_TEXT[m['status']]}")
        if (r.get("revision",state["pi"]["revision"] if state["pi"] else 0)!=state["revision"]
                or (r.get("controller") and state["pi"]["configuration"] != {k:state["fields"][k] for k in ("gain","integral_time","setpoint")})):
            extra.append("Снимок прежних настроек.")
        sections.append((label,"\n".join(extra)))
        figure=Figure(figsize=(9,7)); axes=figure.subplots(2,1)
        figure.subplots_adjust(left=.14,right=.98,bottom=.09,top=.93,hspace=.5)
        axes[0].plot(r["time"],r[u],label="Подача насоса")
        if "withdrawal" in r: axes[0].plot(r["time"],r["withdrawal"],label="Дополнительный отбор")
        if "parameters" in r:
            axes[0].axhline(r["parameters"]["pump_limit"],linestyle="--",label="Qmax")
            axes[1].axhline(r["parameters"]["height"],linestyle="--",label="Hmax")
        axes[1].plot(r["time"],r[y],label=label)
        if "model" in r: axes[1].plot(r["time"],r["model"],label="Локальное приближение")
        if "prediction" in r: axes[1].plot(r["time"],r["prediction"],linestyle=":",label="Локальный прогноз по подаче")
        if r.get("controller"): axes[1].axhline(r["controller"]["setpoint"],linestyle="--",label="Задание")
        for axis,ylabel in zip(axes,("Расход, м³/с","Уровень, м")):
            axis.set_xlabel("Время, с"); axis.set_ylabel(ylabel); axis.grid(alpha=.2); axis.legend()
        figure.suptitle(label); figures.append(figure)
    if state["sensitivity"] is not None:
        from .tank_sensitivity_exporting import report_parts
        snapshot = dict(state["sensitivity"])
        snapshot["exported_stale"] = (snapshot["revision"] != state["revision"]
            or snapshot["configuration"] != dict(fields=state["sensitivity_fields"],controller={k:state["fields"][k] for k in ("gain","integral_time","setpoint")}))
        extra_sections, extra_figures = report_parts(snapshot,state["selected_sensitivity"])
        sections.extend(extra_sections); figures.extend(extra_figures)
    if format_name=="pdf": return _write_pdf(path,[*_text_pages(title,sections),*figures])
    if format_name!="html": raise ValueError("Поддерживаются CSV, HTML и PDF.")
    markup="".join(f"<h2>{html.escape(name)}</h2><pre>{html.escape(text)}</pre>" for name,text in sections)
    Path(path).write_text(f'<!doctype html><html lang="ru"><meta charset="utf-8"><title>{html.escape(title)}</title><style>body{{font:16px sans-serif;max-width:1000px;margin:32px auto}}pre{{white-space:pre-wrap}}img{{max-width:100%}}</style><h1>{html.escape(title)}</h1>'+markup+_figures_markup(figures,"Работа с баком")+"</html>",encoding="utf-8")
    return Path(path)
