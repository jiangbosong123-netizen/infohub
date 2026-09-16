from __future__ import annotations

"""Source-time parsing with explicit semantics and host-timezone independence."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .timeutil import format_utc, parse_utc


TIME_RULE_VERSION = "source-time-v1"
ROLES = {"published", "updated", "accepted", "filing_date", "report_period", "other"}


@lru_cache(maxsize=1)
def _tzdb_version() -> str:
    try:
        return version("tzdata")
    except PackageNotFoundError:
        return "system-unknown"


@dataclass(frozen=True)
class SourceTime:
    field_path: str
    raw_value: str | None
    role: str
    timezone: str | None
    utc: str | None
    range_start_utc: str | None
    range_end_utc: str | None
    precision: str
    interpretation: str
    status: str
    rule_version: str = TIME_RULE_VERSION
    tzdb_version: str = "system-unknown"

    def to_dict(self) -> dict:
        return asdict(self)


def _result(
    *, field_path: str, raw_value: object, role: str, timezone_name: str | None,
    precision: str, interpretation: str, status: str, utc: str | None = None,
    range_start_utc: str | None = None, range_end_utc: str | None = None,
) -> SourceTime:
    if role not in ROLES:
        raise ValueError(f"unsupported source-time role: {role}")
    return SourceTime(
        field_path=field_path,
        raw_value=None if raw_value is None else str(raw_value),
        role=role,
        timezone=timezone_name,
        utc=utc,
        range_start_utc=range_start_utc,
        range_end_utc=range_end_utc,
        precision=precision,
        interpretation=interpretation,
        status=status,
        tzdb_version=_tzdb_version(),
    )


def _zone(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown IANA timezone: {name}") from exc


def _localize(naive: datetime, zone: ZoneInfo) -> tuple[datetime | None, str]:
    candidates: list[datetime] = []
    for fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=fold)
        roundtrip = aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        if roundtrip == naive and all(
            aware.astimezone(timezone.utc) != item.astimezone(timezone.utc)
            for item in candidates
        ):
            candidates.append(aware)
    if not candidates:
        return None, "nonexistent_local_time"
    if len(candidates) > 1:
        return None, "ambiguous_local_time"
    return candidates[0], "valid"


def parse_source_time(
    raw_value: object,
    *,
    field_path: str,
    role: str,
    interpretation: str,
    timezone_name: str | None = None,
    parser: str = "iso",
    pattern: str | None = None,
    epoch_unit: str | None = None,
    calendar_date: bool = False,
    observed_at: datetime | str | None = None,
    future_tolerance: timedelta = timedelta(minutes=10),
    check_future: bool | None = None,
) -> SourceTime:
    """Parse one field. Epoch units and naive-source zones must be explicit."""
    if raw_value is None or str(raw_value).strip() == "":
        return _result(
            field_path=field_path, raw_value=raw_value, role=role,
            timezone_name=timezone_name, precision="unknown",
            interpretation=interpretation, status="missing",
        )
    text = str(raw_value).strip()
    zone = _zone(timezone_name)
    precision = (
        "minute"
        if parser == "iso" and re.search(
            r"[T ]\d{2}:\d{2}(?:Z|[+-]\d{2}:?\d{2})?$", text
        )
        else "second"
    )
    try:
        if epoch_unit:
            if epoch_unit not in {"seconds", "milliseconds"}:
                raise ValueError("epoch unit must be seconds or milliseconds")
            scale = 1 if epoch_unit == "seconds" else 1000
            parsed = datetime.fromtimestamp(int(text) / scale, tz=timezone.utc)
            source_timezone = "UTC"
        elif parser in {"rfc2822", "feed"}:
            try:
                parsed = parsedate_to_datetime(text)
            except (TypeError, ValueError):
                if parser != "feed":
                    raise
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed is None:
                raise ValueError("time parser returned no value")
            source_timezone = timezone_name or (
                str(parsed.tzinfo) if parsed.tzinfo is not None else None
            )
        elif pattern:
            parsed = datetime.strptime(text, pattern)
            source_timezone = timezone_name
            if "%S" not in pattern:
                precision = "minute"
        elif re.fullmatch(r"\d{4}-\d{2}", text):
            precision = "month"
            start_naive = datetime.strptime(text, "%Y-%m")
            if not zone:
                return _result(
                    field_path=field_path, raw_value=raw_value, role=role,
                    timezone_name=None, precision=precision,
                    interpretation=interpretation, status="missing_timezone",
                )
            year, month = (start_naive.year + 1, 1) if start_naive.month == 12 else (
                start_naive.year, start_naive.month + 1
            )
            start, start_status = _localize(start_naive, zone)
            end, end_status = _localize(datetime(year, month, 1), zone)
            if not start or not end:
                return _result(
                    field_path=field_path, raw_value=raw_value, role=role,
                    timezone_name=timezone_name, precision=precision,
                    interpretation=interpretation,
                    status=start_status if not start else end_status,
                )
            return _result(
                field_path=field_path, raw_value=raw_value, role=role,
                timezone_name=timezone_name, precision=precision,
                interpretation=interpretation, status="valid",
                range_start_utc=format_utc(start), range_end_utc=format_utc(end),
            )
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            precision = "date"
            start_naive = datetime.strptime(text, "%Y-%m-%d")
            if calendar_date:
                return _result(
                    field_path=field_path, raw_value=raw_value, role=role,
                    timezone_name=timezone_name, precision=precision,
                    interpretation=interpretation, status="valid",
                )
            if not zone:
                return _result(
                    field_path=field_path, raw_value=raw_value, role=role,
                    timezone_name=None, precision=precision,
                    interpretation=interpretation, status="missing_timezone",
                )
            start, start_status = _localize(start_naive, zone)
            end, end_status = _localize(start_naive + timedelta(days=1), zone)
            if not start or not end:
                return _result(
                    field_path=field_path, raw_value=raw_value, role=role,
                    timezone_name=timezone_name, precision=precision,
                    interpretation=interpretation,
                    status=start_status if not start else end_status,
                )
            return _result(
                field_path=field_path, raw_value=raw_value, role=role,
                timezone_name=timezone_name, precision=precision,
                interpretation=interpretation, status="valid",
                range_start_utc=format_utc(start), range_end_utc=format_utc(end),
            )
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            source_timezone = timezone_name or (
                str(parsed.tzinfo) if parsed.tzinfo is not None else None
            )
    except (OverflowError, OSError, TypeError, ValueError):
        return _result(
            field_path=field_path, raw_value=raw_value, role=role,
            timezone_name=timezone_name, precision="unknown",
            interpretation=interpretation, status="invalid",
        )

    if parsed.tzinfo is None:
        if not zone:
            return _result(
                field_path=field_path, raw_value=raw_value, role=role,
                timezone_name=None, precision=precision,
                interpretation=interpretation, status="missing_timezone",
            )
        localized, status = _localize(parsed, zone)
        if not localized:
            return _result(
                field_path=field_path, raw_value=raw_value, role=role,
                timezone_name=timezone_name, precision=precision,
                interpretation=interpretation, status=status,
            )
        parsed = localized
    instant = parsed.astimezone(timezone.utc)
    status = "valid"
    should_check_future = role == "published" if check_future is None else check_future
    if should_check_future and observed_at is not None:
        observed = observed_at if isinstance(observed_at, datetime) else parse_utc(observed_at)
        if observed.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        if instant > observed.astimezone(timezone.utc) + future_tolerance:
            status = "future_suspect"
    return _result(
        field_path=field_path, raw_value=raw_value, role=role,
        timezone_name=source_timezone, precision=precision,
        interpretation=interpretation, status=status, utc=format_utc(instant),
    )
