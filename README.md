# fit-fix

A minimal MyWhoosh `.fit` converter by **not_kler**.

It changes exactly two `file_id` fields:

- manufacturer: MyWhoosh (`331`) to Garmin (`1`)
- product: Edge 1050 (`4440`)

Apart from the required file CRC, everything else remains byte-for-byte
unchanged. The converter validates the FIT structure and CRC before writing.

## Windows

Drag one or more MyWhoosh `.fit` files onto
`Convert MyWhoosh to Edge 1050.bat`.

The converted file appears beside the original as:

```text
ride_edge1050.fit
```

You can also double-click the launcher and select files. Original files are never
overwritten.

## Command line

Python 3.10 or newer is required. There are no runtime dependencies.

```text
python fix_fit.py ride.fit
```

Multiple files can be converted in one command:

```text
python fix_fit.py ride1.fit ride2.fit
```

## What it does not do

It does not rewrite timestamps, inject metrics, add device records, or modify the
activity stream. Garmin Connect acceptance and training-load processing are not
guaranteed.

## Development

```text
python -m pip install .[dev]
python -m pytest -q
python -m ruff check .
```

MIT licensed.
