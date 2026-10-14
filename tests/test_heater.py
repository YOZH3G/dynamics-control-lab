import copy
import math
import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.heater import (BOILING, DEFAULTS, FIELD_DEFAULTS, equilibrium_power, equilibrium_temperature, identification_data,
                        identify, pi_tuning, simulate, time_constant, validate_fit, validate_inputs, validate_run,
                        validate_state)
from app.identification import bootstrap_intervals
from app.model_contract import read_result, write_result
from app.physical_models import MODELS, run_model


class HeaterTests(unittest.TestCase):
    def test_open_loop_matches_analytic_response_and_energy_balance(self):
        p = validate_inputs(DEFAULTS)
        run = simulate(DEFAULTS)
        time, temperature = np.asarray(run["time"]), np.asarray(run["temperature"])
        start, tau = run["initial_temperature"], time_constant(p)
        final = equilibrium_temperature(p, p["power"] + p["power_step"])
        expected = np.where(time <= p["step_time"], start, final + (start - final) * np.exp(-(time - p["step_time"]) / tau))
        np.testing.assert_allclose(temperature, expected, atol=1e-10)
        self.assertAlmostEqual(equilibrium_power(p, start), p["power"], places=10)
        # Stored energy equals the heater energy (held per interval) plus the smooth exchange terms.
        c, w, ua = p["heat_capacity"], p["flow"], p["loss_coefficient"]
        heater_energy = float(np.dot(np.asarray(run["power"])[:-1], np.diff(time)))
        exchange = w * c * (p["inlet_temperature"] - temperature) - ua * (temperature - p["ambient_temperature"])
        stored = p["mass"] * c * (temperature[-1] - temperature[0])
        self.assertAlmostEqual(stored, heater_energy + np.trapezoid(exchange, time), delta=1e-4 * abs(stored))

    def test_sensor_delay_and_input_steps(self):
        run = simulate(dict(DEFAULTS, power_step=0., inlet_step=10., flow_step=.05, sensor_delay=30.))
        time, temperature, measured = (np.asarray(run[k]) for k in ("time", "temperature", "measured"))
        later = time > 30
        np.testing.assert_allclose(measured[later], np.interp(time[later] - 30, time, temperature), atol=1e-12)
        self.assertEqual(run["inlet"][-1], DEFAULTS["inlet_temperature"] + 10)
        self.assertAlmostEqual(run["flow"][-1], .15)
        p = validate_inputs(DEFAULTS)
        self.assertAlmostEqual(temperature[-1], equilibrium_temperature(p, p["power"], .15, 25.), delta=.05)

    def test_boiling_stops_at_exact_time(self):
        values = dict(DEFAULTS, power_step=8., flow_step=-.09)
        run = simulate(values)
        p = validate_inputs(values)
        start = run["initial_temperature"]
        final = equilibrium_temperature(p, 20., .01)
        rate = 1 / time_constant(p, .01)
        expected = p["step_time"] + math.log((final - start) / (final - BOILING)) / rate
        self.assertEqual(run["event"]["kind"], "boiling")
        self.assertAlmostEqual(run["event"]["time"], expected, places=6)
        self.assertEqual(run["temperature"][-1], BOILING)

    def test_pi_settles_saturates_and_reports_unreachable_setpoint(self):
        run = simulate(DEFAULTS, controller=dict(gain=3., integral_time=300., setpoint=45.))
        m = run["metrics"]
        self.assertEqual(m["status"], "settled")
        self.assertLess(abs(m["final_error"]), .01)
        self.assertGreater(m["saturation_duration"], 0)
        self.assertTrue(all(0 <= q <= DEFAULTS["power_limit"] for q in run["power"]))
        unreachable = simulate(DEFAULTS, controller=dict(gain=3., integral_time=300., setpoint=95.))
        self.assertIn(unreachable["metrics"]["status"], ("unreachable", "physical_stop"))
        self.assertFalse(unreachable["metrics"]["reachable"])

    def test_invalid_inputs_are_rejected(self):
        for change in (dict(mass=0.), dict(flow=0., loss_coefficient=0.), dict(power=25.), dict(step_time=3000.),
                       dict(power=99.), dict(horizon=1e6, sample_time=1.), dict(flow_step=-.2), dict(mass=True)):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_inputs(dict(DEFAULTS, **change))
        with self.assertRaises(ValueError):
            validate_inputs({k: v for k, v in DEFAULTS.items() if k != "mass"})
        with self.assertRaises(ValueError):
            simulate(DEFAULTS, controller=dict(gain=-1., integral_time=300., setpoint=45.))

    def test_stored_runs_states_and_common_results_are_checked(self):
        run = simulate(DEFAULTS, controller=dict(gain=3., integral_time=300., setpoint=45.))
        self.assertIs(validate_run(run), run)
        for key, index in (("temperature", 100), ("power", 5)):
            broken = copy.deepcopy(run); broken[key][index] += .5
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_run(broken)
        state = dict(model_id="heated_tank", model_version=1, fields=dict(FIELD_DEFAULTS), revision=2, view="PI",
                     result=dict(simulate(DEFAULTS), revision=1), pi=dict(run, revision=2))
        self.assertIs(validate_state(state), state)
        for change in (dict(revision=0), dict(view="Неизвестно"), dict(pi=dict(simulate(DEFAULTS), revision=1))):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_state(dict(state, **change))
        common = MODELS.result("heated_tank", run_model("heated_tank", dict(parameters=DEFAULTS)))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "heater.json"
            write_result(path, common, MODELS)
            self.assertEqual(read_result(path, MODELS), common)
        tampered = copy.deepcopy(common); tampered["signals"]["temperature"][50] += 1.
        with self.assertRaises(ValueError):
            MODELS.validate(tampered)


    def test_identification_recovers_physics_and_tuned_pi_settles(self):
        run = simulate(DEFAULTS)
        fit = identify(run, .05, 1)
        physical = fit["physical"]
        self.assertAlmostEqual(physical["gain"], 1 / (.1 * 4.19 + .05))
        self.assertAlmostEqual(physical["time_constant"], time_constant(DEFAULTS))
        self.assertAlmostEqual(fit["gain"], physical["gain"], delta=.03 * physical["gain"])
        self.assertAlmostEqual(fit["time_constant"], physical["time_constant"], delta=.05 * physical["time_constant"])
        self.assertAlmostEqual(fit["delay"], DEFAULTS["sensor_delay"], delta=3.)
        self.assertEqual(identify(run, .05, 1), fit)
        settings = pi_tuning(fit)
        self.assertTrue(all(math.isfinite(v) and v > 0 for v in settings.values()))
        tuned = simulate(DEFAULTS, controller=dict(settings, setpoint=45.))
        self.assertEqual(tuned["metrics"]["status"], "settled")
        self.assertLess(tuned["metrics"]["overshoot_percent"], 10)

    def test_identification_needs_a_power_step_and_valid_noise(self):
        for change in (dict(inlet_step=2.), dict(flow_step=.02), dict(power_step=0.)):
            with self.subTest(change=change), self.assertRaises(ValueError):
                identification_data(simulate(dict(DEFAULTS, **change)), .05, 1)
        with self.assertRaises(ValueError):
            identification_data(simulate(DEFAULTS, controller=dict(gain=3., integral_time=300., setpoint=45.)), .05, 1)
        run = simulate(DEFAULTS)
        for noise, seed in ((-1., 1), (math.nan, 1), (.05, -1), (.05, True), (.05, 1.5)):
            with self.subTest(noise=noise, seed=seed), self.assertRaises(ValueError):
                identification_data(run, noise, seed)

    def test_stored_fits_and_older_states_are_checked(self):
        fit = dict(identify(simulate(DEFAULTS), .05, 2), revision=1)
        fit["uncertainty"] = bootstrap_intervals(fit, replicates=20)
        self.assertIs(validate_fit(fit), fit)
        for key, change in (("gain", 1.01), ("output", None), ("noise", .06), ("physical", None)):
            broken = copy.deepcopy(fit)
            if key == "output":
                broken["output"][300] += .1
            elif key == "physical":
                broken["physical"]["delay"] += 1
            else:
                broken[key] = fit[key] * change if key == "gain" else change
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_fit(broken)
        state = dict(model_id="heated_tank", model_version=1, fields=dict(FIELD_DEFAULTS), revision=1, view="Идентификация",
                     result=dict(simulate(DEFAULTS), revision=1), pi=None, fit=fit)
        self.assertIs(validate_state(state), state)
        with self.assertRaises(ValueError):
            validate_state(dict(state, revision=0))
        older = dict(state, fields={k: v for k, v in FIELD_DEFAULTS.items() if k not in ("noise", "seed")}, view="PI")
        del older["fit"]
        restored = validate_state(older)
        self.assertEqual(restored["fields"], FIELD_DEFAULTS)
        self.assertNotIn("noise", older["fields"])


if __name__ == "__main__":
    unittest.main()
