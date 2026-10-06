import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.topic_assignment_reviews import record_topic_assignment_review
from app.topic_statistics_admission import _metrics

NOW = "2026-09-23T18:00:00.000000Z"
LATEST_REVIEW = """WITH latest_review AS (
       SELECT review.* FROM topic_assignment_reviews AS review
       WHERE NOT EXISTS(
           SELECT 1 FROM topic_assignment_reviews AS later
           WHERE later.assignment_id=review.assignment_id
             AND later.version>review.version
       )
   )"""


def old_effective_and_topics(db, dataset_id):
    """The two separate passes _metrics made before, kept verbatim."""
    rows = db.execute(LATEST_REVIEW + """
           SELECT COALESCE(review.decision,assignment.status) AS effective_status,
                  COUNT(*) AS count
           FROM document_topic_assignments AS assignment
           JOIN document_versions AS version ON version.id=assignment.document_version_id
           JOIN documents AS document ON document.id=version.document_id
           LEFT JOIN latest_review AS review ON review.assignment_id=assignment.id
           WHERE document.dataset_id=?
           GROUP BY COALESCE(review.decision,assignment.status)""", (dataset_id,)).fetchall()
    accepted_topics = db.execute(LATEST_REVIEW + """
           SELECT COUNT(DISTINCT topic.topic_id)
           FROM document_topic_assignments AS assignment
           JOIN topic_versions AS topic ON topic.id=assignment.topic_version_id
           JOIN document_versions AS version ON version.id=assignment.document_version_id
           JOIN documents AS document ON document.id=version.document_id
           LEFT JOIN latest_review AS review ON review.assignment_id=assignment.id
           WHERE document.dataset_id=?
             AND COALESCE(review.decision,assignment.status)='accepted'""",
        (dataset_id,)).fetchone()[0]
    return {row["effective_status"]: row["count"] for row in rows}, accepted_topics


class TopicAdmissionMetricTests(unittest.TestCase):
    """Random assignments, topic versions, reviews and corrections, plus another dataset."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "app.db"
        for mocked in (patch.object(database, "DB_PATH", path), patch.object(config, "DB_PATH", path)):
            mocked.start()
            self.addCleanup(mocked.stop)
        database.init_schema()
        self.rng = random.Random(20261006)
        with database.get_db() as db:
            self.dataset = db.execute("SELECT dataset_id FROM dataset_state").fetchone()[0]
            db.execute("INSERT INTO sources(id,key,name,channel,type) VALUES(1,'s','S','ai','rss')")
            topic_versions = []
            for topic in range(6):
                previous = None
                for version in range(1, 3 if topic % 2 else 2):
                    db.execute("""INSERT INTO topic_catalog(id,dataset_id,status,created_at)
                                  VALUES(?,?,'active',?) ON CONFLICT DO NOTHING""",
                               (f"t{topic}", self.dataset if topic < 5 else "other", NOW))
                    version_id = f"tv{topic}-{version}"
                    db.execute("""INSERT INTO topic_versions(
                                      id,topic_id,version,previous_version_id,slug,name,group_key,
                                      description,rules_json,rules_hash,version_sha256,status,
                                      available_at)
                                  VALUES(?,?,?,?,?,?,'technology','','{}',?,?,'active',?)""",
                               (version_id, f"t{topic}", version, previous, f"topic-{topic}",
                                f"Topic {topic}", f"{topic}{version}".ljust(64, "c"),
                                f"{topic}{version}".ljust(64, "d"), NOW))
                    topic_versions.append(version_id)
                    previous = version_id
            self.assignments = []
            for item in range(1, 61):
                dataset = self.dataset if item <= 50 else "other"
                db.execute("""INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at)
                              VALUES(?,1,?,?,'ai',?,?)""",
                           (item, f"https://example.test/{item}", f"Item {item}", NOW, NOW))
                db.execute("""INSERT INTO documents(id,dataset_id,legacy_item_id,kind,first_seen_at)
                              VALUES(?,?,?,'article',?)""", (f"d{item}", dataset, item, NOW))
                for version in range(1, self.rng.choice((2, 2, 3)) + 0):
                    db.execute("""INSERT INTO document_versions(
                           id,document_id,version,normalizer_version,normalized_at,title_original,
                           language,text,content_sha256,version_sha256,canonical_url,source_id,
                           published_precision,time_status,time_rule_version,tzdb_version,
                           content_origin,content_extent,truncated,extraction_status,correction_kind,
                           available_at,availability_basis,point_in_time_eligible,previous_version_id)
                       VALUES(?,?,?,'v1',?,'T','en','',?,?,?,1,'unknown','legacy_unverified','legacy',
                              'unknown','legacy_unknown','none',0,'not_attempted',?,?,'legacy_unknown',0,?)""",
                        (f"dv{item}-{version}", f"d{item}", version, NOW, f"{item}{version}".ljust(64, "a"),
                         f"{item}{version}".ljust(64, "b"), f"https://example.test/{item}",
                         "initial" if version == 1 else "content_change", NOW,
                         None if version == 1 else f"dv{item}-{version - 1}"))
                    for topic_version in self.rng.sample(topic_versions, self.rng.choice((0, 1, 2, 3))):
                        assignment = f"a{item}-{version}-{topic_version}"
                        db.execute("""INSERT INTO document_topic_assignments(
                               id,document_version_id,topic_version_id,method,method_version,status,
                               available_at) VALUES(?,?,?,'fixture','fixture-v1',?,?)""",
                            (assignment, f"dv{item}-{version}", topic_version,
                             self.rng.choice(("candidate", "candidate", "accepted", "rejected", "superseded")),
                             NOW))
                        self.assignments.append(assignment)
                db.execute("UPDATE documents SET current_version_id=? WHERE id=?",
                           (f"dv{item}-1", f"d{item}"))

    def assert_same_metrics(self):
        publication = {"publication_id": "p", "publication_version": 1, "build_id": "b",
                       "dataset_id": self.dataset}
        with database.get_db() as db:
            effective, accepted_topics = old_effective_and_topics(db, self.dataset)
            metrics = _metrics(db, publication)
        self.assertEqual(metrics["assignment_total"], sum(effective.values()))
        for state in ("accepted", "rejected", "candidate", "superseded"):
            self.assertEqual(metrics[f"effective_{state}"], effective.get(state, 0), state)
        self.assertEqual(metrics["topics_with_accepted_documents"], accepted_topics)
        return metrics

    def test_single_pass_matches_both_previous_passes(self):
        before = self.assert_same_metrics()
        latest = {}
        for _ in range(3):
            for assignment in self.rng.sample(self.assignments, len(self.assignments) // 3):
                with database.get_db() as db:
                    review = record_topic_assignment_review(
                        db, assignment_id=assignment,
                        decision=self.rng.choice(("accepted", "rejected")),
                        expected_previous_review_id=latest.get(assignment),
                        reviewer_id="fixture", reason="Synthetic review.", now=NOW,
                    )
                latest[assignment] = review.current_review_id
            after = self.assert_same_metrics()
        self.assertNotEqual(before["effective_accepted"], after["effective_accepted"])
        self.assertGreater(after["topics_with_accepted_documents"], 1)
        self.assertGreater(after["reviewed_assignments"], 0)

    def test_no_accepted_assignment_counts_no_topic(self):
        with database.get_db() as db:
            accepted = [row[0] for row in db.execute(
                "SELECT id FROM document_topic_assignments WHERE status='accepted'")]
            for assignment in accepted:
                record_topic_assignment_review(
                    db, assignment_id=assignment, decision="rejected",
                    expected_previous_review_id=None, reviewer_id="fixture",
                    reason="Synthetic rejection.", now=NOW,
                )
        self.assertTrue(accepted)
        metrics = self.assert_same_metrics()
        self.assertEqual((metrics["effective_accepted"], metrics["topics_with_accepted_documents"]),
                         (0, 0))


if __name__ == "__main__":
    unittest.main()
