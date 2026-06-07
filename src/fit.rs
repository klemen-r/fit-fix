use std::fs::File;
use std::io::{Cursor, Read};
use std::path::Path;

use anyhow::{Context, Result, bail};
use chrono::{DateTime, Utc};
use fitparser::{FitDataRecord, Value, profile::MesgNum};

const MAX_FIT_BYTES: u64 = 128 * 1024 * 1024;

#[derive(Clone, Debug, PartialEq)]
pub struct ActivityFingerprint {
    pub start_utc: DateTime<Utc>,
    pub duration_seconds: Option<f64>,
    pub distance_meters: Option<f64>,
}

pub struct ValidatedFit {
    pub fingerprint: ActivityFingerprint,
    pub bytes: Vec<u8>,
}

pub fn fingerprint(path: &Path) -> Result<ActivityFingerprint> {
    Ok(load(path)?.fingerprint)
}

pub fn load(path: &Path) -> Result<ValidatedFit> {
    if !path.is_file() {
        bail!("FIT file not found: {}", path.display());
    }
    let is_fit = path
        .extension()
        .and_then(|value| value.to_str())
        .is_some_and(|value| value.eq_ignore_ascii_case("fit"));
    if !is_fit {
        bail!("Selected file is not a FIT");
    }

    let file = File::open(path).with_context(|| format!("Could not open {}", path.display()))?;
    let length = file
        .metadata()
        .with_context(|| format!("Could not inspect {}", path.display()))?
        .len();
    if length > MAX_FIT_BYTES {
        bail!("Selected FIT is larger than 128 MiB");
    }
    let mut bytes = Vec::with_capacity(length as usize);
    file.take(MAX_FIT_BYTES + 1)
        .read_to_end(&mut bytes)
        .with_context(|| format!("Could not read {}", path.display()))?;
    if bytes.len() as u64 > MAX_FIT_BYTES {
        bail!("Selected FIT is larger than 128 MiB");
    }

    let mut reader = Cursor::new(bytes.as_slice());
    let records = fitparser::from_reader(&mut reader)
        .with_context(|| format!("Invalid FIT file: {}", path.display()))?;
    let sessions: Vec<_> = records
        .iter()
        .filter(|record| record.kind() == MesgNum::Session)
        .collect();
    if sessions.len() != 1 {
        bail!("Expected one FIT session, found {}", sessions.len());
    }
    Ok(ValidatedFit {
        fingerprint: fingerprint_session(sessions[0])?,
        bytes,
    })
}

fn fingerprint_session(session: &FitDataRecord) -> Result<ActivityFingerprint> {
    let start_utc = session
        .fields()
        .iter()
        .find(|field| field.name() == "start_time")
        .and_then(timestamp)
        .context("FIT session has no valid start time")?;
    Ok(ActivityFingerprint {
        start_utc,
        duration_seconds: numeric_field(session, "total_timer_time"),
        distance_meters: numeric_field(session, "total_distance"),
    })
}

fn timestamp(field: &fitparser::FitDataField) -> Option<DateTime<Utc>> {
    match field.value() {
        Value::Timestamp(value) => Some(value.with_timezone(&Utc)),
        _ => None,
    }
}

fn numeric_field(record: &FitDataRecord, name: &str) -> Option<f64> {
    record
        .fields()
        .iter()
        .find(|field| field.name() == name)
        .and_then(|field| numeric(field.value()))
        .filter(|value| value.is_finite() && *value >= 0.0)
}

fn numeric(value: &Value) -> Option<f64> {
    match value {
        Value::Byte(value) | Value::Enum(value) | Value::UInt8(value) | Value::UInt8z(value) => {
            Some(f64::from(*value))
        }
        Value::SInt8(value) => Some(f64::from(*value)),
        Value::SInt16(value) => Some(f64::from(*value)),
        Value::UInt16(value) | Value::UInt16z(value) => Some(f64::from(*value)),
        Value::SInt32(value) => Some(f64::from(*value)),
        Value::UInt32(value) | Value::UInt32z(value) => Some(f64::from(*value)),
        Value::SInt64(value) => Some(*value as f64),
        Value::UInt64(value) | Value::UInt64z(value) => Some(*value as f64),
        Value::Float32(value) => Some(f64::from(*value)),
        Value::Float64(value) => Some(*value),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_non_fit_path() {
        let error = fingerprint(Path::new("README.md")).expect_err("README is not FIT");
        assert!(error.to_string().contains("not a FIT"));
    }

    #[test]
    fn rejects_oversized_fit_before_parsing() {
        let path =
            std::env::temp_dir().join(format!("fit-fix-oversized-{}.fit", std::process::id()));
        let file = File::create(&path).expect("create sparse test file");
        file.set_len(MAX_FIT_BYTES + 1)
            .expect("set sparse test file length");
        drop(file);
        let error = fingerprint(&path).expect_err("oversized FIT should fail");
        let _ = std::fs::remove_file(path);
        assert!(error.to_string().contains("larger than 128 MiB"));
    }
}
