"""The level tank has its own form and snapshots; absorber state stays separate."""

import copy
import csv
import tkinter as tk
from tkinter import filedialog, ttk
from pathlib import Path

import numpy as np

from app.tank import DEFAULTS, MODEL_ID, MODEL_VERSION, equilibrium, compare_regimes, validate_inputs
from app.physical_models import MODELS, run_model
from app.tank_state import FIELD_DEFAULTS, VIEWS, validate_tank_state
from app.identification import bootstrap_intervals, identify_step, read_experiment, quality_text, uncertainty_text
from app.experiment_control import validate_step
from app.tank_exporting import STATUS_TEXT, EVENT_TEXT
from app.model_contract import copy_data
from ui.tank_sensitivity import TankSensitivity, VIEW as SENSITIVITY_VIEW


class TankPage(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, style="CardBody.TFrame", padding=12)
        self.app = app
        self.fields = {k: tk.StringVar(value=v) for k,v in FIELD_DEFAULTS.items()}
        self.view = tk.StringVar(value=VIEWS[0])
        self.summary = tk.StringVar(value="Выберите исходное состояние, затем снимите ступенчатый эксперимент.")
        self.result = self.fit = self.validation = self.pi = None
        self.revision = 0
        self.restoring = False
        self.headers, self.rows, self.source = [], [], ""
        self.columns = [tk.StringVar() for _ in range(3)]
        self.units = [tk.StringVar(value=v) for v in ("с","м³/с","м")]
        self.delimiter = tk.StringVar(value=";")
        self.same_signals = tk.BooleanVar(value=False)
        self.columnconfigure(1,weight=1)
        ttk.Label(self,text="Бак с регулированием уровня",style="CardTitle.TLabel").grid(row=0,column=0,columnspan=2,sticky="w")
        ttk.Label(self,text="Баланс → отклик → эксперимент → модель → PI → режимы → отчёт",style="Hint.TLabel",wraplength=320).grid(row=1,column=0,columnspan=2,sticky="ew",pady=8)
        labels = (("area","Площадь A, м²"),("coefficient","Коэффициент c, м^(5/2)/с"),("height","Высота Hmax, м"),
                  ("pump_limit","Предел Qmax, м³/с"),("mode","Исходное состояние"),("initial_level","Исходный h0, м"),
                  ("pump","Подача Q0, м³/с"),("withdrawal","Отбор d0, м³/с"),("step_time","Время ступени, с"),
                  ("pump_step","Ступень подачи, м³/с"),("withdrawal_step","Ступень отбора, м³/с"),
                  ("horizon","Горизонт, с"),("sample_time","Период расчёта, с"),("noise","Шум уровня σ, м"),("seed","Число для генератора"))
        row = 2
        for key,label in labels:
            ttk.Label(self,text=label,style="Body.TLabel").grid(row=row,column=0,sticky="w",pady=2)
            if key == "mode":
                ttk.Combobox(self,textvariable=self.fields[key],values=("Равновесие","Заданный уровень"),state="readonly",width=15).grid(row=row,column=1,sticky="ew")
            else:
                entry=ttk.Entry(self,textvariable=self.fields[key],width=14)
                entry.grid(row=row,column=1,sticky="ew")
                if key == "initial_level": self.initial_entry=entry
            row += 1
        for text,command in (("Показать баланс",self.balance),("Свободный отклик",self.calculate),("Снять эксперимент",self.generate),
                             ("Учебный пример",self.example)):
            ttk.Button(self,text=text,command=command).grid(row=row,column=0,columnspan=2,sticky="ew",pady=3); row+=1
        ttk.Combobox(self,textvariable=self.view,values=VIEWS,state="readonly").grid(row=row,column=0,columnspan=2,sticky="ew"); row+=1
        ttk.Label(self,textvariable=self.summary,style="Body.TLabel",wraplength=320).grid(row=row,column=0,columnspan=2,sticky="ew",pady=8); row+=1
        ttk.Label(self,text="CSV: подтвердите подачу насоса и уровень. При идентификации отбор постоянный.",style="Hint.TLabel",wraplength=320).grid(row=row,column=0,columnspan=2,sticky="ew"); row+=1
        ttk.Combobox(self,textvariable=self.delimiter,values=(";",",","табуляция"),state="readonly").grid(row=row,column=0,columnspan=2,sticky="ew"); row+=1
        ttk.Button(self,text="Импортировать CSV бака",command=self.import_csv).grid(row=row,column=0,columnspan=2,sticky="ew",pady=4); row+=1
        self.boxes=[]
        for i,label in enumerate(("Время","Подача насоса","Уровень")):
            ttk.Label(self,text=label,style="Body.TLabel").grid(row=row,column=0,sticky="w")
            box=ttk.Combobox(self,textvariable=self.columns[i],state="readonly",width=15)
            box.grid(row=row,column=1,sticky="ew"); self.boxes.append(box); row+=1
            options=("с","мин") if i==0 else ("м³/с","л/с") if i==1 else ("м","см")
            ttk.Combobox(self,textvariable=self.units[i],values=options,state="readonly").grid(row=row,column=1,sticky="ew"); row+=1
        ttk.Button(self,text="Оценить локальные K, T, L",command=self.identify).grid(row=row,column=0,columnspan=2,sticky="ew",pady=4); row+=1
        self.uncertainty_button=ttk.Button(self,text="Оценить неопределённость K, T, L",command=self.estimate_uncertainty)
        self.uncertainty_button.grid(row=row,column=0,columnspan=2,sticky="ew",pady=(0,4)); row+=1
        ttk.Checkbutton(self,text="Те же сигналы и постоянный отбор",variable=self.same_signals).grid(row=row,column=0,columnspan=2,sticky="w"); row+=1
        ttk.Button(self,text="Проверить на другом CSV",command=self.validate_file).grid(row=row,column=0,columnspan=2,sticky="ew",pady=4); row+=1
        for key,label in (("setpoint","Задание уровня, м"),("gain","Исходное Kp, м²/с"),("integral_time","Исходное Ti, с"),
                          ("title","Название опыта"),("prediction","Прогноз"),("conclusion","Вывод")):
            ttk.Label(self,text=label,style="Body.TLabel").grid(row=row,column=0,sticky="w")
            ttk.Entry(self,textvariable=self.fields[key]).grid(row=row,column=1,sticky="ew"); row+=1
        ttk.Button(self,text="Сравнить PI и рабочие уровни",command=self.check_pi).grid(row=row,column=0,columnspan=2,sticky="ew",pady=3); row+=1
        self.sensitivity = TankSensitivity(self)
        self.sensitivity.grid(row=row,column=0,columnspan=2,sticky="ew",pady=8); row+=1
        for text,command in (("Сохранить эксперимент CSV",self.export_experiment),
                             ("Сохранить результаты CSV",lambda:self.export("csv")),("Протокол HTML",lambda:self.export("html")),
                             ("Протокол PDF",lambda:self.export("pdf")),("Сохранить сеанс JSON",app._save_comparison_session),
                             ("Открыть сеанс JSON",app._open_comparison_session)):
            ttk.Button(self,text=text,command=command).grid(row=row,column=0,columnspan=2,sticky="ew",pady=3); row+=1
        for variable in (*(self.fields[k] for k in (*DEFAULTS,"mode")),*self.columns,*self.units,self.delimiter):
            variable.trace_add("write",self.changed)
        for k,var in self.fields.items():
            if k not in (*DEFAULTS,"mode"): var.trace_add("write",self.settings_changed)
        self.view.trace_add("write",lambda *_: self.draw())
        self.initial_entry.configure(state="disabled")

    def signature(self):
        return (self.revision,tuple(v.get() for v in self.fields.values()),tuple(v.get() for v in self.columns),tuple(v.get() for v in self.units),self.delimiter.get(),self.sensitivity.configuration())

    def changed(self,*_):
        if self.restoring: return
        self.revision+=1
        if getattr(self.app,"_task",None) is not None and self.app._task_kind == "tank": self.app._cancel_task()
        self.initial_entry.configure(state="disabled" if self.fields["mode"].get()=="Равновесие" else "normal")
        self.draw()

    def settings_changed(self,*_):
        if not self.restoring and getattr(self.app,"_task",None) is not None and self.app._task_kind == "tank":
            self.app._cancel_task()
        self.draw()

    def inputs(self):
        balanced=self.fields["mode"].get()=="Равновесие"
        try:
            p={k:float(self.fields[k].get().replace(",",".")) for k in DEFAULTS if k!="initial_level" or not balanced}
            if balanced: p["initial_level"]=p["height"]/2
        except ValueError as error: raise ValueError("Бак: заполните числовые параметры.") from error
        if balanced: p["initial_level"]=equilibrium(p)
        return validate_inputs(p)

    def perform(self,name,calculate,accept):
        self.app._start_task(name,calculate,accept,"tank")

    def fail(self,error):
        self.summary.set(str(error)); self.app._set_status(str(error),error=True)

    def balance(self):
        try:
            p=self.inputs(); expected=equilibrium(p)
        except ValueError as error: self.fail(error); return
        self.summary.set(f"Равновесие: h={expected:.6g} м; фактическая Q={min(p['pump'],p['pump_limit']):.6g} м³/с. Исходный h={p['initial_level']:.6g} м.")

    def calculate(self):
        try: p=self.inputs()
        except ValueError as error: self.fail(error); return
        revision=self.revision
        self.perform("Отклик бака",lambda cancel,progress: dict(run_model(MODEL_ID,dict(parameters=p),cancel=cancel,progress=progress),revision=revision),self.accept_result)

    def accept_result(self,result):
        self.app.model_results[MODEL_ID] = MODELS.result(MODEL_ID,result)
        self.result=result; self.view.set("Свободный отклик")
        self.summary.set(f"Конечный уровень {result['level'][-1]:.6g} м. " + (f"Остановка: {EVENT_TEXT[result['event']['kind']]}, {result['event']['time']:.6g} с." if result['event'] else "Горизонт опыта завершён."))
        self.draw(); self.app._set_status("Расчёт бака выполнен")

    def example(self):
        self.restore(None)
        self.fields["prediction"].set("При большей подаче уровень вырастет; PI из среднего режима проверю на низком и высоком уровнях.")
        self.summary.set("Учебный пример: покажите баланс, снимите эксперимент, оцените K/T/L, сравните PI и запишите вывод.")
        self.calculate()

    def generate(self):
        try:
            p=self.inputs()
            if p["withdrawal_step"] != 0 or not np.isclose(p["initial_level"],equilibrium(p),atol=1e-10):
                raise ValueError("Для идентификации нужны равновесие и постоянный отбор.")
            sigma=float(self.fields["noise"].get().replace(",",".")); seed=int(self.fields["seed"].get())
            if not np.isfinite(sigma) or sigma<0 or not 0 <= seed <= 4294967295:
                raise ValueError("Нужны σ ≥ 0 и целое число генератора 0…4294967295.")
        except ValueError as error: self.fail(error); return
        revision=self.revision
        def accept(result):
            self.revision+=1; result["revision"]=self.revision
            self.accept_result(result)
            if result["event"]:
                self.summary.set("Опыт остановлен на границе. Измените параметры до идентификации."); return
            y=np.asarray(result["level"])+np.random.default_rng(seed).normal(0,sigma,len(result["time"]))
            self.restoring=True
            self.headers=["Время, с","Подача, м³/с","Уровень, м"]
            self.rows=[[str(float(v)) for v in row] for row in zip(result["time"],result["pump"],y)]
            self.source="Симуляция бака"
            for i,box in enumerate(self.boxes): box.configure(values=self.headers); self.columns[i].set(self.headers[i])
            for var,value in zip(self.units,("с","м³/с","м")): var.set(value)
            self.restoring=False
            self.summary.set(f"Снят эксперимент: {len(self.rows)} точек; σ={sigma:g} м; генератор={seed}. Теперь оцените K/T/L.")
        self.perform("Эксперимент бака",lambda cancel,progress:dict(run_model(MODEL_ID,dict(parameters=p),cancel=cancel,progress=progress),revision=revision),accept)

    def import_csv(self):
        path=filedialog.askopenfilename(title="Эксперимент бака",filetypes=(("CSV","*.csv"),))
        if path: self.load_path(path)

    def load_path(self,path):
        try: headers,rows=read_experiment(path,"\t" if self.delimiter.get()=="табуляция" else self.delimiter.get())
        except (ValueError,OSError,UnicodeError) as error: self.fail(error); return
        self.headers,self.rows,self.source=headers,rows,str(path)
        self.changed()
        for i,box in enumerate(self.boxes): box.configure(values=headers); self.columns[i].set(headers[i])
        self.summary.set(f"{Path(path).name}: {len(rows)} точек. Подтвердите столбцы и единицы подачи/уровня.")

    def arrays(self,headers=None,rows=None):
        headers=self.headers if headers is None else headers; rows=self.rows if rows is None else rows
        columns=[v.get() for v in self.columns]
        if not rows or len(set(columns)) != 3 or any(c not in headers for c in columns): raise ValueError("Выберите три разных столбца CSV бака.")
        try: arrays=np.asarray([[float(row[headers.index(c)].replace(",",".")) for c in columns] for row in rows]).T
        except (ValueError,IndexError) as error: raise ValueError("В выбранных столбцах нужны числовые значения без пропусков.") from error
        arrays[0]*=60 if self.units[0].get()=="мин" else 1
        arrays[1]*=.001 if self.units[1].get()=="л/с" else 1
        arrays[2]*=.01 if self.units[2].get()=="см" else 1
        if not np.isfinite(arrays).all() or np.any(arrays[1:]<0): raise ValueError("Подача и уровень должны быть конечными и неотрицательными.")
        return arrays

    def origin(self):
        return dict(model_id=MODEL_ID,model_version=MODEL_VERSION,revision=self.revision,source=self.source,
                    columns=[v.get() for v in self.columns],units=[v.get() for v in self.units],fields={k:v.get() for k,v in self.fields.items()})

    def identify(self):
        try: arrays=self.arrays()
        except ValueError as error: self.fail(error); return
        origin=self.origin(); revision=self.revision
        def calculate(cancel,progress):
            result=identify_step(*arrays,cancel=cancel,progress=progress)
            if result["gain"] <= 0: raise ValueError("Нужен положительный канал: подача насоса → уровень.")
            return dict({k:v.tolist() if isinstance(v,np.ndarray) else v for k,v in result.items()},source=origin["source"],origin=origin,revision=revision)
        def accept(result):
            self.fit=result; self.view.set("Идентификация")
            self.summary.set(self.fit_text(result))
            self.draw(); self.app._set_status("Идентификация бака выполнена")
        self.perform("Идентификация бака",calculate,accept)

    def fit_text(self,result):
        return (f"Локальная модель: K={result['gain']:.6g} с/м²; T={result['time_constant']:.6g} с; L={result['delay']:.6g} с; RMSE всей записи={result['rmse']:.4g} м. Подгонка не заменяет нелинейный бак.\n" + quality_text(result) +
                ("\n" + uncertainty_text(result["uncertainty"]) if result.get("uncertainty") else ""))

    def estimate_uncertainty(self):
        if self.fit is None or self.fit["revision"] != self.revision:
            self.fail("Сначала оцените актуальную локальную модель."); return
        fit=self.fit
        def accept(record):
            if self.fit is not fit or fit["revision"] != self.revision: return
            fit["uncertainty"]=record; self.summary.set(self.fit_text(fit))
            self.app._set_status("Интервалы K, T и L бака рассчитаны")
        self.perform("Неопределённость K, T, L бака",lambda cancel,progress: bootstrap_intervals(fit,cancel=cancel,progress=progress),accept)

    def validate_file(self):
        if self.fit is None or self.fit["revision"] != self.revision or not self.same_signals.get():
            self.fail("Сначала оцените актуальную модель и подтвердите те же сигналы с постоянным отбором."); return
        path=filedialog.askopenfilename(title="Независимая запись бака",filetypes=(("CSV","*.csv"),))
        if not path: return
        try:
            headers,rows=read_experiment(path,"\t" if self.delimiter.get()=="табуляция" else self.delimiter.get())
            arrays=self.arrays(headers,rows)
            if all(np.array_equal(a,self.fit[k]) for a,k in zip(arrays,("time","input","output"))): raise ValueError("Запись совпадает с данными подгонки.")
        except (ValueError,OSError,UnicodeError) as error: self.fail(error); return
        fit=copy.deepcopy(self.fit); origin=copy.deepcopy(fit["origin"]); revision=self.revision
        def accept(result):
            self.validation=result; self.view.set("Проверка")
            self.summary.set(f"Независимая запись: RMSE всей записи={result['rmse']:.5g} м; ошибка {result['normalized_rmse']*100:.3g}%. K/T/L сохранены. {result['warning']}\n" + quality_text(result))
            self.draw(); self.app._set_status("Проверка бака выполнена")
        self.perform("Проверка бака",lambda cancel,progress:dict(validate_step(*arrays,fit,cancel=cancel),source=Path(path).name,origin=origin,revision=revision),accept)

    def check_pi(self):
        if self.fit is None or self.fit["revision"] != self.revision:
            self.fail("Сначала оцените актуальную локальную модель бака."); return
        try:
            p=self.inputs(); kp,ti,sp=(float(self.fields[k].get().replace(",",".")) for k in ("gain","integral_time","setpoint"))
        except ValueError as error: self.fail(error); return
        fit=copy.deepcopy(self.fit); revision=self.revision
        configuration={k:self.fields[k].get() for k in ("gain","integral_time","setpoint")}
        def accept(result):
            self.app.model_results[MODEL_ID] = MODELS.result(MODEL_ID,result["cases"][0])
            self.pi=result; self.view.set("PI")
            self.summary.set("\n".join(f"{r['name']}: IAE={r['metrics']['iae']:.5g} м·с; перерегулирование={r['metrics']['overshoot_percent']:.3g}%; "
                 f"установление={r['metrics']['settling_time'] if r['metrics']['settling_time'] is not None else 'не достигнуто'} с; насыщение={r['metrics']['saturation_duration']:.4g} с; {STATUS_TEXT[r['metrics']['status']]}." for r in result['cases']))
            self.draw(); self.app._set_status("PI бака проверен на разных уровнях")
        self.perform("PI бака",lambda cancel,progress:dict(compare_regimes(p,fit,kp,ti,sp,cancel=cancel,progress=progress),origin=fit["origin"],revision=revision,configuration=configuration),accept)

    def draw(self):
        if getattr(self.app,"current_page",None) != "tank": return
        app=self.app; app._remove_controller_axis(); app.analysis_panel.remove_map_colorbar(); app.analysis_panel.clear_map_click_callback()
        if self.view.get() == SENSITIVITY_VIEW:
            self.sensitivity.draw(); return
        app._style_axis(app.disturbance_axis,"Время, с","Расход, м³/с")
        app._style_axis(app.response_axis,"Время, с","Уровень, м")
        view=self.view.get(); record=self.pi if view=="PI" else self.fit if view in ("Идентификация","Остаток") else self.validation if view=="Проверка" else self.result
        if record is not None:
            if view=="PI":
                for r in record["cases"]:
                    app.disturbance_axis.plot(r["time"],r["pump"],label=r["name"])
                    app.response_axis.plot(r["time"],r["level"],label=r["name"])
                    if "prediction" in r: app.response_axis.plot(r["time"],r["prediction"],linestyle=":",label=f"Локальный прогноз: {r['name']}")
                app.response_axis.axhline(record["cases"][0]["controller"]["setpoint"],color="#64748B",linestyle="--",label="Задание")
                nominal=record["cases"][0]
                app.disturbance_axis.plot(nominal["time"],nominal["withdrawal"],color="#64748B",linestyle=":",label="Дополнительный отбор")
                app.disturbance_axis.axhline(nominal["parameters"]["pump_limit"],color="#B91C1C",linestyle="--",label="Qmax")
                app.response_axis.axhline(nominal["parameters"]["height"],color="#B91C1C",linestyle="--",label="Hmax")
            elif view in ("Идентификация","Остаток","Проверка"):
                app.disturbance_axis.plot(record["time"],record["input"],label="Подача насоса")
                if view=="Остаток":
                    app.response_axis.plot(record["time"],record["residual"],label="Эксперимент − приближение")
                    app.response_axis.set_ylabel("Остаток, м")
                else:
                    app.response_axis.plot(record["time"],record["output"],label="Эксперимент")
                    app.response_axis.plot(record["time"],record["model"],label="Локальное приближение")
            else:
                app.disturbance_axis.plot(record["time"],record["pump"],label="Фактическая подача")
                app.disturbance_axis.plot(record["time"],record["commanded"],linestyle=":",label="Заданная подача")
                app.disturbance_axis.plot(record["time"],record["withdrawal"],label="Дополнительный отбор")
                app.response_axis.plot(record["time"],record["level"],label="Нелинейный бак")
                app.response_axis.axhline(record["parameters"]["height"],color="#B91C1C",linestyle="--",label="Hmax")
                app.disturbance_axis.axhline(record["parameters"]["pump_limit"],color="#B91C1C",linestyle="--",label="Qmax")
        app.primary_chart_title.set("Подача и отбор бака"); app.primary_chart_subtitle.set("Расходы в м³/с; уровень в метрах")
        app.response_chart_title.set(f"Бак: {view}")
        stale=record is not None and (record["revision"] != self.revision or (view=="PI" and record.get("configuration") != {k:self.fields[k].get() for k in ("gain","integral_time","setpoint")}))
        app.response_subtitle.set("Снимок прежних настроек; повторите расчёт." if stale else "Прогноз канала подачи; изменение отбора не учитывается." if view=="PI" else "Физический объект: A·dh/dt = Q − c√h − d; идеальный насос")
        if record is not None and view in ("Идентификация", "Остаток", "Проверка"):
            self.summary.set(("Снимок прежних настроек. " if stale else "")
                + ("Независимая проверка; K/T/L сохранены. " if view=="Проверка" else "Локальная подгонка; не заменяет нелинейный бак. ")
                + f"K={record['gain']:.6g} с/м²; T={record['time_constant']:.6g} с; L={record['delay']:.6g} с. "
                + f"RMSE всей записи={record['rmse']:.5g} м.\n" + quality_text(record))
        for axis,canvas in ((app.disturbance_axis,app.disturbance_canvas),(app.response_axis,app.response_canvas)):
            axis.figure.set_layout_engine("none")
            axis.set_position((.11,.32,.87,.62))
            callback=getattr(canvas,"_legend_resize_callback",None)
            if callback is not None: canvas.mpl_disconnect(callback)
            if axis.lines:
                handles,labels=axis.get_legend_handles_labels()
                display=[label.replace("Локальный прогноз: Исходные:","Прогноз И:").replace("Локальный прогноз: Предложенные:","Прогноз П:").replace("Исходные:","И:").replace("Предложенные:","П:") for label in labels]
                legend=axis.legend(handles,display,loc="upper center",frameon=True,framealpha=.85,fontsize=7,ncol=4 if view=="PI" else 3,columnspacing=.8)
                legend._source_labels=tuple(labels)
                app._enable_legend_toggles(axis,canvas)
            canvas.draw_idle()
        app._charts_show_comparison=False

    def capture(self, *, copy_results=True):
        snapshot = dict(model_id=MODEL_ID,model_version=MODEL_VERSION,fields={k:v.get() for k,v in self.fields.items()},
            revision=self.revision,view=self.view.get(),headers=self.headers,rows=self.rows,source=self.source,
            columns=[v.get() for v in self.columns],units=[v.get() for v in self.units],delimiter=self.delimiter.get(),same_signals=self.same_signals.get(),
            result=self.result,fit=self.fit,validation=self.validation,pi=self.pi,
            sensitivity_fields={k:v.get() for k,v in self.sensitivity.fields.items()},
            sensitivity=self.sensitivity.result,selected_sensitivity=self.sensitivity.index())
        return copy_data(snapshot) if copy_results else snapshot

    def restore(self,state):
        state=validate_tank_state(state)
        self.restoring=True
        try:
            for k,var in self.fields.items(): var.set(FIELD_DEFAULTS[k] if state is None else state["fields"][k])
            self.revision=0 if state is None else state["revision"]
            self.headers=[] if state is None else state["headers"]; self.rows=[] if state is None else state["rows"]
            self.source="" if state is None else state["source"]
            for key in ("result","fit","validation","pi"): setattr(self,key,None if state is None else state[key])
            for i,box in enumerate(self.boxes): box.configure(values=self.headers); self.columns[i].set("" if state is None else state["columns"][i])
            for i,var in enumerate(self.units): var.set(("с","м³/с","м")[i] if state is None else state["units"][i])
            self.delimiter.set(";" if state is None else state["delimiter"])
            self.same_signals.set(False if state is None else state["same_signals"])
            self.sensitivity.restore(state)
            self.view.set(VIEWS[0] if state is None else state["view"])
        finally: self.restoring=False
        self.initial_entry.configure(state="disabled" if self.fields["mode"].get()=="Равновесие" else "normal")
        self.summary.set("Работа с баком восстановлена." if state else "Покажите баланс и снимите эксперимент.")
        self.draw()

    def export_experiment(self):
        if not self.rows: self.fail("Сначала снимите или импортируйте эксперимент."); return
        path=filedialog.asksaveasfilename(title="Эксперимент бака",defaultextension=".csv",filetypes=(("CSV","*.csv"),))
        if path:
            try:
                with open(path,"w",encoding="utf-8-sig",newline="") as stream:
                    writer=csv.writer(stream,delimiter="\t" if self.delimiter.get()=="табуляция" else self.delimiter.get()); writer.writerow(self.headers); writer.writerows(self.rows)
                self.app._set_status("Эксперимент бака сохранён")
            except OSError as error: self.fail(error)

    def export(self,format_name):
        from app.tank_exporting import write_tank_report
        path=filedialog.asksaveasfilename(title="Работа с баком",defaultextension=f".{format_name}",filetypes=((format_name.upper(),f"*.{format_name}"),))
        if path:
            try: write_tank_report(path,self.capture(),format_name); self.app._set_status("Работа с баком сохранена")
            except (ValueError,OSError) as error: self.fail(error)
