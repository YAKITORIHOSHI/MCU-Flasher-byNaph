//! Create-new preparation of one explicitly supplied Uno/AVR archive fixture.
//! No registry resolution, network, runtime/tool launch, live store, `.piopm`
//! synthesis, installed readiness or execution grant exists in this module.
//! Expected archive identity comes from a trusted native caller, never IPC.
//! Source/package observations are not immutable dependency generations.

use crate::file_identity::{native_identity, single_link, NativeFileIdentity};
use crate::recipe_snapshot::{alias, open_native, pin_chain, verify_directories, PinnedDirectory};
use flate2::read::MultiGzDecoder;
use mcu_core::compile_inputs::ExactCompileTarget;
use sha2::{Digest, Sha256};
use std::cell::Cell;
use std::collections::{BTreeMap, BTreeSet};
#[cfg(windows)]
use std::fs::OpenOptions;
use std::fs::{self, File};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::rc::Rc;

type Result<T> = std::result::Result<T, AvrPreparationRefusal>;
const MAX_ARCHIVE_BYTES: u64 = 256 * 1024 * 1024;
const MAX_FILE_BYTES: u64 = 64 * 1024 * 1024;
const MAX_TOTAL_BYTES: u64 = 512 * 1024 * 1024;
const MAX_FILES: usize = 16_384;
const MAX_DIRECTORIES: usize = 4_096;
const MAX_DEPTH: usize = 24;
const MAX_PATH_BYTES: usize = 1024;
const MAX_EXTENSION_BYTES: u64 = 8 * 1024;
const MAX_HEADERS: usize = 65_536;
const MAX_EXPANSION_RATIO: u64 = 200;

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum AvrArchiveRole {
    Platform,
    Framework,
    ToolScons,
    Toolchain,
}

impl AvrArchiveRole {
    fn name(self) -> &'static str {
        match self {
            Self::Platform => "atmelavr",
            Self::Framework => "framework-arduino-avr",
            Self::ToolScons => "tool-scons",
            Self::Toolchain => "toolchain-atmelavr",
        }
    }
    fn version(self) -> &'static str {
        match self {
            Self::Platform => "5.3.0",
            Self::Framework => "5.4.0",
            Self::ToolScons => "4.41101.0",
            Self::Toolchain => "1.70300.191015",
        }
    }
    fn destination(self) -> String {
        if self == Self::Platform {
            "core/platforms/atmelavr".into()
        } else {
            format!("core/packages/{}", self.name())
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AvrFixtureHost {
    LinuxX86_64,
    LinuxAarch64,
    WindowsX86_64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AvrArchiveHost {
    Universal,
    Native(AvrFixtureHost),
}

pub struct AvrArchiveInput {
    pub role: AvrArchiveRole,
    pub archive_path: PathBuf,
    pub expected_sha256: String,
    pub expected_size: u64,
    pub host: AvrArchiveHost,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AvrPreparationRefusal {
    InvalidScope,
    InvalidInputs,
    HostMismatch,
    UnsafePath,
    HashMismatch,
    Changed,
    TooLarge,
    InvalidArchive,
    UnsupportedEntry,
    InvalidManifest,
    Io,
}

#[derive(Debug)]
pub struct PreparedAvrFile {
    pub path: String,
    pub sha256: String,
    pub bytes: u64,
    pub unix_mode: u32,
}

/// An archive symlink explicitly materialized as an independent regular copy.
/// Both paths are prepared-root relative; no filesystem symlink is emitted.
#[derive(Debug, serde::Serialize)]
pub struct PreparedAvrLinkCopy {
    pub path: String,
    pub target: String,
    pub sha256: String,
    pub bytes: u64,
}

/// Process-local observation only. Drop never deletes a partial/full fixture.
#[derive(Debug)]
pub struct PreparedAvrFixture {
    root: PathBuf,
    target: ExactCompileTarget,
    host: AvrFixtureHost,
    files: Vec<PreparedAvrFile>,
    materialized_links: Vec<PreparedAvrLinkCopy>,
}

impl PreparedAvrFixture {
    pub fn root(&self) -> &Path {
        &self.root
    }
    pub fn target(&self) -> &ExactCompileTarget {
        &self.target
    }
    pub fn host(&self) -> AvrFixtureHost {
        self.host
    }
    pub fn files(&self) -> &[PreparedAvrFile] {
        &self.files
    }
    pub fn materialized_links(&self) -> &[PreparedAvrLinkCopy] {
        &self.materialized_links
    }
}

pub fn prepare_avr_fixture(
    application_root: &Path,
    parent: &Path,
    label: &str,
    host: AvrFixtureHost,
    mut archives: [AvrArchiveInput; 4],
) -> std::result::Result<PreparedAvrFixture, AvrPreparationRefusal> {
    if current_host() != Some(host) {
        return Err(AvrPreparationRefusal::HostMismatch);
    }
    if label.is_empty()
        || label.len() > 64
        || !label.as_bytes()[0].is_ascii_alphanumeric()
        || !label
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_".contains(&b))
        || mcu_core::source_names::is_windows_reserved_device_name(label)
    {
        return Err(AvrPreparationRefusal::InvalidScope);
    }
    let mut scope = native(pin_chain(application_root))?;
    let sandbox = io(application_root.canonicalize())?.join("temp/audit/native-compile-inputs");
    scope.extend(native(pin_chain(&sandbox))?);
    scope.extend(native(pin_chain(parent))?);
    let parent = io(parent.canonicalize())?;
    let sandbox = io(sandbox.canonicalize())?;
    if !parent.starts_with(&sandbox) || protected_path(&parent) {
        return Err(AvrPreparationRefusal::InvalidScope);
    }
    archives.sort_by_key(|archive| archive.role);
    if archives
        .iter()
        .map(|a| a.role)
        .collect::<BTreeSet<_>>()
        .len()
        != 4
    {
        return Err(AvrPreparationRefusal::InvalidInputs);
    }
    let mut sources = Vec::with_capacity(4);
    for archive in archives {
        let expected_host = if archive.role == AvrArchiveRole::Toolchain {
            AvrArchiveHost::Native(host)
        } else {
            AvrArchiveHost::Universal
        };
        if archive.host != expected_host {
            return Err(AvrPreparationRefusal::HostMismatch);
        }
        if archive.expected_size == 0
            || archive.expected_size > MAX_ARCHIVE_BYTES
            || archive.expected_sha256.len() != 64
            || !archive
                .expected_sha256
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(AvrPreparationRefusal::InvalidInputs);
        }
        sources.push(Source::open(archive, &sandbox)?);
    }
    // Finish every complete compressed archive hash before creating any output.
    for source in &mut sources {
        source.verify()?;
    }
    native(verify_directories(&scope))?;
    let mut tree = Tree::reserve(&parent, label)?;
    let mut files = Vec::new();
    let mut materialized_links = Vec::new();
    let mut payload_bytes = 0;
    for source in &mut sources {
        extract(
            source,
            &mut tree,
            &mut files,
            &mut payload_bytes,
            &mut materialized_links,
        )?;
        validate_manifest(source.input.role, &tree.root)?;
    }
    for source in &mut sources {
        source.verify()?;
    }
    tree.verify()?;
    for output in &files {
        verify_output(&tree.root, output)?;
    }
    native(verify_directories(&scope))?;
    files.sort_by(|a, b| a.path.cmp(&b.path));
    Ok(PreparedAvrFixture {
        root: tree.root.clone(),
        target: ExactCompileTarget::new("platformio/atmelavr@5.3.0", "uno", "arduino")
            .map_err(|_| AvrPreparationRefusal::InvalidManifest)?,
        host,
        files,
        materialized_links,
    })
}

fn current_host() -> Option<AvrFixtureHost> {
    match (std::env::consts::OS, std::env::consts::ARCH) {
        ("linux", "x86_64") => Some(AvrFixtureHost::LinuxX86_64),
        ("linux", "aarch64") => Some(AvrFixtureHost::LinuxAarch64),
        ("windows", "x86_64") => Some(AvrFixtureHost::WindowsX86_64),
        _ => None,
    }
}
fn io<T>(value: io::Result<T>) -> Result<T> {
    value.map_err(|_| AvrPreparationRefusal::Io)
}
fn native<T>(
    value: std::result::Result<T, mcu_core::board_compatibility::SnapshotRefusal>,
) -> Result<T> {
    value.map_err(|_| AvrPreparationRefusal::UnsafePath)
}
fn protected_component(name: &str) -> bool {
    matches!(
        name.to_ascii_lowercase().as_str(),
        ".mcu_flasher_build_cache"
            | ".pio"
            | ".vscode"
            | ".clangd"
            | ".opencode"
            | ".git"
            | ".piopm"
    )
}
fn protected_path(path: &Path) -> bool {
    path.components()
        .any(|c| c.as_os_str().to_str().is_some_and(protected_component))
}
fn portable_path(raw: &str) -> Result<String> {
    let raw = raw.strip_prefix("./").unwrap_or(raw);
    if raw.is_empty() || raw.len() > MAX_PATH_BYTES || raw.split('/').count() > MAX_DEPTH {
        return Err(AvrPreparationRefusal::UnsafePath);
    }
    if raw.split('/').any(|part| {
        part.is_empty()
            || matches!(part, "." | "..")
            || part.len() > 255
            || part != part.trim()
            || part.ends_with('.')
            || part
                .chars()
                .any(|c| c.is_control() || "\\:<>\"|?*".contains(c))
            || protected_component(part)
            || mcu_core::source_names::is_windows_reserved_device_name(part)
    }) {
        return Err(AvrPreparationRefusal::UnsafePath);
    }
    Ok(raw.into())
}
fn fold(name: &str) -> String {
    name.to_lowercase().to_uppercase()
}

struct Source {
    input: AvrArchiveInput,
    file: File,
    identity: NativeFileIdentity,
    prefixes: Vec<PinnedDirectory>,
}
impl Source {
    fn open(input: AvrArchiveInput, sandbox: &Path) -> Result<Self> {
        if !input.archive_path.is_absolute() || protected_path(&input.archive_path) {
            return Err(AvrPreparationRefusal::UnsafePath);
        }
        let prefixes = native(pin_chain(
            input
                .archive_path
                .parent()
                .ok_or(AvrPreparationRefusal::UnsafePath)?,
        ))?;
        let metadata = io(fs::symlink_metadata(&input.archive_path))?;
        if alias(&metadata)
            || !metadata.is_file()
            || !io(input.archive_path.canonicalize())?.starts_with(sandbox)
        {
            return Err(AvrPreparationRefusal::UnsafePath);
        }
        let file = native(open_native(&input.archive_path, false))?;
        let metadata = io(file.metadata())?;
        if alias(&metadata) || !metadata.is_file() || !io(single_link(&file))? {
            return Err(AvrPreparationRefusal::UnsafePath);
        }
        let identity = io(native_identity(&file))?;
        Ok(Self {
            input,
            file,
            identity,
            prefixes,
        })
    }
    fn verify(&mut self) -> Result<()> {
        native(verify_directories(&self.prefixes))?;
        let current = native(open_native(&self.input.archive_path, false))?;
        if io(native_identity(&current))? != self.identity
            || io(native_identity(&self.file))? != self.identity
            || alias(&io(current.metadata())?)
            || !io(single_link(&self.file))?
        {
            return Err(AvrPreparationRefusal::Changed);
        }
        if io(self.file.metadata())?.len() != self.input.expected_size {
            return Err(AvrPreparationRefusal::HashMismatch);
        }
        let (digest, size) = hash_file(&mut self.file, MAX_ARCHIVE_BYTES)?;
        if size != self.input.expected_size || digest != self.input.expected_sha256 {
            return Err(AvrPreparationRefusal::HashMismatch);
        }
        Ok(())
    }
}
fn hash_file(file: &mut File, bound: u64) -> Result<(String, u64)> {
    io(file.seek(SeekFrom::Start(0)))?;
    let mut digest = Sha256::new();
    let mut bytes = 0u64;
    let mut buffer = [0; 64 * 1024];
    loop {
        let read = io(file.read(&mut buffer))?;
        if read == 0 {
            break;
        }
        bytes = bytes
            .checked_add(read as u64)
            .filter(|n| *n <= bound)
            .ok_or(AvrPreparationRefusal::TooLarge)?;
        digest.update(&buffer[..read]);
    }
    Ok((format!("{:x}", digest.finalize()), bytes))
}

struct Directory {
    path: PathBuf,
    file: File,
    identity: NativeFileIdentity,
}
struct Tree {
    root: PathBuf,
    directories: BTreeMap<String, Directory>,
    names: BTreeMap<String, (String, bool)>,
    explicit_directories: BTreeSet<String>,
}
impl Tree {
    fn reserve(parent: &Path, label: &str) -> Result<Self> {
        let parent_file = native(open_native(parent, true))?;
        let root = parent.join(label);
        let file = create_directory(&parent_file, parent, label)
            .map_err(|_| AvrPreparationRefusal::InvalidScope)?;
        let identity = io(native_identity(&file))?;
        let mut directories = BTreeMap::new();
        directories.insert(
            String::new(),
            Directory {
                path: root.clone(),
                file,
                identity,
            },
        );
        Ok(Self {
            root,
            directories,
            names: BTreeMap::new(),
            explicit_directories: BTreeSet::new(),
        })
    }
    fn ensure_directory(&mut self, name: &str) -> Result<()> {
        if name.is_empty() {
            return Ok(());
        }
        let name = portable_path(name)?;
        let mut parent = String::new();
        for leaf in name.split('/') {
            let next = if parent.is_empty() {
                leaf.into()
            } else {
                format!("{parent}/{leaf}")
            };
            let key = fold(&next);
            if let Some((literal, directory)) = self.names.get(&key) {
                if literal != &next || !directory {
                    return Err(AvrPreparationRefusal::UnsafePath);
                }
            } else {
                if self.directories.len() >= MAX_DIRECTORIES {
                    return Err(AvrPreparationRefusal::TooLarge);
                }
                let retained = self
                    .directories
                    .get(&parent)
                    .ok_or(AvrPreparationRefusal::UnsafePath)?;
                let file = io(create_directory(&retained.file, &retained.path, leaf))?;
                let identity = io(native_identity(&file))?;
                self.directories.insert(
                    next.clone(),
                    Directory {
                        path: self.root.join(&next),
                        file,
                        identity,
                    },
                );
                self.names.insert(key, (next.clone(), true));
            }
            parent = next;
        }
        Ok(())
    }
    fn directory_entry(&mut self, name: &str) -> Result<()> {
        if !self.explicit_directories.insert(name.into()) {
            return Err(AvrPreparationRefusal::UnsafePath);
        }
        self.ensure_directory(name)
    }
    fn create_file(&mut self, name: &str) -> Result<File> {
        let (parent, leaf) = name.rsplit_once('/').unwrap_or(("", name));
        self.ensure_directory(parent)?;
        if self
            .names
            .insert(fold(name), (name.into(), false))
            .is_some()
        {
            return Err(AvrPreparationRefusal::UnsafePath);
        }
        let directory = self
            .directories
            .get(parent)
            .ok_or(AvrPreparationRefusal::UnsafePath)?;
        io(create_regular(&directory.file, &directory.path, leaf))
    }
    fn verify(&self) -> Result<()> {
        let mut observed = BTreeSet::new();
        for directory in self.directories.values() {
            let current = native(open_native(&directory.path, true))?;
            if alias(&io(current.metadata())?)
                || io(native_identity(&current))? != directory.identity
                || io(native_identity(&directory.file))? != directory.identity
            {
                return Err(AvrPreparationRefusal::Changed);
            }
            let relative = directory
                .path
                .strip_prefix(&self.root)
                .map_err(|_| AvrPreparationRefusal::Changed)?;
            let relative = relative
                .to_str()
                .ok_or(AvrPreparationRefusal::UnsafePath)?
                .replace('\\', "/");
            for entry in io(fs::read_dir(&directory.path))? {
                if observed.len() >= MAX_FILES + MAX_DIRECTORIES {
                    return Err(AvrPreparationRefusal::TooLarge);
                }
                let entry = io(entry)?;
                let leaf = entry.file_name();
                let leaf = leaf.to_str().ok_or(AvrPreparationRefusal::UnsafePath)?;
                let name = if relative.is_empty() {
                    leaf.into()
                } else {
                    format!("{relative}/{leaf}")
                };
                portable_path(&name)?;
                let expected = self
                    .names
                    .get(&fold(&name))
                    .filter(|(literal, _)| literal == &name)
                    .ok_or(AvrPreparationRefusal::Changed)?;
                let metadata = io(fs::symlink_metadata(entry.path()))?;
                if alias(&metadata)
                    || (expected.1 && !metadata.is_dir())
                    || (!expected.1 && !metadata.is_file())
                    || !observed.insert(name)
                {
                    return Err(AvrPreparationRefusal::Changed);
                }
            }
        }
        if observed.len() != self.names.len() {
            return Err(AvrPreparationRefusal::Changed);
        }
        Ok(())
    }
}

#[cfg(target_os = "linux")]
fn create_directory(parent: &File, _path: &Path, leaf: &str) -> io::Result<File> {
    use std::os::fd::{AsRawFd, FromRawFd};
    let leaf = std::ffi::CString::new(leaf).map_err(|_| io::Error::other("Invalid leaf"))?;
    // SAFETY: the retained native directory fd and NUL-terminated single leaf
    // are valid. mkdirat is create-new and follows no caller-selected ancestor.
    if unsafe { libc::mkdirat(parent.as_raw_fd(), leaf.as_ptr(), 0o755) } != 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: only the newly created single child is opened with NOFOLLOW and
    // DIRECTORY. A successful real descriptor transfers once into File.
    let fd = unsafe {
        libc::openat(
            parent.as_raw_fd(),
            leaf.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: fd is newly owned and has not been transferred or closed.
    Ok(unsafe { File::from_raw_fd(fd) })
}
#[cfg(target_os = "linux")]
fn create_regular(parent: &File, _path: &Path, leaf: &str) -> io::Result<File> {
    use std::os::fd::{AsRawFd, FromRawFd};
    let leaf = std::ffi::CString::new(leaf).map_err(|_| io::Error::other("Invalid leaf"))?;
    // SAFETY: the parent is retained and the single validated leaf cannot
    // traverse. EXCL|NOFOLLOW refuses any existing link/file; no overwrite.
    let fd = unsafe {
        libc::openat(
            parent.as_raw_fd(),
            leaf.as_ptr(),
            libc::O_RDWR | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            0o600,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: this newly returned descriptor transfers to one owning File.
    Ok(unsafe { File::from_raw_fd(fd) })
}
#[cfg(windows)]
fn create_directory(_parent: &File, path: &Path, leaf: &str) -> io::Result<File> {
    let prefixes = pin_chain(path).map_err(|_| io::Error::other("Unsafe parent"))?;
    fs::create_dir(path.join(leaf))?;
    verify_directories(&prefixes).map_err(|_| io::Error::other("Changed parent"))?;
    open_native(&path.join(leaf), true).map_err(|_| io::Error::other("Unsafe created directory"))
}
#[cfg(windows)]
fn create_regular(_parent: &File, path: &Path, leaf: &str) -> io::Result<File> {
    use std::os::windows::fs::OpenOptionsExt;
    use windows_sys::Win32::Storage::FileSystem::FILE_FLAG_OPEN_REPARSE_POINT;
    let prefixes = pin_chain(path).map_err(|_| io::Error::other("Unsafe parent"))?;
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path.join(leaf))?;
    verify_directories(&prefixes).map_err(|_| io::Error::other("Changed parent"))?;
    Ok(file)
}
#[cfg(not(any(target_os = "linux", windows)))]
fn create_directory(_parent: &File, _path: &Path, _leaf: &str) -> io::Result<File> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "Unsupported fixture host",
    ))
}
#[cfg(not(any(target_os = "linux", windows)))]
fn create_regular(_parent: &File, _path: &Path, _leaf: &str) -> io::Result<File> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "Unsupported fixture host",
    ))
}

struct HashReader<R> {
    inner: R,
    hash: Sha256,
    bytes: u64,
}
impl<R> HashReader<R> {
    fn new(inner: R) -> Self {
        Self {
            inner,
            hash: Sha256::new(),
            bytes: 0,
        }
    }
    fn finish(self) -> (String, u64) {
        (format!("{:x}", self.hash.finalize()), self.bytes)
    }
}
impl<R: Read> Read for HashReader<R> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        let read = self.inner.read(buffer)?;
        self.bytes = self
            .bytes
            .checked_add(read as u64)
            .filter(|n| *n <= MAX_ARCHIVE_BYTES)
            .ok_or_else(|| io::Error::other("Compressed byte bound"))?;
        self.hash.update(&buffer[..read]);
        Ok(read)
    }
}
struct DecodedBound<R> {
    inner: R,
    bytes: Rc<Cell<u64>>,
    exceeded: Rc<Cell<bool>>,
    bound: u64,
}
impl<R: Read> Read for DecodedBound<R> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        let read = self.inner.read(buffer)?;
        let bytes = self
            .bytes
            .get()
            .checked_add(read as u64)
            .ok_or_else(|| io::Error::other("Decoded byte overflow"))?;
        self.bytes.set(bytes);
        if bytes > self.bound {
            self.exceeded.set(true);
            return Err(io::Error::other("Decoded archive bound"));
        }
        Ok(read)
    }
}
fn archive_error<T>(result: io::Result<T>, exceeded: &Cell<bool>) -> Result<T> {
    result.map_err(|_| {
        if exceeded.get() {
            AvrPreparationRefusal::TooLarge
        } else {
            AvrPreparationRefusal::InvalidArchive
        }
    })
}
#[derive(Default)]
struct Extension {
    path: Option<String>,
    size: Option<u64>,
}
fn pax_extension(raw: &[u8]) -> Result<Extension> {
    if raw.is_empty()
        || !raw.ends_with(b"\n")
        || raw[..raw.len() - 1]
            .split(|b| *b == b'\n')
            .any(|line| line.is_empty())
    {
        return Err(AvrPreparationRefusal::InvalidArchive);
    }
    let mut keys = BTreeSet::new();
    let mut extension = Extension::default();
    for item in tar::PaxExtensions::new(raw) {
        let item = item.map_err(|_| AvrPreparationRefusal::InvalidArchive)?;
        let key = item
            .key()
            .map_err(|_| AvrPreparationRefusal::InvalidArchive)?;
        let value = item
            .value()
            .map_err(|_| AvrPreparationRefusal::InvalidArchive)?;
        if keys.len() >= 64 || !keys.insert(key) || value.chars().any(char::is_control) {
            return Err(AvrPreparationRefusal::InvalidArchive);
        }
        match key {
            "path" => extension.path = Some(value.into()),
            "size" => {
                extension.size = Some(
                    value
                        .parse()
                        .map_err(|_| AvrPreparationRefusal::InvalidArchive)?,
                )
            }
            "mtime" | "atime" | "ctime" | "uid" | "gid" | "uname" | "gname" | "charset"
            | "comment" => {}
            _ => return Err(AvrPreparationRefusal::UnsupportedEntry),
        }
    }
    Ok(extension)
}

fn extract(
    source: &mut Source,
    tree: &mut Tree,
    files: &mut Vec<PreparedAvrFile>,
    payload_bytes: &mut u64,
    links: &mut Vec<PreparedAvrLinkCopy>,
) -> Result<()> {
    io(source.file.seek(SeekFrom::Start(0)))?;
    let raw = HashReader::new(&mut source.file);
    let decoded = MultiGzDecoder::new(raw);
    let progress = Rc::new(Cell::new(0));
    let exceeded = Rc::new(Cell::new(false));
    let reader = DecodedBound {
        inner: decoded,
        bytes: progress.clone(),
        exceeded: exceeded.clone(),
        bound: (source
            .input
            .expected_size
            .saturating_mul(MAX_EXPANSION_RATIO)
            .saturating_add(64 * 1024))
        .min(MAX_TOTAL_BYTES + 32 * 1024 * 1024),
    };
    let mut archive = tar::Archive::new(reader);
    archive.set_ignore_zeros(false);
    let prefix = source.input.role.destination();
    tree.ensure_directory(&prefix)?;
    let mut pending: Option<Extension> = None;
    let mut deferred_links: Vec<(String, String)> = Vec::new();
    let mut last_end = 0;
    for (index, entry) in archive_error(archive.entries(), &exceeded)?
        .raw(true)
        .enumerate()
    {
        if index >= MAX_HEADERS {
            return Err(AvrPreparationRefusal::TooLarge);
        }
        let mut entry = archive_error(entry, &exceeded)?;
        let kind = entry.header().entry_type();
        let size = entry.size();
        last_end = entry
            .raw_file_position()
            .checked_add(
                size.checked_add(511)
                    .ok_or(AvrPreparationRefusal::TooLarge)?
                    / 512
                    * 512,
            )
            .ok_or(AvrPreparationRefusal::TooLarge)?;
        if matches!(kind, tar::EntryType::XHeader | tar::EntryType::GNULongName) {
            if pending.is_some() || size > MAX_EXTENSION_BYTES {
                return Err(AvrPreparationRefusal::InvalidArchive);
            }
            let mut bytes = Vec::new();
            archive_error(entry.read_to_end(&mut bytes), &exceeded)?;
            if bytes.len() as u64 != size {
                return Err(AvrPreparationRefusal::InvalidArchive);
            }
            pending = Some(if kind == tar::EntryType::XHeader {
                pax_extension(&bytes)?
            } else {
                let name = bytes
                    .strip_suffix(&[0])
                    .ok_or(AvrPreparationRefusal::InvalidArchive)?;
                Extension {
                    path: Some(
                        std::str::from_utf8(name)
                            .map_err(|_| AvrPreparationRefusal::UnsafePath)?
                            .into(),
                    ),
                    size: None,
                }
            });
            continue;
        }
        if !matches!(
            kind,
            tar::EntryType::Regular | tar::EntryType::Directory | tar::EntryType::Symlink
        ) {
            return Err(AvrPreparationRefusal::UnsupportedEntry);
        }
        let raw_name = entry.path_bytes();
        let raw_name =
            std::str::from_utf8(&raw_name).map_err(|_| AvrPreparationRefusal::UnsafePath)?;
        if matches!(raw_name, "." | "./")
            && kind == tar::EntryType::Directory
            && size == 0
            && pending.is_none()
        {
            continue;
        }
        let raw_name = if kind == tar::EntryType::Directory {
            raw_name.strip_suffix('/').unwrap_or(raw_name)
        } else {
            raw_name
        };
        portable_path(raw_name)?;
        let extension = pending.take().unwrap_or_default();
        if extension.size.is_some_and(|expected| expected != size) {
            return Err(AvrPreparationRefusal::InvalidArchive);
        }
        let name = portable_path(extension.path.as_deref().unwrap_or(raw_name))?;
        let output = format!("{prefix}/{name}");
        if kind == tar::EntryType::Directory {
            if size != 0 {
                return Err(AvrPreparationRefusal::InvalidArchive);
            }
            tree.directory_entry(&output)?;
            continue;
        }
        if kind == tar::EntryType::Symlink {
            if size != 0 {
                return Err(AvrPreparationRefusal::UnsupportedEntry);
            }
            let target = entry
                .link_name_bytes()
                .ok_or(AvrPreparationRefusal::UnsupportedEntry)?;
            let target =
                std::str::from_utf8(&target).map_err(|_| AvrPreparationRefusal::UnsafePath)?;
            if !reviewed_link(&source.input, &name, target) {
                return Err(AvrPreparationRefusal::UnsupportedEntry);
            }
            if deferred_links
                .iter()
                .any(|(path, _)| fold(path) == fold(&name))
                || tree.names.contains_key(&fold(&output))
            {
                return Err(AvrPreparationRefusal::UnsafePath);
            }
            deferred_links.push((name, target.to_string()));
            continue;
        }
        if deferred_links
            .iter()
            .any(|(path, _)| fold(path) == fold(&name))
        {
            return Err(AvrPreparationRefusal::UnsafePath);
        }
        admit_file(files.len(), payload_bytes, size)?;
        let mode = entry
            .header()
            .mode()
            .map_err(|_| AvrPreparationRefusal::InvalidArchive)?
            & 0o777;
        let mut output_file = tree.create_file(&output)?;
        let digest = copy_exact(&mut entry, &mut output_file, size).map_err(|_| {
            if exceeded.get() {
                AvrPreparationRefusal::TooLarge
            } else {
                AvrPreparationRefusal::InvalidArchive
            }
        })?;
        set_mode(&output_file, mode)?;
        files.push(PreparedAvrFile {
            path: output,
            sha256: digest,
            bytes: size,
            unix_mode: mode,
        });
    }
    if pending.is_some() || progress.get() < last_end.saturating_add(512) {
        return Err(AvrPreparationRefusal::InvalidArchive);
    }
    let mut stream = archive.into_inner();
    let mut buffer = [0; 64 * 1024];
    loop {
        let read = archive_error(stream.read(&mut buffer), &exceeded)?;
        if read == 0 {
            break;
        }
        if buffer[..read].iter().any(|b| *b != 0) {
            return Err(AvrPreparationRefusal::InvalidArchive);
        }
    }
    if progress.get() < last_end.saturating_add(1024)
        || !(progress.get() - last_end).is_multiple_of(512)
    {
        return Err(AvrPreparationRefusal::InvalidArchive);
    }
    let (digest, bytes) = stream.inner.into_inner().finish();
    if digest != source.input.expected_sha256 || bytes != source.input.expected_size {
        return Err(AvrPreparationRefusal::Changed);
    }
    materialize_links(
        &source.input,
        tree,
        files,
        payload_bytes,
        links,
        deferred_links,
    )?;
    Ok(())
}

fn admit_file(count: usize, total: &mut u64, size: u64) -> Result<()> {
    if count >= MAX_FILES || size > MAX_FILE_BYTES {
        return Err(AvrPreparationRefusal::TooLarge);
    }
    *total = total
        .checked_add(size)
        .filter(|n| *n <= MAX_TOTAL_BYTES)
        .ok_or(AvrPreparationRefusal::TooLarge)?;
    Ok(())
}
fn copy_exact(reader: &mut impl Read, writer: &mut File, size: u64) -> io::Result<String> {
    let mut hash = Sha256::new();
    let mut written = 0u64;
    let mut buffer = [0; 64 * 1024];
    while written < size {
        let remaining = (size - written).min(buffer.len() as u64) as usize;
        let read = reader.read(&mut buffer[..remaining])?;
        if read == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "Truncated archive entry",
            ));
        }
        writer.write_all(&buffer[..read])?;
        hash.update(&buffer[..read]);
        written += read as u64;
    }
    writer.flush()?;
    Ok(format!("{:x}", hash.finalize()))
}
fn set_mode(file: &File, mode: u32) -> Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        io(file.set_permissions(fs::Permissions::from_mode(mode & 0o777)))?;
    }
    #[cfg(not(unix))]
    let _ = (file, mode);
    Ok(())
}
fn verify_output(root: &Path, output: &PreparedAvrFile) -> Result<()> {
    let mut file = native(open_native(&root.join(&output.path), false))?;
    let metadata = io(file.metadata())?;
    if alias(&metadata)
        || !metadata.is_file()
        || !io(single_link(&file))?
        || metadata.len() != output.bytes
    {
        return Err(AvrPreparationRefusal::Changed);
    }
    let (digest, bytes) = hash_file(&mut file, MAX_FILE_BYTES)?;
    if digest != output.sha256 || bytes != output.bytes {
        return Err(AvrPreparationRefusal::Changed);
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if metadata.permissions().mode() & 0o7777 != output.unix_mode {
            return Err(AvrPreparationRefusal::Changed);
        }
    }
    Ok(())
}
fn manifest_bytes(path: &Path) -> Result<Vec<u8>> {
    let mut file = native(open_native(path, false))?;
    let metadata = io(file.metadata())?;
    if alias(&metadata)
        || !metadata.is_file()
        || !io(single_link(&file))?
        || metadata.len() > 64 * 1024
    {
        return Err(AvrPreparationRefusal::InvalidManifest);
    }
    let mut bytes = Vec::new();
    io((&mut file).take(64 * 1024 + 1).read_to_end(&mut bytes))?;
    if bytes.len() > 64 * 1024 {
        return Err(AvrPreparationRefusal::InvalidManifest);
    }
    Ok(bytes)
}
fn validate_manifest(role: AvrArchiveRole, root: &Path) -> Result<()> {
    let destination = role.destination();
    if role == AvrArchiveRole::Platform {
        let target = ExactCompileTarget::new("platformio/atmelavr@5.3.0", "uno", "arduino")
            .map_err(|_| AvrPreparationRefusal::InvalidManifest)?;
        let platform = format!("{destination}/platform.json");
        let board = format!("{destination}/boards/uno.json");
        mcu_core::prepared_target::validate_prepared_descriptors(
            &target,
            &platform,
            &manifest_bytes(&root.join(&platform))?,
            &board,
            &manifest_bytes(&root.join(&board))?,
        )
        .map_err(|_| AvrPreparationRefusal::InvalidManifest)
    } else {
        #[derive(serde::Deserialize)]
        struct Identity {
            name: String,
            version: String,
        }
        let bytes = manifest_bytes(&root.join(destination).join("package.json"))?;
        let value: serde_json::Value =
            serde_json::from_slice(&bytes).map_err(|_| AvrPreparationRefusal::InvalidManifest)?;
        if !value.is_object() {
            return Err(AvrPreparationRefusal::InvalidManifest);
        }
        // Derive rejects duplicate decoded name/version identity fields. Other
        // package fields are unqualified metadata, never execution authority.
        let identity: Identity =
            serde_json::from_slice(&bytes).map_err(|_| AvrPreparationRefusal::InvalidManifest)?;
        if identity.name != role.name() || identity.version != role.version() {
            return Err(AvrPreparationRefusal::InvalidManifest);
        }
        Ok(())
    }
}

const REVIEWED_LINUX_TOOLCHAIN_SHA: &str =
    "664f0b7b08a15e8d6362b87e2fdb5a4dcfd5959ebc5b19d938e7dff6f12f0524";
const REVIEWED_LINKS: [(&str, &str); 8] = [
    (
        "lib/gcc/avr/7.3.0/plugin/libcc1plugin.so",
        "libcc1plugin.so.0.0.0",
    ),
    (
        "lib/gcc/avr/7.3.0/plugin/libcc1plugin.so.0",
        "libcc1plugin.so.0.0.0",
    ),
    (
        "lib/gcc/avr/7.3.0/plugin/libcp1plugin.so",
        "libcp1plugin.so.0.0.0",
    ),
    (
        "lib/gcc/avr/7.3.0/plugin/libcp1plugin.so.0",
        "libcp1plugin.so.0.0.0",
    ),
    ("lib/libcc1.so", "libcc1.so.0.0.0"),
    ("lib/libcc1.so.0", "libcc1.so.0.0.0"),
    (
        "libexec/gcc/avr/7.3.0/liblto_plugin.so",
        "liblto_plugin.so.0.0.0",
    ),
    (
        "libexec/gcc/avr/7.3.0/liblto_plugin.so.0",
        "liblto_plugin.so.0.0.0",
    ),
];
fn reviewed_link(input: &AvrArchiveInput, path: &str, target: &str) -> bool {
    input.role == AvrArchiveRole::Toolchain
        && input.host == AvrArchiveHost::Native(AvrFixtureHost::LinuxX86_64)
        && input.expected_sha256 == REVIEWED_LINUX_TOOLCHAIN_SHA
        && REVIEWED_LINKS.contains(&(path, target))
}
fn materialize_links(
    input: &AvrArchiveInput,
    tree: &mut Tree,
    files: &mut Vec<PreparedAvrFile>,
    total: &mut u64,
    links: &mut Vec<PreparedAvrLinkCopy>,
    deferred: Vec<(String, String)>,
) -> Result<()> {
    let prefix = input.role.destination();
    for (path, target_leaf) in deferred {
        if !reviewed_link(input, &path, &target_leaf) {
            return Err(AvrPreparationRefusal::UnsupportedEntry);
        }
        let parent = path
            .rsplit_once('/')
            .map(|(parent, _)| parent)
            .unwrap_or("");
        let target = format!("{prefix}/{parent}/{target_leaf}");
        let admitted = files
            .iter()
            .find(|f| f.path == target)
            .ok_or(AvrPreparationRefusal::UnsupportedEntry)?;
        let digest = admitted.sha256.clone();
        let bytes = admitted.bytes;
        let mode = admitted.unix_mode;
        verify_output(&tree.root, admitted)?;
        admit_file(files.len(), total, bytes)?;
        let path = format!("{prefix}/{path}");
        let mut source = native(open_native(&tree.root.join(&target), false))?;
        let identity = io(native_identity(&source))?;
        let mut output = tree.create_file(&path)?;
        let copied = io(copy_exact(&mut source, &mut output, bytes))?;
        if copied != digest || io(native_identity(&source))? != identity {
            return Err(AvrPreparationRefusal::Changed);
        }
        set_mode(&output, mode)?;
        verify_output(
            &tree.root,
            files
                .iter()
                .find(|f| f.path == target)
                .ok_or(AvrPreparationRefusal::Changed)?,
        )?;
        files.push(PreparedAvrFile {
            path: path.clone(),
            sha256: digest.clone(),
            bytes,
            unix_mode: mode,
        });
        links.push(PreparedAvrLinkCopy {
            path,
            target,
            sha256: digest,
            bytes,
        });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use flate2::{write::GzEncoder, Compression};
    use sha2::{Digest, Sha256};
    use std::fs;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(0);
    struct Fixture {
        app: PathBuf,
        root: PathBuf,
    }
    impl Fixture {
        fn new() -> Self {
            let app = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("../../..")
                .canonicalize()
                .unwrap();
            let base = app.join("temp/audit/native-compile-inputs");
            fs::create_dir_all(&base).unwrap();
            let root = base.join(format!(
                "prepare-avr-tests-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&root).unwrap();
            Self { app, root }
        }
        fn archives(&self) -> [AvrArchiveInput; 4] {
            [
                AvrArchiveRole::Platform,
                AvrArchiveRole::Framework,
                AvrArchiveRole::ToolScons,
                AvrArchiveRole::Toolchain,
            ]
            .map(|role| {
                let mut files = vec![(
                    if role == AvrArchiveRole::Platform {
                        "platform.json"
                    } else {
                        "package.json"
                    },
                    format!(
                        "{{\"name\":\"{}\",\"version\":\"{}\"{}}}",
                        role.name(),
                        role.version(),
                        if role == AvrArchiveRole::Platform {
                            ",\"frameworks\":{\"arduino\":{}}"
                        } else {
                            ""
                        }
                    )
                    .into_bytes(),
                    0o644,
                )];
                if role == AvrArchiveRole::Platform {
                    files.push((
                        "boards/uno.json",
                        br#"{"frameworks":["arduino"]}"#.to_vec(),
                        0o644,
                    ));
                }
                if role == AvrArchiveRole::Toolchain {
                    files.push(("bin/avr-gcc", b"fixture bytes only\0\xff".to_vec(), 0o6755));
                }
                self.archive(role, &files)
            })
        }
        fn archive(&self, role: AvrArchiveRole, files: &[(&str, Vec<u8>, u32)]) -> AvrArchiveInput {
            let encoder = GzEncoder::new(Vec::new(), Compression::default());
            let mut tar = tar::Builder::new(encoder);
            for (name, bytes, mode) in files {
                let mut header = tar::Header::new_gnu();
                header.set_size(bytes.len() as u64);
                header.set_mode(*mode);
                header.set_cksum();
                tar.append_data(&mut header, name, bytes.as_slice())
                    .unwrap();
            }
            let bytes = tar.into_inner().unwrap().finish().unwrap();
            let path = self.root.join(format!(
                "{}-{}.tar.gz",
                role.name(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::write(&path, &bytes).unwrap();
            AvrArchiveInput {
                role,
                archive_path: path,
                expected_sha256: format!("{:x}", Sha256::digest(&bytes)),
                expected_size: bytes.len() as u64,
                host: if role == AvrArchiveRole::Toolchain {
                    AvrArchiveHost::Native(native_host())
                } else {
                    AvrArchiveHost::Universal
                },
            }
        }
        fn raw_archive(
            &self,
            role: AvrArchiveRole,
            entries: &[(&str, tar::EntryType, &str, &[u8], u32)],
        ) -> AvrArchiveInput {
            let encoder = GzEncoder::new(Vec::new(), Compression::default());
            let mut tar = tar::Builder::new(encoder);
            for (name, kind, target, bytes, mode) in entries {
                let mut header = tar::Header::new_gnu();
                header.as_mut_bytes()[..100].fill(0);
                header.as_mut_bytes()[..name.len()].copy_from_slice(name.as_bytes());
                header.set_entry_type(*kind);
                header.set_size(bytes.len() as u64);
                header.set_mode(*mode);
                if !target.is_empty() {
                    header.set_link_name(target).unwrap();
                }
                header.set_cksum();
                tar.append(&header, *bytes).unwrap();
            }
            self.encoded_archive(role, tar.into_inner().unwrap().finish().unwrap())
        }
        fn encoded_archive(&self, role: AvrArchiveRole, bytes: Vec<u8>) -> AvrArchiveInput {
            let path = self.root.join(format!(
                "{}-raw-{}.tar.gz",
                role.name(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::write(&path, &bytes).unwrap();
            AvrArchiveInput {
                role,
                archive_path: path,
                expected_sha256: format!("{:x}", Sha256::digest(&bytes)),
                expected_size: bytes.len() as u64,
                host: if role == AvrArchiveRole::Toolchain {
                    AvrArchiveHost::Native(native_host())
                } else {
                    AvrArchiveHost::Universal
                },
            }
        }
        fn run(&self, archives: [AvrArchiveInput; 4]) -> Result<PreparedAvrFixture> {
            prepare_avr_fixture(&self.app, &self.root, "prepared", native_host(), archives)
        }
        fn extract(&self, input: AvrArchiveInput) -> Result<(Tree, Vec<PreparedAvrFile>)> {
            let mut source = Source::open(input, &self.root)?;
            source.verify()?;
            let mut tree = Tree::reserve(&self.root, "extracted")?;
            let mut files = Vec::new();
            extract(&mut source, &mut tree, &mut files, &mut 0, &mut Vec::new())?;
            tree.verify()?;
            Ok((tree, files))
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.root);
        }
    }
    fn native_host() -> AvrFixtureHost {
        match (std::env::consts::OS, std::env::consts::ARCH) {
            ("linux", "aarch64") => AvrFixtureHost::LinuxAarch64,
            ("windows", "x86_64") => AvrFixtureHost::WindowsX86_64,
            _ => AvrFixtureHost::LinuxX86_64,
        }
    }
    fn pax_record(key: &str, value: &str) -> Vec<u8> {
        let tail = format!(" {key}={value}\n");
        let mut size = tail.len() + 1;
        loop {
            let record = format!("{size}{tail}");
            if record.len() == size {
                return record.into_bytes();
            }
            size = record.len();
        }
    }

    #[test]
    fn exact_four_archive_fixture_preserves_sources_bytes_and_unix_executable_bits() {
        let fixture = Fixture::new();
        let archives = fixture.archives();
        let originals = archives
            .iter()
            .map(|a| (a.archive_path.clone(), fs::read(&a.archive_path).unwrap()))
            .collect::<Vec<_>>();
        let result = fixture.run(archives).unwrap();
        assert_eq!(result.target().platform_spec(), "platformio/atmelavr@5.3.0");
        assert_eq!(result.target().board_id(), "uno");
        assert_eq!(result.target().framework(), "arduino");
        let compiler = result
            .root()
            .join("core/packages/toolchain-atmelavr/bin/avr-gcc");
        assert_eq!(fs::read(&compiler).unwrap(), b"fixture bytes only\0\xff");
        assert!(result
            .files()
            .iter()
            .any(|f| f.path == "core/packages/toolchain-atmelavr/bin/avr-gcc"
                && f.unix_mode == 0o755));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                fs::metadata(compiler).unwrap().permissions().mode() & 0o7777,
                0o755
            );
        }
        for (path, bytes) in originals {
            assert_eq!(fs::read(path).unwrap(), bytes);
        }
        assert!(!result.root().join(".piopm").exists());
        assert!(!result.root().join("ready.json").exists());
    }

    #[test]
    fn wrong_hash_or_size_refuses_before_creating_any_output() {
        let fixture = Fixture::new();
        let mut archives = fixture.archives();
        archives[3].expected_sha256 = "0".repeat(64);
        assert_eq!(
            fixture.run(archives).unwrap_err(),
            AvrPreparationRefusal::HashMismatch
        );
        assert!(!fixture.root.join("prepared").exists());
        let mut archives = fixture.archives();
        archives[0].expected_size += 1;
        assert_eq!(
            fixture.run(archives).unwrap_err(),
            AvrPreparationRefusal::HashMismatch
        );
        assert!(!fixture.root.join("prepared").exists());
    }

    #[test]
    fn duplicate_roles_and_host_mismatch_cannot_select_fallback_inputs() {
        let fixture = Fixture::new();
        let mut archives = fixture.archives();
        archives[3].role = AvrArchiveRole::Framework;
        assert_eq!(
            fixture.run(archives).unwrap_err(),
            AvrPreparationRefusal::InvalidInputs
        );
        let mut archives = fixture.archives();
        archives[3].host = AvrArchiveHost::Universal;
        assert_eq!(
            fixture.run(archives).unwrap_err(),
            AvrPreparationRefusal::HostMismatch
        );
    }

    #[test]
    fn reservation_is_create_new_and_retains_preexisting_bytes() {
        let fixture = Fixture::new();
        fs::create_dir(fixture.root.join("prepared")).unwrap();
        fs::write(fixture.root.join("prepared/keep"), b"keep unchanged").unwrap();
        assert_eq!(
            fixture.run(fixture.archives()).unwrap_err(),
            AvrPreparationRefusal::InvalidScope
        );
        assert_eq!(
            fs::read(fixture.root.join("prepared/keep")).unwrap(),
            b"keep unchanged"
        );
    }

    #[test]
    fn malformed_gzip_retains_partial_reservation_without_success_evidence() {
        let fixture = Fixture::new();
        let mut archives = fixture.archives();
        fs::write(&archives[0].archive_path, b"not gzip").unwrap();
        archives[0].expected_size = 8;
        archives[0].expected_sha256 = format!("{:x}", Sha256::digest(b"not gzip"));
        assert_eq!(
            fixture.run(archives).unwrap_err(),
            AvrPreparationRefusal::InvalidArchive
        );
        assert!(fixture.root.join("prepared").is_dir());
        assert!(!fixture.root.join("prepared/ready.json").exists());
    }

    #[test]
    fn wrong_package_identity_retains_only_owned_partial_tree() {
        let fixture = Fixture::new();
        let mut archives = fixture.archives();
        archives[1] = fixture.archive(
            AvrArchiveRole::Framework,
            &[(
                "package.json",
                br#"{"name":"framework-arduino-avr","version":"5.4.1"}"#.to_vec(),
                0o644,
            )],
        );
        assert_eq!(
            fixture.run(archives).unwrap_err(),
            AvrPreparationRefusal::InvalidManifest
        );
        assert!(fixture
            .root
            .join("prepared/core/platforms/atmelavr/platform.json")
            .is_file());
        assert!(!fixture.root.join("prepared/ready.json").exists());
    }

    #[test]
    fn unsafe_archive_paths_and_case_collisions_cannot_escape_or_overwrite() {
        for name in [
            "../escaped",
            "/absolute",
            "C:/drive",
            "folder\\escape",
            "NUL.txt",
            "COM1/file",
            "file:stream",
            "trailing./file",
            "a//b",
            ".pio/cache",
            "safe/../../escape",
        ] {
            let fixture = Fixture::new();
            let mut archives = fixture.archives();
            archives[0] = fixture.raw_archive(
                AvrArchiveRole::Platform,
                &[(name, tar::EntryType::Regular, "", b"payload", 0o644)],
            );
            assert_eq!(
                fixture.run(archives).unwrap_err(),
                AvrPreparationRefusal::UnsafePath,
                "{name}"
            );
            assert!(!fixture.root.join("escaped").exists());
        }
        let fixture = Fixture::new();
        let mut archives = fixture.archives();
        archives[0] = fixture.raw_archive(
            AvrArchiveRole::Platform,
            &[
                ("First.h", tar::EntryType::Regular, "", b"first", 0o644),
                ("FIRST.H", tar::EntryType::Regular, "", b"second", 0o644),
            ],
        );
        assert_eq!(
            fixture.run(archives).unwrap_err(),
            AvrPreparationRefusal::UnsafePath
        );
        assert_eq!(
            fs::read(
                fixture
                    .root
                    .join("prepared/core/platforms/atmelavr/First.h")
            )
            .unwrap(),
            b"first"
        );
    }

    #[test]
    fn links_devices_sparse_and_unknown_entry_types_refuse_explicitly() {
        for kind in [
            tar::EntryType::Symlink,
            tar::EntryType::Link,
            tar::EntryType::Fifo,
            tar::EntryType::Char,
            tar::EntryType::Block,
            tar::EntryType::Continuous,
            tar::EntryType::GNUSparse,
            tar::EntryType::XGlobalHeader,
            tar::EntryType::GNULongLink,
        ] {
            let fixture = Fixture::new();
            let mut archives = fixture.archives();
            archives[0] = fixture.raw_archive(
                AvrArchiveRole::Platform,
                &[("unsafe", kind, "target", b"", 0o755)],
            );
            assert_eq!(
                fixture.run(archives).unwrap_err(),
                AvrPreparationRefusal::UnsupportedEntry,
                "{kind:?}"
            );
            assert!(!fixture
                .root
                .join("prepared/core/platforms/atmelavr/unsafe")
                .exists());
        }
    }

    #[test]
    fn gzip_crc_truncation_and_concatenated_hidden_payload_cannot_be_ignored() {
        for variant in 0..4 {
            let fixture = Fixture::new();
            let mut archives = fixture.archives();
            let mut bytes = fs::read(&archives[0].archive_path).unwrap();
            match variant {
                0 => {
                    let index = bytes.len() - 8;
                    bytes[index] ^= 0x80;
                }
                1 => {
                    bytes.pop();
                }
                2 => {
                    let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
                    std::io::Write::write_all(&mut encoder, b"hidden trailing payload").unwrap();
                    bytes.extend(encoder.finish().unwrap());
                }
                _ => bytes.extend_from_slice(b"trailing junk"),
            }
            archives[0] = fixture.encoded_archive(AvrArchiveRole::Platform, bytes);
            assert_eq!(
                fixture.run(archives).unwrap_err(),
                AvrPreparationRefusal::InvalidArchive,
                "variant {variant}"
            );
            assert!(!fixture.root.join("prepared/ready.json").exists());
        }
    }

    #[test]
    fn file_count_payload_size_depth_and_expansion_bounds_refuse() {
        assert_eq!(
            admit_file(16_384, &mut 0, 1),
            Err(AvrPreparationRefusal::TooLarge)
        );
        assert_eq!(
            admit_file(0, &mut 0, 64 * 1024 * 1024 + 1),
            Err(AvrPreparationRefusal::TooLarge)
        );
        assert_eq!(
            admit_file(0, &mut (512 * 1024 * 1024), 1),
            Err(AvrPreparationRefusal::TooLarge)
        );
        assert!(portable_path(&format!("{}file", "a/".repeat(24))).is_err());
        let fixture = Fixture::new();
        let mut archives = fixture.archives();
        archives[0] = fixture.archive(
            AvrArchiveRole::Platform,
            &[("zeros", vec![0; 2 * 1024 * 1024], 0o644)],
        );
        assert_eq!(
            fixture.run(archives).unwrap_err(),
            AvrPreparationRefusal::TooLarge
        );
    }

    #[test]
    fn final_inventory_refuses_an_undeclared_file_or_directory() {
        for directory in [false, true] {
            let fixture = Fixture::new();
            let tree = Tree::reserve(&fixture.root, "inventory").unwrap();
            if directory {
                fs::create_dir(tree.root.join("injected")).unwrap();
            } else {
                fs::write(tree.root.join("injected"), b"not declared").unwrap();
            }
            assert_eq!(tree.verify(), Err(AvrPreparationRefusal::Changed));
        }
    }

    #[test]
    fn directory_entries_are_preserved_once_and_cannot_replace_files() {
        let fixture = Fixture::new();
        let archive = fixture.raw_archive(
            AvrArchiveRole::Platform,
            &[
                ("empty/", tar::EntryType::Directory, "", b"", 0o755),
                ("implicit/file", tar::EntryType::Regular, "", b"data", 0o644),
                ("implicit/", tar::EntryType::Directory, "", b"", 0o755),
            ],
        );
        let (tree, files) = fixture.extract(archive).unwrap();
        assert!(tree.root.join("core/platforms/atmelavr/empty").is_dir());
        assert_eq!(files.len(), 1);
        for entries in [
            vec![
                (
                    "same",
                    tar::EntryType::Regular,
                    "",
                    b"first".as_slice(),
                    0o644,
                ),
                (
                    "same",
                    tar::EntryType::Regular,
                    "",
                    b"second".as_slice(),
                    0o644,
                ),
            ],
            vec![
                (
                    "same/",
                    tar::EntryType::Directory,
                    "",
                    b"".as_slice(),
                    0o755,
                ),
                (
                    "same/",
                    tar::EntryType::Directory,
                    "",
                    b"".as_slice(),
                    0o755,
                ),
            ],
            vec![
                (
                    "same",
                    tar::EntryType::Regular,
                    "",
                    b"first".as_slice(),
                    0o644,
                ),
                (
                    "same/",
                    tar::EntryType::Directory,
                    "",
                    b"".as_slice(),
                    0o755,
                ),
            ],
            vec![
                (
                    "same/",
                    tar::EntryType::Directory,
                    "",
                    b"".as_slice(),
                    0o755,
                ),
                (
                    "same",
                    tar::EntryType::Regular,
                    "",
                    b"second".as_slice(),
                    0o644,
                ),
            ],
        ] {
            let fixture = Fixture::new();
            let archive = fixture.raw_archive(AvrArchiveRole::Platform, &entries);
            assert!(matches!(
                fixture.extract(archive),
                Err(AvrPreparationRefusal::UnsafePath)
            ));
        }
    }

    #[test]
    fn directory_count_and_portable_path_lengths_have_explicit_bounds() {
        let fixture = Fixture::new();
        let mut tree = Tree::reserve(&fixture.root, "directories").unwrap();
        for index in 1..MAX_DIRECTORIES {
            tree.ensure_directory(&format!("d{index}")).unwrap();
        }
        assert_eq!(
            tree.ensure_directory("one-more"),
            Err(AvrPreparationRefusal::TooLarge)
        );
        assert!(!tree.root.join("one-more").exists());
        assert_eq!(
            portable_path(&"a".repeat(256)),
            Err(AvrPreparationRefusal::UnsafePath)
        );
        let long = std::array::from_fn::<_, 4, _>(|_| "a".repeat(255)).join("/") + "/a";
        assert!(long.len() > MAX_PATH_BYTES);
        assert_eq!(portable_path(&long), Err(AvrPreparationRefusal::UnsafePath));
    }

    #[test]
    fn bounded_local_pax_and_gnu_long_names_preserve_effective_literal_names() {
        for gnu in [false, true] {
            let fixture = Fixture::new();
            let path = "literal/path with spaces.bin";
            let bytes = if gnu {
                format!("{path}\0").into_bytes()
            } else {
                pax_record("path", path)
            };
            let kind = if gnu {
                tar::EntryType::GNULongName
            } else {
                tar::EntryType::XHeader
            };
            let archive = fixture.raw_archive(
                AvrArchiveRole::Platform,
                &[
                    ("extension", kind, "", &bytes, 0o644),
                    (
                        "placeholder",
                        tar::EntryType::Regular,
                        "",
                        b"\0\xffpayload",
                        0o644,
                    ),
                ],
            );
            let (tree, files) = fixture.extract(archive).unwrap();
            assert_eq!(files.len(), 1);
            assert_eq!(files[0].path, format!("core/platforms/atmelavr/{path}"));
            assert_eq!(
                fs::read(tree.root.join(&files[0].path)).unwrap(),
                b"\0\xffpayload"
            );
        }
    }

    #[test]
    fn pax_traversal_sparse_unknown_keys_and_duplicate_keys_refuse() {
        let mut duplicate = pax_record("path", "first");
        duplicate.extend(pax_record("path", "second"));
        for (bytes, expected) in [
            (
                pax_record("path", "../escape"),
                AvrPreparationRefusal::UnsafePath,
            ),
            (
                pax_record("GNU.sparse.map", "0,1"),
                AvrPreparationRefusal::UnsupportedEntry,
            ),
            (
                pax_record("SCHILY.xattr.user.data", "unqualified"),
                AvrPreparationRefusal::UnsupportedEntry,
            ),
            (
                pax_record("linkpath", "target"),
                AvrPreparationRefusal::UnsupportedEntry,
            ),
            (duplicate, AvrPreparationRefusal::InvalidArchive),
            (
                pax_record("size", "999"),
                AvrPreparationRefusal::InvalidArchive,
            ),
            (
                b"99 path=incorrect-length\n".to_vec(),
                AvrPreparationRefusal::InvalidArchive,
            ),
            (
                b"12 path=a\n\n".to_vec(),
                AvrPreparationRefusal::InvalidArchive,
            ),
        ] {
            let fixture = Fixture::new();
            let archive = fixture.raw_archive(
                AvrArchiveRole::Platform,
                &[
                    ("extension", tar::EntryType::XHeader, "", &bytes, 0o644),
                    ("safe", tar::EntryType::Regular, "", b"data", 0o644),
                ],
            );
            assert!(
                matches!(fixture.extract(archive),Err(value) if value==expected),
                "{bytes:?}"
            );
            assert!(!fixture.root.join("escape").exists());
        }
    }

    #[test]
    fn oversized_repeated_and_dangling_extensions_cannot_be_silently_applied() {
        let record = pax_record("path", "effective");
        let oversized = vec![b'a'; MAX_EXTENSION_BYTES as usize + 1];
        for entries in [
            vec![(
                "extension",
                tar::EntryType::XHeader,
                "",
                oversized.as_slice(),
                0o644,
            )],
            vec![(
                "extension",
                tar::EntryType::XHeader,
                "",
                record.as_slice(),
                0o644,
            )],
            vec![
                (
                    "extension",
                    tar::EntryType::XHeader,
                    "",
                    record.as_slice(),
                    0o644,
                ),
                (
                    "extension2",
                    tar::EntryType::XHeader,
                    "",
                    record.as_slice(),
                    0o644,
                ),
            ],
            vec![(
                "extension",
                tar::EntryType::GNULongName,
                "",
                b"missing-terminator".as_slice(),
                0o644,
            )],
        ] {
            let fixture = Fixture::new();
            let archive = fixture.raw_archive(AvrArchiveRole::Platform, &entries);
            assert!(matches!(
                fixture.extract(archive),
                Err(AvrPreparationRefusal::InvalidArchive)
            ));
        }
    }

    #[test]
    fn nonempty_directory_and_invalid_tar_header_checksum_refuse() {
        let fixture = Fixture::new();
        let archive = fixture.raw_archive(
            AvrArchiveRole::Platform,
            &[(
                "directory/",
                tar::EntryType::Directory,
                "",
                b"unexpected",
                0o755,
            )],
        );
        assert!(matches!(
            fixture.extract(archive),
            Err(AvrPreparationRefusal::InvalidArchive)
        ));
        let fixture = Fixture::new();
        let archive = fixture.archive(
            AvrArchiveRole::Platform,
            &[("safe", b"data".to_vec(), 0o644)],
        );
        let mut decoded = Vec::new();
        MultiGzDecoder::new(fs::read(archive.archive_path).unwrap().as_slice())
            .read_to_end(&mut decoded)
            .unwrap();
        decoded[0] ^= 1;
        let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
        encoder.write_all(&decoded).unwrap();
        let archive = fixture.encoded_archive(AvrArchiveRole::Platform, encoder.finish().unwrap());
        assert!(matches!(
            fixture.extract(archive),
            Err(AvrPreparationRefusal::InvalidArchive)
        ));
    }

    fn reviewed_input() -> AvrArchiveInput {
        AvrArchiveInput {
            role: AvrArchiveRole::Toolchain,
            archive_path: PathBuf::new(),
            expected_sha256: REVIEWED_LINUX_TOOLCHAIN_SHA.into(),
            expected_size: 1,
            host: AvrArchiveHost::Native(AvrFixtureHost::LinuxX86_64),
        }
    }

    #[test]
    fn reviewed_link_copy_policy_binds_digest_role_host_and_exact_mapping() {
        let mut input = reviewed_input();
        let (path, target) = REVIEWED_LINKS[0];
        assert!(reviewed_link(&input, path, target));
        assert!(!reviewed_link(&input, path, "../libcc1plugin.so.0.0.0"));
        assert!(!reviewed_link(&input, "lib/other", target));
        input.role = AvrArchiveRole::Framework;
        assert!(!reviewed_link(&input, path, target));
        input.role = AvrArchiveRole::Toolchain;
        input.host = AvrArchiveHost::Native(AvrFixtureHost::LinuxAarch64);
        assert!(!reviewed_link(&input, path, target));
        input.host = AvrArchiveHost::Native(AvrFixtureHost::LinuxX86_64);
        input.expected_sha256 = "0".repeat(64);
        assert!(!reviewed_link(&input, path, target));
    }

    #[test]
    fn reviewed_links_become_independent_regular_copies_with_recorded_bytes() {
        let fixture = Fixture::new();
        let mut tree = Tree::reserve(&fixture.root, "copies").unwrap();
        let input = reviewed_input();
        let prefix = input.role.destination();
        let (link, target_leaf) = REVIEWED_LINKS[0];
        let parent = link.rsplit_once('/').unwrap().0;
        let target = format!("{prefix}/{parent}/{target_leaf}");
        let payload = b"target\0\xff bytes";
        let file = tree.create_file(&target).unwrap();
        {
            let mut write = &file;
            write.write_all(payload).unwrap();
        }
        set_mode(&file, 0o755).unwrap();
        let mut files = vec![PreparedAvrFile {
            path: target.clone(),
            sha256: format!("{:x}", Sha256::digest(payload)),
            bytes: payload.len() as u64,
            unix_mode: 0o755,
        }];
        let mut total = payload.len() as u64;
        let mut copies = Vec::new();
        materialize_links(
            &input,
            &mut tree,
            &mut files,
            &mut total,
            &mut copies,
            vec![(link.into(), target_leaf.into())],
        )
        .unwrap();
        assert_eq!(copies.len(), 1);
        assert_eq!(files.len(), 2);
        assert_eq!(total, 2 * payload.len() as u64);
        assert_eq!(copies[0].target, target);
        assert_eq!(copies[0].path, format!("{prefix}/{link}"));
        assert_eq!(fs::read(tree.root.join(&copies[0].path)).unwrap(), payload);
        assert!(fs::symlink_metadata(tree.root.join(&copies[0].path))
            .unwrap()
            .is_file());
        assert!(
            single_link(&open_native(&tree.root.join(&copies[0].path), false).unwrap()).unwrap()
        );
        fs::write(tree.root.join(&target), b"modified original").unwrap();
        assert_eq!(fs::read(tree.root.join(&copies[0].path)).unwrap(), payload);
        assert_eq!(
            materialize_links(
                &input,
                &mut tree,
                &mut files,
                &mut total,
                &mut copies,
                vec![(link.into(), target_leaf.into())]
            ),
            Err(AvrPreparationRefusal::Changed)
        );
    }

    #[test]
    fn missing_link_target_cannot_be_adopted() {
        let fixture = Fixture::new();
        let mut tree = Tree::reserve(&fixture.root, "missing").unwrap();
        let (path, target) = REVIEWED_LINKS[0];
        assert_eq!(
            materialize_links(
                &reviewed_input(),
                &mut tree,
                &mut Vec::new(),
                &mut 0,
                &mut Vec::new(),
                vec![(path.into(), target.into())]
            ),
            Err(AvrPreparationRefusal::UnsupportedEntry)
        );
    }

    #[test]
    fn reviewed_link_target_must_remain_regular_and_copy_path_must_be_new() {
        for replaced_by_directory in [false, true] {
            let fixture = Fixture::new();
            let mut tree = Tree::reserve(&fixture.root, "copies").unwrap();
            let input = reviewed_input();
            let (path, target_leaf) = REVIEWED_LINKS[0];
            let parent = path.rsplit_once('/').unwrap().0;
            let target = format!("{}/{parent}/{target_leaf}", input.role.destination());
            let mut target_file = tree.create_file(&target).unwrap();
            target_file.write_all(b"target").unwrap();
            set_mode(&target_file, 0o644).unwrap();
            drop(target_file);
            let mut files = vec![PreparedAvrFile {
                path: target.clone(),
                sha256: format!("{:x}", Sha256::digest(b"target")),
                bytes: 6,
                unix_mode: 0o644,
            }];
            let output = format!("{}/{path}", input.role.destination());
            let expected = if replaced_by_directory {
                fs::remove_file(tree.root.join(&target)).unwrap();
                fs::create_dir(tree.root.join(&target)).unwrap();
                AvrPreparationRefusal::Changed
            } else {
                let mut file = tree.create_file(&output).unwrap();
                file.write_all(b"keep existing").unwrap();
                AvrPreparationRefusal::UnsafePath
            };
            assert_eq!(
                materialize_links(
                    &input,
                    &mut tree,
                    &mut files,
                    &mut 6,
                    &mut Vec::new(),
                    vec![(path.into(), target_leaf.into())]
                ),
                Err(expected)
            );
            if !replaced_by_directory {
                assert_eq!(fs::read(tree.root.join(&output)).unwrap(), b"keep existing");
            }
        }
    }

    #[test]
    fn reviewed_link_copies_obey_aggregate_file_count_and_byte_bounds() {
        for file_limit in [false, true] {
            let fixture = Fixture::new();
            let mut tree = Tree::reserve(&fixture.root, "copies").unwrap();
            let input = reviewed_input();
            let (path, target_leaf) = REVIEWED_LINKS[0];
            let parent = path.rsplit_once('/').unwrap().0;
            let target = format!("{}/{parent}/{target_leaf}", input.role.destination());
            let mut file = tree.create_file(&target).unwrap();
            file.write_all(b"target").unwrap();
            set_mode(&file, 0o644).unwrap();
            let mut files = vec![PreparedAvrFile {
                path: target,
                sha256: format!("{:x}", Sha256::digest(b"target")),
                bytes: 6,
                unix_mode: 0o644,
            }];
            let mut total = if file_limit { 6 } else { MAX_TOTAL_BYTES };
            if file_limit {
                files.extend((1..MAX_FILES).map(|index| PreparedAvrFile {
                    path: format!("unrelated-{index}"),
                    sha256: String::new(),
                    bytes: 0,
                    unix_mode: 0,
                }));
            }
            let mut links = Vec::new();
            assert_eq!(
                materialize_links(
                    &input,
                    &mut tree,
                    &mut files,
                    &mut total,
                    &mut links,
                    vec![(path.into(), target_leaf.into())]
                ),
                Err(AvrPreparationRefusal::TooLarge)
            );
            assert!(!tree
                .root
                .join(format!("{}/{path}", input.role.destination()))
                .exists());
            assert!(links.is_empty());
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn source_symlink_and_hardlink_aliases_are_refused_without_mutation() {
        use std::os::unix::fs::symlink;
        let fixture = Fixture::new();
        let mut archives = fixture.archives();
        let path = fixture.root.join("alias.tar.gz");
        symlink(&archives[0].archive_path, &path).unwrap();
        archives[0].archive_path = path;
        assert_eq!(
            fixture.run(archives).unwrap_err(),
            AvrPreparationRefusal::UnsafePath
        );
        let archives = fixture.archives();
        fs::hard_link(
            &archives[0].archive_path,
            fixture.root.join("hardlink.tar.gz"),
        )
        .unwrap();
        assert_eq!(
            fixture.run(archives).unwrap_err(),
            AvrPreparationRefusal::UnsafePath
        );
    }
}
