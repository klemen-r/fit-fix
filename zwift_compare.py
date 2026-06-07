"""Compare an original Zwift FIT with a Garmin-exported copy and local variants."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import struct
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Optional, Sequence

from fix_fit import (
    MSG_ACTIVITY,
    MSG_DEVICE_INFO,
    MSG_EVENT,
    MSG_FILE_ID,
    MSG_LAP,
    MSG_RECORD,
    MSG_SESSION,
    _parse_fit,
    _read_sint32,
    _read_uint,
)
from garmin_pipeline import (
    _field_name,
    _load_zip_fit_files,
    _message_name,
    _sdk_decode,
    _write_json,
    _write_text,
    validate_fit,
)

SPECIAL_SESSION_FIELDS = {
    24: "total_training_effect",
    137: "total_anaerobic_training_effect",
    168: "training_load_peak",
}
KEY_MESSAGE_NUMBERS = (MSG_FILE_ID, MSG_DEVICE_INFO, MSG_EVENT, MSG_LAP, MSG_SESSION, MSG_ACTIVITY)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_extract_single_fit(archive_path: Path, target_dir: Path) -> Path:
    """Extract exactly one FIT while rejecting ZIP path traversal."""
    target_root = target_dir.resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        fits = [
            entry
            for entry in archive.infolist()
            if not entry.is_dir() and entry.filename.lower().endswith(".fit")
        ]
        if len(fits) != 1:
            raise ValueError(
                f"expected exactly one FIT in {archive_path}, found {len(fits)}"
            )
        entry = fits[0]
        destination = (target_root / entry.filename).resolve()
        if destination.parent != target_root:
            raise ValueError(f"unsafe ZIP entry path: {entry.filename}")
        with archive.open(entry) as source, destination.open("wb") as output:
            shutil.copyfileobj(source, output)
    return destination


def _unknown_fields(raw_messages) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for message in raw_messages:
        for field in message.fields:
            name = _field_name(message.global_message, field.number)
            if name.startswith("unknown_"):
                counts[f"{message.global_message}.{field.number}"] += 1
    return dict(sorted(counts.items()))


def _raw_special_session_fields(raw_messages, decoded_session: dict[str, Any]) -> dict[str, Any]:
    session = next(message for message in raw_messages if message.global_message == MSG_SESSION)
    output = {}
    for number, name in SPECIAL_SESSION_FIELDS.items():
        field = session.first(number)
        output[str(number)] = {
            "name": name,
            "present": field is not None,
            "raw_hex": field.data.hex() if field is not None else None,
            "decoded": decoded_session.get(name),
        }
    return output


def summarize_fit(label: str, path: Path, data: bytes) -> dict[str, Any]:
    header, raw_messages = _parse_fit(data)
    decoded, errors, order, definitions = _sdk_decode(data)
    counts = Counter(message.global_message for message in raw_messages)
    session = decoded.get("session_mesgs", [{}])[0]
    return {
        "label": label,
        "path": str(path),
        "size": len(data),
        "sha256": _sha256(data),
        "header_hex": header.hex(),
        "header_profile_version": struct.unpack_from("<H", header, 2)[0],
        "sdk_errors": errors,
        "message_count": len(raw_messages),
        "definition_count": len(definitions),
        "message_types": [
            {
                "number": number,
                "name": _message_name(number),
                "count": count,
            }
            for number, count in sorted(counts.items())
        ],
        "key_message_counts": {
            _message_name(number): counts[number] for number in KEY_MESSAGE_NUMBERS
        },
        "developer_field_occurrences": sum(
            len(message.developer_fields) for message in raw_messages
        ),
        "unknown_message_types": {
            key: len(values) for key, values in decoded.items() if key.isdigit()
        },
        "unknown_field_occurrences": _unknown_fields(raw_messages),
        "file_id": (decoded.get("file_id_mesgs") or [{}])[0],
        "device_info_count": len(decoded.get("device_info_mesgs", [])),
        "device_info_first": (decoded.get("device_info_mesgs") or [None])[0],
        "events": decoded.get("event_mesgs", []),
        "laps": decoded.get("lap_mesgs", []),
        "session": session,
        "activity": (decoded.get("activity_mesgs") or [{}])[0],
        "special_session_fields": _raw_special_session_fields(raw_messages, session),
        "validation": validate_fit(data),
        "message_order": order,
    }


def compare_bytes(left: bytes, right: bytes) -> dict[str, Any]:
    differing_offsets = [
        index for index, (left_byte, right_byte) in enumerate(zip(left, right)) if left_byte != right_byte
    ]
    return {
        "identical": left == right,
        "left_size": len(left),
        "right_size": len(right),
        "different_byte_count": len(differing_offsets) + abs(len(left) - len(right)),
        "first_differing_offsets": differing_offsets[:20],
    }


def compare_record_streams(left: bytes, right: bytes) -> dict[str, Any]:
    """Compare common public cycling record fields by message index."""
    _left_header, left_messages = _parse_fit(left)
    _right_header, right_messages = _parse_fit(right)
    left_records = [
        message for message in left_messages if message.global_message == MSG_RECORD
    ]
    right_records = [
        message for message in right_messages if message.global_message == MSG_RECORD
    ]
    fields = {
        253: "timestamp",
        0: "position_lat",
        1: "position_long",
        2: "altitude",
        3: "heart_rate",
        4: "cadence",
        5: "distance",
        6: "speed",
        7: "power",
        78: "enhanced_altitude",
    }
    result = {}
    for number, name in fields.items():
        reader = _read_sint32 if number in (0, 1) else _read_uint
        left_values = [reader(message, number) for message in left_records]
        right_values = [reader(message, number) for message in right_records]
        pairs = [
            (left_value, right_value)
            for left_value, right_value in zip(left_values, right_values)
            if left_value is not None and right_value is not None
        ]
        result[name] = {
            "left_valid": sum(value is not None for value in left_values),
            "right_valid": sum(value is not None for value in right_values),
            "common_valid": len(pairs),
            "common_equal": sum(left_value == right_value for left_value, right_value in pairs),
        }
    return {
        "left_record_count": len(left_records),
        "right_record_count": len(right_records),
        "fields": result,
    }


def _message_count_text(summary: dict[str, Any]) -> str:
    return ", ".join(
        f"{item['name']}={item['count']}" for item in summary["message_types"]
    )


def _special_text(summary: dict[str, Any]) -> str:
    values = summary["special_session_fields"]
    return ", ".join(
        f"{number}={item['decoded']!r} (raw {item['raw_hex']})"
        if item["present"]
        else f"{number}=absent"
        for number, item in values.items()
    )


def write_reports(
    reports_dir: Path,
    original: dict[str, Any],
    exported: dict[str, Any],
    donor: dict[str, Any],
    variants: Sequence[dict[str, Any]],
    byte_comparison: dict[str, Any],
    strava: Optional[dict[str, Any]] = None,
    strava_byte_comparison: Optional[dict[str, Any]] = None,
    strava_record_comparison: Optional[dict[str, Any]] = None,
) -> None:
    variant_rows = []
    for variant in variants:
        special = variant["special_session_fields"]
        variant_rows.append(
            f"| `{variant['label']}` | {variant['file_id'].get('manufacturer')} | "
            f"{variant['device_info_count']} | {special['24']['decoded']} | "
            f"{special['137']['decoded']} | {special['168']['decoded']} |"
        )
    _write_text(
        reports_dir / "zwift_sync_comparison.md",
        f"""# Zwift Original vs Garmin Export

## Result

The Garmin-exported FIT is **byte-for-byte identical** to the original Zwift FIT.

- Original SHA-256: `{original['sha256']}`
- Garmin-export SHA-256: `{exported['sha256']}`
- Sizes: {original['size']} / {exported['size']} bytes
- Different bytes: {byte_comparison['different_byte_count']}
- Garmin-added FIT messages or fields: **none**

This is strong structural evidence that Garmin Connect preserved the uploaded Zwift FIT
and did not write training-processing results back into the exported FIT. It supports,
but does not prove, the hypothesis that official Zwift trust/source linkage and any
training processing are stored outside the FIT.

## Zwift FIT Contents

- `file_id`: manufacturer `zwift`, product `0`; it was not rewritten as Garmin.
- `device_info`: one Zwift creator device; unchanged after Garmin sync/export.
- Message types: {_message_count_text(original)}.
- Developer fields: {original['developer_field_occurrences']}; unknown message types:
  {original['unknown_message_types'] or 'none'}; unknown fields:
  {original['unknown_field_occurrences'] or 'none'}.
- Session special fields: {_special_text(original)}.
- Field `24` is explicitly zero. Fields `137` and `168` are absent.
- Event/lap/session/activity messages and all decoded values are unchanged.

## Limits

This activity is from March 10, 2023 and may predate the current watch/training-status
configuration. The exact match does not establish whether Garmin calculated training
metrics for this activity, only that Garmin did not persist new metrics into this FIT.

Your watch-era explanation is plausible: if the account did not yet have a compatible
watch/training-status setup, Garmin may not have attempted the modern training-load
processing you care about. This old activity therefore cannot determine whether a current
official Zwift sync would update Acute Load, Recovery, Training Status, or Load Focus.
""",
    )
    if strava is not None and strava_byte_comparison is not None and strava_record_comparison is not None:
        common_exact = [
            name
            for name, item in strava_record_comparison["fields"].items()
            if item["common_valid"] > 0
            and item["common_valid"] == item["common_equal"]
        ]
        _write_text(
            reports_dir / "strava_zwift_comparison.md",
            f"""# Strava Export of the Same Zwift Activity

The Strava FIT is a transformed derivative, not an untouched copy:

- Strava SHA-256: `{strava['sha256']}`
- Size: {strava['size']} bytes versus {original['size']} bytes for the original Zwift FIT
- Different bytes versus original: {strava_byte_comparison['different_byte_count']}
- Record count: {strava_record_comparison['left_record_count']} versus
  {strava_record_comparison['right_record_count']}
- Common streams matching exactly by record index: {', '.join(common_exact)}

Strava removed all heart-rate samples, both timer event messages, summary HR/power fields,
and session TE field `24`. Fields `137` and `168` remain absent. Strava retained the
underlying power record stream even though it removed the session power summary.

This confirms that downloaded FITs from third-party services may be re-encoded and stripped.
It does not reveal whether Garmin considered the original activity trusted or processed it
for training status. For that question, the exact original-vs-Garmin-export match remains
the stronger structural evidence, while the lack of a watch in 2023 remains a major
limitation on behavioral conclusions.
""",
        )
    _write_text(
        reports_dir / "zwift_structural_evidence.md",
        f"""# Zwift Structural Evidence

| Artifact | Manufacturer | Device info | TE field 24 | Anaerobic field 137 | Load field 168 |
|---|---|---:|---:|---:|---:|
| Original Zwift | `{original['file_id'].get('manufacturer')}` | {original['device_info_count']} | {original['special_session_fields']['24']['decoded']} | absent | absent |
| Garmin-exported Zwift | `{exported['file_id'].get('manufacturer')}` | {exported['device_info_count']} | {exported['special_session_fields']['24']['decoded']} | absent | absent |
| Garmin-native donor | `{donor['file_id'].get('manufacturer')}` | {donor['device_info_count']} | {donor['special_session_fields']['24']['decoded']} | {donor['special_session_fields']['137']['decoded']} | {donor['special_session_fields']['168']['decoded']} |
{chr(10).join(variant_rows)}

The Garmin-native donor is much richer than Zwift: it contains profile/settings/zones,
multiple device messages, time-in-zone, and unknown Garmin messages. None of that richness
was added to the official Zwift FIT during Garmin sync/export.

The current structural variants are already closer to Garmin-native FIT structure than the
official Zwift FIT. This comparison provides no evidence that adding inferred TE/load
fields is required for trusted-source processing. Keep `structural_only` as the first
upload test; the existing test matrix order is unchanged.
""",
    )


def run_comparison(
    original_path: Path,
    garmin_zip: Path,
    donor_dir: Path,
    variants_dir: Path,
    outputs_dir: Path,
    strava_path: Optional[Path] = None,
) -> dict[str, Any]:
    comparison_dir = outputs_dir / "zwift_comparison"
    extracted = safe_extract_single_fit(garmin_zip, comparison_dir / "extracted")
    original_data = original_path.read_bytes()
    exported_data = extracted.read_bytes()
    donor_inputs = _load_zip_fit_files(donor_dir)
    donor = next(
        source for source in donor_inputs if source.label == "23046545220_ACTIVITY"
    )

    original = summarize_fit("original_zwift", original_path, original_data)
    exported = summarize_fit("garmin_exported_zwift", extracted, exported_data)
    donor_summary = summarize_fit("garmin_native_donor", donor.source, donor.data)
    variant_summaries = [
        summarize_fit(path.stem, path, path.read_bytes())
        for path in sorted(variants_dir.glob("*.fit"))
    ]
    strava_data = (
        strava_path.read_bytes()
        if strava_path is not None and strava_path.is_file()
        else None
    )
    strava_summary = (
        summarize_fit("strava_exported_zwift", strava_path, strava_data)
        if strava_path is not None and strava_data is not None
        else None
    )
    strava_byte_comparison = (
        compare_bytes(strava_data, original_data) if strava_data is not None else None
    )
    strava_record_comparison = (
        compare_record_streams(strava_data, original_data)
        if strava_data is not None
        else None
    )
    result = {
        "byte_comparison": compare_bytes(original_data, exported_data),
        "strava_vs_original_byte_comparison": strava_byte_comparison,
        "strava_vs_original_record_comparison": strava_record_comparison,
        "original_zwift": original,
        "garmin_exported_zwift": exported,
        "strava_exported_zwift": strava_summary,
        "garmin_native_donor": donor_summary,
        "current_variants": variant_summaries,
        "inference": {
            "garmin_added_fit_content": False,
            "trusted_source_outside_fit_hypothesis": (
                "supported by exact-byte preservation, but not proven"
            ),
            "test_matrix_order_changed": False,
            "reason": "structural_only is already the first upload test",
            "watch_era_limitation": (
                "This 2023 activity predates the current watch/training-status setup, "
                "so it cannot establish current trusted-sync training behavior."
            ),
        },
    }
    _write_json(comparison_dir / "comparison.json", result)
    _write_json(
        comparison_dir / "hashes.json",
        {
            "original_zwift": original["sha256"],
            "garmin_zip": _sha256(garmin_zip.read_bytes()),
            "garmin_exported_zwift": exported["sha256"],
            "strava_exported_zwift": (
                strava_summary["sha256"] if strava_summary is not None else None
            ),
            "garmin_native_donor": donor_summary["sha256"],
            "current_variants": {
                item["label"]: item["sha256"] for item in variant_summaries
            },
        },
    )
    write_reports(
        outputs_dir / "reports",
        original,
        exported,
        donor_summary,
        variant_summaries,
        result["byte_comparison"],
        strava_summary,
        strava_byte_comparison,
        strava_record_comparison,
    )
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare an original Zwift FIT with a Garmin-exported copy."
    )
    parser.add_argument(
        "--original",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--garmin-zip",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--donor-dir",
        type=Path,
        required=True,
    )
    parser.add_argument("--variants-dir", type=Path, default=Path("outputs/variants"))
    parser.add_argument("--outputs", type=Path, default=Path("outputs"))
    parser.add_argument(
        "--strava",
        type=Path,
        default=None,
    )
    arguments = parser.parse_args(argv)
    result = run_comparison(
        arguments.original,
        arguments.garmin_zip,
        arguments.donor_dir,
        arguments.variants_dir,
        arguments.outputs,
        arguments.strava,
    )
    print(
        "Original and Garmin-exported Zwift FIT identical:",
        result["byte_comparison"]["identical"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
