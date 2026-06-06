# fit-fix

A focused MyWhoosh `.fit` normalizer by **not_kler**.

MyWhoosh exports contain metadata and summary defects that Garmin Connect web
mostly tolerates but Garmin watches may not. Relabeling the device alone does
not repair those defects.

## What it fixes

- identifies the activity as Garmin Edge 1050
- replaces the malformed event stream with `timer/start` and `timer/stop_all`
- rebuilds dense lap and session summaries from the existing activity data
- adds standard descent, total-work, normalized-power, end-position, and
  timer-trigger fields
- keeps record timestamps unchanged while repairing MyWhoosh's broken summary
  times: lap end, session end, activity end, event times, and the Unix-epoch-
  shifted local timestamp
- adds Garmin-style start/end creator-device metadata and sport metadata
- removes MyWhoosh-only developer metadata
- removes redundant enhanced fields produced by some online converters
- canonicalizes retained messages to little-endian definitions
- preserves the original record streams: timestamps, HR, cadence, power,
  distance, speed, altitude, and position
- rejects unrelated Garmin and multi-session files instead of silently
  discarding their structure

Original files are never overwritten.

## Windows

Drag one or more `.fit` files onto `Convert MyWhoosh to Edge 1050.bat`, or
double-click the launcher and select files.

The normalized file appears beside the original:

```text
ride_garmin.fit
```

## Command line

Python 3.10 or newer is required. There are no runtime dependencies.

```text
python fix_fit.py ride.fit
```

## Limits

This repairs the FIT structure that a watch reads. Garmin Connect may calculate
proprietary metrics such as Training Effect, Acute Load, and Recovery Time on
its servers; those values cannot be recreated accurately from the activity file
alone.

## Development

```text
python -m pip install .[dev]
python -m pytest -q
python -m ruff check .
```

MIT licensed.
