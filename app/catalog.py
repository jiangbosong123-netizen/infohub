from __future__ import annotations

"""Versioned shadow catalogs for entities, topics, and publishers.

The portal continues to read legacy company/topic rows. This module creates
stable identities without promoting watchlist keywords or legacy market fields
into verified facts.
"""

import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from uuid import uuid4

import yaml

from . import config
from .provenance import PUBLISHERS
from .timeutil import utc_now


class CatalogConflict(RuntimeError):
    """A stable alias or legacy key points at conflicting identities."""


@dataclass(frozen=True)
class CatalogSyncReport:
    companies_seen: int
    company_entities_created: int
    entity_versions_created: int
    topics_seen: int
    topics_created: int
    topic_versions_created: int
    publishers_seen: int
    publishers_created: int
    publisher_versions_created: int

    def to_dict(self) -> dict:
        return asdict(self)


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def _sha(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _text_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _dataset_id(db: sqlite3.Connection) -> str:
    row = db.execute("SELECT dataset_id FROM dataset_state WHERE singleton=1").fetchone()
    if not row:
        raise CatalogConflict("dataset identity is missing")
    return row[0]


def _append_entity_version(
    db: sqlite3.Connection,
    *,
    entity_id: str,
    entity_type: str,
    canonical_name: str,
    status: str,
    attributes: dict,
    now: str,
) -> bool:
    payload = {
        "type": entity_type,
        "canonical_name": canonical_name,
        "status": status,
        "attributes": attributes,
    }
    version_sha = _sha(payload)
    current = db.execute(
        """SELECT version.id,version.version,version.version_sha256
           FROM entities AS entity
           LEFT JOIN entity_versions AS version ON version.id=entity.current_version_id
           WHERE entity.id=?""",
        (entity_id,),
    ).fetchone()
    if current and current["version_sha256"] == version_sha:
        return False
    version = (current["version"] + 1) if current and current["version"] else 1
    version_id = f"entity_version_{uuid4().hex}"
    db.execute(
        """INSERT INTO entity_versions(
               id,entity_id,version,previous_version_id,type,canonical_name,status,
               attributes_json,version_sha256,available_at,created_by
           ) VALUES(?,?,?,?,?,?,?,?,?,?, 'catalog_sync_v1')""",
        (
            version_id, entity_id, version, current["id"] if current else None,
            entity_type, canonical_name, status, _json(attributes), version_sha, now,
        ),
    )
    db.execute(
        """UPDATE entities SET type=?,status=?,current_version_id=? WHERE id=?""",
        (entity_type, status, version_id, entity_id),
    )
    return True


def _insert_alias(
    db: sqlite3.Connection,
    *,
    entity_id: str,
    alias: str,
    language: str,
    match_mode: str,
    ambiguity: str,
    now: str,
) -> None:
    alias = re.sub(r"\s+", " ", alias.strip())
    if not alias:
        return
    assertion = _sha({
        "alias": alias,
        "language": language,
        "match_mode": match_mode,
        "ambiguity": ambiguity,
        "status": "active",
        "evidence_id": None,
        "valid_from": None,
        "valid_to": None,
    })
    db.execute(
        """INSERT OR IGNORE INTO entity_aliases(
               id,entity_id,alias,alias_key,language,match_mode,ambiguity,status,
               assertion_sha256,available_at
           ) VALUES(?,?,?,?,?,?,?,'active',?,?)""",
        (
            f"entity_alias_{uuid4().hex}", entity_id, alias, _text_key(alias),
            language, match_mode, ambiguity, assertion, now,
        ),
    )


def _insert_identifier(
    db: sqlite3.Connection,
    *,
    entity_id: str,
    namespace: str,
    value: str,
    qualifier: dict,
    now: str,
) -> None:
    value = value.strip()
    if not value:
        return
    qualifier_json = _json(qualifier)
    assertion = _sha({
        "namespace": namespace,
        "value": value,
        "qualifier": qualifier,
        "valid_from": None,
        "valid_to": None,
        "evidence_id": None,
        "verification_status": "legacy_unverified",
    })
    db.execute(
        """INSERT OR IGNORE INTO entity_identifiers(
               id,entity_id,namespace,value,qualifier_json,verification_status,
               assertion_sha256,available_at
           ) VALUES(?,?,?,?,?,'legacy_unverified',?,?)""",
        (
            f"entity_identifier_{uuid4().hex}", entity_id, namespace, value,
            qualifier_json, assertion, now,
        ),
    )


def _sync_companies(db: sqlite3.Connection, now: str) -> tuple[int, int, int]:
    rows = db.execute("SELECT * FROM companies ORDER BY id").fetchall()
    canonical_keys = Counter(
        _text_key(name)
        for row in rows
        for name in (row["name"], row["name_zh"])
        if name and _text_key(name)
    )
    created = versions = 0
    dataset_id = _dataset_id(db)
    for company in rows:
        mapping = db.execute(
            "SELECT entity_id FROM legacy_company_entities WHERE company_id=?",
            (company["id"],),
        ).fetchone()
        if mapping:
            entity_id = mapping["entity_id"]
        else:
            entity_id = f"entity_{uuid4().hex}"
            db.execute(
                """INSERT INTO entities(id,dataset_id,type,status,created_at)
                   VALUES(?,?,'organization','active',?)""",
                (entity_id, dataset_id, now),
            )
            snapshot = {key: company[key] for key in company.keys()}
            db.execute(
                """INSERT INTO legacy_company_entities(
                       company_id,entity_id,legacy_sha256,available_at
                   ) VALUES(?,?,?,?)""",
                (company["id"], entity_id, _sha(snapshot), now),
            )
            created += 1

        attributes = {
            "legacy_company_id": company["id"],
            "legacy_slug": company["slug"],
            "name_zh": company["name_zh"],
            "legacy_market": company["market"],
        }
        versions += int(_append_entity_version(
            db, entity_id=entity_id, entity_type="organization",
            canonical_name=company["name"], status="active",
            attributes=attributes, now=now,
        ))
        for alias, language in ((company["name"], "en"), (company["name_zh"], "zh")):
            if alias:
                ambiguity = "ambiguous" if canonical_keys[_text_key(alias)] > 1 else "unique"
                _insert_alias(
                    db, entity_id=entity_id, alias=alias, language=language,
                    match_mode="casefold" if alias.isascii() else "exact",
                    ambiguity=ambiguity, now=now,
                )
        try:
            legacy_aliases = json.loads(company["aliases"] or "[]")
        except (TypeError, json.JSONDecodeError):
            legacy_aliases = []
        for alias in legacy_aliases if isinstance(legacy_aliases, list) else []:
            if isinstance(alias, str):
                # Watchlist keywords mix people, products, and companies. They
                # remain discovery candidates and never become certain aliases.
                _insert_alias(
                    db, entity_id=entity_id, alias=alias, language="und",
                    match_mode="candidate_only", ambiguity="unreviewed", now=now,
                )
        _insert_identifier(
            db, entity_id=entity_id, namespace="legacy_company_slug",
            value=company["slug"], qualifier={}, now=now,
        )
        _insert_identifier(
            db, entity_id=entity_id, namespace="legacy_ticker",
            value=company["ticker"], qualifier={"market": company["market"]}, now=now,
        )
        _insert_identifier(
            db, entity_id=entity_id, namespace="hk_stock_code",
            value=company["code"], qualifier={"exchange": "HKEX"}, now=now,
        )
        _insert_identifier(
            db, entity_id=entity_id, namespace="cik", value=company["cik"],
            qualifier={"source": "legacy_company_cache"}, now=now,
        )
        _insert_identifier(
            db, entity_id=entity_id, namespace="hkex_stock_id",
            value=company["hkex_stock_id"],
            qualifier={"source": "legacy_company_cache"}, now=now,
        )
    return len(rows), created, versions


def _topic_previous_slugs() -> dict[str, list[str]]:
    raw = yaml.safe_load(config.BASE_DIR.joinpath("config/topics.yaml").read_text("utf-8")) or {}
    result = {}
    for topic in raw.get("topics", []):
        previous = topic.get("previous_slugs", [])
        if isinstance(previous, list):
            result[topic["slug"]] = [value for value in previous if isinstance(value, str)]
    return result


def _topic_group(value: str) -> str:
    return {"company": "company_model"}.get(value, value)


def sync_topic_catalog(
    db: sqlite3.Connection,
    definitions: list[dict],
    *,
    previous_slugs: dict[str, list[str]] | None = None,
    now: str | None = None,
) -> tuple[int, int, int]:
    now = now or utc_now()
    previous_slugs = previous_slugs or {}
    dataset_id = _dataset_id(db)
    created = versions = 0
    for topic in definitions:
        slug = topic["slug"]
        candidates = [slug, *previous_slugs.get(slug, [])]
        matches = db.execute(
            f"SELECT DISTINCT topic_id FROM topic_slug_aliases WHERE slug IN ({','.join('?' * len(candidates))})",
            candidates,
        ).fetchall()
        if len(matches) > 1:
            raise CatalogConflict(f"topic aliases disagree for {slug}")
        if matches:
            topic_id = matches[0]["topic_id"]
        else:
            topic_id = f"topic_{uuid4().hex}"
            db.execute(
                """INSERT INTO topic_catalog(id,dataset_id,status,created_at)
                   VALUES(?,?,'active',?)""",
                (topic_id, dataset_id, now),
            )
            created += 1
        for alias in candidates:
            existing = db.execute(
                "SELECT topic_id FROM topic_slug_aliases WHERE slug=?", (alias,)
            ).fetchone()
            if existing and existing["topic_id"] != topic_id:
                raise CatalogConflict(f"topic slug {alias} already belongs to another topic")
            db.execute(
                """INSERT OR IGNORE INTO topic_slug_aliases(slug,topic_id,available_at)
                   VALUES(?,?,?)""",
                (alias, topic_id, now),
            )
        rules = topic.get("rules", {})
        rules_json = _json(rules)
        payload = {
            "slug": slug,
            "name": topic["name"],
            "group_key": _topic_group(topic["group_key"]),
            "description": topic["description"],
            "rules": rules,
            "status": topic.get("status", "active"),
        }
        version_sha = _sha(payload)
        current = db.execute(
            """SELECT version.id,version.version,version.version_sha256
               FROM topic_catalog AS topic
               LEFT JOIN topic_versions AS version ON version.id=topic.current_version_id
               WHERE topic.id=?""",
            (topic_id,),
        ).fetchone()
        if not current or current["version_sha256"] != version_sha:
            version = (current["version"] + 1) if current and current["version"] else 1
            version_id = f"topic_version_{uuid4().hex}"
            db.execute(
                """INSERT INTO topic_versions(
                       id,topic_id,version,previous_version_id,slug,name,group_key,
                       description,rules_json,rules_hash,version_sha256,status,available_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    version_id, topic_id, version, current["id"] if current else None,
                    slug, topic["name"], payload["group_key"], topic["description"],
                    rules_json, _sha(rules), version_sha, payload["status"], now,
                ),
            )
            db.execute(
                """UPDATE topic_catalog SET current_version_id=?,status=? WHERE id=?""",
                (version_id, payload["status"], topic_id),
            )
            versions += 1
    return len(definitions), created, versions


def _sync_topics(db: sqlite3.Connection, now: str) -> tuple[int, int, int]:
    definitions = []
    for row in db.execute("SELECT * FROM topics ORDER BY position,slug"):
        try:
            rules = json.loads(row["rules"] or "{}")
        except (TypeError, json.JSONDecodeError):
            rules = {}
        definitions.append({
            "slug": row["slug"],
            "name": row["name"],
            "group_key": row["group_key"],
            "description": row["description"],
            "rules": rules,
            "status": "active" if row["enabled"] else "inactive",
        })
    return sync_topic_catalog(
        db, definitions, previous_slugs=_topic_previous_slugs(), now=now,
    )


def _sync_publishers(db: sqlite3.Connection, now: str) -> tuple[int, int, int]:
    grouped: dict[str, dict] = defaultdict(lambda: {"domains": [], "aliases": []})
    for domain, (key, label, aliases) in PUBLISHERS.items():
        grouped[key]["label"] = label
        grouped[key]["domains"].append(domain)
        grouped[key]["aliases"].extend(aliases)
    dataset_id = _dataset_id(db)
    created = versions = 0
    for key, definition in sorted(grouped.items()):
        mapping = db.execute(
            "SELECT publisher_id FROM publisher_legacy_keys WHERE legacy_key=?", (key,)
        ).fetchone()
        if mapping:
            publisher_id = mapping["publisher_id"]
        else:
            publisher_id = f"publisher_{uuid4().hex}"
            db.execute(
                """INSERT INTO publishers(id,dataset_id,status,created_at)
                   VALUES(?,?,'active',?)""",
                (publisher_id, dataset_id, now),
            )
            db.execute(
                """INSERT INTO publisher_legacy_keys(legacy_key,publisher_id,available_at)
                   VALUES(?,?,?)""",
                (key, publisher_id, now),
            )
            created += 1
        payload = {"name": definition["label"], "status": "active"}
        version_sha = _sha(payload)
        current = db.execute(
            """SELECT version.id,version.version,version.version_sha256
               FROM publishers AS publisher
               LEFT JOIN publisher_versions AS version
                 ON version.id=publisher.current_version_id
               WHERE publisher.id=?""",
            (publisher_id,),
        ).fetchone()
        if not current or current["version_sha256"] != version_sha:
            version = (current["version"] + 1) if current and current["version"] else 1
            version_id = f"publisher_version_{uuid4().hex}"
            db.execute(
                """INSERT INTO publisher_versions(
                       id,publisher_id,version,previous_version_id,name,status,
                       version_sha256,available_at
                   ) VALUES(?,?,?,?,?,'active',?,?)""",
                (
                    version_id, publisher_id, version, current["id"] if current else None,
                    definition["label"], version_sha, now,
                ),
            )
            db.execute(
                "UPDATE publishers SET current_version_id=?,status='active' WHERE id=?",
                (version_id, publisher_id),
            )
            versions += 1
        names = [definition["label"], *definition["aliases"]]
        for name in dict.fromkeys(value for value in names if value):
            assertion = _sha({
                "name": name, "language": "und", "status": "active",
            })
            db.execute(
                """INSERT OR IGNORE INTO publisher_names(
                       id,publisher_id,name,name_key,status,assertion_sha256,available_at
                   ) VALUES(?,?,?,?, 'active',?,?)""",
                (
                    f"publisher_name_{uuid4().hex}", publisher_id, name,
                    _text_key(name), assertion, now,
                ),
            )
        for domain in definition["domains"]:
            existing = db.execute(
                "SELECT publisher_id FROM publisher_domains WHERE domain=?", (domain,)
            ).fetchone()
            if existing and existing["publisher_id"] != publisher_id:
                raise CatalogConflict(f"publisher domain {domain} has conflicting owners")
            assertion = _sha({
                "domain": domain, "valid_from": None, "valid_to": None,
                "evidence_id": None, "verification_status": "legacy_unverified",
            })
            db.execute(
                """INSERT OR IGNORE INTO publisher_domains(
                       id,publisher_id,domain,verification_status,assertion_sha256,available_at
                   ) VALUES(?,?,?,'legacy_unverified',?,?)""",
                (
                    f"publisher_domain_{uuid4().hex}", publisher_id, domain,
                    assertion, now,
                ),
            )
    return len(grouped), created, versions


def sync_identity_catalog(db: sqlite3.Connection) -> CatalogSyncReport:
    """Synchronize legacy configuration into append-only shadow catalogs."""
    now = utc_now()
    companies_seen, company_created, entity_versions = _sync_companies(db, now)
    topics_seen, topics_created, topic_versions = _sync_topics(db, now)
    publishers_seen, publishers_created, publisher_versions = _sync_publishers(db, now)
    return CatalogSyncReport(
        companies_seen, company_created, entity_versions,
        topics_seen, topics_created, topic_versions,
        publishers_seen, publishers_created, publisher_versions,
    )
