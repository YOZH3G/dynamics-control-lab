import tempfile
import unittest
from pathlib import Path
from threading import Event

import numpy as np

from app.background_tasks import CalculationCancelled
from app.identification import experiment_columns, export_identification, identify_step, read_experiment, validate_step_quality


def experiment(constant=8., delay=2., gain=.4, irregular=False):
    time = np.linspace(0, 80, 401)
    if irregular:
        time = np.r_[np.linspace(0, 10, 51), np.linspace(10.2, 80, 287)]
    signal = np.where(time < 10, 0., 1.)
    output = .2 + gain * -np.expm1(-np.maximum(time - 10 - delay, 0) / constant)
    return time, signal, output


class IdentificationTests(unittest.TestCase):
    def test_quality_roundtrip_and_tampered_diagnostics(self):
        import copy
        import json
        result = identify_step(*experiment(irregular=True))
        record = {k: v.tolist() if isinstance(v, np.ndarray) else v for k, v in result.items()}
        restored = json.loads(json.dumps(record))
        validate_step_quality(restored)
        residual = np.asarray(result["residual"])
        expected = np.sqrt(np.trapezoid(residual**2, result["time"])/np.ptp(result["time"]))
        self.assertAlmostEqual(result["rmse"], expected, places=14)
        for key in ("quality", "model", "rmse", "amplitude"):
            bad = copy.deepcopy(restored)
            if key == "quality": bad[key]["response_rmse"] = 1
            elif key == "model": bad[key][-1] += .01
            else: bad[key] += .01
            with self.assertRaisesRegex(ValueError, "качества"):
                validate_step_quality(bad)
        old = copy.deepcopy(restored); old.pop("quality")
        validate_step_quality(old)

    def test_reference_parameters_including_negative_gain_and_irregular_times(self):
        for constant, delay, gain, irregular in ((8, 2, .4, False), (12, 3.7, 2, False),
                                               (8, 0, -1.5, False), (3, 1.3, .4, True)):
            with self.subTest(constant=constant, delay=delay, gain=gain):
                result = identify_step(*experiment(constant, delay, gain, irregular))
                self.assertAlmostEqual(result["time_constant"], constant, delta=constant * .01)
                self.assertAlmostEqual(result["gain"], gain, delta=abs(gain) * .01)
                self.assertAlmostEqual(result["delay"], delay, delta=.2)
                self.assertLess(result["rmse"], .0001)

    def test_rejects_insufficient_excitation_missing_unsorted_and_multiple_steps(self):
        t, u, y = experiment()
        for arrays in ((t, np.zeros_like(u), y), (t, u, np.full_like(y, .2)),
                       (t[::-1], u, y), (np.r_[t[:20], t[19:]], np.r_[u[:20], u[19:]], np.r_[y[:20], y[19:]]),
                       (t, np.where(t > 50, 0, u), y), (t[:75], u[:75], y[:75]),
                       (t, u, np.where(t > 20, float("nan"), y))):
            with self.subTest(size=len(arrays[0])), self.assertRaises(ValueError):
                identify_step(*arrays)

    def test_cancelled_fit_and_noisy_fit(self):
        t, u, y = experiment()
        cancel = Event()
        cancel.set()
        with self.assertRaises(CalculationCancelled):
            identify_step(t, u, y, cancel=cancel)
        result = identify_step(t, u, y + np.random.default_rng(42).normal(0, .001, t.size))
        self.assertAlmostEqual(result["time_constant"], 8, delta=.1)
        self.assertAlmostEqual(result["delay"], 2, delta=.1)

    def test_fast_unresolved_transition_and_baseline_drift_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "слишком редкие"):
            identify_step(*experiment(constant=.001))
        t, u, y = experiment()
        with self.assertRaisesRegex(ValueError, "дрейфует"):
            identify_step(t, u, y + .002 * t)

    def test_csv_units_errors_and_export_preserve_snapshot(self):
        t, u, y = experiment()
        rows = [[str(a / 60), str(b * 100), str(c * 100)] for a, b, c in zip(t, u, y)]
        arrays = experiment_columns(["t", "u", "y"], rows, ("t", "u", "y"), ("мин", "%", "%"))
        for actual, expected in zip(arrays, (t, u, y)):
            np.testing.assert_allclose(actual, expected, atol=1e-14)
        rows[2][1] = ""
        with self.assertRaisesRegex(ValueError, "строка 4"):
            experiment_columns(["t", "u", "y"], rows, ("t", "u", "y"), ("с", "%", "%"))
        with tempfile.TemporaryDirectory() as directory:
            result = identify_step(t, u, y)
            path = Path(directory) / "result.csv"
            export_identification(path, result, "csv")
            headers, data = read_experiment(path)
            self.assertEqual(len(data), len(t))
            np.testing.assert_array_equal(np.array([[float(v) for v in row] for row in data]),
                                          np.column_stack([result[k] for k in ("time", "input", "output", "model", "residual")]))


if __name__ == "__main__":
    unittest.main()
