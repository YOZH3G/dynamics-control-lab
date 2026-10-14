import copy
import csv
from pathlib import Path
import tempfile
import unittest

import numpy as np

from app.calculations import DEFAULT_MODEL_VALUES, STEP
from app.model_contract import ModelRegistry, Parameter, PhysicalModel, Signal, copy_data, read_result, write_result
from app.model_output import result_figures, write_model_report
from app.physical_models import MODELS
from app.simulation import run_simulation
from app.tank import DEFAULTS, simulate


DYNAMICS = dict(kind=STEP, start_time=10., simulation_duration=40., effect_duration=1., time_constant=10., delay=2.)


def heater():
    # A test-only energy balance, with units and equations unrelated to concentration.
    def calculate(request, *, cancel=None, progress=None):
        p = request['parameters']
        time = [0., 1., 2.]
        return dict(time=time, parameters=p, signals=dict(power=[2., 2., 2.],
                    temperature=[p['initial'] + 2*t/p['capacity'] for t in time]),
                    units=dict(power='Вт', temperature='°C'), events=[], metrics={})
    return PhysicalModel('test_heater', 1, 'Тестовый нагреватель',
        (Parameter('capacity', 'Теплоёмкость', 'Дж/К', 4., 0., strict_minimum=True),
         Parameter('initial', 'Исходная температура', '°C', 20.)),
        (Signal('power', 'Мощность', 'Вт', 'input', required=True),
         Signal('temperature', 'Температура', '°C', 'output', required=True)),
        (('power', 'temperature'),), ('dynamics',), (), calculate, copy.deepcopy,
        'model_result', 'НАГРЕВАТЕЛЬ', 'Тест подключения')


class PhysicalModelTests(unittest.TestCase):
    def test_absorber_adapter_preserves_both_channels_and_control(self):
        controller = dict(controller_type='PI', controller_gain=0.1, integral_time=20.,
                          derivative_time=0., control_limit=1., setpoint=0.2)
        for chain in ('lean_gas', 'rich_absorbent'):
            for control in (None, controller):
                with self.subTest(chain=chain, controller=control):
                    request = dict(chain=chain, parameters=DEFAULT_MODEL_VALUES.copy(), component=0.1,
                                   flow=0.05, dynamics=DYNAMICS, controller=control)
                    original = run_simulation(chain, DEFAULT_MODEL_VALUES, .1, .05, DYNAMICS, control)
                    actual = MODELS.run('absorber', request)
                    np.testing.assert_array_equal(original['final_response'], actual['final_response'])
                    self.assertEqual(original['metrics'], actual['metrics'])
                    common = MODELS.result('absorber', actual)
                    for name in ('xog', 'xna'):
                        np.testing.assert_array_equal(common['signals'][name], original['phase_responses'][name])
                    self.assertEqual(common['units']['xog'], 'доля')
                    if control is not None:
                        damaged = copy.deepcopy(common)
                        damaged['signals']['control'][0] = 100.
                        with self.assertRaises(ValueError): MODELS.validate(damaged)
                    figure = result_figures(common, MODELS)[1]
                    np.testing.assert_array_equal(figure.axes[0].lines[0].get_ydata(), np.asarray(common['signals']['xog'])*100)

    def test_tank_adapter_preserves_events_and_physical_pi(self):
        for control in (None, dict(gain=.015, integral_time=200., setpoint=1.4)):
            p = dict(DEFAULTS, horizon=40., withdrawal_step=.2)
            original = simulate(p, controller=control)
            actual = MODELS.run('level_tank', dict(parameters=p, controller=control))
            self.assertEqual(original, actual)
            self.assertIsInstance(actual['level'], list)
            result = MODELS.result('level_tank', actual)
            self.assertEqual(result['events'], [original['event']])
            self.assertEqual(result['signals']['level'], original['level'])
            self.assertEqual(result['units']['pump'], 'м³/с')

    def test_local_prediction_keeps_identified_parameters(self):
        local = dict(gain=200., time_constant=150., delay=1.)
        raw = MODELS.run('level_tank', dict(parameters=dict(DEFAULTS, horizon=40.), local_model=local))
        result = MODELS.result('level_tank', raw)
        self.assertEqual(result['configuration']['local_model'], local)
        self.assertEqual(result['signals']['prediction'], raw['prediction'])
        for mutation in (lambda r:r['configuration'].pop('local_model'),
                         lambda r:r['configuration']['local_model'].update(delay=-1.)):
            damaged = copy.deepcopy(result); mutation(damaged)
            with self.assertRaises(ValueError): MODELS.validate(damaged)

    def test_third_model_run_plot_all_exports_and_portable_snapshot(self):
        registry = ModelRegistry((*MODELS.all(), heater()))
        request = dict(parameters=dict(capacity=4., initial=20.))
        native = registry.run('test_heater', request)
        result = registry.result('test_heater', native)
        self.assertEqual(result['signals']['temperature'], [20., 20.5, 21.])
        figures = result_figures(result, registry)
        self.assertEqual(len(figures), 2)
        self.assertEqual(figures[1].axes[0].get_ylabel(), '°C')
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        for figure in figures:
            canvas = FigureCanvasAgg(figure); canvas.draw()
            renderer = canvas.get_renderer()
            title_box = figure._suptitle.get_window_extent(renderer)
            legend_box = figure.axes[0].get_legend().get_window_extent(renderer)
            self.assertFalse(title_box.overlaps(legend_box))
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            write_result(folder/'result.json', result, registry)
            restored = read_result(folder/'result.json', registry)
            self.assertEqual(restored, result)
            for fmt in ('csv', 'html', 'pdf'):
                path = write_model_report(folder/f'result.{fmt}', restored, fmt, registry)
                self.assertGreater(path.stat().st_size, 30)
            with (folder/'result.csv').open(encoding='utf-8-sig', newline='') as stream:
                rows = list(csv.reader(stream, delimiter=';'))
            self.assertEqual(rows[0], ['Время, с', 'Мощность, Вт', 'Температура, °C'])
            self.assertEqual(float(rows[-1][-1]), 21.)
            self.assertIn('Температура: хранение °C', (folder/'result.html').read_text(encoding='utf-8'))
            self.assertTrue((folder/'result.pdf').read_bytes().startswith(b'%PDF'))

    def test_corrupt_snapshot_cannot_replace_existing_result(self):
        raw = MODELS.run('level_tank', dict(parameters=dict(DEFAULTS, horizon=40.)))
        valid = MODELS.result('level_tank', raw)
        mutations = [lambda r:r.update(model_id='unknown'), lambda r:r.update(model_version=99),
                     lambda r:r['units'].update(level='см'), lambda r:r['signals']['level'].pop(),
                     lambda r:r['signals']['level'].__setitem__(0, float('nan')),
                     lambda r:r['signals']['level'].__setitem__(0, True),
                     lambda r:r['time'].__setitem__(1, 0.), lambda r:r['signals'].pop('level'),
                     lambda r:r['parameters'].update(area=0),
                     lambda r:r['signals']['level'].__setitem__(0, 20.),
                     lambda r:r.update(events=[dict(kind='unknown', time=1.)]),
                     lambda r:r['limits'][0].update(value=float('inf')),
                     lambda r:r.update(events=[dict(kind="full", time=10., level=0.)]),
                     lambda r:r["parameters"].update(initial_level=100.),
                     lambda r:r["time"].__setitem__(-1, 1000.),
                     lambda r:r["signals"]["withdrawal"].__setitem__(0, -100.),
                     lambda r:r["signals"]["level"].__setitem__(0, 10**400),
                     lambda r:r["parameters"].update(area=10**400),
                     lambda r:r["limits"][0].update(value=100)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'result.json'
            write_result(path, valid, MODELS)
            before = path.read_bytes()
            for mutate in mutations:
                bad = copy.deepcopy(valid); mutate(bad)
                with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                    write_result(path, bad, MODELS)
                self.assertEqual(path.read_bytes(), before)
            self.assertEqual(read_result(path, MODELS), valid)

    def test_data_copy_matches_deepcopy_without_shared_containers(self):
        array = np.arange(3.)
        data = dict(time=[0, .5, 1.], flags=[True, None, "x"], nested=[[1., 2.], dict(a=[3])],
                    pair=(1, [2.]), array=array)
        copied = copy_data(data)
        self.assertEqual(copied.keys(), data.keys())
        self.assertEqual({k: v for k, v in copied.items() if k != "array"}, {k: v for k, v in data.items() if k != "array"})
        np.testing.assert_array_equal(copied["array"], array)
        for path in (("time",), ("flags",), ("nested",), ("nested", 0), ("nested", 1), ("nested", 1, "a"), ("pair", 1), ("array",)):
            original, duplicate = data, copied
            for key in path:
                original, duplicate = original[key], duplicate[key]
            self.assertIsNot(duplicate, original, path)
        self.assertEqual(copy_data(MODELS.result("level_tank", simulate(DEFAULTS))), MODELS.result("level_tank", simulate(DEFAULTS)))

    def test_result_validation_still_rejects_bool_and_non_finite_extras(self):
        result = MODELS.result("level_tank", simulate(DEFAULTS))
        broken = copy.deepcopy(result)
        broken["signals"]["level"][3] = True
        with self.assertRaises(ValueError):
            MODELS.validate(broken)
        broken = copy.deepcopy(result)
        broken["configuration"] = dict(note=float("nan"))
        with self.assertRaises(ValueError):
            MODELS.validate(broken)

    def test_registry_rejects_duplicate_and_unknown_models(self):
        registry = ModelRegistry((heater(),))
        with self.assertRaises(ValueError): registry.register(heater())
        with self.assertRaises(ValueError): registry.get('missing')


if __name__ == '__main__':
    unittest.main()
