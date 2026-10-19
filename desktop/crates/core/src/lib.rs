//! Headless, explicitly registered project services for the migration preview.
//!
//! This crate never picks a project from the process working directory and never
//! reads caches, IDE metadata, settings, hardware, or nested sketch folders.
//! Call it on a worker thread behind one native host owner. Revision checks and
//! source-byte digests reject known stale saves; they are not an interprocess
//! lock. In particular, an unrelated writer can still race the final filesystem
//! replace. The preview must stay fixture-only until shared store/workspace
//! leases and native filesystem adversarial checks have been qualified.

use mcu_contracts::{
    ContentDigest, DocumentId, DocumentSnapshot, ErrorCode, Revision, SaveAcknowledgement,
    SaveDocumentRequest, ScaffoldOptions, ScaffoldTemplate, ServiceError, ServiceResult,
    SourceFile, SourceManifest, WorkspaceId, WorkspaceRegistration, PROTOCOL_VERSION,
};
pub mod ownership;
pub mod preferences;
pub mod resource_budget;
pub mod serial_send;
pub mod serial_text;
pub mod source_names;
pub mod target_transport;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashSet};
use std::fs::{self, File, Metadata, OpenOptions};
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

const MAX_EXACT_REVISION: u64 = (1u64 << 53) - 1;
static NEXT_SERVICE: AtomicU64 = AtomicU64::new(1);

#[derive(Clone, Debug)]
pub struct ProjectLimits {
    pub max_source_bytes: u64,
    pub max_manifest_bytes: u64,
    pub max_sources: usize,
    pub max_root_entries: usize,
    pub max_workspaces: usize,
}

impl Default for ProjectLimits {
    fn default() -> Self {
        Self {
            max_source_bytes: 8 * 1024 * 1024,
            max_manifest_bytes: 32 * 1024 * 1024,
            max_sources: 256,
            max_root_entries: 4096,
            max_workspaces: 16,
        }
    }
}

struct Workspace {
    owner: String,
    root: PathBuf,
    revision: Revision,
    files: BTreeMap<String, SourceFile>,
}

struct ScannedSource {
    name: String,
    bytes: Vec<u8>,
}

/// One native owner serializes all calls. No service is registered implicitly.
pub struct ProjectService {
    application_root: PathBuf,
    fixture_root: Option<PathBuf>,
    limits: ProjectLimits,
    session: String,
    next_id: u64,
    workspaces: BTreeMap<WorkspaceId, Workspace>,
}

impl ProjectService {
    pub fn new(application_root: impl AsRef<Path>) -> ServiceResult<Self> {
        Self::with_limits(application_root, ProjectLimits::default())
    }

    pub fn with_limits(
        application_root: impl AsRef<Path>,
        limits: ProjectLimits,
    ) -> ServiceResult<Self> {
        let application_root = checked_directory(application_root.as_ref())?;
        if limits.max_source_bytes == 0
            || limits.max_manifest_bytes < limits.max_source_bytes
            || limits.max_source_bytes >= usize::MAX as u64
            || limits.max_sources == 0
            || limits.max_root_entries == 0
            || limits.max_workspaces == 0
        {
            return Err(error(
                ErrorCode::LimitExceeded,
                "Invalid project-service limits.",
            ));
        }
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let session = format!(
            "{:x}-{:x}-{:x}",
            std::process::id(),
            nanos,
            NEXT_SERVICE.fetch_add(1, Ordering::Relaxed)
        );
        Ok(Self {
            application_root,
            fixture_root: None,
            limits,
            session,
            next_id: 1,
            workspaces: BTreeMap::new(),
        })
    }

    /// Explicit developer-only authority for the isolated prototype. This is a
    /// host constructor, never an IPC request. Every project/create parent must
    /// remain below the named sandbox in `audit/migration/scratch`.
    pub fn fixture(
        application_root: impl AsRef<Path>,
        sandbox_root: impl AsRef<Path>,
    ) -> ServiceResult<Self> {
        let mut service = Self::new(application_root)?;
        let sandbox = checked_directory(sandbox_root.as_ref())?;
        let scratch = service.application_root.join("audit/migration/scratch");
        if !sandbox.starts_with(&scratch) {
            return Err(error(
                ErrorCode::ForbiddenRoot,
                "Fixture projects must stay inside audit/migration/scratch.",
            ));
        }
        service.fixture_root = Some(sandbox);
        Ok(service)
    }

    pub fn limits(&self) -> &ProjectLimits {
        &self.limits
    }

    /// `owner` comes from the native invoking window, never renderer input.
    pub fn register_project(
        &mut self,
        owner: &str,
        root: impl AsRef<Path>,
    ) -> ServiceResult<WorkspaceRegistration> {
        validate_owner(owner)?;
        let root = self.allowed_directory(root.as_ref())?;
        if let Some((id, workspace)) = self.workspaces.iter().find(|(_, w)| w.root == root) {
            if workspace.owner != owner {
                return Err(error(
                    ErrorCode::WorkspaceBusy,
                    "This project already belongs to another preview window.",
                ));
            }
            let id = id.clone();
            let manifest = self.manifest(owner, &id)?;
            return Ok(registration(&id, &root, manifest));
        }
        if self.workspaces.len() >= self.limits.max_workspaces {
            return Err(error(
                ErrorCode::LimitExceeded,
                "Too many registered workspaces.",
            ));
        }
        // Complete the strict scan before publishing a workspace. A failed scan
        // never certifies a missing/partially readable project as an empty set.
        let scanned = scan_sources(&root, &self.limits)?;
        let id = WorkspaceId(self.allocate_id("workspace")?);
        let mut workspace = Workspace {
            owner: owner.into(),
            root: root.clone(),
            revision: Revision(1),
            files: BTreeMap::new(),
        };
        for source in scanned {
            let document_id = DocumentId(self.allocate_id("document")?);
            let file = source_file(document_id, &source, Revision(1));
            workspace.files.insert(source.name, file);
        }
        let manifest = workspace_manifest(&id, &workspace);
        self.workspaces.insert(id.clone(), workspace);
        Ok(registration(&id, &root, manifest))
    }

    pub fn unregister_project(&mut self, owner: &str, id: &WorkspaceId) -> ServiceResult<()> {
        self.workspace(owner, id)?;
        self.workspaces.remove(id);
        Ok(())
    }

    pub fn manifest(&mut self, owner: &str, id: &WorkspaceId) -> ServiceResult<SourceManifest> {
        let root = self.workspace(owner, id)?.root.clone();
        self.revalidate_root(&root)?;
        let scanned = scan_sources(&root, &self.limits)?;
        let current = &self.workspace(owner, id)?.files;
        let mut updated = BTreeMap::new();
        let mut changed = current.len() != scanned.len();
        for source in scanned {
            let old = current.get(&source.name);
            let digest = source_digest(&source.bytes);
            let file = if let Some(old) = old {
                let mut file = old.clone();
                if file.content_digest != digest {
                    file.revision = next_revision(file.revision)?;
                    file.content_digest = digest;
                    file.size_bytes = source.bytes.len() as u64;
                    changed = true;
                }
                file
            } else {
                // Allocate after leaving the immutable workspace borrow.
                changed = true;
                source_file(DocumentId(String::new()), &source, Revision(1))
            };
            updated.insert(source.name, file);
        }
        for file in updated.values_mut().filter(|f| f.document_id.0.is_empty()) {
            file.document_id = DocumentId(self.allocate_id("document")?);
        }
        let workspace = self.workspaces.get_mut(id).expect("owner checked above");
        if changed {
            workspace.revision = next_revision(workspace.revision)?;
            workspace.files = updated;
        }
        Ok(workspace_manifest(id, workspace))
    }

    pub fn read_document(
        &mut self,
        owner: &str,
        workspace_id: &WorkspaceId,
        document_id: &DocumentId,
    ) -> ServiceResult<DocumentSnapshot> {
        let (root, file) = self.registered_document(owner, workspace_id, document_id)?;
        self.revalidate_root(&root)?;
        let bytes = read_source(&root, &file.name, self.limits.max_source_bytes)?;
        let content = String::from_utf8(bytes.clone()).map_err(|_| {
            error(
                ErrorCode::InvalidEncoding,
                "Source is not valid UTF-8. It was not decoded lossily or changed.",
            )
        })?;
        let digest = source_digest(&bytes);
        let mut file = file;
        if file.content_digest != digest {
            file.content_digest = digest;
            file.size_bytes = bytes.len() as u64;
            file.revision = next_revision(file.revision)?;
            let workspace = self
                .workspaces
                .get_mut(workspace_id)
                .expect("owner checked above");
            workspace.revision = next_revision(workspace.revision)?;
            workspace.files.insert(file.name.clone(), file.clone());
        }
        Ok(DocumentSnapshot {
            workspace_id: workspace_id.clone(),
            document: file,
            content,
        })
    }

    /// Acknowledges only successfully replaced bytes (or a true no-op). The
    /// renderer must additionally compare `edit_revision` before clearing dirty.
    pub fn save_document(
        &mut self,
        owner: &str,
        request: SaveDocumentRequest,
    ) -> ServiceResult<SaveAcknowledgement> {
        if request.edit_revision.0 > MAX_EXACT_REVISION {
            return Err(error(
                ErrorCode::LimitExceeded,
                "Renderer revision exceeds the wire limit.",
            ));
        }
        if request.content.len() as u64 > self.limits.max_source_bytes {
            return Err(error(
                ErrorCode::TooLarge,
                "Source exceeds the per-file byte limit.",
            ));
        }
        let (root, baseline) =
            self.registered_document(owner, &request.workspace_id, &request.document_id)?;
        self.revalidate_root(&root)?;
        if baseline.revision != request.expected_revision {
            return Err(error(
                ErrorCode::StaleRevision,
                "Document revision changed. Read it again before saving.",
            ));
        }
        if baseline.content_digest != request.expected_digest {
            return Err(error(
                ErrorCode::ExternalChange,
                "Save digest does not match the registered source.",
            ));
        }
        let previous = read_source(&root, &baseline.name, self.limits.max_source_bytes)?;
        if source_digest(&previous) != request.expected_digest {
            return Err(error(
                ErrorCode::ExternalChange,
                "Source changed on disk. Your unsaved buffer was not written.",
            ));
        }
        let encoded = request.content.as_bytes();
        let unchanged = previous == encoded;
        let mut file = baseline;
        if !unchanged {
            let next_document_revision = next_revision(file.revision)?;
            let next_workspace_revision =
                next_revision(self.workspace(owner, &request.workspace_id)?.revision)?;
            let total_bytes = self
                .workspace(owner, &request.workspace_id)?
                .files
                .values()
                .map(|source| source.size_bytes)
                .sum::<u64>();
            if total_bytes
                .saturating_sub(file.size_bytes)
                .saturating_add(encoded.len() as u64)
                > self.limits.max_manifest_bytes
            {
                return Err(error(
                    ErrorCode::TooLarge,
                    "Project source bytes exceed the manifest limit.",
                ));
            }
            let temporary_id = self.allocate_id("save")?;
            replace_source(
                &root,
                &file.name,
                &previous,
                encoded,
                &temporary_id,
                self.limits.max_source_bytes,
            )?;
            file.revision = next_document_revision;
            file.content_digest = source_digest(encoded);
            file.size_bytes = encoded.len() as u64;
            let workspace = self
                .workspaces
                .get_mut(&request.workspace_id)
                .expect("owner checked above");
            workspace.revision = next_workspace_revision;
            workspace.files.insert(file.name.clone(), file.clone());
        }
        Ok(SaveAcknowledgement {
            workspace_id: request.workspace_id,
            document_id: request.document_id,
            revision: file.revision,
            content_digest: file.content_digest,
            edit_revision: request.edit_revision,
            bytes_written: if unchanged { 0 } else { encoded.len() as u64 },
            unchanged,
        })
    }

    /// Parent comes from a trusted native chooser/fixture controller. Scaffold
    /// uses create-only ownership; an existing folder is never adopted.
    pub fn create_project(
        &mut self,
        owner: &str,
        parent: impl AsRef<Path>,
        options: ScaffoldOptions,
    ) -> ServiceResult<WorkspaceRegistration> {
        validate_owner(owner)?;
        let parent = self.allowed_directory(parent.as_ref())?;
        let name = sanitized_project_name(&options.name);
        source_extension(&format!("{name}.ino"))?;
        source_extension(&format!("{name}.cpp"))?;
        let target = parent.join(&name);
        let files = scaffold_files(&name, &options);
        if files.len() > self.limits.max_sources
            || files
                .iter()
                .any(|(_, bytes)| bytes.len() as u64 > self.limits.max_source_bytes)
            || files
                .iter()
                .map(|(_, bytes)| bytes.len() as u64)
                .sum::<u64>()
                > self.limits.max_manifest_bytes
        {
            return Err(error(
                ErrorCode::TooLarge,
                "Scaffold exceeds source limits.",
            ));
        }
        fs::create_dir(&target).map_err(|e| io_error("Create project folder", e))?;
        // On failure retain the newly owned partial folder for inspection; do
        // not recursively clean anything or adopt an existing folder on retry.
        for (filename, bytes) in files {
            // The legacy scaffold opens text with newline=None: native CRLF on
            // Windows, LF on Linux. Existing source saves never translate EOLs.
            #[cfg(windows)]
            let bytes = bytes.replace('\n', "\r\n");
            let mut stream = OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(target.join(filename))
                .map_err(|e| io_error("Create scaffold source", e))?;
            stream
                .write_all(bytes.as_bytes())
                .map_err(|e| io_error("Write scaffold source", e))?;
            stream
                .sync_all()
                .map_err(|e| io_error("Flush scaffold source", e))?;
        }
        self.register_project(owner, target)
    }

    fn allocate_id(&mut self, kind: &str) -> ServiceResult<String> {
        let number = self.next_id;
        self.next_id = number
            .checked_add(1)
            .ok_or_else(|| error(ErrorCode::LimitExceeded, "Service identity limit reached."))?;
        Ok(format!("{kind}-{}-{number}", self.session))
    }

    fn allowed_directory(&self, path: &Path) -> ServiceResult<PathBuf> {
        let root = checked_directory(path)?;
        if let Some(sandbox) = &self.fixture_root {
            if !root.starts_with(sandbox) {
                return Err(error(
                    ErrorCode::ForbiddenRoot,
                    "This preview only opens its explicit fixture sandbox.",
                ));
            }
        } else if root.starts_with(&self.application_root) {
            return Err(error(
                ErrorCode::ForbiddenRoot,
                "The application checkout is not a sketch project.",
            ));
        }
        if is_application_root(&root) {
            return Err(error(
                ErrorCode::ForbiddenRoot,
                "The selected folder contains MCU Flasher application code.",
            ));
        }
        Ok(root)
    }

    fn revalidate_root(&self, root: &Path) -> ServiceResult<()> {
        if self.allowed_directory(root)? != root {
            return Err(error(
                ErrorCode::InvalidPath,
                "Registered project root changed.",
            ));
        }
        Ok(())
    }

    fn workspace(&self, owner: &str, id: &WorkspaceId) -> ServiceResult<&Workspace> {
        let workspace = self.workspaces.get(id).ok_or_else(|| {
            error(
                ErrorCode::WorkspaceNotFound,
                "Workspace is not registered in this session.",
            )
        })?;
        if workspace.owner != owner {
            return Err(error(
                ErrorCode::WrongOwner,
                "Workspace belongs to a different native window.",
            ));
        }
        Ok(workspace)
    }

    fn registered_document(
        &self,
        owner: &str,
        workspace_id: &WorkspaceId,
        document_id: &DocumentId,
    ) -> ServiceResult<(PathBuf, SourceFile)> {
        let workspace = self.workspace(owner, workspace_id)?;
        let document = workspace
            .files
            .values()
            .find(|file| &file.document_id == document_id)
            .ok_or_else(|| {
                error(
                    ErrorCode::NotFound,
                    "Document is not registered in this workspace.",
                )
            })?;
        Ok((workspace.root.clone(), document.clone()))
    }
}

fn error(code: ErrorCode, message: &str) -> ServiceError {
    ServiceError {
        code,
        message: message.into(),
        retryable: false,
    }
}

fn io_error(action: &str, error_value: std::io::Error) -> ServiceError {
    let code = match error_value.kind() {
        std::io::ErrorKind::NotFound => ErrorCode::NotFound,
        std::io::ErrorKind::AlreadyExists => ErrorCode::AlreadyExists,
        std::io::ErrorKind::PermissionDenied => ErrorCode::ReadOnly,
        _ => ErrorCode::Io,
    };
    ServiceError {
        code,
        message: format!("{action}: {error_value}"),
        retryable: false,
    }
}

fn next_revision(revision: Revision) -> ServiceResult<Revision> {
    if revision.0 >= MAX_EXACT_REVISION {
        return Err(error(
            ErrorCode::LimitExceeded,
            "Document revision limit reached.",
        ));
    }
    Ok(Revision(revision.0 + 1))
}

fn validate_owner(owner: &str) -> ServiceResult<()> {
    if owner.is_empty() || owner.len() > 128 || owner.chars().any(char::is_control) {
        return Err(error(
            ErrorCode::WrongOwner,
            "A valid native window owner is required.",
        ));
    }
    Ok(())
}

fn is_link(metadata: &Metadata) -> bool {
    if metadata.file_type().is_symlink() {
        return true;
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        // Reject junctions and other reparse points, not only symlink entries.
        metadata.file_attributes() & 0x400 != 0
    }
    #[cfg(not(windows))]
    {
        false
    }
}

fn checked_directory(path: &Path) -> ServiceResult<PathBuf> {
    if !path.is_absolute() {
        return Err(error(
            ErrorCode::InvalidPath,
            "An explicit absolute directory is required.",
        ));
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        // A canonical Windows path has a verbatim prefix. Its components do
        // not recognize '/' as a separator, so reject mixed-separator parent
        // syntax lexically before any metadata call rather than allowing a
        // malformed path to reach the filesystem or normalizing it elsewhere.
        if windows_has_parent_segment(path.as_os_str().encode_wide()) {
            return Err(error(
                ErrorCode::InvalidPath,
                "Parent traversal is not allowed.",
            ));
        }
    }
    let mut prefix = PathBuf::new();
    for component in path.components() {
        if matches!(component, Component::ParentDir) {
            return Err(error(
                ErrorCode::InvalidPath,
                "Parent traversal is not allowed.",
            ));
        }
        prefix.push(component.as_os_str());
        // A Windows drive/UNC prefix alone is not a full filesystem location.
        // Inspect it after RootDir completes the absolute path.
        if matches!(component, Component::Prefix(_)) {
            continue;
        }
        let metadata =
            fs::symlink_metadata(&prefix).map_err(|e| io_error("Inspect project path", e))?;
        if is_link(&metadata) {
            return Err(error(
                ErrorCode::Symlink,
                "Symlink/reparse project paths are not allowed.",
            ));
        }
    }
    let canonical = fs::canonicalize(path).map_err(|e| io_error("Resolve project directory", e))?;
    if !fs::metadata(&canonical)
        .map_err(|e| io_error("Inspect project directory", e))?
        .is_dir()
    {
        return Err(error(
            ErrorCode::InvalidPath,
            "Project root must be a directory.",
        ));
    }
    Ok(canonical)
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

fn is_application_root(root: &Path) -> bool {
    root.join("main/web_bridge.py").is_file()
        || root.join("main/mcu_flash_gui.py").is_file()
        || root.join("src/modules/bootstrap.py").is_file()
}

/// Portable root source filename rules also reject Windows device/ADS aliases
/// on Linux, so a project accepted here does not gain unsafe names on Windows.
fn source_extension(name: &str) -> ServiceResult<String> {
    if name.is_empty()
        || name.len() > 255
        || name != name.trim()
        || name.starts_with('.')
        || name.ends_with('.')
        || name
            .chars()
            .any(|c| c.is_control() || "\\/:<>\"|?*".contains(c))
        || Path::new(name).components().count() != 1
    {
        return Err(error(
            ErrorCode::InvalidPath,
            "Use one visible root source filename.",
        ));
    }
    if source_names::is_windows_reserved_device_name(name) {
        return Err(error(
            ErrorCode::InvalidPath,
            "Windows reserved device filenames are not allowed.",
        ));
    }
    let extension = Path::new(name)
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    if !matches!(
        extension.as_str(),
        "ino" | "cpp" | "c" | "h" | "hpp" | "txt"
    ) {
        return Err(error(
            ErrorCode::UnsupportedFile,
            "Supported root files are .ino, .cpp, .c, .h, .hpp and .txt.",
        ));
    }
    Ok(format!(".{extension}"))
}

fn source_path(root: &Path, name: &str) -> ServiceResult<PathBuf> {
    source_extension(name)?;
    let path = root.join(name);
    let metadata = fs::symlink_metadata(&path).map_err(|e| io_error("Inspect source file", e))?;
    if is_link(&metadata) {
        return Err(error(
            ErrorCode::Symlink,
            "Source symlinks/reparse points are not allowed.",
        ));
    }
    if !metadata.is_file() {
        return Err(error(
            ErrorCode::InvalidPath,
            "Source must be a regular root file.",
        ));
    }
    if fs::canonicalize(&path)
        .map_err(|e| io_error("Resolve source file", e))?
        .parent()
        != Some(root)
    {
        return Err(error(
            ErrorCode::InvalidPath,
            "Source escaped its registered root.",
        ));
    }
    Ok(path)
}

fn read_source(root: &Path, name: &str, limit: u64) -> ServiceResult<Vec<u8>> {
    let path = source_path(root, name)?;
    let mut stream = File::open(&path).map_err(|e| io_error("Open source file", e))?;
    let metadata = stream
        .metadata()
        .map_err(|e| io_error("Inspect open source file", e))?;
    if !metadata.is_file() {
        return Err(error(
            ErrorCode::InvalidPath,
            "Opened source is not a regular file.",
        ));
    }
    if metadata.len() > limit {
        return Err(error(
            ErrorCode::TooLarge,
            "Source exceeds the per-file byte limit.",
        ));
    }
    let mut bytes = Vec::with_capacity(metadata.len() as usize);
    Read::by_ref(&mut stream)
        .take(limit + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| io_error("Read source file", e))?;
    if bytes.len() as u64 > limit {
        return Err(error(
            ErrorCode::TooLarge,
            "Source grew beyond the per-file byte limit.",
        ));
    }
    // Recheck aliases after the read. Fully adversarial check/use races require
    // host-native directory handles and are an explicit later qualification gate.
    source_path(root, name)?;
    Ok(bytes)
}

fn scan_sources(root: &Path, limits: &ProjectLimits) -> ServiceResult<Vec<ScannedSource>> {
    let entries = fs::read_dir(root).map_err(|e| io_error("Read project directory", e))?;
    let mut sources = Vec::new();
    let mut total = 0u64;
    let mut names = HashSet::new();
    for (index, entry) in entries.enumerate() {
        if index >= limits.max_root_entries {
            return Err(error(
                ErrorCode::LimitExceeded,
                "Project root has too many entries for a bounded scan.",
            ));
        }
        let entry = entry.map_err(|e| io_error("Read root directory entry", e))?;
        // No recursion: folders, caches and IDE metadata are never opened.
        let name = entry.file_name().into_string().map_err(|_| {
            error(
                ErrorCode::InvalidPath,
                "Root filename is not valid Unicode.",
            )
        })?;
        if name.starts_with('.') {
            continue;
        }
        let extension = Path::new(&name)
            .extension()
            .and_then(|s| s.to_str())
            .unwrap_or_default()
            .to_ascii_lowercase();
        if !matches!(
            extension.as_str(),
            "ino" | "cpp" | "c" | "h" | "hpp" | "txt"
        ) {
            continue;
        }
        let metadata =
            fs::symlink_metadata(entry.path()).map_err(|e| io_error("Inspect root source", e))?;
        if is_link(&metadata) {
            return Err(error(
                ErrorCode::Symlink,
                "A root source is a symlink/reparse point.",
            ));
        }
        if metadata.is_dir() {
            continue;
        }
        source_extension(&name)?;
        if !names.insert(name.to_lowercase()) {
            return Err(error(
                ErrorCode::InvalidPath,
                "Source names differ only by case and conflict on Windows.",
            ));
        }
        if sources.len() >= limits.max_sources {
            return Err(error(
                ErrorCode::LimitExceeded,
                "Too many root source files.",
            ));
        }
        let bytes = read_source(root, &name, limits.max_source_bytes)?;
        total = total
            .checked_add(bytes.len() as u64)
            .ok_or_else(|| error(ErrorCode::TooLarge, "Source byte total overflowed."))?;
        if total > limits.max_manifest_bytes {
            return Err(error(
                ErrorCode::TooLarge,
                "Project source bytes exceed the manifest limit.",
            ));
        }
        sources.push(ScannedSource { name, bytes });
    }
    Ok(sources)
}

pub fn source_digest(bytes: &[u8]) -> ContentDigest {
    ContentDigest(format!("{:x}", Sha256::digest(bytes)))
}

fn source_file(document_id: DocumentId, source: &ScannedSource, revision: Revision) -> SourceFile {
    let extension = source_extension(&source.name).expect("source name was validated");
    SourceFile {
        document_id,
        name: source.name.clone(),
        is_main: extension == ".ino",
        extension,
        size_bytes: source.bytes.len() as u64,
        revision,
        content_digest: source_digest(&source.bytes),
    }
}

fn workspace_manifest(id: &WorkspaceId, workspace: &Workspace) -> SourceManifest {
    let mut files: Vec<_> = workspace.files.values().cloned().collect();
    files.sort_by_key(|f| (!f.is_main, f.name.to_lowercase()));
    SourceManifest {
        protocol_version: PROTOCOL_VERSION,
        workspace_id: id.clone(),
        revision: workspace.revision,
        files,
    }
}

fn registration(id: &WorkspaceId, root: &Path, manifest: SourceManifest) -> WorkspaceRegistration {
    WorkspaceRegistration {
        protocol_version: PROTOCOL_VERSION,
        workspace_id: id.clone(),
        root: root.to_string_lossy().into_owned(),
        name: root
            .file_name()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_default(),
        manifest,
    }
}

/// Matches `main/core/project_scaffold.py` ASCII replacement/fallback rules.
pub fn sanitized_project_name(name: &str) -> String {
    let clean: String = name
        .trim()
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '_' || c == '-' {
                c
            } else {
                '_'
            }
        })
        .collect();
    if clean.is_empty() {
        "NewSketch".into()
    } else {
        clean
    }
}

fn scaffold_files(name: &str, options: &ScaffoldOptions) -> Vec<(String, String)> {
    let include = if options.include_h {
        format!("#include \"{name}.h\"\n")
    } else {
        String::new()
    };
    let body = match options.template {
        ScaffoldTemplate::Bare => "void setup() {\n  \n}\n\nvoid loop() {\n  \n}\n",
        ScaffoldTemplate::Blink => "void setup() {\n  pinMode(LED_BUILTIN, OUTPUT);\n}\n\nvoid loop() {\n  digitalWrite(LED_BUILTIN, HIGH);\n  delay(1000);\n  digitalWrite(LED_BUILTIN, LOW);\n  delay(1000);\n}\n",
        ScaffoldTemplate::Standard => "void setup() {\n\n}\n\nvoid loop() {\n\n}\n",
    };
    let mut files = vec![(
        format!("{name}.ino"),
        if include.is_empty() {
            body.into()
        } else {
            format!("{include}\n{body}")
        },
    )];
    if options.include_h {
        files.push((format!("{name}.h"), "#pragma once\n".into()));
    }
    if options.include_cpp {
        files.push((format!("{name}.cpp"), include));
    }
    files
}

struct PendingSource {
    path: PathBuf,
}

impl Drop for PendingSource {
    fn drop(&mut self) {
        // Only this process's create-new staging file; never recurse or clean a
        // project/cache on an error. An interrupted process can leave this file.
        let _ = fs::remove_file(&self.path);
    }
}

fn replace_source(
    root: &Path,
    name: &str,
    previous: &[u8],
    content: &[u8],
    temporary_id: &str,
    limit: u64,
) -> ServiceResult<()> {
    let target = source_path(root, name)?;
    let permissions = fs::metadata(&target)
        .map_err(|e| io_error("Inspect source permissions", e))?
        .permissions();
    if permissions.readonly() {
        return Err(error(
            ErrorCode::ReadOnly,
            "Source is read-only; permissions were not changed.",
        ));
    }
    let pending_path = root.join(format!(".mcu-migration-{temporary_id}.tmp"));
    let mut stream = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&pending_path)
        .map_err(|e| io_error("Create source staging file", e))?;
    let pending = PendingSource { path: pending_path };
    stream
        .write_all(content)
        .map_err(|e| io_error("Write source staging file", e))?;
    stream
        .set_permissions(permissions)
        .map_err(|e| io_error("Preserve source permissions", e))?;
    stream
        .sync_all()
        .map_err(|e| io_error("Flush source staging file", e))?;
    drop(stream);
    if read_source(root, name, limit)? != previous {
        return Err(error(
            ErrorCode::ExternalChange,
            "Source changed while staging the save. Nothing was replaced.",
        ));
    }
    fs::rename(&pending.path, &target).map_err(|e| io_error("Replace source file", e))?;
    // The known final replace race is documented at crate level. Detect a writer
    // that has already superseded our bytes instead of returning a false receipt.
    if read_source(root, name, limit)? != content {
        return Err(error(
            ErrorCode::ExternalChange,
            "Source changed immediately after replacement. Re-read before saving.",
        ));
    }
    Ok(())
}

#[cfg(test)]
mod directory_path_tests {
    use super::*;

    #[test]
    fn root_source_validation_refuses_windows_device_aliases_on_every_host() {
        for name in [
            "COM¹.ino",
            "lpt².cpp",
            "COM³.hpp",
            "COM1 .h",
            "LPT9   .txt",
            "CON .ino",
            "aux .cpp",
        ] {
            assert_eq!(
                source_extension(name).unwrap_err().code,
                ErrorCode::InvalidPath,
                "{name}"
            );
        }
        for name in ["Sketch.ino", "COM10.cpp", "COM１.hpp", "café 日本語.h"] {
            assert!(source_extension(name).is_ok(), "{name}");
        }
    }

    #[test]
    fn windows_parent_syntax_is_rejected_with_native_and_mixed_separators() {
        for path in [
            r"C:\fixture\..\other",
            r"C:/fixture/../other",
            r"\\?\C:\fixture/../../../../",
            r"\\?\C:\fixture\..\..\",
            r"\\?\UNC\server\share\fixture/..\other",
            r"\\server\share\fixture\..",
            r"\\?\C:\café 日本語\../other",
            "..",
            "/../",
            "\\..\\",
        ] {
            assert!(windows_has_parent_segment(path.encode_utf16()), "{path}");
        }
        for path in [
            r"C:\fixture\file.ino",
            r"\\?\C:\fixture\file..ino",
            r"\\?\UNC\server\share\fixture\.hidden",
            r"\\server\share\café 日本語\dots...",
            r"C:\fixture\.",
            r"C:\fixture\..suffix",
            r"C:\fixture\prefix..",
            "",
            ".",
            "...",
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
            let error = checked_directory(Path::new(path)).unwrap_err();
            assert_eq!(error.code, ErrorCode::InvalidPath, "{path}");
        }
    }
}
