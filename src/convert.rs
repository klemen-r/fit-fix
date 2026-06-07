use std::path::{Path, PathBuf};
use std::process::{Child, Command, Output, Stdio};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

use anyhow::{Context, Result, bail};
use sha2::{Digest, Sha256};

use crate::fit;

const DONOR_ARCHIVE: &str = "23128003580.zip";
const CONSERVATIVE_VARIANT: &str = "conservative_garmin_device_spoof.fit";
const CONVERSION_TIMEOUT: Duration = Duration::from_secs(5 * 60);
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const VARIANT_NAMES: [&str; 4] = [
    "conservative_garmin_device_spoof.fit",
    "garmin_ordered_spoof.fit",
    "full_training_spoof.fit",
    "donor_max_spoof.fit",
];

pub struct PreparedFit {
    pub path: PathBuf,
    pub converted: bool,
}

pub fn prepare_for_upload<F>(source: &Path, report: F) -> Result<PreparedFit>
where
    F: Fn(&str),
{
    if is_generated_variant(source) {
        fit::fingerprint(source)?;
        return Ok(PreparedFit {
            path: source.to_owned(),
            converted: false,
        });
    }

    report("Converting MyWhoosh FIT...");
    let source = source
        .canonicalize()
        .with_context(|| format!("Could not resolve {}", source.display()))?;
    let validated = fit::load(&source)?;
    let script = converter_script_path()?;
    let project_dir = script
        .parent()
        .context("Garmin converter script has no parent directory")?;
    let donor = donor_path(project_dir)?;
    let output_dir = conversion_output_dir(project_dir, &source, &validated.bytes);
    run_converter(&script, &source, &donor, &output_dir)?;

    let converted = output_dir.join(CONSERVATIVE_VARIANT);
    fit::fingerprint(&converted).context("Converted Garmin FIT failed final validation")?;
    Ok(PreparedFit {
        path: converted,
        converted: true,
    })
}

fn is_generated_variant(path: &Path) -> bool {
    path.file_name()
        .and_then(|value| value.to_str())
        .is_some_and(|name| {
            VARIANT_NAMES
                .iter()
                .any(|variant| name.eq_ignore_ascii_case(variant))
        })
}

fn converter_script_path() -> Result<PathBuf> {
    find_project_file("garmin_donor_spoof.py")
        .context("garmin_donor_spoof.py was not found beside the project or executable")
}

fn donor_path(project_dir: &Path) -> Result<PathBuf> {
    let mut candidates = Vec::new();
    if let Some(profile) = std::env::var_os("USERPROFILE") {
        candidates.push(PathBuf::from(profile).join("Downloads").join(DONOR_ARCHIVE));
    }
    candidates.push(project_dir.join(DONOR_ARCHIVE));
    candidates.push(
        project_dir
            .join("outputs")
            .join("garmin_donor")
            .join("23128003580_ACTIVITY.fit"),
    );
    candidates
        .into_iter()
        .find(|path| path.is_file())
        .map(|path| path.canonicalize())
        .transpose()?
        .context("Garmin donor was not found. Keep 23128003580.zip in your Downloads folder.")
}

fn find_project_file(name: &str) -> Result<PathBuf> {
    let mut candidates = vec![std::env::current_dir()?.join(name)];
    if let Some(directory) = std::env::current_exe()?.parent() {
        candidates.push(directory.join(name));
        if let Some(parent) = directory.parent() {
            candidates.push(parent.join(name));
        }
    }
    candidates
        .into_iter()
        .find(|path| path.is_file())
        .map(|path| path.canonicalize())
        .transpose()?
        .context("Project file was not found")
}

fn conversion_output_dir(project_dir: &Path, source: &Path, bytes: &[u8]) -> PathBuf {
    let stem = safe_stem(
        source
            .file_stem()
            .and_then(|value| value.to_str())
            .unwrap_or("activity"),
    );
    let digest = Sha256::digest(bytes);
    let short_hash = digest[..6]
        .iter()
        .map(|value| format!("{value:02x}"))
        .collect::<String>();
    project_dir
        .join("outputs")
        .join("garmin_donor_spoof")
        .join("generated")
        .join(format!("{stem}-{short_hash}"))
}

fn safe_stem(value: &str) -> String {
    let mut result = String::with_capacity(value.len().min(40));
    for character in value.chars().take(40) {
        if character.is_ascii_alphanumeric() || matches!(character, '-' | '_') {
            result.push(character);
        } else {
            result.push('_');
        }
    }
    let result = result.trim_matches('_');
    if result.is_empty() {
        "activity".to_owned()
    } else {
        result.to_owned()
    }
}

fn run_converter(script: &Path, source: &Path, donor: &Path, output_dir: &Path) -> Result<()> {
    let project_dir = script
        .parent()
        .context("Garmin converter script has no parent directory")?;
    let mut spawn_failures = Vec::new();
    for (program, prefix) in [("python", None), ("py", Some("-3"))] {
        let mut command = Command::new(program);
        if let Some(argument) = prefix {
            command.arg(argument);
        }
        command
            .arg(script)
            .arg("--mywhoosh")
            .arg(source)
            .arg("--donor")
            .arg(donor)
            .arg("--output-dir")
            .arg(output_dir)
            .current_dir(project_dir)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(windows)]
        command.creation_flags(CREATE_NO_WINDOW);

        let child = match command.spawn() {
            Ok(child) => child,
            Err(error) => {
                spawn_failures.push(format!("{program}: {error}"));
                continue;
            }
        };
        let output = wait_with_timeout(child)?;
        if output.status.success() {
            return Ok(());
        }
        bail!("Garmin FIT conversion failed: {}", output_message(&output));
    }
    bail!(
        "Could not start Garmin FIT conversion. Install Python and keep the project files together. Attempts: {spawn_failures:?}"
    )
}

fn wait_with_timeout(mut child: Child) -> Result<Output> {
    let started = Instant::now();
    loop {
        if child
            .try_wait()
            .context("Could not monitor Garmin FIT conversion")?
            .is_some()
        {
            return child
                .wait_with_output()
                .context("Could not read Garmin FIT conversion result");
        }
        if started.elapsed() >= CONVERSION_TIMEOUT {
            let _ = child.kill();
            let _ = child.wait();
            bail!("Garmin FIT conversion timed out after 5 minutes");
        }
        thread::sleep(Duration::from_millis(100));
    }
}

fn output_message(output: &Output) -> String {
    let stderr = String::from_utf8_lossy(&output.stderr);
    let stdout = String::from_utf8_lossy(&output.stdout);
    let message = if stderr.trim().is_empty() {
        stdout.trim()
    } else {
        stderr.trim()
    };
    let mut shortened = message.chars().rev().take(1_000).collect::<String>();
    shortened = shortened.chars().rev().collect();
    if shortened.is_empty() {
        format!("process exited with {}", output.status)
    } else {
        shortened
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recognizes_only_named_spoof_variants() {
        assert!(is_generated_variant(Path::new(
            "outputs/conservative_garmin_device_spoof.fit"
        )));
        assert!(is_generated_variant(Path::new("DONOR_MAX_SPOOF.FIT")));
        assert!(!is_generated_variant(Path::new("MyWhoosh.fit")));
    }

    #[test]
    fn safe_stem_is_bounded_and_path_safe() {
        assert_eq!(safe_stem("MyWhoosh Limmat/Loop"), "MyWhoosh_Limmat_Loop");
        assert_eq!(safe_stem("..."), "activity");
        assert!(safe_stem(&"a".repeat(100)).len() <= 40);
    }

    #[test]
    fn output_directory_is_stable_per_source_content() {
        let root = Path::new("C:/fit-fix");
        let source = Path::new("C:/rides/My Ride.fit");
        assert_eq!(
            conversion_output_dir(root, source, b"same"),
            conversion_output_dir(root, source, b"same")
        );
        assert_ne!(
            conversion_output_dir(root, source, b"same"),
            conversion_output_dir(root, source, b"different")
        );
    }

    #[test]
    fn bridge_converts_local_mywhoosh_fixture_when_available() {
        let Some(profile) = std::env::var_os("USERPROFILE") else {
            return;
        };
        let source = PathBuf::from(profile)
            .join("Downloads")
            .join("MyWhoosh_Limmat_Loop.fit");
        if !source.is_file() {
            return;
        }

        let prepared = prepare_for_upload(&source, |_| {}).expect("conversion bridge should work");
        assert!(prepared.converted);
        assert_eq!(
            prepared.path.file_name().and_then(|value| value.to_str()),
            Some(CONSERVATIVE_VARIANT)
        );
        fit::fingerprint(&prepared.path).expect("converted FIT should parse");
    }
}
