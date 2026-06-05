# fit-fix

Small drag-and-drop fixer for MyWhoosh FIT exports.

It repairs broken activity timestamps and can present virtual MyWhoosh rides as
activities recorded by a Garmin Edge 530. Heart rate, power, cadence, distance,
GPS, calories, and activity records are preserved.

## Use

Drag one or more `.fit` files onto `Fix MyWhoosh FIT.bat`.

Fixed copies are created next to the originals:

```text
ride.fit
ride_fixed.fit
```

Upload the fixed copy to Garmin Connect. Delete an earlier import of the same
ride first, since Garmin rejects duplicate activities.

The launcher only accepts activities marked as `cycling / virtual_activity`.
Outdoor rides, runs, and ambiguous files are refused.

## CLI

```text
python fix_fit.py ride.fit
python fix_fit.py --mimic-garmin ride.fit
python fix_fit.py --mimic-zwift ride.fit
python fix_fit.py --in-place ride.fit
python fix_fit.py -o out.fit ride.fit
```

Install the command:

```text
pip install .
fit-fix --mimic-garmin ride.fit
```

## What It Changes

- Repairs the MyWhoosh Unix/FIT epoch timestamp bug.
- Corrects session and activity end timestamps.
- Recomputes FIT checksums.
- With `--mimic-garmin`, changes MyWhoosh creator metadata to Garmin Edge 530.
- Leaves native Garmin, Zwift, and unrelated files alone.

The drag-and-drop launcher enables `--mimic-garmin` by default.

## Garmin Metrics

The files contain the raw data Garmin can use for cycling metrics, including
complete heart-rate and power records. Garmin calculates Training Load,
Training Effect, VO2 Max, and Recovery Time after import.

Presenting a manual import as a Garmin device is an unsupported workaround.
Garmin may still exclude the activity from some metrics or change this behavior.

## Development

```text
python -m pip install .[dev]
python -m pytest
python -m ruff check .
python -m mypy --check-untyped-defs fix_fit.py tests
```

No runtime dependencies. Python 3.10+.

MIT. Created by not_kler.
