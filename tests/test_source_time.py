import os
import time
import unittest
from datetime import datetime, timezone

from app.source_time import parse_source_time


class SourceTimeTests(unittest.TestCase):
    def test_explicit_offset_is_host_timezone_independent(self):
        previous = os.environ.get("TZ")
        try:
            values = []
            for host_tz in ("UTC", "America/Los_Angeles", "Asia/Shanghai"):
                os.environ["TZ"] = host_tz
                if hasattr(time, "tzset"):
                    time.tzset()
                values.append(parse_source_time(
                    "2026-01-15T09:30:00-05:00", field_path="published",
                    role="published", interpretation="publisher timestamp",
                ).utc)
            self.assertEqual(values, ["2026-01-15T14:30:00.000000Z"] * 3)
        finally:
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            if hasattr(time, "tzset"):
                time.tzset()

    def test_iso_minute_precision_is_not_promoted_to_seconds(self):
        value = parse_source_time(
            "2026-09-16T09:30+08:00", field_path="published", role="published",
            interpretation="publisher minute timestamp",
        )
        self.assertEqual(value.precision, "minute")
        self.assertEqual(value.utc, "2026-09-16T01:30:00.000000Z")

    def test_naive_time_requires_source_rule_and_dst_is_not_guessed(self):
        missing = parse_source_time(
            "2026-01-15T09:30:00", field_path="published", role="published",
            interpretation="unknown local timestamp",
        )
        ambiguous = parse_source_time(
            "2026-11-01T01:30:00", field_path="published", role="published",
            timezone_name="America/New_York", interpretation="publisher local timestamp",
        )
        nonexistent = parse_source_time(
            "2026-03-08T02:30:00", field_path="published", role="published",
            timezone_name="America/New_York", interpretation="publisher local timestamp",
        )
        self.assertEqual(missing.status, "missing_timezone")
        self.assertEqual(ambiguous.status, "ambiguous_local_time")
        self.assertEqual(nonexistent.status, "nonexistent_local_time")
        self.assertIsNone(ambiguous.utc)

    def test_date_precision_is_a_natural_range_across_dst(self):
        spring = parse_source_time(
            "2026-03-08", field_path="filingDate", role="filing_date",
            timezone_name="America/New_York", interpretation="SEC filing date",
        )
        fall = parse_source_time(
            "2026-11-01", field_path="filingDate", role="filing_date",
            timezone_name="America/New_York", interpretation="SEC filing date",
        )
        spring_hours = (
            datetime.fromisoformat(spring.range_end_utc.replace("Z", "+00:00"))
            - datetime.fromisoformat(spring.range_start_utc.replace("Z", "+00:00"))
        ).total_seconds() / 3600
        fall_hours = (
            datetime.fromisoformat(fall.range_end_utc.replace("Z", "+00:00"))
            - datetime.fromisoformat(fall.range_start_utc.replace("Z", "+00:00"))
        ).total_seconds() / 3600
        self.assertEqual((spring.precision, spring.utc, spring_hours), ("date", None, 23))
        self.assertEqual(fall_hours, 25)

    def test_epoch_unit_is_explicit_and_future_is_not_rewritten(self):
        observed = datetime(2026, 1, 1, tzinfo=timezone.utc)
        seconds = parse_source_time(
            1767225600, field_path="ctime", role="published", epoch_unit="seconds",
            interpretation="CLS Unix seconds", observed_at=observed,
        )
        millis = parse_source_time(
            1767225600000, field_path="ctime", role="published", epoch_unit="milliseconds",
            interpretation="fixture Unix milliseconds", observed_at=observed,
        )
        self.assertEqual(seconds.utc, millis.utc)
        self.assertEqual(seconds.status, "valid")
        future = parse_source_time(
            "2027-01-01T00:00:00Z", field_path="published", role="published",
            interpretation="publisher timestamp", observed_at=observed,
        )
        self.assertEqual(future.status, "future_suspect")
        self.assertEqual(future.utc, "2027-01-01T00:00:00.000000Z")

    def test_rss_updated_is_not_relabelled_published(self):
        updated = parse_source_time(
            "Wed, 16 Sep 2026 09:00:00 GMT", field_path="entry.updated",
            role="updated", parser="rfc2822", interpretation="RSS update timestamp",
        )
        self.assertEqual(updated.role, "updated")
        self.assertEqual(updated.utc, "2026-09-16T09:00:00.000000Z")
        invalid = parse_source_time(
            "not a timestamp", field_path="entry.updated", role="updated",
            parser="feed", interpretation="RSS update timestamp",
        )
        self.assertEqual(invalid.status, "invalid")

    def test_calendar_date_preserves_semantics_without_inventing_an_instant(self):
        filing = parse_source_time(
            "2026-09-16", field_path="filingDate", role="filing_date",
            interpretation="SEC filing calendar date", calendar_date=True,
        )
        self.assertEqual((filing.status, filing.precision, filing.utc), ("valid", "date", None))


if __name__ == "__main__":
    unittest.main()
