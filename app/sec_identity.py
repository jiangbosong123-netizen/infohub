from __future__ import annotations

"""Evidence-linked SEC issuer, security, listing, and filing projections."""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Mapping
from uuid import uuid4

from .documents import DocumentProjection
from .ingest import RawObservation, verify_payload
from .timeutil import utc_now


class SecProjectionError(RuntimeError):
    """A SEC record cannot be mapped without weakening identity guarantees."""


@dataclass(frozen=True)
class SecProjection:
    filing_id: str
    filing_version_id: str
    issuer_entity_id: str
    securities_seen: int
    amendment_status: str


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _sha(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _clean(value: object) -> str:
    return str(value or "").strip()


def _cik(value: object) -> str:
    text = _clean(value)
    return text.zfill(10) if text else ""


def _base_form(form: str) -> tuple[str, bool]:
    normalized = form.strip().upper()
    amended = normalized.endswith("/A")
    return normalized[:-2] if amended else normalized, amended


def _validate_raw_evidence(candidate: Mapping, observation: RawObservation) -> None:
    payload = json.loads(
        verify_payload(observation.payload_ref, observation.payload_sha256).read_text("utf-8")
    )
    source_record = payload.get("source_record") if isinstance(payload, dict) else None
    filing = source_record.get("filing") if isinstance(source_record, dict) else None
    issuer = source_record.get("issuer") if isinstance(source_record, dict) else None
    extra = candidate.get("extra")
    if not isinstance(filing, dict) or not isinstance(issuer, dict) or not isinstance(extra, Mapping):
        raise SecProjectionError("SEC projection requires filing and issuer raw evidence")
    comparisons = (
        (filing.get("accessionNumber"), extra.get("accession")),
        (filing.get("form"), extra.get("form")),
        (filing.get("primaryDocument"), extra.get("primary_document")),
        (filing.get("filingDate"), extra.get("filing_date")),
        (filing.get("reportDate"), extra.get("report_date")),
        (filing.get("items"), extra.get("items")),
    )
    if any(_clean(left) != _clean(right) for left, right in comparisons):
        raise SecProjectionError("SEC normalized filing metadata disagrees with raw evidence")
    if _cik(issuer.get("cik")) != _cik(extra.get("cik")):
        raise SecProjectionError("SEC issuer CIK disagrees with raw evidence")
    if _json(issuer.get("tickerExchangeAssociations") or []) != _json(
        extra.get("sec_associations") or []
    ):
        raise SecProjectionError("SEC security associations disagree with raw evidence")


def _issuer_for_company(db: sqlite3.Connection, slug: str) -> str:
    row = db.execute(
        """SELECT mapping.entity_id
           FROM companies AS company
           JOIN legacy_company_entities AS mapping ON mapping.company_id=company.id
           WHERE company.slug=?""",
        (slug,),
    ).fetchone()
    if not row:
        raise SecProjectionError(
            f"SEC company {slug!r} has no stable identity; run catalog sync first"
        )
    return row["entity_id"]


def _insert_identifier(
    db: sqlite3.Connection,
    *,
    entity_id: str,
    namespace: str,
    value: str,
    qualifier: dict,
    evidence_id: str,
    now: str,
) -> None:
    qualifier_json = _json(qualifier)
    existing = db.execute(
        """SELECT 1 FROM entity_identifiers
           WHERE entity_id=? AND namespace=? AND value=? AND qualifier_json=?
             AND verification_status='candidate'""",
        (entity_id, namespace, value, qualifier_json),
    ).fetchone()
    if existing:
        return
    assertion = _sha({
        "namespace": namespace,
        "value": value,
        "qualifier": qualifier,
        "valid_from": None,
        "valid_to": None,
        "evidence_id": evidence_id,
        "verification_status": "candidate",
    })
    db.execute(
        """INSERT INTO entity_identifiers(
               id,entity_id,namespace,value,qualifier_json,evidence_id,
               verification_status,assertion_sha256,available_at
           ) VALUES(?,?,?,?,?,?,'candidate',?,?)""",
        (
            f"entity_identifier_{uuid4().hex}", entity_id, namespace, value,
            qualifier_json, evidence_id, assertion, now,
        ),
    )


def _security(
    db: sqlite3.Connection,
    *,
    issuer_entity_id: str,
    association: Mapping,
    evidence_id: str,
    now: str,
) -> str:
    cik = _cik(association.get("cik"))
    ticker = _clean(association.get("ticker")).upper()
    exchange = _clean(association.get("exchange")).upper()
    if not cik or not ticker or not exchange:
        raise SecProjectionError("SEC security association requires CIK, ticker, and exchange")
    name = _clean(association.get("name")) or ticker
    attributes = {
        "cik": cik,
        "exchange": exchange,
        "ticker": ticker,
        "listing_type": "unknown",
        "source": "sec_company_tickers_exchange",
    }
    canonical_name = f"{name} {ticker}"
    version_sha = _sha({
        "type": "security", "canonical_name": canonical_name,
        "status": "active", "attributes": attributes,
    })
    existing = db.execute(
        """SELECT security_entity_id FROM sec_security_keys
           WHERE cik=? AND exchange=? AND ticker=?""",
        (cik, exchange, ticker),
    ).fetchone()
    if existing:
        owner = db.execute(
            """SELECT issuer_entity_id FROM security_listings
               WHERE security_entity_id=? AND exchange=? AND ticker=?
               ORDER BY available_at LIMIT 1""",
            (existing["security_entity_id"], exchange, ticker),
        ).fetchone()
        if not owner or owner["issuer_entity_id"] != issuer_entity_id:
            raise SecProjectionError("one SEC security key resolved to multiple issuers")
        entity_id = existing["security_entity_id"]
        current = db.execute(
            """SELECT version.id,version.version,version.version_sha256
               FROM entities AS entity
               JOIN entity_versions AS version ON version.id=entity.current_version_id
               WHERE entity.id=?""",
            (entity_id,),
        ).fetchone()
        if not current:
            raise SecProjectionError("SEC security current version is missing")
        if current["version_sha256"] != version_sha:
            version_id = f"entity_version_{uuid4().hex}"
            db.execute(
                """INSERT INTO entity_versions(
                       id,entity_id,version,previous_version_id,type,canonical_name,status,
                       attributes_json,version_sha256,available_at,created_by
                   ) VALUES(?,?,?,?, 'security',?,'active',?,?,?,'sec_projection_v1')""",
                (
                    version_id, entity_id, current["version"] + 1, current["id"],
                    canonical_name, _json(attributes), version_sha, now,
                ),
            )
            db.execute(
                "UPDATE entities SET current_version_id=? WHERE id=?",
                (version_id, entity_id),
            )
        return entity_id

    dataset_id = db.execute(
        "SELECT dataset_id FROM dataset_state WHERE singleton=1"
    ).fetchone()[0]
    entity_id = f"entity_{uuid4().hex}"
    version_id = f"entity_version_{uuid4().hex}"
    db.execute(
        """INSERT INTO entities(id,dataset_id,type,status,created_at)
           VALUES(?,?,'security','active',?)""",
        (entity_id, dataset_id, now),
    )
    db.execute(
        """INSERT INTO entity_versions(
               id,entity_id,version,previous_version_id,type,canonical_name,status,
               attributes_json,version_sha256,available_at,created_by
           ) VALUES(?,?,1,NULL,'security',?,'active',?,?,?,'sec_projection_v1')""",
        (version_id, entity_id, canonical_name, _json(attributes), version_sha, now),
    )
    db.execute(
        "UPDATE entities SET current_version_id=? WHERE id=?",
        (version_id, entity_id),
    )
    db.execute(
        """INSERT INTO sec_security_keys(
               cik,exchange,ticker,security_entity_id,first_evidence_id,available_at
           ) VALUES(?,?,?,?,?,?)""",
        (cik, exchange, ticker, entity_id, evidence_id, now),
    )
    _insert_identifier(
        db, entity_id=entity_id, namespace="exchange_ticker", value=ticker,
        qualifier={"exchange": exchange, "source": "SEC association file"},
        evidence_id=evidence_id, now=now,
    )
    db.execute(
        """INSERT INTO entity_relations(
               id,from_entity_id,to_entity_id,relation,evidence_id,
               verification_status,available_at
           ) VALUES(?,?,?,'issues',?,'candidate',?)""",
        (f"entity_relation_{uuid4().hex}", issuer_entity_id, entity_id, evidence_id, now),
    )
    db.execute(
        """INSERT INTO security_listings(
               id,security_entity_id,issuer_entity_id,exchange,ticker,listing_type,
               evidence_id,verification_status,available_at
           ) VALUES(?,?,?,?,?,'unknown',?,'candidate',?)""",
        (
            f"security_listing_{uuid4().hex}", entity_id, issuer_entity_id,
            exchange, ticker, evidence_id, now,
        ),
    )
    return entity_id


def _accepted_at(db: sqlite3.Connection, raw_record_id: str) -> str | None:
    row = db.execute(
        """SELECT utc FROM source_time_values
           WHERE raw_record_id=? AND role='accepted' AND status='valid' AND utc IS NOT NULL
           ORDER BY ordinal LIMIT 1""",
        (raw_record_id,),
    ).fetchone()
    return row["utc"] if row else None


def _amendment_target(
    db: sqlite3.Connection,
    *,
    issuer_entity_id: str,
    base_form: str,
    report_period_end: str | None,
    filing_date: str | None,
    exclude_filing_id: str,
) -> tuple[str | None, str]:
    if not report_period_end:
        return None, "unresolved"
    rows = db.execute(
        """SELECT filing.id
           FROM sec_filings AS filing
           JOIN sec_filing_versions AS version ON version.id=filing.current_version_id
           WHERE filing.issuer_entity_id=? AND filing.id<>?
             AND version.base_form=? AND version.is_amendment=0
             AND version.report_period_end=?
             AND (? IS NULL OR version.filing_date IS NULL OR version.filing_date<=?)
           ORDER BY version.filing_date DESC,filing.accession_number DESC""",
        (
            issuer_entity_id, exclude_filing_id, base_form, report_period_end,
            filing_date, filing_date,
        ),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["id"], "linked"
    if len(rows) > 1:
        return None, "ambiguous"
    return None, "unresolved"


def _append_filing_version(
    db: sqlite3.Connection,
    *,
    filing_id: str,
    document_version_id: str,
    raw_record_id: str,
    form: str,
    primary_document: str,
    filing_date: str | None,
    report_period_end: str | None,
    accepted_at: str | None,
    items: list[str],
    issuer_entity_id: str,
    now: str,
) -> tuple[str, str]:
    base_form, is_amendment = _base_form(form)
    target, amendment_status = (None, "not_amendment")
    if is_amendment:
        target, amendment_status = _amendment_target(
            db, issuer_entity_id=issuer_entity_id, base_form=base_form,
            report_period_end=report_period_end, filing_date=filing_date,
            exclude_filing_id=filing_id,
        )
    payload = {
        "document_version_id": document_version_id,
        "raw_record_id": raw_record_id,
        "form": form,
        "base_form": base_form,
        "is_amendment": is_amendment,
        "amends_filing_id": target,
        "amendment_status": amendment_status,
        "primary_document": primary_document,
        "filing_date": filing_date,
        "report_period_end": report_period_end,
        "accepted_at": accepted_at,
        "items": items,
    }
    metadata_sha = _sha(payload)
    current = db.execute(
        """SELECT version.id,version.version,version.metadata_sha256
           FROM sec_filings AS filing
           LEFT JOIN sec_filing_versions AS version ON version.id=filing.current_version_id
           WHERE filing.id=?""",
        (filing_id,),
    ).fetchone()
    if current and current["metadata_sha256"] == metadata_sha:
        return current["id"], amendment_status
    version = (current["version"] + 1) if current and current["version"] else 1
    version_id = f"sec_filing_version_{uuid4().hex}"
    db.execute(
        """INSERT INTO sec_filing_versions(
               id,filing_id,version,previous_version_id,document_version_id,raw_record_id,
               form,base_form,is_amendment,amends_filing_id,amendment_status,
               primary_document,filing_date,report_period_end,accepted_at,items_json,
               metadata_sha256,available_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            version_id, filing_id, version, current["id"] if current else None,
            document_version_id, raw_record_id, form, base_form, int(is_amendment),
            target, amendment_status, primary_document, filing_date, report_period_end,
            accepted_at, _json(items), metadata_sha, now,
        ),
    )
    db.execute(
        "UPDATE sec_filings SET current_version_id=? WHERE id=?",
        (version_id, filing_id),
    )
    return version_id, amendment_status


def _reconcile_amendments(
    db: sqlite3.Connection,
    *,
    issuer_entity_id: str,
    base_form: str,
    report_period_end: str | None,
    now: str,
) -> None:
    if not report_period_end:
        return
    rows = db.execute(
        """SELECT filing.id,version.*
           FROM sec_filings AS filing
           JOIN sec_filing_versions AS version ON version.id=filing.current_version_id
           WHERE filing.issuer_entity_id=? AND version.base_form=?
             AND version.report_period_end=? AND version.is_amendment=1
             AND version.amendment_status IN ('unresolved','ambiguous')""",
        (issuer_entity_id, base_form, report_period_end),
    ).fetchall()
    for row in rows:
        _append_filing_version(
            db, filing_id=row["filing_id"], document_version_id=row["document_version_id"],
            raw_record_id=row["raw_record_id"], form=row["form"],
            primary_document=row["primary_document"], filing_date=row["filing_date"],
            report_period_end=row["report_period_end"], accepted_at=row["accepted_at"],
            items=json.loads(row["items_json"]), issuer_entity_id=issuer_entity_id, now=now,
        )


def project_sec_candidate(
    db: sqlite3.Connection,
    *,
    candidate: Mapping,
    observation: RawObservation,
    document: DocumentProjection,
) -> SecProjection:
    """Project a SEC candidate using only evidence captured in its raw record."""
    _validate_raw_evidence(candidate, observation)
    extra = candidate.get("extra")
    if not isinstance(extra, Mapping):
        raise SecProjectionError("SEC candidate is missing structured metadata")
    cik = _cik(extra.get("cik"))
    accession = _clean(extra.get("accession"))
    form = _clean(extra.get("form")).upper()
    slugs = candidate.get("companies") or []
    if not cik or not accession or not form or len(slugs) != 1:
        raise SecProjectionError("SEC candidate requires CIK, accession, form, and one company")
    issuer_entity_id = _issuer_for_company(db, str(slugs[0]))
    now = utc_now()
    _insert_identifier(
        db, entity_id=issuer_entity_id, namespace="cik", value=cik,
        qualifier={"source": "SEC submissions"},
        evidence_id=observation.raw_record_id, now=now,
    )
    associations = extra.get("sec_associations") or []
    if not isinstance(associations, list):
        raise SecProjectionError("SEC associations must be a list")
    securities = 0
    for association in associations:
        if isinstance(association, Mapping) and _cik(association.get("cik")) == cik:
            _security(
                db, issuer_entity_id=issuer_entity_id, association=association,
                evidence_id=observation.raw_record_id, now=now,
            )
            securities += 1

    dataset_id = db.execute(
        "SELECT dataset_id FROM dataset_state WHERE singleton=1"
    ).fetchone()[0]
    filing = db.execute(
        """SELECT id,issuer_entity_id FROM sec_filings
           WHERE dataset_id=? AND cik=? AND accession_number=?""",
        (dataset_id, cik, accession),
    ).fetchone()
    if filing and filing["issuer_entity_id"] != issuer_entity_id:
        raise SecProjectionError("one SEC filing resolved to multiple issuers")
    if filing:
        filing_id = filing["id"]
    else:
        filing_id = f"sec_filing_{uuid4().hex}"
        db.execute(
            """INSERT INTO sec_filings(
                   id,dataset_id,cik,accession_number,issuer_entity_id,
                   first_seen_at,status
               ) VALUES(?,?,?,?,?,?,'observed')""",
            (filing_id, dataset_id, cik, accession, issuer_entity_id, now),
        )
    raw_items = extra.get("items") or []
    if isinstance(raw_items, str):
        items = [item.strip() for item in raw_items.split(",") if item.strip()]
    elif isinstance(raw_items, list):
        items = [_clean(item) for item in raw_items if _clean(item)]
    else:
        items = []
    version_id, amendment_status = _append_filing_version(
        db, filing_id=filing_id, document_version_id=document.version_id,
        raw_record_id=observation.raw_record_id, form=form,
        primary_document=_clean(extra.get("primary_document")),
        filing_date=_clean(extra.get("filing_date")) or None,
        report_period_end=_clean(extra.get("report_date")) or None,
        accepted_at=_accepted_at(db, observation.raw_record_id), items=items,
        issuer_entity_id=issuer_entity_id, now=now,
    )
    base_form, is_amendment = _base_form(form)
    if not is_amendment:
        _reconcile_amendments(
            db, issuer_entity_id=issuer_entity_id, base_form=base_form,
            report_period_end=_clean(extra.get("report_date")) or None, now=now,
        )
    return SecProjection(
        filing_id=filing_id, filing_version_id=version_id,
        issuer_entity_id=issuer_entity_id, securities_seen=securities,
        amendment_status=amendment_status,
    )
