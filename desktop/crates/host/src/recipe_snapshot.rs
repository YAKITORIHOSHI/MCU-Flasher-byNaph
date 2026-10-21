//! Fixture-only native recipe observations; no tool or package-write authority.
use crate::file_identity::{native_identity, NativeFileIdentity};
use mcu_core::board_compatibility::{
    CompatibilityDecision, FixtureCompatibilityScope, NativeHost, NativeRecipeSelection,
    SnapshotReader, SnapshotRefusal, MAX_METADATA_BYTES,
};
use std::fs::{self, File, Metadata, OpenOptions};
use std::io::Read;
use std::path::{Component, Path, PathBuf};
use std::sync::Arc;

fn identity(file: &File) -> Result<NativeFileIdentity, SnapshotRefusal> {
    native_identity(file).map_err(|_| SnapshotRefusal::Io)
}

#[derive(Default)]
pub struct NativeReader {
    pub attempts: usize,
    pub bytes: usize,
    recording: bool,
    directories: Vec<PinnedDirectory>,
    files: Vec<PinnedFile>,
    #[cfg(test)]
    before_content_open: Option<fn(&Path)>,
}

impl SnapshotReader for NativeReader {
    fn directory(&mut self, path: &Path) -> Result<PathBuf, SnapshotRefusal> {
        let observed = pin_chain(path)?;
        let canonical = path.canonicalize().map_err(|_| SnapshotRefusal::Io)?;
        directory_chain(&canonical)?;
        verify_directories(&observed)?;
        if self.recording {
            self.retain_directories(observed)?;
        }
        Ok(canonical)
    }

    fn read_regular(&mut self, path: &Path, boundary: &Path) -> Result<Vec<u8>, SnapshotRefusal> {
        self.attempts += 1;
        let boundary = self.directory(boundary)?;
        checked_file(path, &boundary)?;
        let parents = pin_chain(path.parent().ok_or(SnapshotRefusal::UnsafePath)?)?;
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
        verify_directories(&parents)?;
        if self.recording {
            self.retain_directories(parents)?;
            if self.attempts > MAX_GENERATION_FILES || self.bytes > MAX_GENERATION_BYTES {
                return Err(SnapshotRefusal::TooLarge);
            }
            let digest = mcu_core::source_digest(&raw).0;
            if let Some(prior) = self.files.iter().find(|entry| entry.path == path) {
                if prior.identity != original || prior.digest != digest {
                    return Err(SnapshotRefusal::Changed);
                }
            } else {
                self.files.push(PinnedFile {
                    path: path.into(),
                    handle: before,
                    identity: original,
                    digest,
                    bytes: raw.len(),
                });
            }
        }
        // Observations cannot freeze content or close final check/use races.
        Ok(raw)
    }
}

const MAX_GENERATION_FILES: usize = 8;
const MAX_GENERATION_DIRECTORIES: usize = 48;
const MAX_GENERATION_BYTES: usize = MAX_GENERATION_FILES * MAX_METADATA_BYTES;

pub(crate) struct PinnedDirectory {
    path: PathBuf,
    handle: File,
    identity: NativeFileIdentity,
}

struct PinnedFile {
    path: PathBuf,
    handle: File,
    identity: NativeFileIdentity,
    digest: String,
    bytes: usize,
}

impl NativeReader {
    fn retain_directories(
        &mut self,
        observed: Vec<PinnedDirectory>,
    ) -> Result<(), SnapshotRefusal> {
        for directory in observed {
            if let Some(prior) = self
                .directories
                .iter()
                .find(|entry| entry.path == directory.path)
            {
                if prior.identity != directory.identity {
                    return Err(SnapshotRefusal::Changed);
                }
            } else {
                if self.directories.len() >= MAX_GENERATION_DIRECTORIES {
                    return Err(SnapshotRefusal::TooLarge);
                }
                self.directories.push(directory);
            }
        }
        verify_directories(&self.directories)
    }
}

pub(crate) fn pin_chain(path: &Path) -> Result<Vec<PinnedDirectory>, SnapshotRefusal> {
    directory_chain(path)?;
    let mut path_so_far = PathBuf::new();
    let mut directories = Vec::new();
    for part in path.components() {
        path_so_far.push(part);
        if matches!(part, Component::Prefix(_)) {
            continue;
        }
        if directories.len() >= MAX_GENERATION_DIRECTORIES {
            return Err(SnapshotRefusal::TooLarge);
        }
        let handle = open_native(&path_so_far, true)?;
        let metadata = handle.metadata().map_err(|_| SnapshotRefusal::Io)?;
        if alias(&metadata) || !metadata.is_dir() {
            return Err(SnapshotRefusal::UnsafePath);
        }
        let selected = identity(&handle)?;
        directories.push(PinnedDirectory {
            path: path_so_far.clone(),
            handle,
            identity: selected,
        });
    }
    verify_directories(&directories)?;
    Ok(directories)
}

pub(crate) fn verify_directories(directories: &[PinnedDirectory]) -> Result<(), SnapshotRefusal> {
    for directory in directories {
        let current = open_native(&directory.path, true)?;
        let metadata = current.metadata().map_err(|_| SnapshotRefusal::Io)?;
        if alias(&metadata) || !metadata.is_dir() {
            return Err(SnapshotRefusal::UnsafePath);
        }
        if identity(&current)? != directory.identity
            || identity(&directory.handle)? != directory.identity
        {
            return Err(SnapshotRefusal::Changed);
        }
    }
    Ok(())
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

pub(crate) fn alias(metadata: &Metadata) -> bool {
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

pub(crate) fn open_native(path: &Path, attributes_only: bool) -> Result<File, SnapshotRefusal> {
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
            FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_READ_ATTRIBUTES,
            FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
        };
        let mut options = OpenOptions::new();
        if attributes_only {
            options.access_mode(FILE_READ_ATTRIBUTES);
        } else {
            options.read(true);
        }
        options
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
            .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
            .open(path)
            .map_err(|_| SnapshotRefusal::Io)
    }
    #[cfg(not(any(target_os = "linux", windows)))]
    {
        let _ = (path, attributes_only);
        Err(SnapshotRefusal::UnsupportedHost)
    }
}

/// A refusal is never an empty recipe decision or permission to run a tool.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GenerationRefusal {
    Snapshot(SnapshotRefusal),
    NoRecipe,
    StaleGeneration,
    ForeignScope,
    SelectionChanged,
    DecisionChanged,
}

/// Existing native fixture authority. No root, receipt, native host, or decision
/// is deserialized. At most 48 directory + 8 file handles are retained per
/// observation; the authority retains at most 48 additional prefix handles.
/// Replay and individual chain checks have the same finite bounds. Calls are
/// synchronous worker operations; OS filesystem stalls are not cancellable here.
pub struct FixtureRecipeAuthority {
    root: PathBuf,
    scope: FixtureCompatibilityScope,
    directories: Vec<PinnedDirectory>,
    owner: Arc<()>,
    next: u64,
    current: Option<u64>,
}

#[derive(PartialEq, Eq)]
struct Selection {
    backend: Option<String>,
    platform: String,
    board: String,
    framework: String,
    manifest: PathBuf,
}
impl Selection {
    fn observed(selection: &NativeRecipeSelection<'_>) -> Result<Self, GenerationRefusal> {
        if [
            selection.backend.unwrap_or(""),
            selection.platform,
            selection.board,
            selection.framework,
        ]
        .iter()
        .any(|value| value.len() > 1024)
            || selection.manifest.as_os_str().len() > 4096
        {
            return Err(GenerationRefusal::Snapshot(SnapshotRefusal::TooLarge));
        }
        Ok(Self {
            backend: selection.backend.map(str::to_owned),
            platform: selection.platform.into(),
            board: selection.board.into(),
            framework: selection.framework.into(),
            manifest: selection.manifest.into(),
        })
    }
}

/// Opaque, process-local native observation. Retained handles prevent identity
/// reuse while it lives. It is neither serializable nor a package/build lease.
/// Trusted callers release each receipt's handles by dropping it; invalidation
/// revokes its generation but does not close handles owned by that caller. The
/// fixture probe retains only one receipt. This is not a global handle budget.
/// Equality after revalidation does not close active ABA or final check/use races
/// and cannot authorize actual tool execution or writes.
pub struct RecipeGeneration {
    decision: CompatibilityDecision,
    generation: u64,
    owner: Arc<()>,
    selection: Selection,
    host: NativeHost,
    snapshot: NativeReader,
}
impl RecipeGeneration {
    pub fn decision(&self) -> &CompatibilityDecision {
        &self.decision
    }
    pub fn generation(&self) -> u64 {
        self.generation
    }
    pub fn files(&self) -> usize {
        self.snapshot.files.len()
    }
    pub fn directories(&self) -> usize {
        self.snapshot.directories.len()
    }
    /// Bytes in the unique observed raw input files, not cumulative replay I/O.
    pub fn bytes_read(&self) -> usize {
        self.snapshot.files.iter().map(|file| file.bytes).sum()
    }
}
impl FixtureRecipeAuthority {
    pub fn new(checkout: &Path, root: &Path) -> Result<Self, GenerationRefusal> {
        let checkout_chain = pin_chain(checkout)?;
        let directories = pin_chain(root)?;
        let scope = FixtureCompatibilityScope::new(checkout, root)?;
        let root = root.canonicalize().map_err(|_| SnapshotRefusal::Io)?;
        verify_directories(&checkout_chain)?;
        verify_directories(&directories)?;
        Ok(Self {
            root,
            scope,
            directories,
            owner: Arc::new(()),
            next: 0,
            current: None,
        })
    }

    /// A failed or partial capture invalidates the previous generation. Positive
    /// recipes alone receive a receipt; valid empty decisions return NoRecipe.
    pub fn capture(
        &mut self,
        selection: &NativeRecipeSelection<'_>,
        host: NativeHost,
    ) -> Result<RecipeGeneration, GenerationRefusal> {
        self.current = None;
        self.next = self.next.checked_add(1).ok_or(SnapshotRefusal::TooLarge)?;
        if self.next > 64 {
            return Err(SnapshotRefusal::TooLarge.into());
        }
        let selected = Selection::observed(selection)?;
        verify_directories(&self.directories)?;
        let mut snapshot = NativeReader {
            recording: true,
            ..NativeReader::default()
        };
        snapshot.directory(&self.root)?;
        let decision = self.scope.options(selection, host, &mut snapshot)?;
        if decision.rule.is_none() || decision.options.is_empty() {
            return Err(GenerationRefusal::NoRecipe);
        }
        snapshot.verify_files()?;
        verify_directories(&self.directories)?;
        self.current = Some(self.next);
        Ok(RecipeGeneration {
            decision,
            generation: self.next,
            owner: self.owner.clone(),
            selection: selected,
            host,
            snapshot,
        })
    }

    /// Recompute the bounded policy from the same trusted authority, compare
    /// every observed native identity and raw digest, then reread all inputs.
    /// Every refusal invalidates this authority's current generation.
    pub fn revalidate(
        &mut self,
        receipt: &RecipeGeneration,
        selection: &NativeRecipeSelection<'_>,
        host: NativeHost,
        decision: &CompatibilityDecision,
    ) -> Result<(), GenerationRefusal> {
        let result = (|| {
            if !Arc::ptr_eq(&self.owner, &receipt.owner) {
                return Err(GenerationRefusal::ForeignScope);
            }
            if self.current != Some(receipt.generation) {
                return Err(GenerationRefusal::StaleGeneration);
            }
            if host != receipt.host || Selection::observed(selection)? != receipt.selection {
                return Err(GenerationRefusal::SelectionChanged);
            }
            if decision != &receipt.decision {
                return Err(GenerationRefusal::DecisionChanged);
            }
            verify_directories(&self.directories)?;
            verify_directories(&receipt.snapshot.directories)?;
            let mut replay = NativeReader {
                recording: true,
                ..NativeReader::default()
            };
            replay.directory(&self.root)?;
            let current = self.scope.options(selection, host, &mut replay)?;
            receipt.snapshot.compare(&replay)?;
            if current != receipt.decision {
                return Err(GenerationRefusal::DecisionChanged);
            }
            replay.verify_files()?;
            verify_directories(&receipt.snapshot.directories)?;
            verify_directories(&self.directories)?;
            Ok(())
        })();
        if result.is_err() {
            self.current = None;
        }
        result
    }
    pub fn clear(&mut self) {
        self.current = None;
    }
}

impl From<SnapshotRefusal> for GenerationRefusal {
    fn from(value: SnapshotRefusal) -> Self {
        Self::Snapshot(value)
    }
}
impl GenerationRefusal {
    pub fn name(self) -> &'static str {
        match self {
            Self::NoRecipe => "NoRecipe",
            Self::StaleGeneration => "StaleGeneration",
            Self::ForeignScope => "ForeignScope",
            Self::SelectionChanged => "SelectionChanged",
            Self::DecisionChanged => "DecisionChanged",
            Self::Snapshot(refusal) => match refusal {
                SnapshotRefusal::InvalidFixture => "InvalidFixture",
                SnapshotRefusal::UnsafePath => "UnsafePath",
                SnapshotRefusal::NonRegular => "NonRegular",
                SnapshotRefusal::Changed => "Changed",
                SnapshotRefusal::TooLarge => "TooLarge",
                SnapshotRefusal::InvalidMetadata => "InvalidMetadata",
                SnapshotRefusal::Io => "Io",
                SnapshotRefusal::UnsupportedHost => "UnsupportedHost",
            },
        }
    }
}

impl NativeReader {
    fn verify_files(&self) -> Result<(), SnapshotRefusal> {
        verify_directories(&self.directories)?;
        let mut reader = Self::default();
        for observed in &self.files {
            let current = open_native(&observed.path, true)?;
            regular(&current.metadata().map_err(|_| SnapshotRefusal::Io)?)?;
            if identity(&current)? != observed.identity
                || identity(&observed.handle)? != observed.identity
            {
                return Err(SnapshotRefusal::Changed);
            }
            let bytes = reader.read_regular(
                &observed.path,
                observed.path.parent().ok_or(SnapshotRefusal::UnsafePath)?,
            )?;
            if bytes.len() != observed.bytes || mcu_core::source_digest(&bytes).0 != observed.digest
            {
                return Err(SnapshotRefusal::Changed);
            }
            if identity(&open_native(&observed.path, true)?)? != observed.identity {
                return Err(SnapshotRefusal::Changed);
            }
        }
        verify_directories(&self.directories)
    }
    fn compare(&self, replay: &Self) -> Result<(), SnapshotRefusal> {
        if self.files.len() != replay.files.len()
            || self.directories.len() != replay.directories.len()
        {
            return Err(SnapshotRefusal::Changed);
        }
        for original in &self.files {
            let current = replay
                .files
                .iter()
                .find(|entry| entry.path == original.path)
                .ok_or(SnapshotRefusal::Changed)?;
            if original.identity != current.identity
                || original.digest != current.digest
                || original.bytes != current.bytes
                || identity(&original.handle)? != original.identity
            {
                return Err(SnapshotRefusal::Changed);
            }
        }
        for original in &self.directories {
            let current = replay
                .directories
                .iter()
                .find(|entry| entry.path == original.path)
                .ok_or(SnapshotRefusal::Changed)?;
            if original.identity != current.identity {
                return Err(SnapshotRefusal::Changed);
            }
        }
        Ok(())
    }
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
    fn replaced_parent_with_same_retained_leaf_is_refused() {
        fn replace(path: &Path) {
            let parent = path.parent().unwrap();
            let old = parent.with_file_name("retained-parent");
            let held = parent.with_file_name("retained-file.json");
            let original = open_native(path, true).unwrap();
            let before = identity(&original).unwrap();
            // Windows can refuse directory renames with open descendants.
            // Move the identical leaf out before replacing its empty ancestor.
            fs::rename(path, &held).unwrap();
            fs::rename(parent, &old).unwrap();
            fs::create_dir(parent).unwrap();
            fs::rename(&held, path).unwrap();
            assert_eq!(identity(&open_native(path, true).unwrap()).unwrap(), before);
        }
        let root = OwnedFixture::new();
        let parent = root.0.join("boards");
        fs::create_dir(&parent).unwrap();
        let path = parent.join("package.json");
        fs::write(&path, b"{}").unwrap();
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

    // Exact upstream fixture bytes; CRLF and final bytes participate in SHA-256.
    const BOARD_BYTES: &[u8] = b"{\r\n  \"build\": {\r\n    \"arduino\":{\r\n      \"ldscript\": \"esp32_out.ld\"\r\n    },\r\n    \"core\": \"esp32\",\r\n    \"extra_flags\": \"-DARDUINO_ONEHORSE_ESP32_DEV\",\r\n    \"f_cpu\": \"240000000L\",\r\n    \"f_flash\": \"40000000L\",\r\n    \"flash_mode\": \"dout\",\r\n    \"mcu\": \"esp32\",\r\n    \"variant\": \"onehorse32dev\"\r\n  },\r\n  \"connectivity\": [\r\n    \"wifi\",\r\n    \"bluetooth\",\r\n    \"ethernet\",\r\n    \"can\"\r\n  ],\r\n  \"frameworks\": [\r\n    \"arduino\",\r\n    \"espidf\"\r\n  ],\r\n  \"name\": \"Onehorse ESP32 Dev Module\",\r\n  \"upload\": {\r\n    \"flash_size\": \"4MB\",\r\n    \"maximum_ram_size\": 327680,\r\n    \"maximum_size\": 4194304,\r\n    \"require_upload_port\": true,\r\n    \"speed\": 460800\r\n  },\r\n  \"url\": \"https://www.tindie.com/products/onehorse/esp32-development-board/\",\r\n  \"vendor\": \"Onehorse\"\r\n}\r\n";
    fn checkout() -> &'static Path {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .ancestors()
            .nth(3)
            .unwrap()
    }
    fn seed(root: &Path) -> PathBuf {
        let platform = root.join("core/platforms/espressif32");
        fs::create_dir_all(platform.join("boards")).unwrap();
        let board = platform.join("boards/onehorse32dev.json");
        fs::write(&board, BOARD_BYTES).unwrap();
        fs::write(platform.join("platform.json"), br#"{ "name":"espressif32","version":"8.0.0","frameworks":{"arduino":{"package":"framework-arduinoespressif32","script":"builder/frameworks/arduino.py"}},"packages":{"framework-arduinoespressif32":{"owner":"platformio"},"framework-arduinoespressif32-esp32-libs":{"owner":"platformio"}}}"#).unwrap();
        for (name, version) in [
            ("framework-arduinoespressif32", "4.30312.261016"),
            ("framework-arduinoespressif32-esp32-libs", "3.3.12"),
        ] {
            let package = root.join("core/packages").join(name);
            fs::create_dir_all(&package).unwrap();
            fs::write(
                package.join("package.json"),
                format!("{{\"name\":\"{name}\",\"version\":\"{version}\"}}"),
            )
            .unwrap();
        }
        board
    }
    fn selected(path: &Path) -> NativeRecipeSelection<'_> {
        NativeRecipeSelection {
            backend: None,
            platform: "espressif32",
            board: "onehorse32dev",
            framework: "arduino",
            manifest: path,
        }
    }
    fn host() -> NativeHost {
        if cfg!(windows) {
            NativeHost::Windows
        } else {
            NativeHost::Linux
        }
    }
    #[test]
    fn unchanged_generation_revalidates_complete_native_recipe() {
        let fixture = OwnedFixture::new();
        let manifest = seed(&fixture.0);
        let selection = selected(&manifest);
        let mut authority = FixtureRecipeAuthority::new(checkout(), &fixture.0).unwrap();
        let receipt = authority.capture(&selection, host()).unwrap();
        assert!(receipt.decision().rule.is_some());
        assert_eq!(receipt.decision().options.len(), 2);
        assert_eq!(
            authority.revalidate(&receipt, &selection, host(), receipt.decision()),
            Ok(())
        );
    }
    #[test]
    fn raw_same_size_same_mtime_edit_refuses_even_when_policy_decision_is_equal() {
        let fixture = OwnedFixture::new();
        let manifest = seed(&fixture.0);
        let selection = selected(&manifest);
        let mut authority = FixtureRecipeAuthority::new(checkout(), &fixture.0).unwrap();
        let receipt = authority.capture(&selection, host()).unwrap();
        let path = fixture.0.join("core/platforms/espressif32/platform.json");
        let before = fs::metadata(&path).unwrap();
        let mut raw = fs::read(&path).unwrap();
        assert_eq!(raw[1], b' ');
        raw[1] = b'\t';
        fs::write(&path, &raw).unwrap();
        OpenOptions::new()
            .write(true)
            .open(&path)
            .unwrap()
            .set_times(std::fs::FileTimes::new().set_modified(before.modified().unwrap()))
            .unwrap();
        assert_eq!(
            fs::metadata(&path).unwrap().modified().unwrap(),
            before.modified().unwrap()
        );
        let policy = FixtureCompatibilityScope::new(checkout(), &fixture.0)
            .unwrap()
            .options(&selection, host(), &mut NativeReader::default())
            .unwrap();
        assert_eq!(&policy, receipt.decision());
        assert_eq!(
            authority.revalidate(&receipt, &selection, host(), receipt.decision()),
            Err(GenerationRefusal::Snapshot(SnapshotRefusal::Changed))
        );
    }
    #[test]
    fn identical_leaf_replacement_refuses_retained_generation() {
        let fixture = OwnedFixture::new();
        let manifest = seed(&fixture.0);
        let selection = selected(&manifest);
        let mut authority = FixtureRecipeAuthority::new(checkout(), &fixture.0).unwrap();
        let receipt = authority.capture(&selection, host()).unwrap();
        fs::rename(&manifest, manifest.with_extension("old")).unwrap();
        fs::write(&manifest, BOARD_BYTES).unwrap();
        assert_eq!(
            authority.revalidate(&receipt, &selection, host(), receipt.decision()),
            Err(GenerationRefusal::Snapshot(SnapshotRefusal::Changed))
        );
    }
    #[test]
    fn replaced_ancestor_refuses_same_root_and_same_files() {
        let fixture = OwnedFixture::new();
        let parent = fixture.0.join("parent");
        let root = parent.join("fixture");
        fs::create_dir_all(&root).unwrap();
        let manifest = seed(&root);
        let selection = selected(&manifest);
        let mut authority = FixtureRecipeAuthority::new(checkout(), &root).unwrap();
        let receipt = authority.capture(&selection, host()).unwrap();
        let old_parent = fixture.0.join("parent-old");
        let holding = fixture.0.join("holding");
        fs::create_dir(&holding).unwrap();
        let original_root = open_native(&root, true).unwrap();
        let root_identity = identity(&original_root).unwrap();
        // Evacuate each retained object, keeping all its original identities,
        // so Windows need only rename empty directories with shared handles.
        let mut stack = vec![root.clone()];
        let mut files = Vec::new();
        let mut directories = Vec::new();
        while let Some(directory) = stack.pop() {
            for entry in fs::read_dir(directory).unwrap() {
                let entry = entry.unwrap();
                if entry.file_type().unwrap().is_dir() {
                    directories.push(entry.path());
                    stack.push(entry.path());
                } else {
                    files.push(entry.path());
                }
            }
        }
        let held_files: Vec<_> = files
            .iter()
            .enumerate()
            .map(|(index, path)| {
                let held = holding.join(format!("file-{index}"));
                fs::rename(path, &held).unwrap();
                (path.clone(), held)
            })
            .collect();
        directories.sort_by_key(|path| std::cmp::Reverse(path.components().count()));
        let mut held_directories: Vec<_> = directories
            .iter()
            .enumerate()
            .map(|(index, path)| {
                let held = holding.join(format!("directory-{index}"));
                fs::rename(path, &held).unwrap();
                (path.clone(), held)
            })
            .collect();
        let held_root = holding.join("original-root");
        fs::rename(&root, &held_root).unwrap();
        fs::rename(&parent, &old_parent).unwrap();
        fs::create_dir(&parent).unwrap();
        fs::rename(&held_root, &root).unwrap();
        held_directories.sort_by_key(|(path, _)| path.components().count());
        for (path, held) in held_directories {
            fs::rename(held, path).unwrap();
        }
        for (path, held) in held_files {
            fs::rename(held, path).unwrap();
        }
        assert_eq!(
            identity(&open_native(&root, true).unwrap()).unwrap(),
            root_identity
        );
        assert_eq!(
            authority.revalidate(&receipt, &selection, host(), receipt.decision()),
            Err(GenerationRefusal::Snapshot(SnapshotRefusal::Changed))
        );
    }
    #[test]
    fn recapture_and_clear_refuse_old_generation() {
        let fixture = OwnedFixture::new();
        let manifest = seed(&fixture.0);
        let selection = selected(&manifest);
        let mut authority = FixtureRecipeAuthority::new(checkout(), &fixture.0).unwrap();
        let first = authority.capture(&selection, host()).unwrap();
        let second = authority.capture(&selection, host()).unwrap();
        assert!(second.generation() > first.generation());
        assert_eq!(
            authority.revalidate(&first, &selection, host(), first.decision()),
            Err(GenerationRefusal::StaleGeneration)
        );
        let third = authority.capture(&selection, host()).unwrap();
        authority.clear();
        assert_eq!(
            authority.revalidate(&third, &selection, host(), third.decision()),
            Err(GenerationRefusal::StaleGeneration)
        );
    }
    #[test]
    fn independently_created_authority_refuses_foreign_receipt_at_same_root() {
        let fixture = OwnedFixture::new();
        let manifest = seed(&fixture.0);
        let selection = selected(&manifest);
        let mut first = FixtureRecipeAuthority::new(checkout(), &fixture.0).unwrap();
        let receipt = first.capture(&selection, host()).unwrap();
        let mut second = FixtureRecipeAuthority::new(checkout(), &fixture.0).unwrap();
        let _own = second.capture(&selection, host()).unwrap();
        assert_eq!(
            second.revalidate(&receipt, &selection, host(), receipt.decision()),
            Err(GenerationRefusal::ForeignScope)
        );
    }
    #[test]
    fn changed_selection_host_and_options_refuse_generation() {
        let fixture = OwnedFixture::new();
        let manifest = seed(&fixture.0);
        let selection = selected(&manifest);
        let mut authority = FixtureRecipeAuthority::new(checkout(), &fixture.0).unwrap();
        let receipt = authority.capture(&selection, host()).unwrap();
        let mut other = selected(&manifest);
        other.backend = Some("arduino-cli");
        assert_eq!(
            authority.revalidate(&receipt, &other, host(), receipt.decision()),
            Err(GenerationRefusal::SelectionChanged)
        );
        let receipt = authority.capture(&selection, host()).unwrap();
        assert_eq!(
            authority.revalidate(
                &receipt,
                &selection,
                NativeHost::Unsupported,
                receipt.decision()
            ),
            Err(GenerationRefusal::SelectionChanged)
        );
        let receipt = authority.capture(&selection, host()).unwrap();
        let altered = CompatibilityDecision::default();
        assert_eq!(
            authority.revalidate(&receipt, &selection, host(), &altered),
            Err(GenerationRefusal::DecisionChanged)
        );
    }
    #[test]
    fn failed_partial_capture_invalidates_previous_receipt() {
        let fixture = OwnedFixture::new();
        let manifest = seed(&fixture.0);
        let selection = selected(&manifest);
        let mut authority = FixtureRecipeAuthority::new(checkout(), &fixture.0).unwrap();
        let receipt = authority.capture(&selection, host()).unwrap();
        fs::remove_file(
            fixture
                .0
                .join("core/packages/framework-arduinoespressif32-esp32-libs/package.json"),
        )
        .unwrap();
        assert!(authority.capture(&selection, host()).is_err());
        assert_eq!(
            authority.revalidate(&receipt, &selection, host(), receipt.decision()),
            Err(GenerationRefusal::StaleGeneration)
        );
    }
    #[test]
    fn empty_decision_does_not_create_generation() {
        let fixture = OwnedFixture::new();
        let manifest = seed(&fixture.0);
        let selection = selected(&manifest);
        let mut authority = FixtureRecipeAuthority::new(checkout(), &fixture.0).unwrap();
        fs::write(&manifest, b"{}").unwrap();
        assert!(matches!(
            authority.capture(&selection, host()),
            Err(GenerationRefusal::NoRecipe)
        ));
    }
}
