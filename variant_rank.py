"""Rank MyWhoosh variant FITs by structural readiness against a trusted reference.

A "trusted" FIT (typically the original Zwift FIT) is used only to compare structural
shape: presence of message types, field schemas, sport/sub-sport class, etc.  Numeric
ride values are explicitly **not** compared - the two activities are different rides.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from fix_fit import (
    FIT_EPOCH,
    F_TIMESTAMP,
    MSG_ACTIVITY,
    MSG_DEVICE_INFO,
    MSG_EVENT,
    MSG_FILE_ID,
    MSG_LAP,
    MSG_RECORD,
    MSG_SESSION,
    MSG_SPORT,
    Message,
    _parse_fit,
    _read_sint32,
    _read_uint,
)
from garmin_pipeline import (
    MSG_HR_ZONE,
    MSG_POWER_ZONE,
    MSG_TIME_IN_ZONE,
    MSG_USER_PROFILE,
    MSG_ZONES_TARGET,
    _messages_of,
    _write_json,
    _write_text,
)

CYCLING_SPORT = 2
ACCEPTABLE_SUB_SPORTS = {6, 7, 58}  # spin, indoor_cycling, virtual_activity


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""
    weight: int = 1


@dataclass
class VariantReadiness:
    path: Path
    sha256: str
    size: int
    checks: list[Check] = field(default_factory=list)
    score: int = 0
    max_score: int = 0
    risks: list[str] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        return self.score / self.max_score if self.max_score else 0.0


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fit_to_iso(value: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    seconds = FIT_EPOCH.timestamp() + value
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def _timestamp_in_range(value: Optional[int]) -> bool:
    if value is None:
        return False
    # FIT timestamps near 0 indicate Unix-epoch leakage. 1989-2099 plausible range.
    return 86_400 < value < 3_500_000_000


def _check_local_timestamp(activity: Optional[Message], end_timestamp: Optional[int]) -> Check:
    if activity is None:
        return Check("local_timestamp", False, "no activity message")
    local_timestamp = _read_uint(activity, 5)
    if local_timestamp is None:
        return Check("local_timestamp", False, "missing field 5")
    if end_timestamp is None:
        return Check("local_timestamp", False, "no session end to compare")
    delta = local_timestamp - end_timestamp
    # Should be a small (timezone-sized) offset, not ~Unix-epoch shift (~631 million s).
    if abs(delta) > 90_000:
        return Check(
            "local_timestamp",
            False,
            f"local_timestamp - end = {delta} s (likely Unix-epoch leak)",
        )
    return Check("local_timestamp", True, f"offset {delta} s")


def _gather_checks(
    messages: Sequence[Message], trusted_messages: Sequence[Message]
) -> tuple[list[Check], list[str]]:
    checks: list[Check] = []
    risks: list[str] = []

    file_id = next(iter(_messages_of(messages, MSG_FILE_ID)), None)
    checks.append(Check("file_id_present", file_id is not None))

    sessions = _messages_of(messages, MSG_SESSION)
    laps = _messages_of(messages, MSG_LAP)
    records = _messages_of(messages, MSG_RECORD)
    events = _messages_of(messages, MSG_EVENT)
    activities = _messages_of(messages, MSG_ACTIVITY)
    devices = _messages_of(messages, MSG_DEVICE_INFO)
    sport_messages = _messages_of(messages, MSG_SPORT)
    user_profile = _messages_of(messages, MSG_USER_PROFILE)
    zones_target = _messages_of(messages, MSG_ZONES_TARGET)
    hr_zones = _messages_of(messages, MSG_HR_ZONE)
    power_zones = _messages_of(messages, MSG_POWER_ZONE)
    time_in_zone = _messages_of(messages, MSG_TIME_IN_ZONE)

    checks.append(Check("single_session", len(sessions) == 1, f"sessions={len(sessions)}"))
    checks.append(Check("single_lap_minimum", len(laps) >= 1, f"laps={len(laps)}"))
    checks.append(Check("records_present", bool(records), f"records={len(records)}"))
    checks.append(Check("events_present", bool(events), f"events={len(events)}"))
    checks.append(Check("activity_present", len(activities) == 1, f"activity={len(activities)}"))
    checks.append(Check("device_info_present", bool(devices), f"device_info={len(devices)}"))

    session = sessions[0] if sessions else None
    end_timestamp = _read_uint(session, F_TIMESTAMP) if session else None
    start_timestamp = _read_uint(session, 2) if session else None

    checks.append(
        Check(
            "session_timestamp_valid",
            _timestamp_in_range(end_timestamp),
            f"end={end_timestamp} ({_fit_to_iso(end_timestamp)})",
        )
    )
    checks.append(
        Check(
            "session_start_valid",
            _timestamp_in_range(start_timestamp),
            f"start={start_timestamp} ({_fit_to_iso(start_timestamp)})",
        )
    )
    activity_message = activities[0] if activities else None
    activity_end = _read_uint(activity_message, F_TIMESTAMP) if activity_message else None
    checks.append(
        Check(
            "activity_end_matches_session",
            activity_end is not None and activity_end == end_timestamp,
            f"activity_end={activity_end} session_end={end_timestamp}",
        )
    )
    lap_end = _read_uint(laps[-1], F_TIMESTAMP) if laps else None
    checks.append(
        Check(
            "lap_end_matches_session",
            lap_end is not None and lap_end == end_timestamp,
            f"lap_end={lap_end} session_end={end_timestamp}",
        )
    )
    checks.append(_check_local_timestamp(activity_message, end_timestamp))

    record_timestamps = [
        _read_uint(record, F_TIMESTAMP) for record in records
    ]
    monotonic = all(
        previous is not None and current is not None and current > previous
        for previous, current in zip(record_timestamps, record_timestamps[1:])
    )
    checks.append(
        Check(
            "record_timestamps_monotonic",
            monotonic,
            f"records={len(record_timestamps)}",
        )
    )
    if records and start_timestamp is not None and end_timestamp is not None:
        first = record_timestamps[0]
        last = record_timestamps[-1]
        in_window = (
            first is not None
            and last is not None
            and start_timestamp <= first
            and last <= end_timestamp
        )
        checks.append(
            Check(
                "records_inside_session_window",
                in_window,
                f"first={first}, last={last}, window=[{start_timestamp},{end_timestamp}]",
            )
        )
    else:
        checks.append(Check("records_inside_session_window", False, "missing window data"))

    sport_message = sport_messages[0] if sport_messages else None
    sport = _read_uint(sport_message, 0) if sport_message else None
    sub_sport = _read_uint(sport_message, 1) if sport_message else None
    session_sport = _read_uint(session, 5) if session else None
    session_sub_sport = _read_uint(session, 6) if session else None
    cycling_safe = (
        sport == CYCLING_SPORT
        and sub_sport in ACCEPTABLE_SUB_SPORTS
        and session_sport == CYCLING_SPORT
        and session_sub_sport in ACCEPTABLE_SUB_SPORTS
    )
    checks.append(
        Check(
            "sport_cycling_safe",
            cycling_safe,
            f"sport={sport}/{session_sport} sub_sport={sub_sport}/{session_sub_sport}",
        )
    )

    hr_count = sum(1 for record in records if _read_uint(record, 3) is not None)
    cadence_count = sum(1 for record in records if _read_uint(record, 4) is not None)
    power_count = sum(1 for record in records if _read_uint(record, 7) is not None)
    speed_count = sum(1 for record in records if _read_uint(record, 6) is not None)
    distance_count = sum(1 for record in records if _read_uint(record, 5) is not None)
    checks.append(Check("hr_records_present", hr_count > 0, f"hr_samples={hr_count}"))
    checks.append(Check("power_records_present", power_count > 0, f"power_samples={power_count}"))
    checks.append(Check("cadence_records_present", cadence_count > 0, f"cadence={cadence_count}"))
    checks.append(Check("speed_records_present", speed_count > 0, f"speed={speed_count}"))
    checks.append(Check("distance_records_present", distance_count > 0, f"distance={distance_count}"))

    te_value = _read_uint(session, 24) if session else None
    ate_value = _read_uint(session, 137) if session else None
    load_value = _read_sint32(session, 168) if session else None
    checks.append(
        Check(
            "te_field_24_present",
            te_value is not None,
            f"raw={te_value} scaled={te_value / 10 if te_value is not None else '-'}",
            weight=0,  # informational
        )
    )
    checks.append(
        Check(
            "te_field_137_present",
            ate_value is not None,
            f"raw={ate_value} scaled={ate_value / 10 if ate_value is not None else '-'}",
            weight=0,
        )
    )
    checks.append(
        Check(
            "te_field_168_present",
            load_value is not None,
            f"raw={load_value} scaled={load_value / 65536 if load_value is not None else '-'}",
            weight=0,
        )
    )

    checks.append(
        Check(
            "user_profile_present",
            bool(user_profile),
            f"count={len(user_profile)}",
            weight=0,
        )
    )
    checks.append(
        Check(
            "zones_target_present",
            bool(zones_target),
            f"count={len(zones_target)}",
            weight=0,
        )
    )
    checks.append(
        Check(
            "hr_zones_present",
            bool(hr_zones),
            f"count={len(hr_zones)}",
            weight=0,
        )
    )
    checks.append(
        Check(
            "power_zones_present",
            bool(power_zones),
            f"count={len(power_zones)}",
            weight=0,
        )
    )
    checks.append(
        Check(
            "time_in_zone_present",
            bool(time_in_zone),
            f"count={len(time_in_zone)}",
            weight=0,
        )
    )

    # Risk surface relative to trusted reference (structural-only).
    trusted_types = {message.global_message for message in trusted_messages}
    variant_types = {message.global_message for message in messages}
    only_in_variant = sorted(variant_types - trusted_types)
    only_in_trusted = sorted(trusted_types - variant_types)
    if only_in_variant:
        risks.append(
            f"variant has {len(only_in_variant)} message type(s) not present in trusted "
            f"reference: {only_in_variant}"
        )
    if only_in_trusted:
        risks.append(
            f"trusted reference has {len(only_in_trusted)} message type(s) absent from "
            f"variant: {only_in_trusted}"
        )
    if not cycling_safe:
        risks.append("sport/sub_sport not in known cycling-safe set")
    if power_count == 0:
        risks.append("no power records - watch may not credit cycling Training Effect")
    if te_value is None and ate_value is None and load_value is None:
        risks.append(
            "no Training Effect or Load fields written; relies entirely on Garmin Connect"
        )

    return checks, risks


def score_variant(path: Path, trusted_messages: Sequence[Message]) -> VariantReadiness:
    data = path.read_bytes()
    _, messages = _parse_fit(data)
    checks, risks = _gather_checks(messages, trusted_messages)
    score = sum(check.weight for check in checks if check.passed)
    max_score = sum(check.weight for check in checks)
    return VariantReadiness(
        path=path,
        sha256=_sha256(data),
        size=len(data),
        checks=checks,
        score=score,
        max_score=max_score,
        risks=risks,
    )


def expand_variant_paths(patterns: Sequence[str]) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        matches = sorted(Path(match) for match in glob.glob(pattern))
        if not matches:
            candidate = Path(pattern)
            if candidate.is_file():
                matches = [candidate]
        for match in matches:
            resolved = match.resolve()
            if resolved not in seen:
                seen.add(resolved)
                paths.append(match)
    return paths


def rank_variants(
    trusted_path: Path, variant_patterns: Sequence[str]
) -> list[VariantReadiness]:
    _, trusted_messages = _parse_fit(trusted_path.read_bytes())
    paths = expand_variant_paths(variant_patterns)
    if not paths:
        raise ValueError(f"no variant FITs matched patterns: {list(variant_patterns)}")
    readiness = [score_variant(path, trusted_messages) for path in paths]
    readiness.sort(
        key=lambda item: (item.ratio, -len(item.risks), item.path.name), reverse=True
    )
    return readiness


def _readiness_to_dict(readiness: VariantReadiness) -> dict[str, Any]:
    return {
        "path": str(readiness.path),
        "sha256": readiness.sha256,
        "size": readiness.size,
        "score": readiness.score,
        "max_score": readiness.max_score,
        "ratio": readiness.ratio,
        "risks": list(readiness.risks),
        "checks": [
            {
                "name": check.name,
                "passed": check.passed,
                "detail": check.detail,
                "weight": check.weight,
            }
            for check in readiness.checks
        ],
    }


def write_rank_reports(
    outputs_dir: Path,
    trusted_path: Path,
    readiness: Sequence[VariantReadiness],
) -> None:
    json_path = outputs_dir / "variant_readiness_rank.json"
    payload = {
        "trusted_reference": str(trusted_path),
        "variants": [_readiness_to_dict(item) for item in readiness],
    }
    _write_json(json_path, payload)

    rows = [
        "| Rank | Variant | Score | Risks | Notes |",
        "|---:|---|---:|---:|---|",
    ]
    for index, item in enumerate(readiness, start=1):
        failures = [check.name for check in item.checks if not check.passed and check.weight > 0]
        notes = "; ".join(failures) if failures else "all weighted checks pass"
        rows.append(
            "| {rank} | `{name}` | {score}/{maxs} ({pct:.0%}) | {risks} | {notes} |".format(
                rank=index,
                name=item.path.name,
                score=item.score,
                maxs=item.max_score,
                pct=item.ratio,
                risks=len(item.risks),
                notes=notes,
            )
        )

    detail_sections = []
    for item in readiness:
        check_rows = "\n".join(
            f"  - `{check.name}` [{'PASS' if check.passed else 'FAIL'}, weight {check.weight}] {check.detail}"
            for check in item.checks
        )
        risk_lines = (
            "\n".join(f"  - {risk}" for risk in item.risks) if item.risks else "  - none"
        )
        detail_sections.append(
            f"### `{item.path.name}`\n\n"
            f"- Score: {item.score}/{item.max_score} ({item.ratio:.0%})\n"
            f"- SHA-256: `{item.sha256}`\n"
            f"- Size: {item.size} bytes\n"
            f"- Risks:\n{risk_lines}\n"
            f"- Checks:\n{check_rows}"
        )

    md_path = outputs_dir / "reports" / "variant_readiness_rank.md"
    _write_text(
        md_path,
        "# Variant Readiness Rank\n\n"
        f"Trusted reference (structural only): `{trusted_path}`\n\n"
        "Higher score = more weighted structural checks pass. Ride values from the\n"
        "trusted FIT are **not** compared - only message-type presence is.\n\n"
        + "\n".join(rows)
        + "\n\n## Per-variant detail\n\n"
        + "\n\n".join(detail_sections)
        + "\n\nMachine-readable rank: `outputs/variant_readiness_rank.json`.\n"
        "This ranking does not by itself justify reordering `test_matrix.md`; the\n"
        "upload order documented there continues to apply unless empirical upload\n"
        "behavior proves otherwise.\n",
    )


def run_rank(
    trusted_path: Path, variant_patterns: Sequence[str], outputs_dir: Path
) -> list[VariantReadiness]:
    readiness = rank_variants(trusted_path, variant_patterns)
    write_rank_reports(outputs_dir, trusted_path, readiness)
    return readiness


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rank MyWhoosh variant FITs by structural readiness."
    )
    parser.add_argument("--trusted", required=True, type=Path)
    parser.add_argument(
        "--variants",
        required=True,
        nargs="+",
        help="One or more FIT file paths or glob patterns.",
    )
    parser.add_argument("--outputs", type=Path, default=Path("outputs"))
    arguments = parser.parse_args(argv)
    run_rank(arguments.trusted, arguments.variants, arguments.outputs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
