//! Read-only native identity evidence for explicit migration fixtures.
//!
//! Handles have metadata capability only: Linux uses `O_PATH`; Windows requests
//! `FILE_READ_ATTRIBUTES`. Opening never creates, reads content, or writes a
//! file. A retained handle identifies its original object after a rename or path
//! replacement. Directory-chain checks and before/after identity comparisons
//! remain point-in-time checks, not race-free handle-relative traversal. This
//! module grants no live source-save, settings, ACL, or replacement authority.

use serde::{Serialize, Serializer};
use std::fs::{self, File, Metadata, OpenOptions};
use std::io;
use std::path::{Component, Path, PathBuf};

/// Native identity is local to a machine/filesystem and an object's lifetime.
/// It is not a persistent global identifier or a content revision. Retaining
/// the owned handle prevents an unlinked inode/file ID being reused underneath
/// that handle. JSON uses decimal strings for 64-bit numbers without JS loss.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(tag = "backend", rename_all = "snake_case")]
pub enum NativeFileIdentity {
    Linux {
        #[serde(serialize_with = "decimal_u64")]
        device: u64,
        #[serde(serialize_with = "decimal_u64")]
        inode: u64,
    },
    Windows {
        #[serde(serialize_with = "decimal_u64")]
        volume_serial: u64,
        file_id: [u8; 16],
    },
}

fn decimal_u64<S: Serializer>(value: &u64, serializer: S) -> Result<S::Ok, S::Error> {
    serializer.serialize_str(&value.to_string())
}

#[derive(Clone, Copy, Debug)]
enum EntryKind {
    Directory,
    Source,
}

#[derive(Debug)]
struct MetadataHandle {
    file: File,
    kind: EntryKind,
}

impl MetadataHandle {
    fn identity(&self) -> io::Result<NativeFileIdentity> {
        validate_kind(&self.file.metadata()?, self.kind)?;
        native_identity(&self.file)
    }
}

/// Retains the original directory object while its pathname is revalidated.
/// Both constructor paths are trusted native inputs, never a generic IPC grant.
#[derive(Debug)]
pub struct FixtureIdentityScope {
    root: PathBuf,
    root_handle: MetadataHandle,
}

/// An owned, metadata-only source handle bound to its original fixture root and
/// portable leaf. It deliberately exposes no native handle or content access.
#[derive(Debug)]
pub struct FixtureSourceHandle {
    handle: MetadataHandle,
    root_identity: NativeFileIdentity,
    leaf: String,
}

impl FixtureSourceHandle {
    pub fn identity(&self) -> io::Result<NativeFileIdentity> {
        self.handle.identity()
    }
}

impl FixtureIdentityScope {
    /// Open one existing directory strictly below this application's explicit
    /// `audit/migration/scratch`. No current directory, HOME, settings, package
    /// store, protected cache, or live sketch is inferred or adopted.
    pub fn new(application_root: &Path, fixture_root: &Path) -> io::Result<Self> {
        validate_directory_chain(application_root)?;
        validate_directory_chain(fixture_root)?;
        let application_root = fs::canonicalize(application_root)?;
        let sandbox = application_root.join("audit/migration/scratch");
        validate_directory_chain(&sandbox)?;
        let sandbox = fs::canonicalize(sandbox)?;
        let root = fs::canonicalize(fixture_root)?;
        if root == sandbox || !root.starts_with(&sandbox) {
            return Err(denied(
                "Identity fixtures must be below audit/migration/scratch",
            ));
        }
        let root_handle = open_metadata(&root, EntryKind::Directory)?;
        let scope = Self { root, root_handle };
        // Catch observable replacement during construction; this is not a
        // claim to close the remaining pathname/ancestor TOCTOU windows.
        scope.verify_root()?;
        Ok(scope)
    }

    /// Identity of the retained object, even if its pathname was replaced.
    pub fn root_identity(&self) -> io::Result<NativeFileIdentity> {
        self.root_handle.identity()
    }

    pub fn verify_root(&self) -> io::Result<()> {
        validate_directory_chain(&self.root)?;
        let current = open_metadata(&self.root, EntryKind::Directory)?;
        if current.identity()? != self.root_identity()? {
            return Err(denied("Fixture root identity changed"));
        }
        Ok(())
    }

    /// Only an existing regular root-level sketch/header/source/note file can
    /// be opened. Intermediate directories, ADS, devices, links and reparses are
    /// refused. Metadata handles remain usable for identity after a rename.
    pub fn open_source(&self, leaf: &str) -> io::Result<FixtureSourceHandle> {
        validate_source_leaf(leaf)?;
        self.verify_root()?;
        let path = self.root.join(leaf);
        validate_kind(&fs::symlink_metadata(&path)?, EntryKind::Source)?;
        let handle = open_metadata(&path, EntryKind::Source)?;
        let identity = handle.identity()?;
        let current = open_metadata(&path, EntryKind::Source)?;
        if current.identity()? != identity {
            return Err(denied("Source path identity changed during observation"));
        }
        self.verify_root()?;
        Ok(FixtureSourceHandle {
            handle,
            root_identity: self.root_identity()?,
            leaf: leaf.into(),
        })
    }

    /// A missing/replaced source returns false; unsafe paths and changed roots
    /// return errors. Another scope cannot borrow this handle's original leaf.
    /// True is only an observation and does not authorize a subsequent write.
    pub fn source_still_matches(&self, source: &FixtureSourceHandle) -> io::Result<bool> {
        if source.root_identity != self.root_identity()? {
            return Err(denied("Source handle belongs to a different fixture root"));
        }
        let current = match self.open_source(&source.leaf) {
            Ok(current) => current,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                // NotFound may have come from an ancestor/root validation,
                // rather than the source leaf. Preserve that unsafe-root
                // distinction before classifying a missing source as false.
                self.verify_root()?;
                return Ok(false);
            }
            Err(error) => return Err(error),
        };
        Ok(current.identity()? == source.identity()?)
    }
}

fn open_metadata(path: &Path, kind: EntryKind) -> io::Result<MetadataHandle> {
    #[cfg(target_os = "linux")]
    let file = {
        use std::os::unix::fs::OpenOptionsExt;
        let directory = match kind {
            EntryKind::Directory => libc::O_DIRECTORY,
            EntryKind::Source => 0,
        };
        // O_PATH cannot read/write device contents or block opening a FIFO.
        // O_NOFOLLOW returns the symlink itself, rejected by handle metadata.
        OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_PATH | libc::O_NOFOLLOW | libc::O_CLOEXEC | directory)
            .open(path)?
    };
    #[cfg(windows)]
    let file = {
        use std::os::windows::fs::OpenOptionsExt;
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_READ_ATTRIBUTES,
            FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
        };
        OpenOptions::new()
            .access_mode(FILE_READ_ATTRIBUTES)
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
            .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
            .open(path)?
    };
    #[cfg(not(any(target_os = "linux", windows)))]
    {
        let _ = (path, kind);
        return Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "Native fixture identity is not implemented on this platform",
        ));
    }
    #[cfg(any(target_os = "linux", windows))]
    {
        let handle = MetadataHandle { file, kind };
        handle.identity()?;
        Ok(handle)
    }
}

pub(crate) fn native_identity(file: &File) -> io::Result<NativeFileIdentity> {
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::fs::MetadataExt;
        let metadata = file.metadata()?;
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
        // SAFETY: file owns a live handle; information is the correctly sized
        // writable FILE_ID_INFO buffer for FileIdInfo. No handle is transferred.
        let result = unsafe {
            GetFileInformationByHandleEx(
                file.as_raw_handle(),
                FileIdInfo,
                (&mut information as *mut FILE_ID_INFO).cast(),
                std::mem::size_of::<FILE_ID_INFO>() as u32,
            )
        };
        if result == 0 {
            // Filesystems/drivers may not support FileIdInfo. Preserve failure;
            // never manufacture identity from paths, size, times or a fallback
            // truncated file index.
            return Err(io::Error::last_os_error());
        }
        Ok(NativeFileIdentity::Windows {
            volume_serial: information.VolumeSerialNumber,
            file_id: information.FileId.Identifier,
        })
    }
    #[cfg(not(any(target_os = "linux", windows)))]
    {
        let _ = file;
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "Native fixture identity is unavailable",
        ))
    }
}

/// Observe whether the retained native file has exactly one filesystem link.
/// This does not prevent another link being created after the observation.
pub(crate) fn single_link(file: &File) -> io::Result<bool> {
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::fs::MetadataExt;
        Ok(file.metadata()?.nlink() == 1)
    }
    #[cfg(windows)]
    {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::Storage::FileSystem::{
            GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
        };
        let mut information = BY_HANDLE_FILE_INFORMATION::default();
        // SAFETY: file owns a live handle; information is a correctly aligned,
        // writable BY_HANDLE_FILE_INFORMATION buffer. No handle is transferred.
        let result = unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut information) };
        if result == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(information.nNumberOfLinks == 1)
    }
    #[cfg(not(any(target_os = "linux", windows)))]
    {
        let _ = file;
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "Native file link count is unavailable",
        ))
    }
}

fn validate_kind(metadata: &Metadata, kind: EntryKind) -> io::Result<()> {
    if is_alias(metadata)
        || !match kind {
            EntryKind::Directory => metadata.is_dir(),
            EntryKind::Source => metadata.is_file(),
        }
    {
        return Err(denied(
            "Fixture entry kind is invalid or is a symlink/reparse alias",
        ));
    }
    Ok(())
}

fn is_alias(metadata: &Metadata) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT;
        metadata.file_type().is_symlink()
            || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
    }
    #[cfg(not(windows))]
    {
        metadata.file_type().is_symlink()
    }
}

fn validate_directory_chain(path: &Path) -> io::Result<()> {
    if !path.is_absolute()
        || path
            .components()
            .any(|part| matches!(part, Component::CurDir | Component::ParentDir))
    {
        return Err(denied(
            "Fixture roots require an absolute path without parent aliases",
        ));
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        use std::path::Prefix;
        if windows_has_parent_segment(path.as_os_str().encode_wide()) {
            return Err(denied("Fixture root parent aliases are prohibited"));
        }
        if !matches!(path.components().next(), Some(Component::Prefix(prefix))
            if matches!(prefix.kind(), Prefix::Disk(_) | Prefix::VerbatimDisk(_) | Prefix::UNC(_, _) | Prefix::VerbatimUNC(_, _)))
        {
            return Err(denied("Fixture roots require an ordinary disk or UNC path"));
        }
    }
    let mut current = PathBuf::new();
    for component in path.components() {
        current.push(component);
        if matches!(component, Component::Prefix(_)) {
            continue;
        }
        validate_kind(&fs::symlink_metadata(&current)?, EntryKind::Directory)?;
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

fn validate_source_leaf(leaf: &str) -> io::Result<()> {
    let extension = leaf.rsplit('.').next().unwrap_or("").to_ascii_lowercase();
    if leaf.is_empty()
        || leaf.len() > 255
        || leaf.starts_with(['.', ' '])
        || leaf.ends_with(['.', ' '])
        || !leaf
            .chars()
            .all(|character| character.is_alphanumeric() || " ._-".contains(character))
        || !matches!(
            extension.as_str(),
            "ino" | "c" | "cpp" | "h" | "hpp" | "txt"
        )
        || mcu_core::source_names::is_windows_reserved_device_name(leaf)
    {
        return Err(denied(
            "Expected one portable root sketch/source/header/note filename",
        ));
    }
    Ok(())
}

fn denied(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, message)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(0);

    struct Fixture {
        app: PathBuf,
        parent: PathBuf,
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
                let parent = sandbox.join(format!(
                    "native-identity-{}-{}",
                    std::process::id(),
                    NEXT.fetch_add(1, Ordering::Relaxed)
                ));
                match fs::create_dir(&parent) {
                    Ok(()) => {
                        let root = parent.join("project");
                        fs::create_dir(&root).unwrap();
                        return Self { app, parent, root };
                    }
                    Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
                    Err(error) => panic!("Cannot reserve owned identity fixture: {error}"),
                }
            }
            panic!("Cannot reserve a unique identity fixture");
        }

        fn scope(&self) -> FixtureIdentityScope {
            FixtureIdentityScope::new(&self.app, &self.root).unwrap()
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            fs::remove_dir_all(&self.parent).unwrap();
        }
    }

    #[test]
    fn original_handle_survives_rename_and_same_size_path_replacement_is_detected() {
        let fixture = Fixture::new();
        let path = fixture.root.join("Sketch.ino");
        fs::write(&path, b"first").unwrap();
        let scope = fixture.scope();
        let source = scope.open_source("Sketch.ino").unwrap();
        let identity = source.identity().unwrap();
        assert_eq!(
            scope.open_source("Sketch.ino").unwrap().identity().unwrap(),
            identity
        );
        assert!(scope.source_still_matches(&source).unwrap());
        fs::rename(&path, fixture.root.join("renamed.ino")).unwrap();
        assert_eq!(source.identity().unwrap(), identity);
        assert!(!scope.source_still_matches(&source).unwrap());
        fs::write(&path, b"other").unwrap();
        assert_ne!(
            scope.open_source("Sketch.ino").unwrap().identity().unwrap(),
            identity
        );
        assert_eq!(source.identity().unwrap(), identity);
        assert!(!scope.source_still_matches(&source).unwrap());
    }

    #[test]
    fn directory_handle_survives_rename_and_refuses_a_replaced_root_path() {
        let fixture = Fixture::new();
        fs::write(fixture.root.join("Sketch.ino"), b"original").unwrap();
        let scope = fixture.scope();
        let original = scope.root_identity().unwrap();
        fs::rename(&fixture.root, fixture.parent.join("moved")).unwrap();
        fs::create_dir(&fixture.root).unwrap();
        fs::write(fixture.root.join("Sketch.ino"), b"replacement").unwrap();
        assert_eq!(scope.root_identity().unwrap(), original);
        assert_eq!(
            scope.verify_root().unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
        assert_eq!(
            scope.open_source("Sketch.ino").unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
        assert_ne!(fixture.scope().root_identity().unwrap(), original);
    }

    #[test]
    fn missing_root_is_an_error_rather_than_a_missing_source_observation() {
        let fixture = Fixture::new();
        fs::write(fixture.root.join("Sketch.ino"), b"original").unwrap();
        let scope = fixture.scope();
        let source = scope.open_source("Sketch.ino").unwrap();
        let identity = source.identity().unwrap();
        // Windows may refuse a directory move while a descendant remains
        // open, even with delete sharing. Move the retained file out first;
        // missing-root classification must still fail before a missing leaf.
        fs::rename(
            fixture.root.join("Sketch.ino"),
            fixture.parent.join("retained.ino"),
        )
        .unwrap();
        fs::rename(&fixture.root, fixture.parent.join("moved")).unwrap();
        assert_eq!(
            scope.source_still_matches(&source).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
        assert_eq!(source.identity().unwrap(), identity);
    }

    #[test]
    fn hardlink_alias_is_explicitly_the_same_object_and_scope_cannot_be_borrowed() {
        let first = Fixture::new();
        let second = Fixture::new();
        fs::write(first.root.join("Sketch.ino"), b"same object").unwrap();
        fs::hard_link(first.root.join("Sketch.ino"), first.root.join("Alias.cpp")).unwrap();
        fs::hard_link(
            first.root.join("Sketch.ino"),
            second.root.join("Sketch.ino"),
        )
        .unwrap();
        let scope = first.scope();
        let source = scope.open_source("Sketch.ino").unwrap();
        assert_eq!(
            source.identity().unwrap(),
            scope.open_source("Alias.cpp").unwrap().identity().unwrap()
        );
        assert_eq!(
            second
                .scope()
                .source_still_matches(&source)
                .unwrap_err()
                .kind(),
            io::ErrorKind::PermissionDenied
        );
    }

    #[cfg(any(target_os = "linux", windows))]
    #[test]
    fn single_link_observes_hardlink_changes_on_the_retained_file() {
        let fixture = Fixture::new();
        let path = fixture.root.join("Sketch.ino");
        let alias = fixture.root.join("Alias.cpp");
        fs::write(&path, b"owned link-count fixture").unwrap();
        let handle = open_metadata(&path, EntryKind::Source).unwrap();
        assert!(single_link(&handle.file).unwrap());
        fs::hard_link(&path, &alias).unwrap();
        assert!(!single_link(&handle.file).unwrap());
        let alias_handle = open_metadata(&alias, EntryKind::Source).unwrap();
        assert!(!single_link(&alias_handle.file).unwrap());
        drop(alias_handle);
        fs::remove_file(&alias).unwrap();
        assert!(single_link(&handle.file).unwrap());
    }

    #[test]
    fn wrong_kinds_escaping_roots_and_nonportable_source_names_are_refused() {
        let fixture = Fixture::new();
        assert!(FixtureIdentityScope::new(&fixture.app, &fixture.app.join("desktop")).is_err());
        assert!(FixtureIdentityScope::new(
            &fixture.app,
            &fixture.app.join("audit/migration/scratch")
        )
        .is_err());
        assert!(FixtureIdentityScope::new(&fixture.app, &fixture.root.join("..")).is_err());
        fs::create_dir(fixture.root.join("directory.ino")).unwrap();
        let scope = fixture.scope();
        assert_eq!(
            scope.open_source("directory.ino").unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
        for leaf in [
            "../Sketch.ino",
            "folder/Sketch.ino",
            "folder\\Sketch.ino",
            "Sketch.ino:stream",
            "CON.cpp",
            "LPT1.h",
            "note.json",
            ".hidden.ino",
            "Sketch.ino ",
            "Sketch.ino.",
            "",
            "Sketch\0.ino",
        ] {
            assert_eq!(
                scope.open_source(leaf).unwrap_err().kind(),
                io::ErrorKind::PermissionDenied,
                "{leaf:?}"
            );
        }
        for leaf in ["Sketch.ino", "File.CPP", "café 日本語.hpp", "NOTE.txt"] {
            fs::write(fixture.root.join(leaf), b"valid").unwrap();
            scope.open_source(leaf).unwrap();
        }
    }

    #[test]
    fn portable_leaf_validation_refuses_windows_superscript_devices() {
        for leaf in [
            "COM¹.ino",
            "com².cpp",
            "CoM³.extra.h",
            "LPT¹.hpp",
            "lpt².c",
            "LpT³.txt",
        ] {
            assert_eq!(
                validate_source_leaf(leaf).unwrap_err().kind(),
                io::ErrorKind::PermissionDenied,
                "{leaf:?}"
            );
        }
        for leaf in [
            "COM10.ino",
            "LPT0.hpp",
            "COM¹project.cpp",
            "café 日本語.hpp",
        ] {
            validate_source_leaf(leaf).unwrap();
        }
    }

    #[test]
    fn portable_leaf_validation_refuses_spaced_windows_reserved_stems() {
        for leaf in [
            "con .ino",
            "PRN  .cpp",
            "Aux .extra.h",
            "nul   .txt",
            "COM1 .ino",
            "lpt9  .hpp",
            "COM¹ .c",
            "LPT³   .cpp",
        ] {
            assert_eq!(
                validate_source_leaf(leaf).unwrap_err().kind(),
                io::ErrorKind::PermissionDenied,
                "{leaf:?}"
            );
        }
        for leaf in ["console .ino", "COM10 .hpp", "project name.cpp"] {
            validate_source_leaf(leaf).unwrap();
        }
    }

    #[test]
    fn identity_json_retains_all_native_integer_bits() {
        let identity = NativeFileIdentity::Linux {
            device: u64::MAX,
            inode: u64::MAX - 1,
        };
        let value = serde_json::to_value(identity).unwrap();
        assert_eq!(value["device"], "18446744073709551615");
        assert_eq!(value["inode"], "18446744073709551614");
        let identity = NativeFileIdentity::Windows {
            volume_serial: u64::MAX,
            file_id: [255; 16],
        };
        let value = serde_json::to_value(identity).unwrap();
        assert_eq!(value["volume_serial"], "18446744073709551615");
        assert_eq!(value["file_id"].as_array().unwrap().len(), 16);
    }

    #[test]
    fn windows_verbatim_parent_syntax_is_rejected_without_normalizing_unicode_paths() {
        for path in [
            r"C:\fixture\..\other",
            r"C:/fixture/../other",
            r"\\?\C:\fixture/../other",
            r"\\?\UNC\server\share\fixture/..\other",
        ] {
            assert!(windows_has_parent_segment(path.encode_utf16()), "{path}");
        }
        for path in [
            r"\\?\C:\café 日本語\suffix..",
            r"\\server\share\dots...\project",
        ] {
            assert!(!windows_has_parent_segment(path.encode_utf16()), "{path}");
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn linux_metadata_capability_cannot_read_or_write_contents() {
        use std::io::{Read, Write};
        let fixture = Fixture::new();
        fs::write(fixture.root.join("Sketch.ino"), b"protected content").unwrap();
        let source = fixture.scope().open_source("Sketch.ino").unwrap();
        let mut file = &source.handle.file;
        assert_eq!(
            file.read(&mut [0u8; 1]).unwrap_err().raw_os_error(),
            Some(libc::EBADF)
        );
        assert_eq!(
            file.write(b"x").unwrap_err().raw_os_error(),
            Some(libc::EBADF)
        );
        assert_eq!(
            fs::read(fixture.root.join("Sketch.ino")).unwrap(),
            b"protected content"
        );
    }

    #[cfg(unix)]
    #[test]
    fn unix_symlink_leaf_and_directory_aliases_are_refused() {
        use std::os::unix::fs::symlink;
        let fixture = Fixture::new();
        fs::write(fixture.root.join("actual.ino"), b"original").unwrap();
        symlink("actual.ino", fixture.root.join("Sketch.ino")).unwrap();
        assert!(fixture.scope().open_source("Sketch.ino").is_err());
        // Test the native open itself, bypassing the preliminary path metadata.
        assert!(open_metadata(&fixture.root.join("Sketch.ino"), EntryKind::Source).is_err());
        symlink("project", fixture.parent.join("alias")).unwrap();
        assert!(FixtureIdentityScope::new(&fixture.app, &fixture.parent.join("alias")).is_err());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn linux_native_metadata_open_refuses_fifo_without_data_open_or_blocking() {
        use std::ffi::CString;
        use std::os::unix::ffi::OsStrExt;
        let fixture = Fixture::new();
        let path = fixture.root.join("device.ino");
        let native_path = CString::new(path.as_os_str().as_bytes()).unwrap();
        // SAFETY: native_path is a live NUL-terminated owned fixture path;
        // mkfifo affects only this test's unique sandbox directory.
        assert_eq!(unsafe { libc::mkfifo(native_path.as_ptr(), 0o600) }, 0);
        // Bypass the preliminary pathname check to exercise a kind replaced
        // immediately before open. O_PATH obtains metadata without waiting for
        // a writer or opening the FIFO for data.
        assert_eq!(
            open_metadata(&path, EntryKind::Source).unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
    }

    #[cfg(windows)]
    #[test]
    fn windows_metadata_capability_cannot_read_or_write_contents() {
        use std::io::{Read, Write};
        let fixture = Fixture::new();
        fs::write(fixture.root.join("Sketch.ino"), b"protected content").unwrap();
        let source = fixture.scope().open_source("Sketch.ino").unwrap();
        assert!(matches!(
            source.identity().unwrap(),
            NativeFileIdentity::Windows { .. }
        ));
        let mut file = &source.handle.file;
        assert!(file.read(&mut [0u8; 1]).is_err());
        assert!(file.write(b"x").is_err());
        assert_eq!(
            fs::read(fixture.root.join("Sketch.ino")).unwrap(),
            b"protected content"
        );
    }

    #[cfg(windows)]
    #[test]
    fn windows_parent_and_device_namespace_refusals_precede_metadata() {
        for path in [
            r"\\?\C:\mcu-nonexistent-fixture/../other",
            r"\\.\C:\mcu-nonexistent-fixture",
            r"\\?\GLOBALROOT\Device\HarddiskVolume1\mcu-nonexistent-fixture",
        ] {
            assert_eq!(
                validate_directory_chain(Path::new(path))
                    .unwrap_err()
                    .kind(),
                io::ErrorKind::PermissionDenied,
                "{path}"
            );
        }
    }

    #[cfg(windows)]
    #[test]
    fn windows_junction_root_ancestor_and_native_directory_opens_are_refused() {
        let fixture = Fixture::new();
        fs::create_dir(fixture.root.join("nested")).unwrap();
        let scope = fixture.scope();
        let original = scope.root_identity().unwrap();
        let alias = fixture.parent.join("junction alias");
        let create_junction = |link: &Path, target: &Path| {
            use std::os::windows::ffi::OsStrExt;
            // PathBuf joins may retain forward slashes in a component string;
            // cmd/mklink interprets those as switches. Rebuild native segments.
            let link: PathBuf = link.components().collect();
            let target: PathBuf = target.components().collect();
            for path in [&link, &target] {
                // cmd expands percent variables even in quoted arguments.
                // Refuse shell-sensitive checkout names before invoking it.
                assert!(
                    !path.as_os_str().encode_wide().any(|unit| {
                        unit < 32 || "&|<>^%!\"()".encode_utf16().any(|bad| unit == bad)
                    }),
                    "Fixture junction paths must not contain cmd metacharacters"
                );
            }
            // Both arguments are native, uniquely owned fixture paths. /d
            // disables AutoRun; a junction needs no symbolic-link privilege.
            let output = std::process::Command::new("cmd.exe")
                .args(["/d", "/c", "mklink", "/J"])
                .arg(link)
                .arg(target)
                .output()
                .unwrap();
            assert!(
                output.status.success(),
                "Cannot create fixture junction: {} {}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            );
        };
        create_junction(&alias, &fixture.root);
        assert_eq!(
            FixtureIdentityScope::new(&fixture.app, &alias)
                .unwrap_err()
                .kind(),
            io::ErrorKind::PermissionDenied
        );
        assert_eq!(
            FixtureIdentityScope::new(&fixture.app, &alias.join("nested"))
                .unwrap_err()
                .kind(),
            io::ErrorKind::PermissionDenied
        );
        // Bypass the pathname chain: OPEN_REPARSE_POINT must retain the
        // junction itself so handle metadata refuses it before any identity.
        assert_eq!(
            open_metadata(&alias, EntryKind::Directory)
                .unwrap_err()
                .kind(),
            io::ErrorKind::PermissionDenied
        );
        fs::remove_dir(&alias).unwrap();

        let moved = fixture.parent.join("moved");
        fs::rename(&fixture.root, &moved).unwrap();
        create_junction(&fixture.root, &moved);
        assert_eq!(scope.root_identity().unwrap(), original);
        assert_eq!(
            scope.verify_root().unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
        // Explicitly unlink the junction before Fixture removes owned data.
        fs::remove_dir(&fixture.root).unwrap();
    }
}
