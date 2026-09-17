import json
import tempfile
import unittest
from pathlib import Path

from app.evaluation import EvaluationDatasetError
from app.evaluation_metrics import evaluate_classification

ROOT=Path(__file__).parents[1]
DATA=ROOT/"evaluation/datasets/foundation-v1"
RUN=ROOT/"evaluation/baselines/relevance-rule-v0/run.json"

class EvaluationMetricTests(unittest.TestCase):
 def test_fixture_metrics_include_unknown_coverage_and_intervals(self):
  report=evaluate_classification(DATA,RUN)
  self.assertEqual((report.total,report.correct,report.abstained),(12,11,1))
  self.assertAlmostEqual(report.coverage,11/12)
  self.assertFalse(report.quality_claim_allowed)
  self.assertLess(report.accuracy_wilson_95[0],report.accuracy)
  self.assertEqual(report.confusion["unknown"]["relevant"],1)

 def test_missing_prediction_counts_as_unknown_not_success(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); run=json.loads(RUN.read_text()); run["predictions_file"]="predictions.jsonl"
   (root/"run.json").write_text(json.dumps(run))
   lines=(RUN.parent/"predictions.jsonl").read_text().splitlines()[:-1]
   (root/"predictions.jsonl").write_text("\n".join(lines)+"\n")
   report=evaluate_classification(DATA,root/"run.json")
  self.assertEqual(report.missing_predictions,1)
  self.assertEqual(report.abstained,2)

 def test_unknown_case_ids_are_rejected(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); run=json.loads(RUN.read_text()); run["predictions_file"]="predictions.jsonl"
   (root/"run.json").write_text(json.dumps(run))
   (root/"predictions.jsonl").write_text('{"case_id":"not-in-dataset","predicted_label":"relevant"}\n')
   with self.assertRaisesRegex(EvaluationDatasetError,"unknown cases"):
    evaluate_classification(DATA,root/"run.json")

if __name__=="__main__": unittest.main()
