use std::collections::HashSet;
use std::path::{Path, PathBuf};

pub struct Candidate {
    pub program: PathBuf,
    pub prefix: Option<&'static str>,
}

pub fn candidates() -> Vec<Candidate> {
    let mut output = Vec::new();
    let mut seen = HashSet::new();
    for path in configured_paths() {
        push(&mut output, &mut seen, path, None);
    }
    push(&mut output, &mut seen, PathBuf::from("python"), None);
    push(&mut output, &mut seen, PathBuf::from("py"), Some("-3"));
    output
}

fn configured_paths() -> Vec<PathBuf> {
    let mut configs = Vec::new();
    if let Ok(current) = std::env::current_dir() {
        configs.push(current.join("python-path.txt"));
    }
    if let Ok(executable) = std::env::current_exe()
        && let Some(directory) = executable.parent()
    {
        configs.push(directory.join("python-path.txt"));
    }
    configs
        .into_iter()
        .filter_map(|path| std::fs::read_to_string(path).ok())
        .filter_map(|value| {
            let path = Path::new(value.trim().trim_start_matches('\u{feff}'));
            path.is_file().then(|| path.to_owned())
        })
        .collect()
}

fn push(
    output: &mut Vec<Candidate>,
    seen: &mut HashSet<String>,
    program: PathBuf,
    prefix: Option<&'static str>,
) {
    let key = program.to_string_lossy().to_ascii_lowercase();
    if seen.insert(key) {
        output.push(Candidate { program, prefix });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn standard_python_candidates_are_present() {
        let values = candidates();
        assert!(
            values
                .iter()
                .any(|value| value.program == Path::new("python"))
        );
        assert!(values.iter().any(|value| value.program == Path::new("py")));
    }
}
