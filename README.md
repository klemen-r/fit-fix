# fit-fix

Analyze, repair, and re-shape MyWhoosh `.fit` cycling exports so Garmin Connect is more likely to treat them as full training-load activities (Training Effect, Acute Load, Recovery Time, Training Status).

This is an experimental converter and test framework. It does not guarantee Garmin acceptance: the pipeline appears to gate on a certified-source allowlist (Garmin devices, Zwift, Rouvy, TrainerRoad, Tacx Training) and file metadata alone may not be sufficient.

## What it does

- **Repairs the MyWhoosh Unix/FIT epoch bug** that places `activity.local_timestamp` ~20 years in the future and excludes the activity from every rolling Garmin metric.
- **Patches device identity** (`file_id` and creator `device_info`) to a chosen profile so the file structurally resembles a known trusted source.
- **Computes Coggan-deterministic session metrics** (Normalized Power, Intensity Factor, Training Stress Score) only when an FTP is provided. Training Effect is Firstbeat-proprietary and is **not** invented by default.
- **Preserves every record** byte-for-byte: HR, power, cadence, distance, speed, altitude, position, calories.

## Commands

```text
fit-fix analyze RIDE.fit [--json out.json]
fit-fix patch RIDE.fit --profile garmin-edge [--inject-metrics --ftp 250]
fit-fix compare RIDE.fit GARMIN_REF.fit [--md report.md]
fit-fix matrix RIDE.fit --out-dir variants/
```

Legacy invocation (no subcommand) is treated as `patch`, so `fit-fix RIDE.fit --profile zwift` still works and the drag-and-drop launcher does not need to change.

### analyze

Reads a FIT file and emits a structured report: file_id, file_creator, device_info, sessions, laps, activity, record-stream presence/counts, suspicious timestamps, source heuristic, and a list of warnings.

```text
fit-fix analyze ride.fit
fit-fix analyze ./rides/*.fit --json report.json
```

Use this to confirm a MyWhoosh file does have the local_timestamp bug, and to compare what fields Garmin / Zwift / Rouvy files actually contain.

### patch

Repairs timestamps and (optionally) applies a profile and metric injection.

Profiles available:

| Profile | manufacturer | product | Notes |
|---|---|---|---|
| `garmin-edge` | garmin (1) | 3121 (Edge 530) | Patches `file_creator.software_version` to 1140. Cycling/virtual_activity only. |
| `garmin-edge-1030` | garmin (1) | 3570 (Edge 1030 Plus) | Cycling/virtual_activity only. |
| `garmin-forerunner` | garmin (1) | 4257 (Forerunner 265) | Cycling/virtual_activity only. |
| `zwift` | zwift (260) | 0 | Clears serial. |
| `rouvy` | rouvy (267) | 0 | Clears serial. |
| `tacx` | tacx (89) | 0 | Clears serial. |

The Garmin-cycling profiles only accept files already marked `cycling / virtual_activity`. They will refuse a real Garmin outdoor ride or a run, so you cannot accidentally relabel a different activity type.

`--inject-metrics --ftp 250` adds the three strict-Coggan fields (NP / IF / TSS) to the session message. FTP is required; if you omit it, a one-time GUI prompt asks for it and persists the value in `fit-fix.cfg` next to the script.

`--inject-te-approx` is reserved for an explicit-opt-in HR-TRIMP-based approximate Training Effect. It requires `--ftp`, `--resting-hr`, `--max-hr`. Currently a stub: the flag exists, the validation runs, but the injection itself is a TODO. **By design**, because the analysis in `tests/` and the regression on the user's own Garmin files showed TE depends on internal Garmin state (VO2max, acute load, recovery, sleep) and cannot be accurately reconstructed from a single FIT file.

### compare

Side-by-side diff of two or more FIT files as a markdown table. Useful for "what is structurally different between MyWhoosh and a Garmin-native cycling FIT".

```text
fit-fix compare mywhoosh.fit garmin_edge_outdoor.fit forerunner_run.fit --md compare.md
```

### matrix

Generates six patched variants from one MyWhoosh source and writes a `test_matrix.md` with manual Garmin Connect test instructions.

```text
fit-fix matrix MyWhoosh_Ride.fit --out-dir ./variants
```

Variants produced:

- `01_timestamp_fixed_only.fit` - just the timestamp repair
- `02_garmin_edge_indoor.fit` - Garmin Edge 530
- `03_garmin_forerunner_indoor.fit` - Forerunner 265
- `04_zwift_virtual.fit` - Zwift
- `05_rouvy_virtual.fit` - Rouvy
- `06_tacx_indoor.fit` - Tacx Training

Use these to find which spoof Garmin Connect actually counts toward Acute Load / Recovery Time. The procedure for each variant: upload to Connect Web, sync your watch twice, observe whether Training Status / Acute Load / Recovery Time updated, then delete the import before testing the next variant (Garmin rejects duplicate uploads of the same time window).

## Drag-and-drop launcher (Windows)

Drop `.fit` files onto `Fix MyWhoosh FIT.bat`. By default it applies `--mimic-garmin --inject-metrics`, which is equivalent to `patch --profile garmin-edge --inject-metrics`. Output is `<name>_fixed.fit` next to each input.

## FTP

`--inject-metrics` needs FTP in watts. First run via the launcher pops a tkinter dialog and saves to `fit-fix.cfg`. Subsequent runs are automatic. CLI users can always pass `--ftp` explicitly.

## What is NOT done

- Aerobic and anaerobic Training Effect are Firstbeat-proprietary. They are not written by default. The `--inject-te-approx` opt-in flag is stubbed (validation only) because regression against five of the user's own Garmin activities showed TE correlates negatively with single-workout TRIMP for this individual - the value depends on external Garmin state.
- VO2max, Recovery Time, Acute Load, Training Status are not written and should not be: Garmin computes those itself from Training Effect plus user history.
- Original FIT files are never modified. Output goes to `<name>_fixed.fit` or `--output` or `--out-dir`.

## Install

Python 3.10+, no runtime dependencies. `tkinter` (stdlib) is only used for the GUI prompt and popups.

```text
pip install .
fit-fix --help
```

## Development

```text
python -m pip install .[dev]
python -m pytest
python -m ruff check .
python -m mypy --check-untyped-defs fix_fit.py tests
```

89 tests cover the parser, timestamp detection, NP/IF/TSS computation, profile patching, atomic-write semantics, and the new analyze / compare / matrix commands.

## License

MIT - not_kler.
