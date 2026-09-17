import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.evaluation import EvaluationDatasetError, validate_evaluation_dataset

FIXTURE=Path(__file__).parents[1]/"evaluation/datasets/foundation-v1"

class EvaluationDatasetTests(unittest.TestCase):
 def test_foundation_fixture_reports_gaps_without_claiming_quality(self):
  report=validate_evaluation_dataset(FIXTURE)
  self.assertEqual(report.cases,12); self.assertEqual(report.event_groups,7)
  self.assertFalse(report.publishable_gold)
  self.assertEqual(report.target_gaps["documents"],588)
  self.assertTrue(any("synthetic" in warning for warning in report.warnings))

 def changed(self,mutate):
  temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
  root=Path(temp.name)/"dataset"; shutil.copytree(FIXTURE,root)
  rows=[json.loads(line) for line in (root/"cases.jsonl").read_text().splitlines()]
  mutate(rows); (root/"cases.jsonl").write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows))
  return root

 def test_event_and_origin_groups_cannot_leak_across_splits(self):
  root=self.changed(lambda rows: rows[1].update(split="test"))
  with self.assertRaisesRegex(EvaluationDatasetError,"leaks across"):
   validate_evaluation_dataset(root)

 def test_embedded_text_hash_is_verified(self):
  root=self.changed(lambda rows: rows[0].update(text="changed"))
  with self.assertRaisesRegex(EvaluationDatasetError,"hash mismatch"):
   validate_evaluation_dataset(root)

 def test_model_generated_annotation_cannot_be_gold(self):
  def mutate(rows): rows[0]["annotation"].update(state="adjudicated",generated_by_model=True)
  root=self.changed(mutate)
  with self.assertRaisesRegex(EvaluationDatasetError,"model output as gold"):
   validate_evaluation_dataset(root)

if __name__=="__main__": unittest.main()
