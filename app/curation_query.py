"""SQL-side read semantics for current relevance and importance publications.

The portal needs to filter before LIMIT/OFFSET. This CTE mirrors the validated
publication projection for the two fields that affect visibility and ranking.
The published result rows are immutable and validated on write.
"""

CURATION_FILTER_CTE = """
WITH curation_refs AS (
  SELECT i.id AS item_id, i.tmt AS legacy_tmt, i.score AS legacy_score,
         i.ai_cat AS legacy_category,
         rp.current_publication_id AS relevance_pointer,
         rv.review_status AS relevance_review, rv.evidence_status AS relevance_evidence,
         rv.subject_type AS relevance_subject, rv.subject_version_id AS relevance_version,
         rv.task_type AS relevance_task,
         rr.schema_version AS relevance_schema, rr.result_status AS relevance_status,
         CASE WHEN json_valid(rr.validated_output_json) THEN rr.validated_output_json
              ELSE '{}' END AS relevance_json,
         ip.current_publication_id AS importance_pointer,
         iv.review_status AS importance_review, iv.evidence_status AS importance_evidence,
         iv.subject_type AS importance_subject, iv.subject_version_id AS importance_version,
         iv.task_type AS importance_task,
         ir.schema_version AS importance_schema, ir.result_status AS importance_status,
         CASE WHEN json_valid(ir.validated_output_json) THEN ir.validated_output_json
              ELSE '{}' END AS importance_json,
         d.current_version_id
  FROM items i
  LEFT JOIN documents d ON d.legacy_item_id=i.id AND d.status='active'
  LEFT JOIN analysis_publications rp ON rp.subject_type='document'
       AND rp.subject_version_id=d.current_version_id AND rp.task_type='relevance'
  LEFT JOIN analysis_publication_versions rv ON rv.id=rp.current_publication_id
  LEFT JOIN analysis_results rr ON rr.id=rv.result_id
  LEFT JOIN analysis_publications ip ON ip.subject_type='document'
       AND ip.subject_version_id=d.current_version_id AND ip.task_type='importance'
  LEFT JOIN analysis_publication_versions iv ON iv.id=ip.current_publication_id
  LEFT JOIN analysis_results ir ON ir.id=iv.result_id
), curation_checked AS (
  SELECT *,
    relevance_pointer IS NOT NULL
      AND relevance_subject='document' AND relevance_version=current_version_id
      AND relevance_task='relevance' AND relevance_schema='infohub.relevance/1.0'
      AND relevance_status IN ('valid','needs_review')
      AND relevance_review!='rejected' AND relevance_evidence IN ('supported','partial')
      AND json_extract(relevance_json,'$.schema_version')=relevance_schema
      AND json_extract(relevance_json,'$.subject.type')='document'
      AND json_extract(relevance_json,'$.subject.version_id')=current_version_id
      AND json_extract(relevance_json,'$.status')=relevance_status
      AND json_extract(relevance_json,'$.data.label') IN ('relevant','not_relevant')
      AND json_type(relevance_json,'$.data.policy_override') IN ('true','false')
      AND (json_type(relevance_json,'$.data.ai_category')='null'
           OR json_extract(relevance_json,'$.data.ai_category') IN
              ('model','product','industry','paper','opinion')) AS relevance_usable,
    importance_pointer IS NOT NULL
      AND importance_subject='document' AND importance_version=current_version_id
      AND importance_task='importance' AND importance_schema='infohub.importance/1.0'
      AND importance_status IN ('valid','needs_review')
      AND importance_review!='rejected' AND importance_evidence IN ('supported','partial')
      AND json_extract(importance_json,'$.schema_version')=importance_schema
      AND json_extract(importance_json,'$.subject.type')='document'
      AND json_extract(importance_json,'$.subject.version_id')=current_version_id
      AND json_extract(importance_json,'$.status')=importance_status
      AND json_type(importance_json,'$.data.score')='integer'
      AND json_extract(importance_json,'$.data.score') BETWEEN 0 AND 100
      AND json_extract(importance_json,'$.data.scale')='editorial_importance_not_probability'
      AS importance_usable
  FROM curation_refs
), curation_values AS (
  SELECT item_id,
    CASE WHEN relevance_pointer IS NULL THEN COALESCE(legacy_tmt,1)!=0
         WHEN relevance_usable THEN
           json_extract(relevance_json,'$.data.label')='relevant'
           OR json_extract(relevance_json,'$.data.policy_override')=1
         ELSE 0 END AS visible,
    CASE WHEN relevance_pointer IS NULL THEN COALESCE(legacy_category,'')
         WHEN relevance_usable THEN COALESCE(json_extract(relevance_json,'$.data.ai_category'),'')
         ELSE '' END AS category,
    CASE WHEN importance_pointer IS NULL THEN legacy_score
         WHEN importance_usable THEN json_extract(importance_json,'$.data.score')
         ELSE NULL END AS score
  FROM curation_checked
)
"""


def portal_curation_sql(enabled: bool) -> tuple[str, str, str, str, str]:
    """Return CTE prefix, join, visibility, score and category SQL fragments."""
    if enabled:
        return CURATION_FILTER_CTE, " JOIN curation_values cv ON cv.item_id=i.id", "cv.visible=1", "cv.score", "cv.category"
    return "", "", "COALESCE(i.tmt,1)!=0", "i.score", "i.ai_cat"
