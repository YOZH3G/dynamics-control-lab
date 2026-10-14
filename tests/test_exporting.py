from app.calculations import DEFAULT_MODEL_VALUES
import tempfile
import unittest
from pathlib import Path

from matplotlib.figure import Figure

from app.calculations import STEP
from app.exporting import (
    build_comparison_figures,
    build_protocol,
    save_graphs,
    write_comparison_html_report,
    write_comparison_pdf_report,
    write_csv,
    write_html_report,
    write_pdf_report,
    write_protocol,
    _text_pages,
)
from app.simulation import LEAN_GAS, run_simulation


class ExportTests(unittest.TestCase):
    def test_long_text_report_retains_the_last_line_on_additional_pages(self):
        pages = _text_pages("Report", [("Protocol", "\n".join(f"line {index}" for index in range(120)))])
        self.assertGreater(len(pages), 1)
        texts = [text.get_text() for page in pages for axis in page.axes for text in axis.texts]
        self.assertIn("line 119", texts)

    def setUp(self):
        model_values = DEFAULT_MODEL_VALUES.copy()
        dynamics = {
            "kind": STEP,
            "start_time": 10.0,
            "simulation_duration": 100.0,
            "effect_duration": 1.0,
            "time_constant": 10.0,
            "delay": 2.0,
        }
        self.result = run_simulation(
            LEAN_GAS,
            model_values,
            component_fraction=0.0,
            flow_fraction=0.1,
            dynamics=dynamics,
        )
        self.result["disturbance_type"] = "Ступенчатое"

    def test_csv_contains_time_targets_and_responses(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_csv(Path(directory) / "result.csv", self.result)
            text = path.read_text(encoding="utf-8-sig")

        header, first_row, *_rest = text.splitlines()
        self.assertIn("Время, с", header)
        self.assertIn("Отклик: Совместное воздействие", header)
        self.assertTrue(first_row.startswith("0.0;"))

    def test_protocol_is_built_and_written(self):
        protocol = build_protocol(self.result, "Учебный сценарий")

        with tempfile.TemporaryDirectory() as directory:
            path = write_protocol(Path(directory) / "protocol.txt", protocol)
            saved = path.read_text(encoding="utf-8")

        self.assertIn("Учебный сценарий", saved)
        self.assertIn("- Запаздывание L: 2 с", saved)
        self.assertIn("- Расчётное значение: 16.666667%", saved)

    def test_two_graphs_are_saved_with_distinct_suffixes(self):
        signal_figure = Figure()
        signal_figure.add_subplot(111).plot([0, 1], [0, 1])
        response_figure = Figure()
        response_figure.add_subplot(111).plot([0, 1], [1, 0])

        with tempfile.TemporaryDirectory() as directory:
            signal_path, response_path = save_graphs(
                signal_figure,
                response_figure,
                Path(directory) / "result.png",
            )
            signal_exists = signal_path.exists() and signal_path.stat().st_size > 0
            response_exists = response_path.exists() and response_path.stat().st_size > 0

        self.assertEqual(signal_path.name, "result_signals.png")
        self.assertEqual(response_path.name, "result_response.png")
        self.assertTrue(signal_exists)
        self.assertTrue(response_exists)

    def test_html_report_contains_lesson_and_embedded_graphs(self):
        figure = Figure()
        figure.add_subplot(111).plot([0, 1], [0, 1])
        lesson = {
            "task": "Оцените реакцию объекта.",
            "guidance": "Сначала сделайте прогноз.",
            "questions": ["Как влияет T?"],
        }
        evaluation = {
            "score": 4,
            "total": 5,
            "criteria": [{"label": "Направление", "passed": True, "points": 1, "maximum": 1}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = write_html_report(
                Path(directory) / "report.html", self.result, "Учебный сценарий",
                lesson, evaluation, "Иванов", "Регулятор уменьшил отклонение.",
                (figure,),
                prediction={"direction": "Увеличится"},
                comparison_runs=[{
                    "name": "Опыт 1", "controller_type": "PI",
                    "maximum_deviation": 0.02, "settling_duration": 12.0,
                }],
            )
            text = path.read_text(encoding="utf-8")

        self.assertIn("Иванов", text)
        self.assertIn("Оцените реакцию объекта.", text)
        self.assertIn("Прогноз студента", text)
        self.assertIn("Опыт 1", text)
        self.assertIn("data:image/png;base64", text)

    def test_pdf_reports_and_comparison_report_include_all_runs(self):
        figure = Figure()
        figure.add_subplot(111).plot([0, 1], [0, 1])
        runs = [
            {
                "name": "Опыт 1", "time": [0, 1], "response": [0.8, 0.82],
                "controller_type": "PI", "maximum_deviation": 0.02,
                "settling_duration": 10.0,
            },
            {
                "name": "Опыт 2", "time": [0, 1], "response": [0.8, 0.79],
                "controller_type": "—", "maximum_deviation": 0.01,
                "settling_duration": None,
            },
        ]
        lesson = {"task": "Проверьте отклик.", "guidance": "Сравните опыты.", "questions": []}
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            report_path = write_pdf_report(
                directory / "lab.pdf", self.result, "Сценарий", lesson, None,
                "Иванов", "Вывод", (figure,), comparison_runs=runs,
            )
            comparison_html = write_comparison_html_report(directory / "comparison.html", runs)
            comparison_pdf = write_comparison_pdf_report(directory / "comparison.pdf", runs)
            html_text = comparison_html.read_text(encoding="utf-8")

            self.assertTrue(report_path.read_bytes().startswith(b"%PDF"))
            self.assertTrue(comparison_pdf.read_bytes().startswith(b"%PDF"))
            self.assertIn("Опыт 1", html_text)
            self.assertIn("Опыт 2", html_text)
            self.assertEqual(len(build_comparison_figures(runs)), 2)


if __name__ == "__main__":
    unittest.main()
