import copy
import json
import unittest
from pathlib import Path

import numpy as np

from app import heater
from app.expressions import Formula
from app.identification import bootstrap_intervals
from app.model_contract import ModelRegistry
from app.object_file import load_description

EXAMPLE = Path(__file__).resolve().parents[1] / "docs/examples/heater_object.json"


def example():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


class FormulaTests(unittest.TestCase):
    def test_arithmetic_functions_and_names(self):
        formula = Formula("-a**2 + sqrt(b) / 2 - max(a, 1) * exp(0)", {"a", "b"})
        self.assertEqual(formula({"a": 3., "b": 16.}), -9 + 2 - 3)
        self.assertEqual(formula.names, {"a", "b"})

    def test_code_and_unknown_names_are_rejected_before_evaluation(self):
        for text in ("__import__('os').system('true')", "a.real", "(lambda: 1)()", "[a for a in b]", "'text'", "c + 1",
                     "exp(x=1)", "a if b else 1", "a < b", "open('f')", "", "1" * 501):
            with self.subTest(text=text), self.assertRaises(ValueError):
                Formula(text, {"a", "b"})

    def test_evaluation_errors_are_reported(self):
        for text, values in (("a / b", {"a": 1., "b": 0.}), ("sqrt(a)", {"a": -1., "b": 0.}),
                             ("a ** 0.5", {"a": -1., "b": 0.}), ("a ** b", {"a": 2., "b": 1000.}),
                             ("exp(a)", {"a": 1e4, "b": 0.})):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "Формула"):
                Formula(text, {"a", "b"})(values)


class ObjectFileTests(unittest.TestCase):
    def setUp(self):
        self.description = load_description(EXAMPLE)
        self.values = {p.key: p.default for p in self.description.parameters}

    def test_file_heater_matches_built_in_heater(self):
        run = self.description.simulate(self.values)
        built_in = heater.simulate(dict(heater.DEFAULTS))
        for own, other in (("temperature", "temperature"), ("measured", "measured"), ("heating", "power"), ("commanded", "commanded")):
            np.testing.assert_allclose(run[own], built_in[other], rtol=0, atol=1e-9, err_msg=own)
        controller = dict(gain=3., integral_time=300., setpoint=45.)
        run = self.description.simulate(self.values, controller=controller)
        built_in = heater.simulate(dict(heater.DEFAULTS), controller=controller)
        np.testing.assert_allclose(run["temperature"], built_in["temperature"], rtol=0, atol=1e-9)
        np.testing.assert_allclose(run["heating"], built_in["power"], rtol=0, atol=1e-9)
        for key in ("final_error", "iae", "overshoot_percent", "settling_time", "saturation_duration", "status"):
            self.assertAlmostEqual(run["metrics"][key], built_in["metrics"][key], places=6, msg=key) \
                if isinstance(run["metrics"][key], float) else self.assertEqual(run["metrics"][key], built_in["metrics"][key])

    def test_identification_tuning_and_stored_records_are_checked(self):
        run = self.description.simulate(self.values)
        fit = dict(self.description.identify(run, .05, 1), revision=1)
        built_in = heater.identify(heater.simulate(dict(heater.DEFAULTS)), .05, 1)
        for key in ("gain", "time_constant", "delay"):
            self.assertAlmostEqual(fit[key], built_in[key], places=6)
        for key, value in heater.pi_tuning(built_in).items():
            self.assertAlmostEqual(self.description.pi_settings(fit)[key], value, places=9)
        fit["uncertainty"] = bootstrap_intervals(fit, replicates=20)
        self.assertIs(self.description.validate_fit(fit), fit)
        broken = copy.deepcopy(fit); broken["output"][100] += .1
        with self.assertRaises(ValueError):
            self.description.validate_fit(broken)
        with self.assertRaises(ValueError):
            self.description.identify(self.description.simulate(dict(self.values, power_step=0)), .05, 1)
        state = dict(model_id="file_heater", fields=self.description.field_defaults(), revision=1, view="Идентификация",
                     result=dict(run, revision=1), pi=None, fit=fit)
        self.assertIs(self.description.validate_state(state), state)
        tampered = copy.deepcopy(state); tampered["result"]["temperature"][10] += 1
        with self.assertRaises(ValueError):
            self.description.validate_state(tampered)
        self.assertEqual(self.description.field_defaults()["setpoint"], "45")

    def test_common_result_is_recomputed_on_validation(self):
        registry = ModelRegistry((self.description.model(),))
        run = self.description.simulate(self.values, controller=dict(gain=3., integral_time=300., setpoint=45.))
        result = registry.result("file_heater", run)
        self.assertEqual(result["units"]["temperature"], "°C")
        self.assertEqual(registry.validate(copy.deepcopy(result)), result)
        tampered = copy.deepcopy(result); tampered["signals"]["temperature"][50] += 1
        with self.assertRaises(ValueError):
            registry.validate(tampered)
        tampered = copy.deepcopy(result); tampered["limits"][1]["value"] = 30.
        with self.assertRaises(ValueError):
            registry.validate(tampered)

    def test_invalid_descriptions_are_rejected(self):
        changes = (dict(format="other"), dict(id="Bad id"), dict(name=""),
                   dict(parameters=[]), dict(states=[{"key": "mass", "label": "x", "unit": "x", "initial": "1", "derivative": "0"}]),
                   dict(output={"key": "measured", "label": "x", "state": "unknown"}),
                   dict(input=dict(example()["input"], key="t")),
                   dict(states=[dict(example()["states"][0], derivative="temperature + unknown")]),
                   dict(states=[dict(example()["states"][0], derivative="__import__('os')")]),
                   dict(pi={"setpoint": 45, "extra": 1}))
        for change in changes:
            with self.subTest(change=list(change)), self.assertRaises(ValueError):
                load_description(dict(example(), **change))
        with self.assertRaises(ValueError):
            self.description.simulate(dict(self.values, mass=0))
        with self.assertRaises(ValueError):
            self.description.simulate(dict(self.values, sample_time=.01))


if __name__ == "__main__":
    unittest.main()
