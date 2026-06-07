# Garmin FIT Upload

A small Windows application that converts a MyWhoosh cycling FIT into a
Garmin-style indoor-cycling FIT and uploads it to Garmin Connect.

## Install

1. Download `garmin-fit-upload-windows-x64.zip` from GitHub Releases.
2. Extract it.
3. Double-click `Setup.bat`.
4. If asked, select one activity recorded by your own Garmin watch.
5. Use the new **Garmin FIT Upload** desktop shortcut.

Setup installs only the required Garmin sign-in support for the current Windows
user. It uses `winget` to install Python when Python 3.10 or newer is unavailable,
then records the exact configured Python executable for reliable launches.

The selected Garmin activity is stored locally as `garmin-template.fit`. It is a
binary template containing Garmin-native message structure and device metadata.
Public manufacturer/product codes alone cannot replace it.

## Use

1. Select a raw MyWhoosh FIT.
2. Click **Convert & Upload**.
3. Sign in to Garmin Connect when prompted.

The app:

- preserves the MyWhoosh ride record stream and summaries
- sets `sport = cycling` and `sub_sport = indoor_cycling`
- applies the local Garmin template's identity, creator, device, and timer metadata
- validates the generated FIT and CRC
- skips upload when the activity already exists
- stores reusable sign-in tokens in Windows Credential Manager

Passwords and MFA codes are never saved.

## Limits

Garmin decides whether Training Effect, Acute Load, Recovery Time, Training
Status, and Load Focus update. FIT metadata cannot guarantee those results.

Use an activity from your own Garmin watch as the template. Do not publish FIT
templates because they can contain device serial numbers and personal metadata.

## Development

```text
python -m pip install .[dev,auth]
python -m pytest -q
python -m ruff check .
cargo test
cargo clippy --all-targets --all-features -- -D warnings
cargo build --release
powershell -File scripts/build-portable.ps1
```

MIT licensed.
