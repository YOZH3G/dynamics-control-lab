import copy
import math
import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.heater import (BOILING, DEFAULTS, FIELD_DEFAULTS, equilibrium_power, equilibrium_temperature, simulate,
                        time_constant, validate_inputs, validate_run, validate_state)
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


if __name__ == "__main__":
    unittest.main()
