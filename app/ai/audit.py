from __future__ import annotations

"""Versioned, evidence-preserving storage for NLP outputs."""
import json
from datetime import datetime, timezone

CURATION_VERSION = "curation-v1"


def save_result(db, *, item_id: int, analysis_type: str, pipeline_version: str,
                model: str, input_data: dict, output_data: dict) -> None:
    db.execute(
        """INSERT INTO nlp_results
           (item_id,analysis_type,pipeline_version,model,input_json,output_json,created_at)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(item_id,analysis_type,pipeline_version) DO UPDATE SET
             model=excluded.model,input_json=excluded.input_json,
             output_json=excluded.output_json,created_at=excluded.created_at""",
        (item_id, analysis_type, pipeline_version, model,
         json.dumps(input_data, ensure_ascii=False, sort_keys=True),
         json.dumps(output_data, ensure_ascii=False, sort_keys=True),
         datetime.now(timezone.utc).isoformat()))
