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

## Empirical Garmin donor pipeline

`garmin_pipeline.py` analyzes Garmin donor activities, builds five cumulative upload-test
variants, validates them with the official Garmin FIT SDK when installed, and writes FITs,
hashes, detailed analysis JSON, and reports under `outputs/`.

```text
python -m pip install .[analysis]
python garmin_pipeline.py build --mywhoosh "C:\path\to\MyWhoosh_ride.fit" ^
  --klemen-dir "C:\path\to\garmin-activity-zips" ^
  --robert-zip "C:\path\to\additional-activities.zip"
```

The reverse-engineered attempt is separate from the structural variants. It writes only
fields supported by evidence from the supplied Garmin files and labels upload/watch-sync
behavior as unproven.

To compare a MyWhoosh ride with a Garmin-native recording of the same ride (for example,
an FR265 Indoor Bike / Virtual Cycling FIT captured at the same time):

```text
python garmin_pipeline.py compare-paired ^
  --mywhoosh "C:\path\to\mywhoosh.fit" ^
  --garmin-native "C:\path\to\fr265_same_ride.fit"
```

The paired comparison aligns the two record streams by timestamp, reports HR/power/
cadence/distance/speed agreement, compares session and lap summaries, and contrasts the
proprietary `total_training_effect` / `total_anaerobic_training_effect` / `training_load_peak`
fields with the donor pipeline's reverse-engineered estimate. It writes
`paired_mywhoosh_vs_fr265.md`, `paired_metric_fields.md`, and
`paired_conversion_recommendations.md` under `outputs/reports/`.

To compare an original Zwift FIT with a Garmin-exported ZIP and the current donor/variants:

```text
python zwift_compare.py ^
  --original "C:\path\to\original-zwift.fit" ^
  --garmin-zip "C:\path\to\garmin-export.zip" ^
  --donor-dir "C:\path\to\garmin-activity-zips"
```

This writes exact byte/hash, identity, structure, and TE/load-field comparisons under
`outputs/zwift_comparison/` and short findings under `outputs/reports/`.

## Garmin Connect Upload

Double-click `Upload selected FIT to Garmin.bat` to open the compact native Rust
uploader. Select a raw MyWhoosh FIT, then click **Convert & Upload**. The app builds all
four Garmin-donor variants with `23128003580.zip`, validates them, and uploads the
conservative variant first. Generated spoof variants can also be selected directly for
the required one-at-a-time follow-up tests. Keep the donor ZIP in your Downloads folder
and install the converter/authentication dependencies with:

```text
python -m pip install ".[analysis,auth]"
```

On the first run, enter the Garmin Connect email, password, and MFA code in the GUI.
The password and MFA code are never saved. Reusable Garmin session tokens are stored in
versioned, integrity-checked chunks in Windows Credential Manager and cleared
automatically when Garmin rejects them. This avoids Windows' per-credential size limit.

Garmin sometimes blocks its direct mobile sign-in endpoint with persistent HTTP 429
responses. When that happens, the Rust app automatically uses
`garmin_auth_bridge.py`, which calls Garmin's browser-compatible sign-in strategy
through anonymous pipes. This fallback usually takes 10-30 seconds and requires:

```text
python -m pip install .[auth]
```

Before uploading, the app validates the FIT and its CRC, then checks Garmin Connect
activities around the FIT's UTC start time and compares duration/distance. If the ride
or another variant of it already exists, the upload is skipped.

To build the uploader from source, install current Rust and Visual Studio C++ Build
Tools, then run:

```text
cargo test
cargo clippy --all-targets --all-features -- -D warnings
cargo build --release
```

The launcher uses `dist/garmin-fit-upload.exe` first and falls back to
`target/release/garmin-fit-upload.exe`.

### Portable Windows install

Download `garmin-fit-upload-windows-x64.zip` from GitHub Releases and extract it. Copy
your private `23128003580.zip` donor beside `Install Garmin FIT Upload.bat`, then
double-click the installer. It installs required Python components for the current user,
copies the app to `%LOCALAPPDATA%\Garmin FIT Upload`, and creates a desktop shortcut.
The public release ZIP intentionally excludes donor files and generated output reports.

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
cargo test
cargo clippy --all-targets --all-features -- -D warnings
```

MIT licensed.
