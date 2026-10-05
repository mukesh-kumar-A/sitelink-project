import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluate_examples import evaluate


class LabeledExampleTests(unittest.TestCase):
    def test_small_synthetic_benchmark_is_reproducible_and_reports_counts(self):
        result = evaluate()
        self.assertEqual(result["example_count"], 5)
        self.assertGreater(result["field_checks"], 0)
        self.assertEqual(result["top3_matches"], 5)
        self.assertEqual(len(result["cases"]), 5)
        self.assertIn("not a production accuracy", result["caveat"])


if __name__ == "__main__":
    unittest.main()
