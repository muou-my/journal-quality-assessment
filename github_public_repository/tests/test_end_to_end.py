"""One reproducible integration test with durable artifacts in an isolated temp dir."""
import json
import tempfile
import unittest
from pathlib import Path

from examples.end_to_end_test import run_integration


class EndToEndTests(unittest.TestCase):
    def test_ten_papers_all_modules_and_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'integration'
            summary=run_integration(root)
            self.assertTrue(summary['passed'],[c for c in summary['checks'] if not c['passed']])
            self.assertGreater(summary['check_count'],50)
            self.assertEqual(summary['live_model_calls'],0)
            self.assertTrue((root/'end_to_end_report.md').is_file())
            main=json.loads((root/'runs/complete_model_a_v1_p1/journal_scores.json').read_text(encoding='utf-8'))
            self.assertEqual(len(main['journals']),2)
            partial=json.loads((root/'runs/partial_then_recovered/journal_scores.json').read_text(encoding='utf-8'))
            self.assertEqual(partial['journals'][1]['coverage'],0.8)
            with self.assertRaises(ValueError):
                run_integration(root)  # Never overwrite a prior evaluation.


if __name__=='__main__':
    unittest.main()
