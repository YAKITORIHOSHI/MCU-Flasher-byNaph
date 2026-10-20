//! Development-only fixture reader. Native command-line authority selects one
//! existing owned scratch root; stdin supplies test records, never live grants.
//! No package installation, tool execution, process launch or filesystem write.
use mcu_core::board_compatibility::{
    FixtureCompatibilityScope, NativeHost, NativeRecipeSelection, SnapshotReader, SnapshotRefusal,
    MAX_METADATA_BYTES,
};
use mcu_host::file_identity::NativeFileIdentity;
use serde_json::Value;
use std::fs::{self, File, Metadata, OpenOptions};
use std::io::Read;
use std::path::{Component, Path, PathBuf};

const INPUT_LIMIT: u64 = 1_000_000;
const CASE_LIMIT: usize = 256;

#[derive(Default)]
struct NativeReader {
    attempts: usize,
    bytes: usize,
    #[cfg(test)]
    before_content_open: Option<fn(&Path)>,
}

impl SnapshotReader for NativeReader {
    fn directory(&mut self, path: &Path) -> Result<PathBuf, SnapshotRefusal> {
        directory_chain(path)?;
        let canonical = path.canonicalize().map_err(|_| SnapshotRefusal::Io)?;
        directory_chain(&canonical)?;
        Ok(canonical)
    }

    fn read_regular(&mut self, path: &Path, boundary: &Path) -> Result<Vec<u8>, SnapshotRefusal> {
        self.attempts += 1;
        let boundary = self.directory(boundary)?;
        checked_file(path, &boundary)?;
        let before = open_native(path, true)?;
        regular(&before.metadata().map_err(|_| SnapshotRefusal::Io)?)?;
        let original = identity(&before)?;
        #[cfg(test)]
        if let Some(callback) = self.before_content_open.take() {
            callback(path);
        }
        let file = open_native(path, false)?;
        let selected = file.metadata().map_err(|_| SnapshotRefusal::Io)?;
        regular(&selected)?;
        if identity(&file)? != original {
            return Err(SnapshotRefusal::Changed);
        }
        let mut raw = Vec::new();
        (&file)
            .take(MAX_METADATA_BYTES as u64 + 1)
            .read_to_end(&mut raw)
            .map_err(|_| SnapshotRefusal::Io)?;
        self.bytes += raw.len();
        if raw.len() > MAX_METADATA_BYTES {
            return Err(SnapshotRefusal::TooLarge);
        }
        let after = file.metadata().map_err(|_| SnapshotRefusal::Io)?;
        if selected.len() != after.len() || selected.modified().ok() != after.modified().ok() {
            return Err(SnapshotRefusal::Changed);
        }
        checked_file(path, &boundary)?;
        let current = open_native(path, true)?;
        regular(&current.metadata().map_err(|_| SnapshotRefusal::Io)?)?;
        if identity(&current)? != original {
            return Err(SnapshotRefusal::Changed);
        }
        // This checks observable leaf replacement. It does not freeze content,
        // pin all ancestors, or close the last check/use race for future tools.
        Ok(raw)
    }
}

fn ordinary_path(path: &Path) -> Result<(), SnapshotRefusal> {
    if !path.is_absolute() {
        return Err(SnapshotRefusal::UnsafePath);
    }
    #[cfg(windows)]
    {
        use std::path::Prefix;
        if !matches!(path.components().next(), Some(Component::Prefix(p))
            if matches!(p.kind(), Prefix::Disk(_) | Prefix::VerbatimDisk(_) | Prefix::UNC(_, _) | Prefix::VerbatimUNC(_, _)))
        {
            return Err(SnapshotRefusal::UnsafePath);
        }
    }
    for part in path.components() {
        match part {
            Component::ParentDir => return Err(SnapshotRefusal::UnsafePath),
            Component::Normal(leaf) => {
                let leaf = leaf.to_str().ok_or(SnapshotRefusal::UnsafePath)?;
                if leaf.contains([':', '\\'])
                    || mcu_core::source_names::is_windows_reserved_device_name(leaf)
                {
                    return Err(SnapshotRefusal::UnsafePath);
                }
            }
            _ => (),
        }
    }
    // Windows verbatim paths retain '..' without Component::ParentDir.
    #[cfg(windows)]
    if path
        .as_os_str()
        .to_string_lossy()
        .split(['/', '\\'])
        .any(|part| part == "..")
    {
        return Err(SnapshotRefusal::UnsafePath);
    }
    Ok(())
}

fn alias(metadata: &Metadata) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        metadata.file_attributes() & 0x400 != 0
    }
    #[cfg(not(windows))]
    {
        metadata.file_type().is_symlink()
    }
}

fn directory_chain(path: &Path) -> Result<(), SnapshotRefusal> {
    ordinary_path(path)?;
    let mut current = PathBuf::new();
    for part in path.components() {
        current.push(part);
        if matches!(part, Component::Prefix(_)) {
            continue;
        }
        let metadata = fs::symlink_metadata(&current).map_err(|_| SnapshotRefusal::Io)?;
        if alias(&metadata) || !metadata.is_dir() {
            return Err(SnapshotRefusal::UnsafePath);
        }
    }
    Ok(())
}

fn regular(metadata: &Metadata) -> Result<(), SnapshotRefusal> {
    if alias(metadata) {
        return Err(SnapshotRefusal::UnsafePath);
    }
    if !metadata.is_file() {
        return Err(SnapshotRefusal::NonRegular);
    }
    Ok(())
}

fn checked_file(path: &Path, boundary: &Path) -> Result<(), SnapshotRefusal> {
    ordinary_path(path)?;
    directory_chain(path.parent().ok_or(SnapshotRefusal::UnsafePath)?)?;
    regular(&fs::symlink_metadata(path).map_err(|_| SnapshotRefusal::Io)?)?;
    if !path
        .canonicalize()
        .map_err(|_| SnapshotRefusal::Io)?
        .starts_with(boundary)
    {
        return Err(SnapshotRefusal::UnsafePath);
    }
    Ok(())
}

fn open_native(path: &Path, attributes_only: bool) -> Result<File, SnapshotRefusal> {
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::fs::OpenOptionsExt;
        let mode = if attributes_only {
            libc::O_PATH
        } else {
            libc::O_NONBLOCK
        };
        OpenOptions::new()
            .read(true)
            .custom_flags(mode | libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(path)
            .map_err(|_| SnapshotRefusal::Io)
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_FLAG_OPEN_REPARSE_POINT, FILE_READ_ATTRIBUTES, FILE_SHARE_DELETE, FILE_SHARE_READ,
            FILE_SHARE_WRITE,
        };
        let mut options = OpenOptions::new();
        if attributes_only {
            options.access_mode(FILE_READ_ATTRIBUTES);
        } else {
            options.read(true);
        }
        options
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .open(path)
            .map_err(|_| SnapshotRefusal::Io)
    }
    #[cfg(not(any(target_os = "linux", windows)))]
    {
        let _ = (path, attributes_only);
        Err(SnapshotRefusal::UnsupportedHost)
    }
}

fn identity(file: &File) -> Result<NativeFileIdentity, SnapshotRefusal> {
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::fs::MetadataExt;
        let metadata = file.metadata().map_err(|_| SnapshotRefusal::Io)?;
        Ok(NativeFileIdentity::Linux {
            device: metadata.dev(),
            inode: metadata.ino(),
        })
    }
    #[cfg(windows)]
    {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::Storage::FileSystem::{
            FileIdInfo, GetFileInformationByHandleEx, FILE_ID_INFO,
        };
        let mut information = FILE_ID_INFO::default();
        // SAFETY: file retains a live handle and information is a valid aligned
        // writable FILE_ID_INFO allocation with exactly the supplied byte size.
        if unsafe {
            GetFileInformationByHandleEx(
                file.as_raw_handle(),
                FileIdInfo,
                (&mut information as *mut FILE_ID_INFO).cast(),
                std::mem::size_of::<FILE_ID_INFO>() as u32,
            )
        } == 0
        {
            return Err(SnapshotRefusal::Io);
        }
        Ok(NativeFileIdentity::Windows {
            volume_serial: information.VolumeSerialNumber,
            file_id: information.FileId.Identifier,
        })
    }
    #[cfg(not(any(target_os = "linux", windows)))]
    {
        let _ = file;
        Err(SnapshotRefusal::UnsupportedHost)
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args_os().collect();
    if args.len() != 3 || args[1] != "--fixture-root" {
        return Err("Use --fixture-root with an existing owned audit scratch directory".into());
    }
    let checkout = Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .ok_or("Missing checkout")?;
    let scope = FixtureCompatibilityScope::new(checkout, Path::new(&args[2]))
        .map_err(|r| format!("Invalid development fixture: {r:?}"))?;
    let mut raw = Vec::new();
    std::io::stdin()
        .take(INPUT_LIMIT + 1)
        .read_to_end(&mut raw)?;
    if raw.len() as u64 > INPUT_LIMIT {
        return Err("Compatibility input exceeds byte limit".into());
    }
    let cases: Vec<Value> = serde_json::from_slice(&raw)?;
    if cases.len() > CASE_LIMIT {
        return Err("Compatibility input exceeds case limit".into());
    }
    let host = if cfg!(target_os = "linux") {
        NativeHost::Linux
    } else if cfg!(windows) {
        NativeHost::Windows
    } else {
        NativeHost::Unsupported
    };
    let results: Vec<_> = cases
        .iter()
        .map(|case| {
            if case.as_object().is_some_and(|fields| {
                ["platform", "board", "framework", "pio_manifest"]
                    .iter()
                    .any(|key| fields.get(*key).is_some_and(|value| !value.is_string()))
            }) {
                return serde_json::json!({"rule":null,"options":{},"refusal":"InvalidMetadata",
                "readAttempts":0,"bytesRead":0});
            }
            let target = NativeRecipeSelection {
                backend: case["backend"].as_str(),
                platform: case["platform"].as_str().unwrap_or(""),
                board: case["board"].as_str().unwrap_or(""),
                framework: case["framework"].as_str().unwrap_or(""),
                manifest: Path::new(case["pio_manifest"].as_str().unwrap_or("")),
            };
            let mut reader = NativeReader::default();
            match scope.options(&target, host, &mut reader) {
                Ok(value) => serde_json::json!({"rule":value.rule,"options":value.options,
                "refusal":null,"readAttempts":reader.attempts,"bytesRead":reader.bytes}),
                Err(refusal) => {
                    serde_json::json!({"rule":null,"options":{},"refusal":format!("{refusal:?}"),
                "readAttempts":reader.attempts,"bytesRead":reader.bytes})
                }
            }
        })
        .collect();
    let output = serde_json::to_string(&results)?;
    if output.len() > 4_000_000 {
        return Err("Compatibility output exceeds limit".into());
    }
    println!("{output}");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};
    static NEXT: AtomicU64 = AtomicU64::new(1);
    struct OwnedFixture(PathBuf);
    impl OwnedFixture {
        fn new() -> Self {
            let checkout = Path::new(env!("CARGO_MANIFEST_DIR"))
                .ancestors()
                .nth(3)
                .unwrap();
            let root = checkout
                .join("audit/migration/scratch/board-compatibility-native")
                .join(format!(
                    "native café 日本語-{}-{}",
                    std::process::id(),
                    NEXT.fetch_add(1, Ordering::Relaxed)
                ));
            fs::create_dir_all(root.parent().unwrap()).unwrap();
            // Never adopt and later clean an older fixture with the same name.
            fs::create_dir(&root).unwrap();
            Self(root.canonicalize().unwrap())
        }
    }
    impl Drop for OwnedFixture {
        fn drop(&mut self) {
            fs::remove_dir_all(&self.0).unwrap();
        }
    }
    #[test]
    fn regular_snapshot_accepts_exact_byte_bound_and_refuses_growth() {
        let root = OwnedFixture::new();
        let path = root.0.join("package.json");
        fs::write(&path, vec![b' '; MAX_METADATA_BYTES]).unwrap();
        let mut reader = NativeReader::default();
        assert_eq!(
            reader.read_regular(&path, &root.0).unwrap().len(),
            MAX_METADATA_BYTES
        );
        fs::write(&path, vec![b' '; MAX_METADATA_BYTES + 1]).unwrap();
        assert_eq!(
            reader.read_regular(&path, &root.0),
            Err(SnapshotRefusal::TooLarge)
        );
        fs::write(&path, []).unwrap();
        assert_eq!(reader.read_regular(&path, &root.0).unwrap(), b"");
    }
    #[test]
    fn replacement_between_metadata_and_content_open_is_refused() {
        fn replace(path: &Path) {
            fs::remove_file(path).unwrap();
            fs::write(path, b"replacement").unwrap();
        }
        let root = OwnedFixture::new();
        let path = root.0.join("package.json");
        fs::write(&path, b"original").unwrap();
        let mut reader = NativeReader {
            before_content_open: Some(replace),
            ..NativeReader::default()
        };
        assert_eq!(
            reader.read_regular(&path, &root.0),
            Err(SnapshotRefusal::Changed)
        );
    }
    #[test]
    fn directory_and_escaped_or_nonportable_paths_are_refused() {
        let root = OwnedFixture::new();
        let path = root.0.join("package.json");
        fs::write(&path, b"{}").unwrap();
        let mut reader = NativeReader::default();
        assert_eq!(
            reader.read_regular(&root.0, &root.0),
            Err(SnapshotRefusal::NonRegular)
        );
        assert_eq!(
            reader.read_regular(&path, &root.0.join("missing")),
            Err(SnapshotRefusal::Io)
        );
        // PathBuf::join normalizes '..' after a Windows verbatim prefix.
        // Preserve the literal native units so the guard receives the alias.
        let mut parent_alias = root.0.as_os_str().to_owned();
        parent_alias.push(std::path::MAIN_SEPARATOR_STR);
        parent_alias.push("..");
        parent_alias.push(std::path::MAIN_SEPARATOR_STR);
        parent_alias.push("package.json");
        let parent_alias = PathBuf::from(parent_alias);
        assert!(parent_alias
            .as_os_str()
            .to_string_lossy()
            .split(['/', '\\'])
            .any(|part| part == ".."));
        for path in [
            PathBuf::from("relative.json"),
            parent_alias,
            root.0.join("NUL.json"),
            root.0.join("COM¹.json"),
        ] {
            assert_eq!(
                ordinary_path(&path),
                Err(SnapshotRefusal::UnsafePath),
                "literal refusal case: {path:?}"
            );
        }
    }
    #[cfg(target_os = "linux")]
    #[test]
    fn replaced_fifo_is_nonblocking_and_parent_alias_is_refused() {
        fn replace(path: &Path) {
            use std::os::unix::ffi::OsStrExt;
            fs::remove_file(path).unwrap();
            let path = std::ffi::CString::new(path.as_os_str().as_bytes()).unwrap();
            // SAFETY: path is a live NUL-terminated allocation with no interior
            // NUL; mkfifo creates only this test's owned fixture leaf.
            assert_eq!(unsafe { libc::mkfifo(path.as_ptr(), 0o600) }, 0);
        }
        let root = OwnedFixture::new();
        let path = root.0.join("package.json");
        fs::write(&path, b"{}").unwrap();
        let mut reader = NativeReader {
            before_content_open: Some(replace),
            ..NativeReader::default()
        };
        assert_eq!(
            reader.read_regular(&path, &root.0),
            Err(SnapshotRefusal::NonRegular)
        );
        let real = root.0.join("real");
        fs::create_dir(&real).unwrap();
        fs::write(real.join("package.json"), b"{}").unwrap();
        let alias = root.0.join("alias");
        std::os::unix::fs::symlink(&real, &alias).unwrap();
        assert_eq!(
            reader.read_regular(&alias.join("package.json"), &root.0),
            Err(SnapshotRefusal::UnsafePath)
        );
    }
}
