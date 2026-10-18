//! Native file-lock feasibility proof confined to explicit migration fixtures.
//!
//! This does not publish the legacy application's PID ownership record, protect
//! source writes, or establish production coexistence. The metadata/open checks
//! below still have a filesystem identity race; live paths remain prohibited.

use std::fs::{self, File, OpenOptions, TryLockError};
use std::io::{self, Write};
use std::path::{Component, Path, PathBuf};

#[derive(Debug)]
pub struct FixtureLockScope {
    root: PathBuf,
}

#[derive(Debug)]
pub enum FixtureLockAttempt {
    Acquired(FixtureFileLease),
    Busy,
}

/// Closing the owned handle releases its native lock, including on process
/// exit. Never unlink a lock file: a waiting process must see the same inode.
#[derive(Debug)]
pub struct FixtureFileLease {
    _file: File,
}

impl FixtureLockScope {
    /// Both paths must already exist, be absolute, and contain no aliases. No
    /// HOME, live settings, package store or cache location is ever inferred.
    pub fn new(application_root: &Path, fixture_root: &Path) -> io::Result<Self> {
        validate_directory_chain(application_root)?;
        validate_directory_chain(fixture_root)?;
        let application_root = fs::canonicalize(application_root)?;
        let root = fs::canonicalize(fixture_root)?;
        let sandbox = application_root.join("audit/migration/scratch");
        validate_directory_chain(&sandbox)?;
        let sandbox = fs::canonicalize(sandbox)?;
        if !root.starts_with(&sandbox) {
            return Err(denied(
                "Lock fixtures must remain in audit/migration/scratch",
            ));
        }
        Ok(Self { root })
    }

    pub fn try_acquire(&self, leaf: &str) -> io::Result<FixtureLockAttempt> {
        validate_leaf(leaf)?;
        // Recheck each directory before every acquisition. This is a fixture
        // boundary, not a claim of race-free native handle-relative traversal.
        validate_directory_chain(&self.root)?;
        let path = self.root.join(leaf);
        match fs::symlink_metadata(&path) {
            Ok(metadata) => {
                if is_alias(&metadata) || !metadata.is_file() || metadata.permissions().readonly() {
                    return Err(denied("Lock file must be a writable regular fixture file"));
                }
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
        let mut file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path)?;
        let metadata = file.metadata()?;
        if !metadata.is_file() || metadata.permissions().readonly() {
            return Err(denied("Opened fixture lock is not a writable regular file"));
        }
        match file.try_lock() {
            Ok(()) => {
                // Windows msvcrt locks byte zero. Initialize only after taking
                // the OS lock, without truncating or replacing any old content.
                if file.metadata()?.len() == 0 {
                    file.write_all(b"0")?;
                    file.flush()?;
                }
                Ok(FixtureLockAttempt::Acquired(FixtureFileLease {
                    _file: file,
                }))
            }
            Err(TryLockError::WouldBlock) => Ok(FixtureLockAttempt::Busy),
            Err(TryLockError::Error(error)) => Err(error),
        }
    }
}

fn validate_leaf(leaf: &str) -> io::Result<()> {
    let stem = leaf.split('.').next().unwrap_or("");
    let upper = stem.to_ascii_uppercase();
    let reserved = matches!(upper.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        || (upper.len() == 4
            && (upper.starts_with("COM") || upper.starts_with("LPT"))
            && matches!(upper.as_bytes()[3], b'1'..=b'9'));
    if leaf.len() > 80
        || !leaf.ends_with(".lock")
        || !leaf
            .as_bytes()
            .first()
            .is_some_and(u8::is_ascii_alphanumeric)
        || !leaf
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
        || leaf.contains("..")
        || reserved
    {
        return Err(denied("Expected one portable fixture .lock filename"));
    }
    Ok(())
}

fn validate_directory_chain(path: &Path) -> io::Result<()> {
    if !path.is_absolute()
        || path
            .components()
            .any(|part| matches!(part, Component::CurDir | Component::ParentDir))
    {
        return Err(denied(
            "Fixture directories require an absolute path without aliases",
        ));
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        // Verbatim Windows prefixes do not parse '/' as a separator. Reject
        // mixed-separator parent syntax before metadata, without rewriting it.
        if windows_has_parent_segment(path.as_os_str().encode_wide()) {
            return Err(denied("Fixture directory parent aliases are prohibited"));
        }
    }
    let mut current = PathBuf::new();
    for component in path.components() {
        current.push(component);
        // A Windows drive/UNC prefix becomes an inspectable directory after
        // its RootDir component has been appended.
        if matches!(component, Component::Prefix(_)) {
            continue;
        }
        let metadata = fs::symlink_metadata(&current)?;
        if is_alias(&metadata) || !metadata.is_dir() {
            return Err(denied("Fixture directory aliases are prohibited"));
        }
    }
    if fs::metadata(path)?.permissions().readonly() {
        return Err(denied("Fixture directory is read-only"));
    }
    Ok(())
}

#[cfg(any(windows, test))]
fn windows_has_parent_segment(units: impl Iterator<Item = u16>) -> bool {
    let mut dots = 0u8;
    let mut only_dots = true;
    for unit in units {
        if unit == u16::from(b'/') || unit == u16::from(b'\\') {
            if only_dots && dots == 2 {
                return true;
            }
            dots = 0;
            only_dots = true;
        } else if unit == u16::from(b'.') {
            dots = dots.saturating_add(1);
        } else {
            only_dots = false;
        }
    }
    only_dots && dots == 2
}

fn is_alias(metadata: &fs::Metadata) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        metadata.file_type().is_symlink() || metadata.file_attributes() & 0x400 != 0
    }
    #[cfg(not(windows))]
    {
        metadata.file_type().is_symlink()
    }
}

fn denied(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, message)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(0);

    #[test]
    fn windows_parent_syntax_is_rejected_with_native_and_mixed_separators() {
        for path in [
            r"C:\fixture\..\other",
            r"C:/fixture/../other",
            r"\\?\C:\fixture/../../../../",
            r"\\?\UNC\server\share\fixture/..\other",
            r"\\server\share\fixture\..",
        ] {
            assert!(windows_has_parent_segment(path.encode_utf16()), "{path}");
        }
        for path in [
            r"\\?\C:\fixture\file..lock",
            r"\\?\UNC\server\share\fixture\.hidden",
            r"\\server\share\café 日本語\dots...",
            r"C:\fixture\..suffix",
            r"C:\fixture\prefix..",
        ] {
            assert!(!windows_has_parent_segment(path.encode_utf16()), "{path}");
        }
    }

    #[cfg(windows)]
    #[test]
    fn windows_parent_rejection_precedes_metadata_for_missing_directories() {
        for path in [
            r"\\?\C:\mcu-missing-path-that-must-not-be-opened\../other",
            r"\\?\C:\mcu-missing-path-that-must-not-be-opened\..\other",
        ] {
            let error = validate_directory_chain(Path::new(path)).unwrap_err();
            assert_eq!(error.kind(), io::ErrorKind::PermissionDenied, "{path}");
            assert!(
                error.to_string().contains("parent aliases")
                    || error.to_string().contains("without aliases"),
                "{path}: {error}"
            );
        }
    }

    struct Fixture {
        app: PathBuf,
        root: PathBuf,
    }

    impl Fixture {
        fn new() -> Self {
            let app = Path::new(env!("CARGO_MANIFEST_DIR"))
                .ancestors()
                .nth(3)
                .unwrap()
                .to_path_buf();
            let sandbox = app.join("audit/migration/scratch");
            fs::create_dir_all(&sandbox).unwrap();
            for _ in 0..100 {
                let root = sandbox.join(format!(
                    "native-lock-{}-{}",
                    std::process::id(),
                    NEXT.fetch_add(1, Ordering::Relaxed)
                ));
                match fs::create_dir(&root) {
                    Ok(()) => return Self { app, root },
                    Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
                    Err(error) => panic!("Cannot create owned lock fixture: {error}"),
                }
            }
            panic!("Cannot reserve a unique lock fixture");
        }

        fn scope(&self) -> FixtureLockScope {
            FixtureLockScope::new(&self.app, &self.root).unwrap()
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            fs::remove_dir_all(&self.root).unwrap();
        }
    }

    #[test]
    fn independent_handles_contend_and_drop_preserves_file_and_payload() {
        let fixture = Fixture::new();
        let scope = fixture.scope();
        let path = fixture.root.join("settings.json.lock");
        fs::write(&path, b"retained marker").unwrap();
        let FixtureLockAttempt::Acquired(lease) = scope.try_acquire("settings.json.lock").unwrap()
        else {
            panic!("First fixture acquisition was busy");
        };
        assert!(matches!(
            scope.try_acquire("settings.json.lock").unwrap(),
            FixtureLockAttempt::Busy
        ));
        drop(lease);
        assert_eq!(fs::read(&path).unwrap(), b"retained marker");
        assert!(matches!(
            scope.try_acquire("settings.json.lock").unwrap(),
            FixtureLockAttempt::Acquired(_)
        ));
    }

    #[test]
    fn empty_marker_is_initialized_only_by_owner() {
        let fixture = Fixture::new();
        let scope = fixture.scope();
        let FixtureLockAttempt::Acquired(lease) = scope.try_acquire("settings.json.lock").unwrap()
        else {
            panic!("First fixture acquisition was busy");
        };
        drop(lease);
        assert_eq!(
            fs::read(fixture.root.join("settings.json.lock")).unwrap(),
            b"0"
        );
    }

    #[test]
    fn escaping_and_nonportable_names_are_rejected() {
        let fixture = Fixture::new();
        let scope = fixture.scope();
        assert!(FixtureLockScope::new(&fixture.app, &fixture.app.join("desktop")).is_err());
        assert!(FixtureLockScope::new(&fixture.app, &fixture.root.join("..")).is_err());
        for leaf in [
            "../other.lock",
            "other/child.lock",
            "CON.lock",
            "COM1.lock",
            ".lock",
            "file",
            "double..lock",
            "trailing .lock",
        ] {
            assert_eq!(
                scope.try_acquire(leaf).unwrap_err().kind(),
                io::ErrorKind::PermissionDenied
            );
        }
    }

    #[test]
    fn readonly_file_is_not_adopted() {
        let fixture = Fixture::new();
        let path = fixture.root.join("settings.json.lock");
        fs::write(&path, b"original").unwrap();
        let original = fs::metadata(&path).unwrap().permissions();
        let mut readonly = original.clone();
        readonly.set_readonly(true);
        fs::set_permissions(&path, readonly).unwrap();
        let result = fixture.scope().try_acquire("settings.json.lock");
        fs::set_permissions(&path, original).unwrap();
        assert_eq!(result.unwrap_err().kind(), io::ErrorKind::PermissionDenied);
        assert_eq!(fs::read(path).unwrap(), b"original");
    }

    #[cfg(unix)]
    #[test]
    fn symlinked_file_and_directory_are_rejected() {
        use std::os::unix::fs::symlink;
        let fixture = Fixture::new();
        fs::write(fixture.root.join("actual.lock"), b"original").unwrap();
        symlink("actual.lock", fixture.root.join("settings.json.lock")).unwrap();
        assert!(fixture.scope().try_acquire("settings.json.lock").is_err());
        fs::create_dir(fixture.root.join("actual")).unwrap();
        symlink("actual", fixture.root.join("alias")).unwrap();
        assert!(FixtureLockScope::new(&fixture.app, &fixture.root.join("alias")).is_err());
    }
}
