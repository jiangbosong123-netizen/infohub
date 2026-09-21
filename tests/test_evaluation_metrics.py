import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.evaluation import EvaluationDatasetError
from app.evaluation_metrics import evaluate_classification

ROOT=Path(__file__).parents[1]
DATA=ROOT/"evaluation/datasets/foundation-v1"
RUN=ROOT/"evaluation/baselines/relevance-rule-v0/run.json"

class EvaluationMetricTests(unittest.TestCase):
 def v2_run(self,root,split):
  cases=[json.loads(line) for line in (DATA/"cases.jsonl").read_text().splitlines()]
  selected={case["case_id"] for case in cases if case["split"]==split}
  predictions=[json.loads(line) for line in (RUN.parent/"predictions.jsonl").read_text().splitlines()]
  lines=''.join(json.dumps(row)+'\n' for row in predictions if row["case_id"] in selected)
  (root/"predictions.jsonl").write_text(lines)
  run={"schema_version":"prediction-run-v2","prediction_run_id":"fixture-v2",
       "dataset_version":"foundation-v1","task":"relevance","label_path":"relevance",
       "split":split,"method_id":"keyword-rule","method_version":"v0",
       "method_config_sha256":hashlib.sha256(b"fixture config").hexdigest(),
       "generated_at":"2026-09-21T10:00:00Z","predictions_file":"predictions.jsonl",
       "abstain_labels":["__abstain__"],
       "dataset_manifest_sha256":hashlib.sha256((DATA/"manifest.json").read_bytes()).hexdigest(),
       "dataset_cases_sha256":hashlib.sha256((DATA/"cases.jsonl").read_bytes()).hexdigest(),
       "predictions_sha256":hashlib.sha256(lines.encode()).hexdigest()}
  (root/"run.json").write_text(json.dumps(run))
  return root/"run.json"

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

 def test_v2_test_and_security_reports_are_separate(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td)
   test_report=evaluate_classification(DATA,self.v2_run(root,"test"))
   self.assertEqual((test_report.split,test_report.total),("test",3))
   self.assertFalse(test_report.quality_claim_allowed)
   security_report=evaluate_classification(DATA,self.v2_run(root,"security"))
   self.assertEqual((security_report.split,security_report.total),("security",2))
   self.assertTrue(any("separately" in warning for warning in security_report.warnings))

 def test_v2_rejects_other_split_predictions_and_changed_files(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); run_path=self.v2_run(root,"test")
   run=json.loads(run_path.read_text())
   extra={"case_id":"fixture-001","predicted_label":"relevant"}
   with (root/"predictions.jsonl").open("a") as handle: handle.write(json.dumps(extra)+"\n")
   with self.assertRaisesRegex(EvaluationDatasetError,"predictions_sha256 mismatch"):
    evaluate_classification(DATA,run_path)
   run["predictions_sha256"]=hashlib.sha256((root/"predictions.jsonl").read_bytes()).hexdigest()
   run_path.write_text(json.dumps(run))
   with self.assertRaisesRegex(EvaluationDatasetError,"other-split"):
    evaluate_classification(DATA,run_path)

 def test_v2_missing_predictions_block_claim_and_count_abstention(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); run_path=self.v2_run(root,"test")
   lines=(root/"predictions.jsonl").read_text().splitlines()[:-1]
   content='\n'.join(lines)+'\n'
   (root/"predictions.jsonl").write_text(content)
   run=json.loads(run_path.read_text())
   run["predictions_sha256"]=hashlib.sha256(content.encode()).hexdigest()
   run_path.write_text(json.dumps(run))
   report=evaluate_classification(DATA,run_path)
   self.assertEqual(report.missing_predictions,1)
   self.assertEqual(report.abstained,1)
   self.assertFalse(report.quality_claim_allowed)

 def test_v2_rejects_abstain_marker_that_is_also_a_gold_label(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); run_path=self.v2_run(root,"security")
   run=json.loads(run_path.read_text())
   run["abstain_labels"]=["unknown"]
   run_path.write_text(json.dumps(run))
   with self.assertRaisesRegex(EvaluationDatasetError,"must differ from gold labels"):
    evaluate_classification(DATA,run_path)

 def test_v2_rejects_dataset_cases_changed_after_prediction_run(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); copy=root/"dataset"; shutil.copytree(DATA,copy)
   run_path=self.v2_run(root,"test")
   rows=[json.loads(line) for line in (copy/"cases.jsonl").read_text().splitlines()]
   (copy/"cases.jsonl").write_text(''.join(json.dumps(row,separators=(',',':'))+'\n' for row in rows))
   with self.assertRaisesRegex(EvaluationDatasetError,"dataset_cases_sha256 mismatch"):
    evaluate_classification(copy,run_path)

if __name__=="__main__": unittest.main()
