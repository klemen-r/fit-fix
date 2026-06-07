use std::path::{Path, PathBuf};
use std::process::{Child, Command, Output, Stdio};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

use anyhow::{Context, Result, bail};
use sha2::{Digest, Sha256};

use crate::fit;

const CONVERTED_NAME: &str = "converted.fit";
const TEMPLATE_NAME: &str = "garmin-template.fit";
const CONVERSION_TIMEOUT: Duration = Duration::from_secs(2 * 60);
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

pub struct PreparedFit {
    pub path: PathBuf,
    pub converted: bool,
}

pub fn prepare_for_upload<F>(source: &Path, report: F) -> Result<PreparedFit>
where
    F: Fn(&str),
{
    if source
        .file_name()
        .and_then(|value| value.to_str())
        .is_some_and(|name| name.eq_ignore_ascii_case(CONVERTED_NAME))
    {
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
    let script = project_file("garmin_converter.py")
        .context("garmin_converter.py was not found beside the application")?;
    let template = project_file(TEMPLATE_NAME).context(
        "Garmin template is missing. Re-run Setup and select one activity recorded by your Garmin watch.",
    )?;
    let output = conversion_output_path(&source, &validated.bytes)?;
    run_converter(&script, &source, &template, &output)?;
    fit::fingerprint(&output).context("Converted Garmin FIT failed final validation")?;
    Ok(PreparedFit {
        path: output,
        converted: true,
    })
}

fn project_file(name: &str) -> Result<PathBuf> {
    let mut candidates = vec![std::env::current_dir()?.join(name)];
    if let Some(directory) = std::env::current_exe()?.parent() {
        candidates.push(directory.join(name));
        if let Some(parent) = directory.parent() {
            candidates.push(parent.join(name));
        }
    }
    if let Some(local) = std::env::var_os("LOCALAPPDATA") {
        candidates.push(PathBuf::from(local).join("Garmin FIT Upload").join(name));
    }
    candidates
        .into_iter()
        .find(|path| path.is_file())
        .map(|path| path.canonicalize())
        .transpose()?
        .context("Project file was not found")
}

fn conversion_output_path(source: &Path, bytes: &[u8]) -> Result<PathBuf> {
    let root = std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .unwrap_or(std::env::current_dir()?)
        .join("Garmin FIT Upload")
        .join("converted");
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
    Ok(root
        .join(format!("{stem}-{short_hash}"))
        .join(CONVERTED_NAME))
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

fn run_converter(script: &Path, source: &Path, template: &Path, output: &Path) -> Result<()> {
    let project_dir = script
        .parent()
        .context("Garmin converter script has no parent directory")?;
    let mut spawn_failures = Vec::new();
    for candidate in crate::python::candidates() {
        let mut command = Command::new(&candidate.program);
        if let Some(argument) = candidate.prefix {
            command.arg(argument);
        }
        command
            .arg(script)
            .arg("--source")
            .arg(source)
            .arg("--template")
            .arg(template)
            .arg("--output")
            .arg(output)
            .current_dir(project_dir)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(windows)]
        command.creation_flags(CREATE_NO_WINDOW);

        let child = match command.spawn() {
            Ok(child) => child,
            Err(error) => {
                spawn_failures.push(format!("{}: {error}", candidate.program.display()));
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
        "Could not start Garmin FIT conversion. Re-run Setup to install Python. Attempts: {spawn_failures:?}"
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
            bail!("Garmin FIT conversion timed out after 2 minutes");
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
    fn safe_stem_is_bounded_and_path_safe() {
        assert_eq!(safe_stem("MyWhoosh Limmat/Loop"), "MyWhoosh_Limmat_Loop");
        assert_eq!(safe_stem("..."), "activity");
        assert!(safe_stem(&"a".repeat(100)).len() <= 40);
    }

    #[test]
    fn output_path_is_stable_per_source_content() {
        let source = Path::new("C:/rides/My Ride.fit");
        assert_eq!(
            conversion_output_path(source, b"same").unwrap(),
            conversion_output_path(source, b"same").unwrap()
        );
        assert_ne!(
            conversion_output_path(source, b"same").unwrap(),
            conversion_output_path(source, b"different").unwrap()
        );
    }

    #[test]
    fn bridge_converts_local_fixture_when_available() {
        let Some(profile) = std::env::var_os("USERPROFILE") else {
            return;
        };
        let source = PathBuf::from(profile)
            .join("Downloads")
            .join("MyWhoosh_Limmat_Loop.fit");
        if !source.is_file() || !Path::new(TEMPLATE_NAME).is_file() {
            return;
        }

        let prepared = prepare_for_upload(&source, |_| {}).expect("conversion bridge should work");
        assert!(prepared.converted);
        assert_eq!(
            prepared.path.file_name().and_then(|value| value.to_str()),
            Some(CONVERTED_NAME)
        );
        fit::fingerprint(&prepared.path).expect("converted FIT should parse");
    }
}
