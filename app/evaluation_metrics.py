from __future__ import annotations

"""Deterministic classification metrics with explicit coverage and uncertainty."""

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .evaluation import EvaluationDatasetError, _load_cases, _load_json, validate_evaluation_dataset

METRICS_VERSION="classification-metrics-v1"

@dataclass(frozen=True)
class ClassMetrics:
 label:str; support:int; predicted:int; true_positive:int
 precision:float|None; recall:float|None; f1:float|None

@dataclass(frozen=True)
class ClassificationReport:
 metrics_version:str; dataset_version:str; prediction_run_id:str; task:str
 total:int; correct:int; accuracy:float; accuracy_wilson_95:tuple[float,float]
 covered:int; coverage:float; abstained:int; missing_predictions:int
 macro_f1:float; labels:tuple[str,...]; per_class:tuple[ClassMetrics,...]
 confusion:dict[str,dict[str,int]]; quality_claim_allowed:bool; warnings:tuple[str,...]
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

def evaluate_classification(dataset_path:Path|str,prediction_run_path:Path|str)->ClassificationReport:
 dataset=validate_evaluation_dataset(dataset_path); root=Path(dataset_path)
 run=_load_json(Path(prediction_run_path)); predictions=_load_cases(Path(prediction_run_path).with_name(run.get("predictions_file","predictions.jsonl")))
 if run.get("schema_version")!="prediction-run-v1": raise EvaluationDatasetError("unsupported prediction run schema")
 if run.get("dataset_version")!=dataset.dataset_version: raise EvaluationDatasetError("prediction dataset version mismatch")
 task=run.get("task"); label_path=run.get("label_path")
 if not isinstance(task,str) or not isinstance(label_path,str): raise EvaluationDatasetError("prediction task and label_path are required")
 abstain=set(run.get("abstain_labels",["unknown"]))
 by_id={}
 for row in predictions:
  case_id=row.get("case_id"); predicted=row.get("predicted_label")
  if not isinstance(case_id,str) or not isinstance(predicted,str): raise EvaluationDatasetError("prediction rows require string IDs and labels")
  if case_id in by_id: raise EvaluationDatasetError(f"duplicate prediction for {case_id}")
  by_id[case_id]=predicted
 cases=_load_cases(root/"cases.jsonl"); known={case["case_id"] for case in cases}
 extra=set(by_id)-known
 if extra: raise EvaluationDatasetError(f"predictions contain unknown cases: {sorted(extra)[:5]}")
 actual=[]; predicted=[]; missing=0
 for case in cases:
  actual.append(_label(case,label_path))
  if case["case_id"] in by_id: predicted.append(by_id[case["case_id"]])
  else: predicted.append("unknown"); missing+=1
 labels=tuple(sorted(set(actual)|set(predicted)))
 confusion={a:{p:0 for p in labels} for a in labels}
 for a,p in zip(actual,predicted): confusion[a][p]+=1
 per=[]
 for label in labels:
  tp=confusion[label][label]; support=sum(confusion[label].values()); pred=sum(confusion[a][label] for a in labels)
  precision=tp/pred if pred else None; recall=tp/support if support else None
  f1=(2*precision*recall/(precision+recall)) if precision is not None and recall is not None and precision+recall else 0.0
  per.append(ClassMetrics(label,support,pred,tp,precision,recall,f1))
 total=len(actual);correct=sum(a==p for a,p in zip(actual,predicted));abstained=sum(p in abstain for p in predicted);covered=total-abstained
 macro=sum(item.f1 or 0.0 for item in per)/len(per) if per else 0.0
 quality_allowed=dataset.publishable_gold
 warnings=[]
 if not quality_allowed:warnings.append("dataset is not publishable gold; metrics validate the runner only")
 if total<30:warnings.append("sample support is below 30; do not generalize point estimates")
 return ClassificationReport(METRICS_VERSION,dataset.dataset_version,str(run.get("prediction_run_id")),task,total,correct,correct/total if total else 0.0,_wilson(correct,total),covered,covered/total if total else 0.0,abstained,missing,macro,labels,tuple(per),confusion,quality_allowed,tuple(warnings))

def write_classification_report(dataset_path,run_path,output_path):
 report=evaluate_classification(dataset_path,run_path)
 Path(output_path).write_text(json.dumps(report.to_dict(),ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")
 return report
