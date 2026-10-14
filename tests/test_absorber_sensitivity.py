import copy
import csv
import tempfile
import unittest
from pathlib import Path
from threading import Event

import numpy as np

from app.absorber_sensitivity import FIELD_DEFAULTS, assess, calculate, metrics, parse_fields, validate_result
from app.background_tasks import CalculationCancelled
from app.calculations import DEFAULT_MODEL_VALUES, absorption_balance
from app.physical_models import MODELS
from app.simulation import eta_gain, run_simulation


class AbsorberSensitivityTests(unittest.TestCase):
    def inputs(self, chain="rich_absorbent", direction=1):
        p = DEFAULT_MODEL_VALUES.copy()
        output = "xog" if chain == "lean_gas" else "xna"
        baseline = float(absorption_balance(**p)[output])
        request = dict(chain=chain, parameters=p, component=0., flow=0.,
            dynamics=dict(kind="step", start_time=3., effect_duration=1., simulation_duration=45., time_constant=4., delay=.2),
            controller=dict(controller_type="PI", controller_gain=2./abs(eta_gain(chain, p)),
                integral_time=4., derivative_time=0., setpoint=baseline+direction*.005, control_limit=.2))
        local = dict(gain=eta_gain(chain, p), time_constant=5., delay=.5)
        grid, requirements = parse_fields(dict(FIELD_DEFAULTS, k_errors="0;20", t_errors="0", l_errors="0;1"))
        return request, local, grid, requirements

    def test_both_channels_and_step_directions_preserve_physics(self):
        for chain in ("lean_gas", "rich_absorbent"):
            for direction in (-1, 1):
                with self.subTest(chain=chain, direction=direction):
                    request, local, grid, requirements = self.inputs(chain, direction)
                    before = copy.deepcopy(request)
                    result = calculate(request, local, grid, requirements)
                    validate_result(result)
                    self.assertEqual(request, before)
                    self.assertEqual(result["cases"][0]["run"], result["controls"][1]["run"])
                    output = "xog" if chain == "lean_gas" else "xna"
                    for case in result["controls"]+result["cases"]:
                        run = case["run"]; c = run["configuration"]["controller"]
                        direct = run_simulation(chain, request["parameters"], request["component"], request["flow"], request["dynamics"], c)
                        self.assertEqual(run, MODELS.result("absorber", direct))
                        self.assertEqual(run["parameters"], request["parameters"])
                        self.assertEqual(run["configuration"]["dynamics"], request["dynamics"])
                        self.assertEqual(c["process_gain"], abs(local["gain"]))
                        self.assertLess(abs(case["metrics"]["final_error"]), .005)
                        self.assertEqual(case["metrics"]["final_error"], c["setpoint"]-run["signals"][output][-1])
                        for signal in ("xog", "xna", "control"):
                            self.assertTrue(np.all((np.asarray(run["signals"][signal]) >= 0) & (np.asarray(run["signals"][signal]) <= 1)))
                        # First control must move in the signed physical direction.
                        self.assertEqual(np.sign(run["signals"]["control"][0]-request["parameters"]["eta"]), np.sign(local["gain"])*direction)

    def test_each_threshold_is_inclusive_and_missing_settling_fails(self):
        values = dict(final_error=-.01, iae=2., overshoot_percent=3., settling_time=4., saturation_duration=5., reachable=True)
        requirements = {key: abs(value) for key, value in values.items() if key != "reachable"}
        self.assertTrue(assess(values, requirements)["passed"])
        for key in requirements:
            self.assertEqual(assess(values, dict(requirements, **{key: 0.}))["reasons"], [key])
        self.assertIn("settling_time", assess(dict(values, settling_time=None), requirements)["reasons"])

    def test_unreachable_and_invalid_regimes_sign_and_limits(self):
        args = self.inputs()
        request, local, grid, requirements = args
        result = calculate(dict(request, controller=dict(request["controller"], setpoint=.5)), local, grid, requirements)
        self.assertTrue(all("unreachable" in case["assessment"]["reasons"] for case in result["controls"]+result["cases"]))
        invalid = [(request, dict(local, gain=-local["gain"])),
            (dict(request, parameters=dict(request["parameters"], xg=0.)), local),
            (dict(request, parameters=dict(request["parameters"], xg=1.)), local),
            (dict(request, controller=dict(request["controller"], setpoint=.3)), local),
            (dict(request, component=4.), local)]
        for r, p in invalid:
            with self.assertRaises(ValueError): calculate(r, p, grid, requirements)
        with self.assertRaises(ValueError): calculate(request, local, dict(grid, l=[-1.]), requirements)
        with self.assertRaises(ValueError): calculate(request, local, dict(grid, k=[-100.]), requirements)
        for key in ("parameters", "controller", "dynamics"):
            for missing in (None, "missing"):
                bad = copy.deepcopy(request)
                if missing is None: bad[key] = None
                else: bad.pop(key)
                with self.assertRaises(ValueError): calculate(bad, local, grid, requirements)

    def test_noise_actuator_and_transient_disturbance_conditions_remain_fixed(self):
        request, local, grid, requirements = self.inputs()
        request["controller"].update(noise_std=.0001, noise_seed=17, actuator_time_constant=.3, actuator_rate_limit=.05)
        request["component"] = .001
        for kind in ("impulse", "rectangle", "ramp"):
            request["dynamics"].update(kind=kind, effect_duration=1.)
            result = calculate(request, local, dict(grid, k=[0.], l=[0.]), requirements)
            validate_result(result)
            self.assertEqual(result["cases"][0]["run"], result["controls"][1]["run"])

    def test_fixed_settling_band_does_not_expand_with_overshoot(self):
        request, local, grid, requirements = self.inputs()
        result = calculate(request, local, grid, requirements)
        run = copy.deepcopy(result["cases"][0]["run"])
        t = np.asarray(run["time"])
        y = np.full(t.size, request["controller"]["setpoint"])
        y[0] = .3; y[1] = .9  # A large early spike must not widen the band.
        y[t >= 10] += .001
        run["signals"]["xna"] = y.tolist()
        self.assertIsNone(metrics(run, result["request"])["settling_time"])
        request["dynamics"]["start_time"] = 44.
        self.assertIsNone(metrics(run, request)["settling_time"])

    def test_corrupt_snapshots_reject_metrics_physics_and_partial_horizon(self):
        result = calculate(*self.inputs())
        for kind in ("metric", "bool_metric", "physics", "controller", "delay", "estimate", "offset", "signal", "time", "missing", "huge"):
            bad = copy.deepcopy(result); case = bad["cases"][0]; run = case["run"]
            if kind == "metric": case["metrics"]["iae"] = 0.
            if kind == "bool_metric": case["metrics"]["iae"] = False
            if kind == "physics": run["parameters"]["gg"] += 1
            if kind == "controller": run["configuration"]["controller"]["controller_gain"] += 1
            if kind == "delay": run["configuration"]["dynamics"]["delay"] += 1
            if kind == "estimate": case["estimate"]["gain"] += 1
            if kind == "offset": case["offsets"][0] = 1.
            if kind == "signal": run["signals"]["component"][0] = .01
            if kind == "time":
                run["time"] = run["time"][:-1]
                run["signals"] = {k: v[:-1] for k, v in run["signals"].items()}
            if kind == "missing": bad["cases"].pop()
            if kind == "huge": bad["local_model"]["gain"] = 10**400
            with self.subTest(kind=kind), self.assertRaises(ValueError): validate_result(bad)

    def test_cancellation_progress_budget_and_full_exports(self):
        args = self.inputs()
        event = Event(); event.set()
        with self.assertRaises(CalculationCancelled): calculate(*args, cancel=event)
        fractions = []
        result = calculate(*args, progress=fractions.append)
        self.assertEqual(fractions[-1], 1.)
        self.assertEqual(fractions, sorted(fractions))
        request, local, grid, requirements = args
        request["dynamics"]["simulation_duration"] = 100000.
        with self.assertRaises(ValueError): calculate(request, local, grid, requirements)
        from app.absorber_sensitivity_exporting import write_report
        result["exported_stale"] = True
        with tempfile.TemporaryDirectory() as folder:
            for fmt in ("csv", "html", "pdf"):
                path = Path(folder)/f"errors.{fmt}"
                write_report(path, result, 1, fmt)
                self.assertGreater(path.stat().st_size, 1000)
            with (Path(folder)/"errors.csv").open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.reader(stream, delimiter=";"))
            self.assertEqual(len(rows)-1, sum(len(c["run"]["time"]) for c in result["controls"]+result["cases"]))
            self.assertIn("Xог, доля", rows[0]); self.assertIn("Xна, доля", rows[0])


if __name__ == "__main__":
    unittest.main()
