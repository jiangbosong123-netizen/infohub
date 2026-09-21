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

 def test_identical_content_cannot_cross_splits_even_with_distinct_groups(self):
  def mutate(rows):
   rows[1]["event_group_id"]="unique-event"
   rows[1]["origin_group_id"]="unique-origin"
   rows[1]["text"]=rows[0]["text"]
   rows[1]["content_sha256"]=rows[0]["content_sha256"]
   rows[1]["split"]="test"
  root=self.changed(mutate)
  with self.assertRaisesRegex(EvaluationDatasetError,"content hash .* leaks"):
   validate_evaluation_dataset(root)

 def test_model_generated_annotation_cannot_be_gold(self):
  def mutate(rows): rows[0]["annotation"].update(state="adjudicated",generated_by_model=True)
  root=self.changed(mutate)
  with self.assertRaisesRegex(EvaluationDatasetError,"model output as gold"):
   validate_evaluation_dataset(root)

 def test_adjudicated_requires_distinct_frozen_human_reviews(self):
  def mutate(rows):
   row=rows[0]
   row["text_storage"]="restricted_reference"
   row.pop("text")
   row["object_ref"]="private-db:items/1"
   row["annotation"]["state"]="adjudicated"
  root=self.changed(mutate)
  with self.assertRaisesRegex(EvaluationDatasetError,"two independent human reviews"):
   validate_evaluation_dataset(root)

 def test_complete_review_trace_is_structurally_valid_but_not_full_gold(self):
  def mutate(rows):
   row=rows[0]
   row["text_storage"]="restricted_reference"
   row.pop("text")
   row["object_ref"]="private-db:items/1"
   digest=row["content_sha256"]
   labels=row["annotation"]["labels"]
   row["annotation"].update(state="adjudicated",reviews=[
    {"reviewer_id":name,"source":"human","independent":True,
     "content_sha256":digest,"recorded_at":"2026-09-20T09:00:00Z","labels":labels}
    for name in ("reviewer-a","reviewer-b")],adjudication={
      "adjudicator_id":"reviewer-c","source":"human","content_sha256":digest,
      "recorded_at":"2026-09-21T09:00:00Z","labels":labels})
  root=self.changed(mutate)
  report=validate_evaluation_dataset(root)
  self.assertFalse(report.publishable_gold)
  rows=[json.loads(line) for line in (root/"cases.jsonl").read_text().splitlines()]
  rows[0]["annotation"]["adjudication"]["adjudicator_id"]="reviewer-a"
  (root/"cases.jsonl").write_text(''.join(json.dumps(row)+'\n' for row in rows))
  with self.assertRaisesRegex(EvaluationDatasetError,"invalid adjudication provenance"):
   validate_evaluation_dataset(root)

 def test_lowering_manifest_targets_cannot_publish_small_fixture(self):
  root=self.changed(lambda rows: None)
  manifest=json.loads((root/"manifest.json").read_text())
  manifest["target_plan"]={"documents":0,"event_groups":0,"impact_annotations":0,"security_cases":0}
  (root/"manifest.json").write_text(json.dumps(manifest))
  report=validate_evaluation_dataset(root)
  self.assertFalse(report.publishable_gold)
  self.assertEqual(report.target_gaps["documents"],588)

if __name__=="__main__": unittest.main()
