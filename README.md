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
python fix_fit.py --mimic-garmin --inject-metrics --ftp 250 ride.fit
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
- With `--mimic-garmin`, changes MyWhoosh creator metadata to Garmin Edge 530, and patches `file_creator.software_version` to a plausible Edge firmware value.
- With `--inject-metrics`, computes Normalized Power (Coggan), Intensity Factor (NP / FTP), and Training Stress Score (Coggan TSS formula) from the record stream and writes them into the session. Garmin does not compute these server-side, so they have to be in the file. Only fields computable from established formulas are written; aerobic and anaerobic Training Effect are Firstbeat-proprietary and are deliberately not faked.
- Leaves native Garmin, Zwift, and unrelated files alone.

The drag-and-drop launcher enables `--mimic-garmin --inject-metrics` by default.

## FTP

`--inject-metrics` needs your FTP in watts. It is not estimated from the ride (a single ride underestimates real FTP and inflates TSS).

The first time you drag a file onto `Fix MyWhoosh FIT.bat`, a dialog asks for your FTP. The value is saved in `fit-fix.cfg` next to the script and reused on every future run. To change it, edit or delete `fit-fix.cfg`, or pass `--ftp` on the CLI.

Look up your FTP in Garmin Connect under User Settings -> Power Zones.

## Garmin Metrics

Garmin Connect does not compute these fields server-side: the device or app has to write them into the FIT session message. MyWhoosh exports without any of them, so even a correctly timestamped file does not feed Training Status, Acute Load, or Recovery Time.

`--inject-metrics` writes only the three fields whose algorithms are public and identical to what a Garmin Edge would compute given the same record stream:

- Normalized Power (Coggan: 30-second rolling mean, raised to the 4th power, averaged, 4th root)
- Intensity Factor (NP / FTP)
- Training Stress Score (`duration_hours * IF^2 * 100`)

Aerobic and anaerobic Training Effect are Firstbeat-proprietary and are not written. If Garmin's recovery-time pipeline strictly requires those, this tool will not unlock it; you would have to record on a real Garmin device.

Presenting a manual import as a Garmin device is an unsupported workaround. Garmin Connect may still exclude the activity from some metrics, or change this behavior in the future.

## Development

```text
python -m pip install .[dev]
python -m pytest
python -m ruff check .
python -m mypy --check-untyped-defs fix_fit.py tests
```

No runtime dependencies. Python 3.10+.

MIT. Created by not_kler.
