"""Tests for the production MyWhoosh-to-Garmin converter."""

from pathlib import Path

import pytest

from fix_fit import MSG_DEVICE_INFO, MSG_RECORD, MSG_SPORT, TYPE_ENUM, Message, _pack_field, _parse_fit
from garmin_converter import (
    _messages_of,
    _payloads,
    _target_sport,
    convert,
)


def test_target_sport_is_indoor_cycling() -> None:
    source = Message(
        MSG_SPORT,
        "<",
        (
            _pack_field("<", 0, TYPE_ENUM, 2),
            _pack_field("<", 1, TYPE_ENUM, 58),
        ),
    )
    converted = _target_sport(source)
    assert converted.first(0).data == b"\x02"
    assert converted.first(1).data == b"\x06"


@pytest.mark.skipif(
    not (Path.home() / "Downloads" / "MyWhoosh_Limmat_Loop.fit").is_file()
    or not Path("garmin-template.fit").is_file(),
    reason="local conversion fixtures are unavailable",
)
def test_real_conversion_preserves_records_and_device_metadata() -> None:
    source = Path.home() / "Downloads" / "MyWhoosh_Limmat_Loop.fit"
    template = Path("garmin-template.fit")
    output = convert(source, template)

    _source_header, source_messages = _parse_fit(output)
    _template_header, template_messages = _parse_fit(template.read_bytes())
    assert _messages_of(source_messages, MSG_RECORD)
    assert _payloads(_messages_of(source_messages, MSG_DEVICE_INFO), {253}) == _payloads(
        _messages_of(template_messages, MSG_DEVICE_INFO), {253}
    )
