"""Build Garmin-donor metadata variants while preserving MyWhoosh ride data."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, Sequence

from fix_fit import (
    F_TIMESTAMP,
    MSG_ACTIVITY,
    MSG_DEVICE_INFO,
    MSG_EVENT,
    MSG_FILE_CREATOR,
    MSG_FILE_ID,
    MSG_LAP,
    MSG_RECORD,
    MSG_SESSION,
    MSG_SPORT,
    TYPE_ENUM,
    TYPE_STRING,
    Message,
    RawField,
    _encode_fit,
    _pack_field,
    _parse_fit,
    _read_sint32,
    _read_uint,
    convert_fit_bytes,
)
from garmin_pipeline import (
    MSG_DEVICE_SETTINGS,
    MSG_TIME_IN_ZONE,
    MSG_USER_PROFILE,
    MSG_ZONES_TARGET,
    PUBLIC_LAP_FIELDS,
    PUBLIC_SESSION_FIELDS,
    FitInput,
    _field_name,
    _json_safe,
    _make_time_in_zone,
    _message_name,
    _messages_of,
    _most_common_template,
    _one,
    _reschema,
    _sdk_decode,
    _unpack_uint_array,
    recalculate_core,
    validate_fit,
)


MSG_SENSOR = 147
CYCLING = 2
INDOOR_CYCLING = 6

VARIANT_NAMES = (
    "conservative_garmin_device_spoof",
    "garmin_ordered_spoof",
    "full_training_spoof",
    "donor_max_spoof",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_input(path: Path) -> FitInput:
    if path.suffix.lower() != ".zip":
        return FitInput(path.stem, path, path.read_bytes())
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".fit")]
        if len(names) != 1:
            raise ValueError(f"expected one FIT in {path}, found {len(names)}")
        name = names[0]
        return FitInput(Path(name).stem, Path(name), archive.read(name), container=path)


def _replace_fields_preserving(
    message: Message, replacements: dict[int, RawField]
) -> Message:
    fields: list[RawField] = []
    used: set[int] = set()
    for field in message.fields:
        if field.number in replacements and field.number not in used:
            fields.append(replacements[field.number])
            used.add(field.number)
        else:
            fields.append(field)
    fields.extend(field for number, field in replacements.items() if number not in used)
    return replace(message, fields=tuple(fields))


def _replace_uint_preserving(message: Message, number: int, value: int) -> Message:
    field = message.first(number)
    if field is None:
        raise ValueError(f"message {message.global_message} has no field {number}")
    return _replace_fields_preserving(
        message,
        {number: _pack_field(message.endian, number, field.base_type, value)},
    )


def _replace_enum_preserving(message: Message, number: int, value: int) -> Message:
    return _replace_fields_preserving(
        message, {number: _pack_field(message.endian, number, TYPE_ENUM, value)}
    )


def _target_sport(message: Message) -> Message:
    name_field = message.first(3)
    replacements = {
        0: _pack_field(message.endian, 0, TYPE_ENUM, CYCLING),
        1: _pack_field(message.endian, 1, TYPE_ENUM, INDOOR_CYCLING),
    }
    if name_field is not None:
        replacements[3] = _pack_field(
            message.endian,
            3,
            TYPE_STRING,
            "INDOOR CYCLING",
            name_field.size,
        )
    return _replace_fields_preserving(message, replacements)


def _target_lap(message: Message) -> Message:
    return _replace_enum_preserving(
        _replace_enum_preserving(message, 25, CYCLING), 39, INDOOR_CYCLING
    )


def _target_session(message: Message) -> Message:
    return _replace_enum_preserving(
        _replace_enum_preserving(message, 5, CYCLING), 6, INDOOR_CYCLING
    )


def _donor_event_pair(donor_messages: Sequence[Message], start: int, end: int) -> tuple[Message, Message]:
    events = _messages_of(donor_messages, MSG_EVENT)
    start_template = next(
        message
        for message in events
        if _read_uint(message, 0) == 0 and _read_uint(message, 1) == 0
    )
    stop_template = next(
        message
        for message in reversed(events)
        if _read_uint(message, 0) == 0 and _read_uint(message, 1) == 4
    )
    return (
        _replace_uint_preserving(start_template, F_TIMESTAMP, start),
        _replace_uint_preserving(stop_template, F_TIMESTAMP, end),
    )


def _donor_devices(
    donor_messages: Sequence[Message], donor_start: int, target_start: int, target_end: int
) -> tuple[list[Message], list[Message]]:
    start_devices: list[Message] = []
    end_devices: list[Message] = []
    for message in _messages_of(donor_messages, MSG_DEVICE_INFO):
        timestamp = _read_uint(message, F_TIMESTAMP)
        is_start = timestamp is None or timestamp <= donor_start + 1
        copied = _replace_uint_preserving(
            message, F_TIMESTAMP, target_start if is_start else target_end
        )
        (start_devices if is_start else end_devices).append(copied)
    return start_devices, end_devices


def _valid_boundaries(message: Message, field_number: int) -> list[int]:
    invalid = {0xFF, 0xFFFF, 0xFFFFFFFF}
    return [value for value in _unpack_uint_array(message, field_number) if value not in invalid]


def _time_in_zone_messages(
    donor_messages: Sequence[Message],
    records: Sequence[Message],
    metrics: Any,
) -> tuple[Message | None, Message | None]:
    templates = [
        message
        for message in _messages_of(donor_messages, MSG_TIME_IN_ZONE)
        if _read_uint(message, 0) == MSG_SESSION
    ]
    if not templates:
        return None, None
    template = templates[0]
    hr_boundaries = _valid_boundaries(template, 6)
    power_boundaries = _valid_boundaries(template, 9)
    if not hr_boundaries:
        return None, None
    return (
        _make_time_in_zone(
            template,
            MSG_LAP,
            metrics,
            records,
            hr_boundaries,
            power_boundaries=power_boundaries or None,
        ),
        _make_time_in_zone(
            template,
            MSG_SESSION,
            metrics,
            records,
            hr_boundaries,
            power_boundaries=power_boundaries or None,
        ),
    )


def _field_numbers(messages: Iterable[Message]) -> list[int]:
    return sorted({field.number for message in messages for field in message.fields})


def _developer_schemas(messages: Iterable[Message]) -> list[tuple[int, int, int]]:
    return sorted(
        {
            (field.number, field.size, field.developer_index)
            for message in messages
            for field in message.developer_fields
        }
    )


def _field_labels(message_number: int, field_numbers: Sequence[int]) -> str:
    return ", ".join(
        f"`{number}` ({_field_name(message_number, number)})"
        for number in field_numbers
    )


def _record_signature(messages: Sequence[Message]) -> list[tuple[Any, ...]]:
    return [
        (
            _read_uint(message, F_TIMESTAMP),
            _read_sint32(message, 0),
            _read_sint32(message, 1),
            _read_uint(message, 2),
            _read_uint(message, 3),
            _read_uint(message, 4),
            _read_uint(message, 5),
            _read_uint(message, 6),
            _read_uint(message, 7),
        )
        for message in _messages_of(messages, MSG_RECORD)
    ]


def _summary_signature(message: Message, lap: bool) -> tuple[Any, ...]:
    fields = (
        (253, 2, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 33, 41)
        if lap
        else (253, 2, 7, 8, 9, 10, 11, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 34, 48)
    )
    return tuple(_read_uint(message, number) for number in fields)


def _device_payload(message: Message) -> tuple[Any, ...]:
    return _message_payload_excluding(message, {F_TIMESTAMP})


def _message_payload_excluding(
    message: Message, excluded_fields: set[int] | None = None
) -> tuple[Any, ...]:
    excluded = excluded_fields or set()
    return (
        message.global_message,
        message.endian,
        tuple(
            (field.number, field.size, field.base_type, field.data)
            for field in message.fields
            if field.number not in excluded
        ),
        tuple(
            (field.number, field.size, field.developer_index, field.data)
            for field in message.developer_fields
        ),
    )


def _validate_variant(
    data: bytes,
    original_source_messages: Sequence[Message],
    normalized_source_messages: Sequence[Message],
    donor_messages: Sequence[Message],
) -> dict[str, Any]:
    result = validate_fit(data)
    _header, messages = _parse_fit(data)
    decoded, sdk_errors, _order, _definitions = _sdk_decode(data)
    source_records = _record_signature(original_source_messages)
    target_records = _record_signature(messages)
    donor_device_payloads = Counter(
        _device_payload(message) for message in _messages_of(donor_messages, MSG_DEVICE_INFO)
    )
    target_device_payloads = Counter(
        _device_payload(message) for message in _messages_of(messages, MSG_DEVICE_INFO)
    )
    donor_file_id = _one(donor_messages, MSG_FILE_ID)
    target_file_id = _one(messages, MSG_FILE_ID)
    donor_events = _messages_of(donor_messages, MSG_EVENT)
    target_events = _messages_of(messages, MSG_EVENT)
    donor_start_event = next(
        message
        for message in donor_events
        if _read_uint(message, 0) == 0 and _read_uint(message, 1) == 0
    )
    donor_stop_event = next(
        message
        for message in reversed(donor_events)
        if _read_uint(message, 0) == 0 and _read_uint(message, 1) == 4
    )
    copied_profile_payloads_exact = all(
        Counter(
            _message_payload_excluding(message)
            for message in _messages_of(messages, message_number)
        )
        == Counter(
            _message_payload_excluding(message)
            for message in _messages_of(donor_messages, message_number)
        )
        for message_number in (MSG_DEVICE_SETTINGS, MSG_USER_PROFILE, MSG_ZONES_TARGET, MSG_SENSOR)
        if _messages_of(messages, message_number)
    )
    donor_timestamps = {
        value
        for message in donor_messages
        if (value := _read_uint(message, F_TIMESTAMP)) is not None
    }
    target_timestamps = {
        value
        for message in messages
        if (value := _read_uint(message, F_TIMESTAMP)) is not None
    }
    sport = _one(messages, MSG_SPORT)
    lap = _one(messages, MSG_LAP)
    session = _one(messages, MSG_SESSION)
    source_lap = _one(normalized_source_messages, MSG_LAP)
    source_session = _one(normalized_source_messages, MSG_SESSION)
    decoded_sport = (decoded.get("sport_mesgs") or [{}])[0]
    decoded_lap = (decoded.get("lap_mesgs") or [{}])[0]
    decoded_session = (decoded.get("session_mesgs") or [{}])[0]
    result.update(
        {
            "record_stream_matches_mywhoosh": source_records == target_records,
            "record_count": len(target_records),
            "summary_totals_match_mywhoosh": (
                _summary_signature(lap, True) == _summary_signature(source_lap, True)
                and _summary_signature(session, False)
                == _summary_signature(source_session, False)
            ),
            "all_donor_device_info_payloads_copied": (
                donor_device_payloads == target_device_payloads
            ),
            "device_info_count": len(_messages_of(messages, MSG_DEVICE_INFO)),
            "file_identity_payload_matches_donor": (
                _message_payload_excluding(donor_file_id, {4})
                == _message_payload_excluding(target_file_id, {4})
            ),
            "file_time_created_matches_mywhoosh": (
                _read_uint(target_file_id, 4) == _read_uint(source_session, 2)
            ),
            "donor_timer_event_payloads_copied": (
                len(target_events) == 2
                and _message_payload_excluding(target_events[0], {F_TIMESTAMP})
                == _message_payload_excluding(donor_start_event, {F_TIMESTAMP})
                and _message_payload_excluding(target_events[-1], {F_TIMESTAMP})
                == _message_payload_excluding(donor_stop_event, {F_TIMESTAMP})
            ),
            "copied_profile_payloads_exact": copied_profile_payloads_exact,
            "no_donor_field_253_timestamps": not (donor_timestamps & target_timestamps),
            "target_sport_is_cycling_indoor_cycling": (
                _read_uint(sport, 0) == CYCLING
                and _read_uint(sport, 1) == INDOOR_CYCLING
                and _read_uint(lap, 25) == CYCLING
                and _read_uint(lap, 39) == INDOOR_CYCLING
                and _read_uint(session, 5) == CYCLING
                and _read_uint(session, 6) == INDOOR_CYCLING
                and not sdk_errors
                and decoded_sport.get("sport") == "cycling"
                and decoded_sport.get("sub_sport") == "indoor_cycling"
                and decoded_lap.get("sport") == "cycling"
                and decoded_lap.get("sub_sport") == "indoor_cycling"
                and decoded_session.get("sport") == "cycling"
                and decoded_session.get("sub_sport") == "indoor_cycling"
            ),
            "proprietary_session_values_not_copied": (
                _read_uint(session, 24) is None
                and _read_uint(session, 137) is None
                and _read_sint32(session, 168) is None
            ),
            "donor_gps_and_run_sample_message_types_absent": not (
                {22, 79, 104, 113, 140, 160, 233, 288, 312, 313, 325, 326, 327, 394, 499}
                & {message.global_message for message in messages}
            ),
        }
    )
    return result


def _rle_message_names(messages: Sequence[Message]) -> str:
    output: list[tuple[int, int]] = []
    for message in messages:
        if output and output[-1][0] == message.global_message:
            output[-1] = (output[-1][0], output[-1][1] + 1)
        else:
            output.append((message.global_message, 1))
    return " -> ".join(
        f"{_message_name(number)} x{count}" if count > 1 else _message_name(number)
        for number, count in output
    )


def _session_summary(data: bytes) -> dict[str, Any]:
    decoded, errors, _order, _definitions = _sdk_decode(data)
    session = (decoded.get("session_mesgs") or [{}])[0]
    file_id = (decoded.get("file_id_mesgs") or [{}])[0]
    return {
        "sdk_errors": errors,
        "file_id": file_id,
        "session": {
            key: session.get(key)
            for key in (
                "timestamp",
                "start_time",
                "sport",
                "sub_sport",
                "total_timer_time",
                "total_distance",
                "avg_heart_rate",
                "max_heart_rate",
                "avg_power",
                "max_power",
                "normalized_power",
                "total_training_effect",
                "total_anaerobic_training_effect",
                "training_load_peak",
            )
        },
        "message_counts": {key: len(value) for key, value in decoded.items()},
    }


def _report(
    output_dir: Path,
    mywhoosh: FitInput,
    donor: FitInput,
    donor_messages: Sequence[Message],
    source_messages: Sequence[Message],
    variant_messages: dict[str, Sequence[Message]],
    variant_paths: dict[str, Path],
    validation: dict[str, Any],
) -> Path:
    donor_summary = _session_summary(donor.data)
    source_summary = _session_summary(mywhoosh.data)
    donor_file_id = _one(donor_messages, MSG_FILE_ID)
    donor_devices = _messages_of(donor_messages, MSG_DEVICE_INFO)
    file_id_fields = _field_numbers([donor_file_id])
    device_fields = _field_numbers(donor_devices)
    device_developer = _developer_schemas(donor_devices)
    copied_identity = donor_summary["file_id"]
    validation_rows = []
    for name in VARIANT_NAMES:
        item = validation[name]
        checks = (
            item.get("internal_decode") == "pass",
            item.get("crc") == "pass",
            item.get("garmin_fit_sdk") == "pass",
            item.get("record_stream_matches_mywhoosh"),
            item.get("summary_totals_match_mywhoosh"),
            item.get("all_donor_device_info_payloads_copied"),
            item.get("file_identity_payload_matches_donor"),
            item.get("file_time_created_matches_mywhoosh"),
            item.get("donor_timer_event_payloads_copied"),
            item.get("copied_profile_payloads_exact"),
            item.get("no_donor_field_253_timestamps"),
            item.get("target_sport_is_cycling_indoor_cycling"),
            item.get("proprietary_session_values_not_copied"),
            item.get("donor_gps_and_run_sample_message_types_absent"),
        )
        validation_rows.append(
            f"| `{name}.fit` | {'pass' if all(checks) else 'fail'} | "
            f"{item.get('message_count')} | {item.get('device_info_count')} | "
            f"{item.get('record_count')} |"
        )
    variant_order = "\n".join(
        f"- `{name}.fit`: {_rle_message_names(variant_messages[name])}"
        for name in VARIANT_NAMES
    )
    paths = "\n".join(f"- `{name}.fit`: `{path}`" for name, path in variant_paths.items())
    text = f"""# Garmin Donor Spoof Report

## Scope And Limitation

These variants copy Garmin-native identity, creator-device metadata, schemas, and selected
profile/training context from the supplied donor while retaining the MyWhoosh ride stream.
FIT metadata alone cannot prove Garmin server/device trust or force Firstbeat calculations;
Training Effect, Acute Load, Recovery Time, Training Status, and Load Focus remain upload-
and-watch-sync test results, not guaranteed outcomes.

## Donor Summary

- Source: `{donor.container or donor.source}` (`{donor.source}`)
- SHA-256: `{_sha256(donor.data)}`
- Garmin identity: manufacturer `{copied_identity.get('manufacturer')}`, product
  `{copied_identity.get('product')}` / `{copied_identity.get('garmin_product')}`, serial
  `{copied_identity.get('serial_number')}`
- Sport/sub-sport: `{donor_summary['session'].get('sport')}` /
  `{donor_summary['session'].get('sub_sport')}` (not copied)
- Timer/distance: `{donor_summary['session'].get('total_timer_time')}` s /
  `{donor_summary['session'].get('total_distance')}` m (not copied)
- Device-info messages: `{len(donor_devices)}`
- Session TE / anaerobic TE / load: `{donor_summary['session'].get('total_training_effect')}` /
  `{donor_summary['session'].get('total_anaerobic_training_effect')}` /
  `{donor_summary['session'].get('training_load_peak')}` (not copied)

## MyWhoosh Summary

- Source: `{mywhoosh.source}`
- SHA-256: `{_sha256(mywhoosh.data)}`
- Sport/sub-sport before conversion: `{source_summary['session'].get('sport')}` /
  `{source_summary['session'].get('sub_sport')}`
- Timer/distance: `{source_summary['session'].get('total_timer_time')}` s /
  `{source_summary['session'].get('total_distance')}` m
- Avg/max HR: `{source_summary['session'].get('avg_heart_rate')}` /
  `{source_summary['session'].get('max_heart_rate')}`
- Avg/max power: `{source_summary['session'].get('avg_power')}` /
  `{source_summary['session'].get('max_power')}`
- Preserved record messages: `{len(_messages_of(source_messages, MSG_RECORD))}`
- Output sport/sub-sport in every variant: `cycling` / `indoor_cycling`

## Variants

- `conservative_garmin_device_spoof.fit`: donor file identity, file creator, all donor
  device-info payloads, donor timer start/stop-all event payloads, standard summary order.
- `garmin_ordered_spoof.fit`: conservative payload with Garmin-donor-style top-level
  ordering, including summaries before the event/device/profile/record stream.
- `full_training_spoof.fit`: ordered variant plus donor user profile, zones target,
  donor-templated lap/session/activity schemas, and HR/power time-in-zone recalculated from
  MyWhoosh samples.
- `donor_max_spoof.fit`: full-training variant plus donor device settings, donor sensor
  association metadata (message `147`), and donor record schema populated only from
  MyWhoosh samples.

Output files:

{paths}

## Exact Copied Fields

- `file_id`: all donor fields copied except `time_created` field `4`, which is replaced
  with the MyWhoosh start time. Fields: {_field_labels(MSG_FILE_ID, file_id_fields)}.
- `file_creator`: the complete donor message is copied unchanged.
- `device_info`: all `{len(donor_devices)}` donor messages and every non-timestamp raw
  field are copied byte-for-byte. Field `253` is remapped to the MyWhoosh start/end.
  Fields: {_field_labels(MSG_DEVICE_INFO, device_fields)}.
- `device_info` developer fields: `{device_developer or 'none present in this donor'}`.
- `event`: donor timer/start and final timer/stop_all messages are copied; only field
  `253` is remapped.
- `user_profile`, `zones_target`: copied unchanged in `full_training_spoof` and
  `donor_max_spoof`.
- `device_settings`, unknown sensor-association message `147`: copied unchanged only in
  `donor_max_spoof`.
- Donor lap/session/activity and record *schemas* are used in the full/max variants.
  Values come from MyWhoosh or are invalid/unset when no source-derived value exists.
- FIT header format/profile version comes from the donor; every output header/file CRC is
  newly computed.

## Fields Not Copied

- Donor `running` / `generic` sport fields and the donor sport name.
- Donor record stream, GPS metadata, positions, HR, power, cadence, speed, distance, and
  samples.
- Donor timestamps, lap/split structure, split summaries, run dynamics, totals, pauses,
  and run-specific training settings.
- Donor lap/session/activity values, including TE field `24`, anaerobic TE field `137`,
  training-load field `168`, workout feel/RPE, and donor developer-field values.
- Unknown donor messages that may contain GPS, samples, totals, timestamps, or run-specific
  state: `22`, `79`, `104`, `113`, `140`, `141`, `233`, `288`, `312`, `313`, `325`,
  `326`, `327`, `394`, and `499`.

## Message Ordering

{variant_order}

## Validation Results

| Variant | Combined result | Messages | Device info | MyWhoosh records |
|---|---|---:|---:|---:|
{chr(10).join(validation_rows)}

Each combined result requires: internal parse, FIT CRC, Garmin FIT SDK decode, exact
MyWhoosh record values, MyWhoosh-derived summary totals, all donor device-info payloads,
exact donor file identity/event/profile payloads where present, no donor field-253
timestamps, cycling/indoor-cycling sport, no donor TE/load values, and no donor
GPS/run-sample message types.
Machine-readable detail: `validation_results.json`.

## Garmin Upload Test Steps

1. Record the current FR265 values for Training Effect, Acute Load, Recovery Time,
   Training Status, and Load Focus.
2. Upload only `conservative_garmin_device_spoof.fit` to Garmin Connect.
3. Sync the FR265, wait for processing, then sync it a second time.
4. Check Garmin Connect and the FR265 for TE, Acute Load, Recovery Time, Training Status,
   and Load Focus changes. Record the result.
5. If the variant fails or has no desired effect, delete it from Garmin Connect and sync
   the FR265 again before testing the next variant.
6. Repeat one at a time in this order: `garmin_ordered_spoof.fit`,
   `full_training_spoof.fit`, then `donor_max_spoof.fit`.
7. Do not leave multiple variants of the same ride uploaded simultaneously; duplicate
   detection and accumulated load changes would invalidate the comparison.
"""
    report_path = output_dir / "garmin_donor_spoof_report.md"
    report_path.write_text(text.rstrip() + "\n", encoding="utf-8")
    return report_path


def build_spoof_variants(
    mywhoosh_path: Path,
    donor_path: Path,
    output_dir: Path,
) -> tuple[dict[str, Path], Path, dict[str, Any]]:
    mywhoosh = _load_input(mywhoosh_path)
    donor = _load_input(donor_path)
    normalized_data = convert_fit_bytes(mywhoosh.data)
    _original_source_header, original_source_messages = _parse_fit(mywhoosh.data)
    _source_header, source_messages = _parse_fit(normalized_data)
    source_messages, metrics = recalculate_core(source_messages)
    donor_header, donor_messages = _parse_fit(donor.data)

    donor_session = _one(donor_messages, MSG_SESSION)
    donor_start = _read_uint(donor_session, 2)
    if donor_start is None:
        raise ValueError("donor session has no start_time")

    donor_file_id = _replace_uint_preserving(
        _one(donor_messages, MSG_FILE_ID), 4, metrics.start
    )
    donor_creator = _messages_of(donor_messages, MSG_FILE_CREATOR)
    start_event, stop_event = _donor_event_pair(
        donor_messages, metrics.start, metrics.end
    )
    start_devices, end_devices = _donor_devices(
        donor_messages, donor_start, metrics.start, metrics.end
    )

    records = _messages_of(source_messages, MSG_RECORD)
    sport = _target_sport(_one(source_messages, MSG_SPORT))
    source_lap = _target_lap(_one(source_messages, MSG_LAP))
    source_session = _target_session(_one(source_messages, MSG_SESSION))
    source_activity = _one(source_messages, MSG_ACTIVITY)

    donor_lap_template = _most_common_template(donor_messages, MSG_LAP)
    donor_record_template = _most_common_template(donor_messages, MSG_RECORD)
    donor_session_template = _one(donor_messages, MSG_SESSION)
    donor_activity_template = _one(donor_messages, MSG_ACTIVITY)
    donor_lap = _target_lap(
        _reschema(
            donor_lap_template,
            source_lap,
            allowed_source_fields=PUBLIC_LAP_FIELDS,
            append_source_fields=True,
        )
    )
    donor_session_summary = _target_session(
        _reschema(
            donor_session_template,
            source_session,
            allowed_source_fields=PUBLIC_SESSION_FIELDS,
            append_source_fields=True,
        )
    )
    donor_activity = _reschema(
        donor_activity_template, source_activity, append_source_fields=True
    )
    donor_records = [
        _reschema(donor_record_template, record, append_source_fields=True)
        for record in records
    ]
    lap_tiz, session_tiz = _time_in_zone_messages(donor_messages, records, metrics)

    user_profile = _messages_of(donor_messages, MSG_USER_PROFILE)
    zones_target = _messages_of(donor_messages, MSG_ZONES_TARGET)
    device_settings = _messages_of(donor_messages, MSG_DEVICE_SETTINGS)
    sensor_metadata = _messages_of(donor_messages, MSG_SENSOR)

    conservative = (
        [donor_file_id]
        + donor_creator
        + [start_event]
        + start_devices
        + [sport]
        + records
        + [stop_event]
        + end_devices
        + [source_lap, source_session, source_activity]
    )
    ordered = (
        [donor_file_id]
        + donor_creator
        + [source_activity, source_session, source_lap, start_event]
        + start_devices
        + [sport]
        + records
        + [stop_event]
        + end_devices
    )
    full_training = (
        [donor_file_id]
        + donor_creator
        + [donor_activity, donor_session_summary]
        + ([session_tiz] if session_tiz is not None else [])
        + [donor_lap]
        + ([lap_tiz] if lap_tiz is not None else [])
        + [start_event]
        + start_devices
        + user_profile
        + [sport]
        + zones_target
        + records
        + [stop_event]
        + end_devices
    )
    donor_max = (
        [donor_file_id]
        + donor_creator
        + [donor_activity, donor_session_summary]
        + ([session_tiz] if session_tiz is not None else [])
        + [donor_lap]
        + ([lap_tiz] if lap_tiz is not None else [])
        + [start_event]
        + start_devices
        + device_settings
        + user_profile
        + sensor_metadata
        + [sport]
        + zones_target
        + donor_records
        + [stop_event]
        + end_devices
    )
    variant_messages = {
        "conservative_garmin_device_spoof": conservative,
        "garmin_ordered_spoof": ordered,
        "full_training_spoof": full_training,
        "donor_max_spoof": donor_max,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    variant_paths: dict[str, Path] = {}
    validation: dict[str, Any] = {}
    for name, messages in variant_messages.items():
        data = _encode_fit(donor_header, messages)
        _parse_fit(data)
        path = output_dir / f"{name}.fit"
        path.write_bytes(data)
        variant_paths[name] = path
        validation[name] = _validate_variant(
            data, original_source_messages, source_messages, donor_messages
        )

    validation_path = output_dir / "validation_results.json"
    validation_path.write_text(
        json.dumps(_json_safe(validation), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report_path = _report(
        output_dir,
        mywhoosh,
        donor,
        donor_messages,
        source_messages,
        variant_messages,
        variant_paths,
        validation,
    )
    manifest = {
        "inputs": {
            "mywhoosh": {
                "path": str(mywhoosh_path),
                "sha256": _sha256(mywhoosh.data),
            },
            "donor": {
                "path": str(donor_path),
                "sha256": _sha256(donor.data),
            },
        },
        "outputs": {
            name: {"path": str(path), "sha256": _sha256(path.read_bytes())}
            for name, path in variant_paths.items()
        },
        "report": str(report_path),
        "validation": str(validation_path),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return variant_paths, report_path, validation


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build Garmin-donor metadata spoof variants from a MyWhoosh FIT."
    )
    parser.add_argument("--mywhoosh", required=True, type=Path)
    parser.add_argument("--donor", required=True, type=Path, help="FIT or ZIP with one FIT")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs/garmin_donor_spoof"),
    )
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    paths, report, validation = build_spoof_variants(
        arguments.mywhoosh, arguments.donor, arguments.output_dir
    )
    all_passed = True
    for name, path in paths.items():
        combined = all(
            (
                validation[name].get("internal_decode") == "pass",
                validation[name].get("crc") == "pass",
                validation[name].get("garmin_fit_sdk") == "pass",
                validation[name].get("record_stream_matches_mywhoosh"),
                validation[name].get("summary_totals_match_mywhoosh"),
                validation[name].get("all_donor_device_info_payloads_copied"),
                validation[name].get("file_identity_payload_matches_donor"),
                validation[name].get("file_time_created_matches_mywhoosh"),
                validation[name].get("donor_timer_event_payloads_copied"),
                validation[name].get("copied_profile_payloads_exact"),
                validation[name].get("no_donor_field_253_timestamps"),
                validation[name].get("target_sport_is_cycling_indoor_cycling"),
                validation[name].get("proprietary_session_values_not_copied"),
                validation[name].get("donor_gps_and_run_sample_message_types_absent"),
            )
        )
        all_passed = all_passed and combined
        print(f"{name}: {'pass' if combined else 'fail'} - {path}")
    print(f"report: {report}")
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
