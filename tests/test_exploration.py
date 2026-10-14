from app.calculations import DEFAULT_MODEL_VALUES
import unittest

import numpy as np

from app.calculations import STEP
from app.exploration import MAP_CATEGORIES, assess_controller_result, controller_setting_map, sensitivity_runs
from app.simulation import LEAN_GAS, run_simulation


MODEL_VALUES = DEFAULT_MODEL_VALUES.copy()
DYNAMICS = {
    "kind": STEP,
    "start_time": 10.0,
    "simulation_duration": 100.0,
    "effect_duration": 1.0,
    "time_constant": 10.0,
    "delay": 2.0,
}


class ExplorationTests(unittest.TestCase):
    def oscillatory_result(self, decay, settled=False, saturated=False, step=0.1, scale=0.1):
        time = np.arange(0, 40 + step / 2, step)
        response = 0.3 + scale * np.exp(-decay * time) * np.sin(time)
        return {"time": time, "response_start": 0, "final_response": response,
                "metrics": {"steady_state": 0.3, "initial_value": 0.3,
                            "settling_time": 30 if settled else None},
                "model_values": dict(MODEL_VALUES),
                "controller": {"control_limit": 0.2},
                "control": np.full(time.size, 1 if saturated else 0.8)}

    def test_settled_damped_oscillation_and_saturation_are_independent(self):
        assessment = assess_controller_result(self.oscillatory_result(0.1, settled=True, saturated=True))
        self.assertEqual(assessment["category"], "Установилось за время опыта")
        self.assertEqual(assessment["oscillation"], "Затухающие колебания")
        self.assertTrue(assessment["saturated"])
        self.assertAlmostEqual(assessment["saturation_duration"], 40)
        self.assertIn("Насыщение η: 40 с", assessment["explanation"])

    def test_amplitude_trends_are_consistent_after_halving_step(self):
        for decay, expected in ((0.1, "Затухающие колебания"), (0, "Незатухающие колебания"),
                                (-0.03, "Растущие колебания")):
            for step in (0.1, 0.05):
                with self.subTest(decay=decay, step=step):
                    assessment = assess_controller_result(self.oscillatory_result(decay, step=step, scale=1e-7))
                    self.assertEqual(assessment["oscillation"], expected)
                    self.assertFalse(assessment["settled"])

    def test_short_horizon_does_not_mean_growing_oscillation(self):
        result = self.oscillatory_result(0.1, saturated=True)
        result["final_response"] = np.linspace(0.2, 0.28, len(result["time"]))
        assessment = assess_controller_result(result)
        self.assertEqual(assessment["category"], "Установление не подтверждено")
        self.assertEqual(assessment["oscillation"], "Колебания не выявлены")
        self.assertTrue(assessment["saturated"])

    def test_map_cell_and_single_run_use_identical_resolution(self):
        mapping = controller_setting_map(
            LEAN_GAS, MODEL_VALUES, 0.1, 0.0, DYNAMICS,
            "PID", (0.7,), (0.5,), 0.2, 1 / 6, derivative_time=0.03,
        )
        cell = mapping["results"][0][0]
        single = run_simulation(LEAN_GAS, MODEL_VALUES, 0.1, 0.0,
                                DYNAMICS, cell["controller"])
        np.testing.assert_array_equal(cell["time"], single["time"])
        np.testing.assert_array_equal(cell["final_response"], single["final_response"])

    def test_real_pi_assessment_survives_grid_refinement(self):
        settings = {"controller_type": "PI", "controller_gain": 6, "integral_time": 2,
                    "derivative_time": 0, "control_limit": 1, "setpoint": 1 / 6}
        assessments = [assess_controller_result(run_simulation(
            LEAN_GAS, MODEL_VALUES, 0.2, 0, DYNAMICS, settings, max_step=step,
        )) for step in (0.02, 0.01)]
        for key in ("category", "settled", "oscillation", "saturated"):
            self.assertEqual(assessments[0][key], assessments[1][key], key)

    def test_sensitivity_uses_each_selected_time_constant(self):
        runs = sensitivity_runs(
            LEAN_GAS, MODEL_VALUES, 0.1, 0.0, DYNAMICS, None,
            "Постоянная времени T", (5.0, 20.0),
        )

        self.assertEqual([run["value"] for run in runs], [5.0, 20.0])
        self.assertEqual(runs[0]["result"]["dynamics"]["time_constant"], 5.0)
        self.assertEqual(runs[1]["result"]["dynamics"]["time_constant"], 20.0)
        self.assertNotEqual(
            np.interp(20, runs[0]["result"]["time"], runs[0]["result"]["final_response"]),
            np.interp(20, runs[1]["result"]["time"], runs[1]["result"]["final_response"]),
        )

    def test_p_and_pi_maps_return_grid_categories(self):
        p_map = controller_setting_map(
            LEAN_GAS, MODEL_VALUES, 0.0, 0.1, DYNAMICS,
            "P", (0.2, 1.0, 3.0), (), 0.2, 1 / 6,
        )
        pi_map = controller_setting_map(
            LEAN_GAS, MODEL_VALUES, 0.0, 0.1, DYNAMICS,
            "PI", (0.2, 1.0), (3.0, 10.0), 0.2, 1 / 6,
        )

        self.assertEqual(p_map["categories"].shape, (1, 3))
        self.assertEqual(pi_map["categories"].shape, (2, 2))
        self.assertEqual(pi_map["assessments"][0][0], assess_controller_result(pi_map["results"][0][0]))
        self.assertTrue(all(
            MAP_CATEGORIES[code] in MAP_CATEGORIES
            for code in pi_map["categories"].flat
        ))

    def test_pid_map_keeps_selected_derivative_time(self):
        pid_map = controller_setting_map(
            LEAN_GAS, MODEL_VALUES, 0.0, 0.1, DYNAMICS,
            "PID", (0.2, 1.0), (3.0, 10.0), 0.2, 1 / 6,
            derivative_time=0.75,
        )

        self.assertEqual(pid_map["categories"].shape, (2, 2))
        self.assertEqual(pid_map["derivative_time"], 0.75)
        self.assertEqual(pid_map["results"][0][0]["controller"]["derivative_time"], 0.75)
