from __future__ import annotations

"""Deterministic classification metrics with explicit coverage and uncertainty."""

import hashlib
import json
import math
import re
from datetime import datetime
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from .evaluation import EvaluationDatasetError, _load_cases, _load_json, _verified_holdout, validate_evaluation_dataset
from .owner_recheck import KEY_LABELS, owner_recheck_report

METRICS_VERSION="classification-metrics-v5"
EVALUATION_SPLITS={"train","dev","test","security"}
SLICE_NAME=re.compile(r"[a-z0-9][a-z0-9_:-]{0,63}")
# D23 single-owner-v1: tiers decide what a score may claim; silver is never evaluation truth.
TIERS={"adjudicated":"gold","owner_labeled":"owner","algorithm_labeled":"silver","synthetic_fixture":"synthetic"}

@dataclass(frozen=True)
class ClassMetrics:
 label:str; support:int; predicted:int; true_positive:int
 precision:float|None; recall:float|None; f1:float|None
 # Wilson 95% intervals: 30 predicted positives only pin precision to about +/-14 points.
 precision_wilson_95:tuple[float,float]|None=None; recall_wilson_95:tuple[float,float]|None=None

@dataclass(frozen=True)
class SliceMetrics:
 total:int; correct:int; accuracy:float; accuracy_wilson_95:tuple[float,float]
 abstained:int; confusion:dict[str,dict[str,int]]

@dataclass(frozen=True)
class ClassificationReport:
 metrics_version:str; dataset_version:str; prediction_run_id:str; task:str
 split:str
 total:int; correct:int; accuracy:float; accuracy_wilson_95:tuple[float,float]
 covered:int; coverage:float; abstained:int; missing_predictions:int
 macro_f1:float; labels:tuple[str,...]; scored_labels:tuple[str,...]; per_class:tuple[ClassMetrics,...]
 confusion:dict[str,dict[str,int]]; quality_claim_allowed:bool; warnings:tuple[str,...]
 # Defaults keep reports built by older callers fail-closed: no tier, no experimental claim.
 annotation_tier:str="other"; experimental_claim_allowed:bool=False
 claim_blockers:tuple[str,...]=("report predates annotation tiers",)
 slices:dict[str,SliceMetrics]=field(default_factory=dict)
 def to_dict(self): return asdict(self)

def _wilson(successes:int,total:int,z:float=1.959963984540054)->tuple[float,float]:
 if total==0:return (0.0,0.0)
 p=successes/total; d=1+z*z/total
 center=(p+z*z/(2*total))/d
 margin=z*math.sqrt(p*(1-p)/total+z*z/(4*total*total))/d
 return (max(0.0,center-margin),min(1.0,center+margin))

def _label(case:dict,path:str):
 value=case.get("annotation",{}).get("labels",{})
 for part in path.split("."):
  if not isinstance(value,dict) or part not in value:
   raise EvaluationDatasetError(f"case {case.get('case_id')} lacks label {path}")
  value=value[part]
 if not isinstance(value,str) or not value: raise EvaluationDatasetError("classification labels must be strings")
 return value

def _prediction_file(run_path:Path,run:dict)->Path:
 name=run.get("predictions_file","predictions.jsonl")
 if not isinstance(name,str) or Path(name).name!=name or name in {"",".",".."} or "\\" in name:
  raise EvaluationDatasetError("predictions_file must be a local filename")
 return run_path.with_name(name)

def _require_v2_provenance(root:Path,run:dict,prediction_path:Path)->None:
 for key in ("prediction_run_id","method_id","method_version","generated_at"):
  if not isinstance(run.get(key),str) or not run[key].strip():
   raise EvaluationDatasetError(f"prediction run requires {key}")
 try: generated=datetime.fromisoformat(run["generated_at"].replace("Z","+00:00"))
 except ValueError as exc: raise EvaluationDatasetError("prediction run has invalid generated_at") from exc
 if generated.tzinfo is None: raise EvaluationDatasetError("prediction run generated_at requires timezone")
 for key,content in (("dataset_manifest_sha256",(root/"manifest.json").read_bytes()),
                     ("dataset_cases_sha256",(root/"cases.jsonl").read_bytes()),
                     ("predictions_sha256",prediction_path.read_bytes())):
  if run.get(key)!=hashlib.sha256(content).hexdigest():
   raise EvaluationDatasetError(f"prediction run {key} mismatch")
 config_hash=run.get("method_config_sha256")
 if not isinstance(config_hash,str) or len(config_hash)!=64:
  raise EvaluationDatasetError("prediction run requires method_config_sha256")
 try: int(config_hash,16)
 except ValueError as exc: raise EvaluationDatasetError("prediction run has invalid method_config_sha256") from exc

def evaluate_classification(dataset_path:Path|str,prediction_run_path:Path|str)->ClassificationReport:
 dataset=validate_evaluation_dataset(dataset_path); root=Path(dataset_path)
 run_path=Path(prediction_run_path); run=_load_json(run_path)
 schema=run.get("schema_version")
 if schema not in {"prediction-run-v1","prediction-run-v2"}: raise EvaluationDatasetError("unsupported prediction run schema")
 if run.get("dataset_version")!=dataset.dataset_version: raise EvaluationDatasetError("prediction dataset version mismatch")
 task=run.get("task"); label_path=run.get("label_path")
 if not isinstance(task,str) or not task or not isinstance(label_path,str) or not label_path:
  raise EvaluationDatasetError("prediction task and label_path are required")
 split="all" if schema=="prediction-run-v1" else run.get("split")
 if schema=="prediction-run-v2" and split not in EVALUATION_SPLITS:
  raise EvaluationDatasetError("v2 prediction run requires one evaluation split")
 if schema=="prediction-run-v1" and run.get("split") is not None:
  raise EvaluationDatasetError("v1 engineering runs cannot claim a split")
 prediction_path=_prediction_file(run_path,run)
 if schema=="prediction-run-v2": _require_v2_provenance(root,run,prediction_path)
 predictions=_load_cases(prediction_path)
 if schema=="prediction-run-v2" and "abstain_labels" not in run:
  raise EvaluationDatasetError("v2 prediction run requires explicit abstain_labels")
 abstain_labels=run.get("abstain_labels",["unknown"])
 if not isinstance(abstain_labels,list) or not abstain_labels or any(
  not isinstance(label,str) or not label for label in abstain_labels):
  raise EvaluationDatasetError("abstain_labels must be a nonempty string list")
 abstain=set(abstain_labels)
 by_id={}; slice_tags={}
 for row in predictions:
  case_id=row.get("case_id"); predicted=row.get("predicted_label")
  if not isinstance(case_id,str) or not isinstance(predicted,str): raise EvaluationDatasetError("prediction rows require string IDs and labels")
  if case_id in by_id: raise EvaluationDatasetError(f"duplicate prediction for {case_id}")
  tags=row.get("slices",[])
  if not isinstance(tags,list) or tags!=sorted(set(tags)) or any(not isinstance(tag,str) or not SLICE_NAME.fullmatch(tag) for tag in tags):
   raise EvaluationDatasetError(f"prediction {case_id} has invalid slices")
  by_id[case_id]=predicted; slice_tags[case_id]=tags
 cases=_load_cases(root/"cases.jsonl")
 selected=cases if split=="all" else [case for case in cases if case["split"]==split]
 if not selected: raise EvaluationDatasetError(f"dataset has no cases in {split} split")
 actual=[_label(case,label_path) for case in selected]
 if schema=="prediction-run-v2" and set(actual)&abstain:
  raise EvaluationDatasetError("v2 abstain labels must differ from gold labels")
 known={case["case_id"] for case in selected}
 extra=set(by_id)-known
 if extra: raise EvaluationDatasetError(f"predictions contain unknown cases or other-split cases: {sorted(extra)[:5]}")
 predicted=[]; missing=0
 for case in selected:
  if case["case_id"] in by_id: predicted.append(by_id[case["case_id"]])
  else: predicted.append(abstain_labels[0]); missing+=1
 labels=tuple(sorted(set(actual)|set(predicted)))
 # A v2 abstention is an outcome in the confusion matrix, not a gold class.
 # Keep v1's historical engineering-only score unchanged for reproducibility.
 scored_labels=tuple(sorted(set(actual))) if schema=="prediction-run-v2" else labels
 confusion={a:{p:0 for p in labels} for a in labels}
 for a,p in zip(actual,predicted): confusion[a][p]+=1
 per=[]
 for label in scored_labels:
  tp=confusion[label][label]; support=sum(confusion[label].values()); pred=sum(confusion[a][label] for a in labels)
  precision=tp/pred if pred else None; recall=tp/support if support else None
  f1=(2*precision*recall/(precision+recall)) if precision is not None and recall is not None and precision+recall else 0.0
  per.append(ClassMetrics(label,support,pred,tp,precision,recall,f1,
                          _wilson(tp,pred) if pred else None,_wilson(tp,support) if support else None))
 total=len(actual);correct=sum(a==p for a,p in zip(actual,predicted));abstained=sum(p in abstain for p in predicted);covered=total-abstained
 macro=sum(item.f1 or 0.0 for item in per)/len(per) if per else 0.0
 states={case["annotation"]["state"] for case in selected}
 tier=TIERS.get(next(iter(states)),"other") if len(states)==1 else "mixed"
 slices={}
 for name in sorted({tag for case in selected for tag in slice_tags.get(case["case_id"],[])}):
  pairs=[(a,p) for case,a,p in zip(selected,actual,predicted) if name in slice_tags.get(case["case_id"],[])]
  hits=sum(a==p for a,p in pairs)
  matrix={a:{p:0 for p in labels} for a in labels}
  for a,p in pairs: matrix[a][p]+=1
  slices[name]=SliceMetrics(len(pairs),hits,hits/len(pairs),_wilson(hits,len(pairs)),sum(p in abstain for _,p in pairs),matrix)
 blockers=[]
 if schema!="prediction-run-v2" or split!="test": blockers.append("experimental claims require a v2 run on the test split")
 if tier!="owner": blockers.append(f"experimental claims require owner-tier truth, not {tier}")
 if missing: blockers.append("missing predictions")
 if covered==0: blockers.append("no covered predictions")
 manifest=_load_json(root/"manifest.json")
 if manifest.get("split_policy")=="blind-holdout" and not _verified_holdout(manifest,cases,root):
  blockers.append("blind holdout is not verified")
 if tier=="owner":
  if task not in KEY_LABELS or KEY_LABELS[task][0]!=label_path:
   blockers.append(f"no owner recheck definition for {task}.{label_path}")
  else:
   recheck=owner_recheck_report(root,task)
   blockers.extend(f"owner recheck: {item}" for item in recheck.blockers)
 quality_allowed=(schema=="prediction-run-v2" and split=="test"
                  and dataset.publishable_gold and missing==0 and covered>0
                  and all(case["annotation"]["state"]=="adjudicated" for case in selected))
 warnings=[]
 if schema=="prediction-run-v1":warnings.append("v1 engineering report mixes splits and cannot support a quality claim")
 if split=="security":warnings.append("security cases are reported separately from natural-distribution accuracy")
 if not quality_allowed:warnings.append("quality claim blocked: requires verified gold and complete v2 blind-test predictions")
 if total<30:warnings.append("sample support is below 30; do not generalize point estimates")
 if tier=="owner":warnings.append("owner tier: single annotator; any claim is experimental, never gold")
 if tier=="silver":warnings.append("silver labels are not evaluation truth; this is agreement with silver, not accuracy")
 for name,item in slices.items():
  if item.total<30:warnings.append(f"slice {name} has support below 30")
 return ClassificationReport(METRICS_VERSION,dataset.dataset_version,str(run.get("prediction_run_id")),task,split,total,correct,correct/total if total else 0.0,_wilson(correct,total),covered,covered/total if total else 0.0,abstained,missing,macro,labels,scored_labels,tuple(per),confusion,quality_allowed,tuple(warnings),tier,not blockers,tuple(blockers),slices)

def write_classification_report(dataset_path,run_path,output_path):
 report=evaluate_classification(dataset_path,run_path)
 Path(output_path).write_text(json.dumps(report.to_dict(),ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")
 return report
