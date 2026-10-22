//! Native root-source transactions for one retained, explicitly opened project.
//!
//! No renderer pathname reaches this service. Native ancestry/source identities,
//! exact bytes and an OS-backed save lease are rechecked before replacement.
//! The lease coordinates native save sessions; it does not establish legacy
//! Python ownership or stop unrelated writers. Every *observed* unexpected
//! displaced identity/byte change is retained for recovery and fails the save.
//!
//! Linux transactions use a create-new mode0700 directory and descriptor-relative
//! operations. This is a private transaction boundary, not protection against
//! hostile same-UID mutation. Linux has no unlink-by-identity CAS: another writer
//! can still change an entry after its final cleanup check or write through an
//! old source descriptor. Such final-use races and legacy/Python coexistence
//! remain unqualified; receipts certify observed bytes, not a perpetual lock.

use crate::file_identity::{native_identity, single_link, NativeFileIdentity};
use mcu_contracts::{
    DocumentSnapshot, ErrorCode, Revision, SaveAcknowledgement, SaveDocumentRequest, ServiceError,
    ServiceResult, WorkspaceId,
};
use mcu_core::{source_digest, ProjectService};
use std::fs::{self, File, Metadata, OpenOptions, Permissions, TryLockError};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

const EXACT_REVISION: u64 = (1_u64 << 53) - 1;
const LEASE_NAME: &str = ".mcu-native-save.lock";
const LEASE_MARKER: &[u8] = b"MCU Flasher native source-save lease v1\n";
static NEXT: AtomicU64 = AtomicU64::new(1);

struct SourceHandle {
    file: File,
    identity: NativeFileIdentity,
    edit_revision: Revision,
}

#[derive(Clone, Eq, PartialEq)]
struct PreservationMetadata {
    permissions: Permissions,
    #[cfg(target_os = "linux")]
    uid: u32,
    #[cfg(target_os = "linux")]
    gid: u32,
}

struct SaveLease {
    file: File,
}
impl Drop for SaveLease {
    fn drop(&mut self) {
        // Explicit unlock also releases a Linux flock if another app thread
        // briefly forked while this CLOEXEC handle was live. Closing the fd
        // alone can otherwise leave it locked until that child reaches exec.
        let _ = self.file.unlock();
    }
}

/// Retained native authority for one explicit project. All filesystem calls,
/// including construction and saving, belong on a native worker. Requests are
/// serialized by `&mut self`; a caller may move this owner between workers.
pub struct NativeProjectSession {
    _service: ProjectService,
    owner: String,
    workspace_id: WorkspaceId,
    documents: Vec<DocumentSnapshot>,
    sources: Vec<SourceHandle>,
    directory: Directory,
    ancestors: Vec<Directory>,
    max_source_bytes: u64,
    max_manifest_bytes: u64,
    poisoned: bool,
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum SavePhase {
    Staged,
    Revalidated,
}

impl NativeProjectSession {
    pub fn open(application_root: &Path, selected: &Path, owner: &str) -> ServiceResult<Self> {
        Self::open_inner(application_root, selected, owner, false)
    }

    /// Explicit native-controller constructor for the existing owned GUI
    /// fixture sandbox. It changes project admission only; save semantics and
    /// native filesystem operations are identical to `open`.
    pub fn open_fixture(
        application_root: &Path,
        selected: &Path,
        owner: &str,
    ) -> ServiceResult<Self> {
        Self::open_inner(application_root, selected, owner, true)
    }

    fn open_inner(
        application_root: &Path,
        selected: &Path,
        owner: &str,
        fixture: bool,
    ) -> ServiceResult<Self> {
        validate_owner(owner)?;
        let ancestors = open_ancestry(selected)?;
        let mut directory = ancestors
            .last()
            .ok_or_else(|| refusal(ErrorCode::InvalidPath, "A project directory is required."))?
            .try_clone()?;
        let mut service = if fixture {
            ProjectService::fixture(application_root, selected)
        } else {
            ProjectService::new(application_root)
        }?;
        let registration = service.register_project(owner, selected)?;
        let canonical = fs::canonicalize(&directory.path).map_err(io_error)?;
        if canonical != Path::new(&registration.root) {
            return Err(refusal(
                ErrorCode::ExternalChange,
                "Project root changed while opening.",
            ));
        }
        // Validation used the literal selection. Subsequent Windows native
        // operations retain its canonical verbatim spelling for long paths.
        directory.path = canonical;
        let mut documents = Vec::new();
        let mut sources = Vec::new();
        for file in &registration.manifest.files {
            let snapshot =
                service.read_document(owner, &registration.workspace_id, &file.document_id)?;
            let (mut stream, identity) = directory.source(&file.name)?;
            let bytes = read_bounded(&mut stream, service.limits().max_source_bytes)?;
            if source_digest(&bytes) != snapshot.document.content_digest
                || bytes != snapshot.content.as_bytes()
            {
                return Err(refusal(
                    ErrorCode::ExternalChange,
                    "Source changed while opening the native editor.",
                ));
            }
            sources.push(SourceHandle {
                file: stream,
                identity,
                edit_revision: Revision(0),
            });
            documents.push(snapshot);
        }
        let session = Self {
            max_source_bytes: service.limits().max_source_bytes,
            max_manifest_bytes: service.limits().max_manifest_bytes,
            _service: service,
            owner: owner.into(),
            workspace_id: registration.workspace_id,
            documents,
            sources,
            directory,
            ancestors,
            poisoned: false,
        };
        session.verify_root()?;
        Ok(session)
    }

    pub fn workspace_id(&self) -> &WorkspaceId {
        &self.workspace_id
    }
    pub fn documents(&self) -> &[DocumentSnapshot] {
        &self.documents
    }

    /// Saves one exact native document baseline. A failed response never grants
    /// permission to clear a renderer's dirty buffer. A receipt echoes the
    /// captured renderer revision, which the editor must still compare.
    pub fn save_document(
        &mut self,
        owner: &str,
        request: SaveDocumentRequest,
    ) -> ServiceResult<SaveAcknowledgement> {
        self.save_inner(owner, request, |_, _| Ok(()))
    }

    fn save_inner(
        &mut self,
        owner: &str,
        request: SaveDocumentRequest,
        mut before_commit: impl FnMut(SavePhase, &Directory) -> ServiceResult<()>,
    ) -> ServiceResult<SaveAcknowledgement> {
        validate_owner(owner)?;
        if owner != self.owner {
            return Err(refusal(
                ErrorCode::WrongOwner,
                "This project belongs to a different native window.",
            ));
        }
        if request.workspace_id != self.workspace_id {
            return Err(refusal(
                ErrorCode::WorkspaceNotFound,
                "Workspace is not retained by this native window.",
            ));
        }
        if self.poisoned {
            return Err(refusal(ErrorCode::ExternalChange, "A previous save needs recovery. Reopen this project after inspecting its reported recovery folder."));
        }
        let index = self
            .documents
            .iter()
            .position(|snapshot| snapshot.document.document_id == request.document_id)
            .ok_or_else(|| {
                refusal(
                    ErrorCode::NotFound,
                    "Document is not part of this native project.",
                )
            })?;
        let baseline = &self.documents[index];
        if request.expected_revision != baseline.document.revision {
            return Err(refusal(
                ErrorCode::StaleRevision,
                "The native source revision changed. Your buffer was not written.",
            ));
        }
        if request.expected_digest != baseline.document.content_digest {
            return Err(refusal(
                ErrorCode::ExternalChange,
                "The save does not match the native source baseline.",
            ));
        }
        if request.edit_revision.0 == 0
            || request.edit_revision.0 > EXACT_REVISION
            || request.edit_revision.0 < self.sources[index].edit_revision.0
        {
            return Err(refusal(
                ErrorCode::StaleRevision,
                "The editor save revision is stale or outside the exact wire range.",
            ));
        }
        let encoded = request.content.as_bytes();
        if encoded.len() as u64 > self.max_source_bytes
            || self
                .documents
                .iter()
                .map(|snapshot| snapshot.document.size_bytes)
                .sum::<u64>()
                .saturating_sub(baseline.document.size_bytes)
                .saturating_add(encoded.len() as u64)
                > self.max_manifest_bytes
        {
            return Err(refusal(
                ErrorCode::TooLarge,
                "Source or project bytes exceed the native editor limit.",
            ));
        }
        self.verify_root()?;
        let _lease = self.directory.acquire_lease()?;
        self.verify_root()?;
        self.directory.refuse_existing_transaction()?;
        let (mut current, current_identity) = self.directory.source(&baseline.document.name)?;
        let original_identity = self.sources[index].identity;
        let previous = read_bounded(&mut current, self.max_source_bytes)?;
        if current_identity != original_identity
            || native_identity(&self.sources[index].file).map_err(io_error)? != original_identity
            || previous != baseline.content.as_bytes()
        {
            return Err(refusal(
                ErrorCode::ExternalChange,
                "Source changed on disk. Your unsaved buffer was not written.",
            ));
        }
        let unchanged = previous == encoded;
        let next_revision = if unchanged {
            baseline.document.revision
        } else {
            Revision(
                baseline
                    .document
                    .revision
                    .0
                    .checked_add(1)
                    .filter(|revision| *revision <= EXACT_REVISION)
                    .ok_or_else(|| {
                        refusal(
                            ErrorCode::LimitExceeded,
                            "Native source revision limit reached.",
                        )
                    })?,
            )
        };
        if !unchanged {
            let metadata = preservation_metadata(&current)?;
            if metadata.permissions.readonly() {
                return Err(refusal(
                    ErrorCode::ReadOnly,
                    "Source is read-only; its permissions were preserved.",
                ));
            }
            self.directory
                .verify_write_access(&baseline.document.name, original_identity)?;
            let mut transaction = Transaction::new(&self.directory)?;
            transaction.write(encoded, &metadata)?;
            before_commit(SavePhase::Staged, &self.directory)?;
            self.verify_root()?;
            let (mut checked, identity) = self.directory.source(&baseline.document.name)?;
            if identity != original_identity
                || read_bounded(&mut checked, self.max_source_bytes)? != previous
                || preservation_metadata(&checked)? != metadata
            {
                return Err(refusal(
                    ErrorCode::ExternalChange,
                    "Source changed while staging. Nothing was replaced.",
                ));
            }
            drop(checked);
            self.directory
                .verify_write_access(&baseline.document.name, original_identity)?;
            before_commit(SavePhase::Revalidated, &self.directory)?;
            // Windows replacement opens its staged source exclusively; no
            // staging handle is kept open across the native atomic call.
            if let Err(mut failure) = transaction.commit(&baseline.document.name) {
                self.poisoned = true;
                transaction.retain = true;
                failure.message = format!(
                    "{} Saved bytes may already be present. Recovery material was retained at {}. Your buffer remains marked unsaved.",
                    failure.message,
                    transaction.directory.path.display()
                );
                return Err(failure);
            }
            let observation = (|| {
                self.verify_root()?;
                let (mut old, old_identity) = transaction.displaced()?;
                if old_identity != original_identity
                    || read_bounded(&mut old, self.max_source_bytes)? != previous
                    || preservation_metadata(&old)? != metadata
                {
                    return Err(refusal(
                        ErrorCode::ExternalChange,
                        "Source changed at replacement; its displaced contents were preserved.",
                    ));
                }
                let (mut published, identity) = self.directory.source(&baseline.document.name)?;
                if identity
                    != transaction.staged_identity.ok_or_else(|| {
                        refusal(
                            ErrorCode::Io,
                            "Native staged source identity is unavailable.",
                        )
                    })?
                    || read_bounded(&mut published, self.max_source_bytes)? != encoded
                    || preservation_metadata(&published)? != metadata
                {
                    return Err(refusal(
                        ErrorCode::ExternalChange,
                        "Source changed after replacement.",
                    ));
                }
                Ok(SourceHandle {
                    file: published,
                    identity,
                    edit_revision: request.edit_revision,
                })
            })();
            match observation {
                Ok(source) => {
                    self.sources[index] = source;
                }
                Err(mut failure) => {
                    self.poisoned = true;
                    transaction.retain = true;
                    failure.message = format!(
                        "{} Saved bytes may already be present. Recovery material was retained at {}. Your buffer remains marked unsaved.",
                        failure.message,
                        transaction.directory.path.display()
                    );
                    return Err(failure);
                }
            }
            // Removal has a final point-in-time identity/byte check and is
            // nonrecursive. Observed unexpected/unremovable material remains;
            // the same-UID final-use limitation is stated at module level.
            if let Err(mut failure) = transaction.finish(
                original_identity,
                &previous,
                self.max_source_bytes,
                &metadata,
            ) {
                self.poisoned = true;
                failure.message = format!("{} Saved bytes may already be present. Recovery material was retained at {}. Your buffer remains unsaved.", failure.message, transaction.directory.path.display());
                return Err(failure);
            }
            let snapshot = &mut self.documents[index];
            snapshot.document.revision = next_revision;
            snapshot.document.content_digest = source_digest(encoded);
            snapshot.document.size_bytes = encoded.len() as u64;
            snapshot.content = request.content;
        } else {
            self.verify_root()?;
            let (mut confirmed, identity) = self.directory.source(&baseline.document.name)?;
            if identity != original_identity
                || read_bounded(&mut confirmed, self.max_source_bytes)? != previous
            {
                return Err(refusal(ErrorCode::ExternalChange, "Source changed during the content-equal save. Your buffer remains marked unsaved."));
            }
            self.sources[index].edit_revision = request.edit_revision;
        }
        let snapshot = &self.documents[index];
        Ok(SaveAcknowledgement {
            workspace_id: self.workspace_id.clone(),
            document_id: request.document_id,
            revision: next_revision,
            content_digest: snapshot.document.content_digest.clone(),
            edit_revision: request.edit_revision,
            bytes_written: if unchanged {
                0
            } else {
                snapshot.document.size_bytes
            },
            unchanged,
        })
    }

    fn verify_root(&self) -> ServiceResult<()> {
        let current = open_ancestry(&self.directory.path)?;
        if current.len() != self.ancestors.len()
            || current
                .iter()
                .zip(&self.ancestors)
                .any(|(left, right)| left.identity != right.identity)
        {
            return Err(refusal(
                ErrorCode::ExternalChange,
                "The selected project directory identity changed.",
            ));
        }
        Ok(())
    }
}

struct Directory {
    path: PathBuf,
    file: File,
    identity: NativeFileIdentity,
}
impl Directory {
    fn try_clone(&self) -> ServiceResult<Self> {
        Ok(Self {
            path: self.path.clone(),
            file: self.file.try_clone().map_err(io_error)?,
            identity: self.identity,
        })
    }
    fn source(&self, leaf: &str) -> ServiceResult<(File, NativeFileIdentity)> {
        validate_leaf(leaf)?;
        let file = native::open(self, leaf, false, false)?;
        validate_regular(&file)?;
        let identity = native_identity(&file).map_err(io_error)?;
        Ok((file, identity))
    }
    fn verify_write_access(&self, leaf: &str, expected: NativeFileIdentity) -> ServiceResult<()> {
        // Opening the existing object for write, without create or truncate,
        // asks the OS to check effective identity/ACL access. Aggregate Unix
        // write bits (Permissions::readonly) cannot establish that authority.
        let file = native::open(self, leaf, true, false)?;
        validate_regular(&file)?;
        if native_identity(&file).map_err(io_error)? != expected {
            return Err(refusal(
                ErrorCode::ExternalChange,
                "Source changed during native write-access validation.",
            ));
        }
        Ok(())
    }
    fn acquire_lease(&self) -> ServiceResult<SaveLease> {
        let (mut file, created) = match native::open(self, LEASE_NAME, true, true) {
            Ok(file) => (file, true),
            Err(failure) if failure.code == ErrorCode::AlreadyExists => {
                (native::open(self, LEASE_NAME, true, false)?, false)
            }
            Err(failure) => return Err(failure),
        };
        validate_regular(&file)?;
        match file.try_lock() {
            Ok(()) => {}
            Err(TryLockError::WouldBlock) => {
                return Err(refusal(
                    ErrorCode::WorkspaceBusy,
                    "Another native save is using this project. Your buffer remains unsaved.",
                ))
            }
            Err(TryLockError::Error(failure)) => return Err(io_error(failure)),
        }
        if created {
            file.write_all(LEASE_MARKER).map_err(io_error)?;
            file.sync_all().map_err(io_error)?;
        } else if read_bounded(&mut file, LEASE_MARKER.len() as u64)? != LEASE_MARKER {
            return Err(refusal(
                ErrorCode::ForbiddenRoot,
                "The native save lease path contains unrecognized user data; it was not changed.",
            ));
        }
        let check = native::open(self, LEASE_NAME, false, false)?;
        if native_identity(&check).map_err(io_error)? != native_identity(&file).map_err(io_error)? {
            return Err(refusal(
                ErrorCode::ExternalChange,
                "The native save lease identity changed.",
            ));
        }
        Ok(SaveLease { file })
    }
    fn refuse_existing_transaction(&self) -> ServiceResult<()> {
        // Never recursively inspect or adopt a previous recovery directory.
        // A crash/reopen must not accumulate another source-sized recovery on
        // every retry. The explicit native lease serializes this bounded scan.
        for (index, entry) in fs::read_dir(&self.path).map_err(io_error)?.enumerate() {
            if index >= 4096 {
                return Err(refusal(
                    ErrorCode::LimitExceeded,
                    "Project root exceeds the bounded native-save scan.",
                ));
            }
            let entry = entry.map_err(io_error)?;
            if entry
                .file_name()
                .to_string_lossy()
                .starts_with(".mcu-native-save-")
            {
                return Err(refusal(ErrorCode::ExternalChange, &format!("A previous native save needs inspection at {}. Its recovery material was preserved; this buffer remains unsaved.", entry.path().display())));
            }
        }
        Ok(())
    }
}

struct Transaction {
    parent: Directory,
    directory: Directory,
    leaf: String,
    staged_identity: Option<NativeFileIdentity>,
    committed: bool,
    retain: bool,
}
impl Transaction {
    fn new(parent: &Directory) -> ServiceResult<Self> {
        let number = NEXT.fetch_add(1, Ordering::Relaxed);
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let leaf = format!(
            ".mcu-native-save-{}-{nanos:x}-{number:x}",
            std::process::id()
        );
        let directory = native::create_directory(parent, &leaf)?;
        Ok(Self {
            parent: parent.try_clone()?,
            directory,
            leaf,
            staged_identity: None,
            committed: false,
            retain: false,
        })
    }
    fn write(&mut self, content: &[u8], expected: &PreservationMetadata) -> ServiceResult<()> {
        let mut file = native::open(&self.directory, "source.tmp", true, true)?;
        self.staged_identity = Some(native_identity(&file).map_err(io_error)?);
        let inherited = preservation_metadata(&file)?;
        #[cfg(target_os = "linux")]
        if inherited.uid != expected.uid || inherited.gid != expected.gid {
            return Err(refusal(ErrorCode::UnsupportedFile, "This source owner/group cannot be preserved by a native atomic save. Nothing was replaced."));
        }
        #[cfg(not(target_os = "linux"))]
        let _ = inherited;
        file.write_all(content).map_err(io_error)?;
        file.set_permissions(expected.permissions.clone())
            .map_err(io_error)?;
        if preservation_metadata(&file)? != *expected {
            return Err(refusal(
                ErrorCode::UnsupportedFile,
                "Source permissions/ownership could not be retained. Nothing was replaced.",
            ));
        }
        file.sync_all().map_err(io_error)?;
        Ok(())
    }
    fn commit(&mut self, source: &str) -> ServiceResult<()> {
        // Mark before the call: Windows may return a partial native replacement
        // failure. The displaced/source names must then remain for inspection.
        self.committed = true;
        native::replace(&self.parent, source, &self.directory)?;
        Ok(())
    }
    fn displaced(&self) -> ServiceResult<(File, NativeFileIdentity)> {
        let file = native::open(&self.directory, native::DISPLACED, false, false)?;
        validate_regular(&file)?;
        let identity = native_identity(&file).map_err(io_error)?;
        Ok((file, identity))
    }
    fn finish(
        &mut self,
        expected: NativeFileIdentity,
        previous: &[u8],
        limit: u64,
        metadata: &PreservationMetadata,
    ) -> ServiceResult<()> {
        self.retain = true;
        // The displaced file has just passed full identity/byte verification.
        // Recheck that this cleanup still targets that original object.
        let (mut displaced, identity) = self.displaced()?;
        if identity != expected
            || read_bounded(&mut displaced, limit)? != previous
            || preservation_metadata(&displaced)? != *metadata
        {
            return Err(refusal(
                ErrorCode::ExternalChange,
                "Recovery entry changed before cleanup.",
            ));
        }
        drop(displaced);
        native::remove_file(&self.directory, native::DISPLACED)?;
        native::remove_transaction_directory(&self.parent, &mut self.directory, &self.leaf)?;
        self.retain = false;
        Ok(())
    }
}
impl Drop for Transaction {
    fn drop(&mut self) {
        if self.retain || self.committed {
            return;
        }
        if let Some(expected) = self.staged_identity {
            if native::open(&self.directory, "source.tmp", false, false)
                .and_then(|file| native_identity(&file).map_err(io_error))
                .is_ok_and(|identity| identity == expected)
            {
                let _ = native::remove_file(&self.directory, "source.tmp");
            }
        }
        let _ = native::remove_transaction_directory(&self.parent, &mut self.directory, &self.leaf);
    }
}

fn read_bounded(file: &mut File, limit: u64) -> ServiceResult<Vec<u8>> {
    validate_regular(file)?;
    if file.metadata().map_err(io_error)?.len() > limit {
        return Err(refusal(
            ErrorCode::TooLarge,
            "Native source exceeds its byte limit.",
        ));
    }
    file.seek(SeekFrom::Start(0)).map_err(io_error)?;
    let mut bytes = Vec::new();
    file.take(limit + 1)
        .read_to_end(&mut bytes)
        .map_err(io_error)?;
    if bytes.len() as u64 > limit {
        return Err(refusal(
            ErrorCode::TooLarge,
            "Native source grew beyond its byte limit.",
        ));
    }
    Ok(bytes)
}
fn preservation_metadata(file: &File) -> ServiceResult<PreservationMetadata> {
    let metadata = file.metadata().map_err(io_error)?;
    #[cfg(target_os = "linux")]
    {
        use std::os::fd::AsRawFd;
        use std::os::unix::fs::MetadataExt;
        // SAFETY: file owns a live fd. A null buffer with size zero only asks
        // the kernel for the bounded list size; no contents or allocation are
        // needed for this conservative ACL/xattr refusal policy.
        let count = unsafe { libc::flistxattr(file.as_raw_fd(), std::ptr::null_mut(), 0) };
        if count < 0 {
            let failure = io::Error::last_os_error();
            if failure.raw_os_error() != Some(libc::ENOTSUP) {
                return Err(io_error(failure));
            }
        } else if count != 0 {
            return Err(refusal(ErrorCode::UnsupportedFile, "Native save cannot yet retain this source's ACL/extended attributes. Its original metadata and bytes were preserved."));
        }
        Ok(PreservationMetadata {
            permissions: metadata.permissions(),
            uid: metadata.uid(),
            gid: metadata.gid(),
        })
    }
    #[cfg(not(target_os = "linux"))]
    {
        Ok(PreservationMetadata {
            permissions: metadata.permissions(),
        })
    }
}
fn validate_regular(file: &File) -> ServiceResult<()> {
    let metadata = file.metadata().map_err(io_error)?;
    if !metadata.is_file() || is_alias(&metadata) {
        return Err(refusal(
            ErrorCode::Symlink,
            "Only regular, unlinked root sources may be saved.",
        ));
    }
    if !single_link(file).map_err(io_error)? {
        return Err(refusal(
            ErrorCode::Symlink,
            "Hardlinked sources or native save files are refused; other links were preserved.",
        ));
    }
    Ok(())
}
fn validate_owner(owner: &str) -> ServiceResult<()> {
    if owner.is_empty() || owner.len() > 128 || owner.chars().any(char::is_control) {
        return Err(refusal(
            ErrorCode::WrongOwner,
            "An explicit native window owner is required.",
        ));
    }
    Ok(())
}
fn validate_leaf(leaf: &str) -> ServiceResult<()> {
    if leaf.is_empty()
        || leaf.len() > 255
        || leaf.starts_with('.')
        || leaf != leaf.trim()
        || leaf.ends_with('.')
        || leaf
            .chars()
            .any(|character| character.is_control() || "\\/:<>\"|?*".contains(character))
        || mcu_core::source_names::is_windows_reserved_device_name(leaf)
        || !matches!(
            Path::new(leaf)
                .extension()
                .and_then(|value| value.to_str())
                .unwrap_or_default()
                .to_ascii_lowercase()
                .as_str(),
            "ino" | "c" | "cpp" | "h" | "hpp" | "txt"
        )
    {
        return Err(refusal(
            ErrorCode::InvalidPath,
            "Expected one visible root C/C++ sketch, header or note filename.",
        ));
    }
    Ok(())
}
fn is_alias(metadata: &Metadata) -> bool {
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
fn open_ancestry(path: &Path) -> ServiceResult<Vec<Directory>> {
    if !path.is_absolute()
        || path
            .components()
            .any(|component| matches!(component, Component::CurDir | Component::ParentDir))
    {
        return Err(refusal(
            ErrorCode::InvalidPath,
            "The native project requires an explicit absolute path without parent traversal.",
        ));
    }
    if path.components().count() > 256 || path.as_os_str().len() > 32767 {
        return Err(refusal(
            ErrorCode::LimitExceeded,
            "Native project ancestry exceeds its bounded path limit.",
        ));
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        use std::path::Prefix;
        if !matches!(path.components().next(), Some(Component::Prefix(prefix)) if matches!(prefix.kind(), Prefix::Disk(_) | Prefix::VerbatimDisk(_) | Prefix::UNC(_, _) | Prefix::VerbatimUNC(_, _)))
        {
            return Err(refusal(
                ErrorCode::InvalidPath,
                "Native projects require an ordinary disk or UNC path.",
            ));
        }
        let mut segment = Vec::new();
        for unit in path
            .as_os_str()
            .encode_wide()
            .chain(std::iter::once(b'/' as u16))
        {
            if unit == b'/' as u16 || unit == b'\\' as u16 {
                if segment == [b'.' as u16, b'.' as u16] {
                    return Err(refusal(
                        ErrorCode::InvalidPath,
                        "Native parent traversal is prohibited.",
                    ));
                }
                segment.clear();
            } else {
                segment.push(unit);
            }
        }
    }
    let mut prefix = PathBuf::new();
    let mut directories = Vec::new();
    for component in path.components() {
        if let Component::Normal(name) = component {
            if matches!(
                name.to_string_lossy().to_ascii_lowercase().as_str(),
                ".mcu_flasher_build_cache"
                    | ".pio"
                    | ".vscode"
                    | ".clangd"
                    | ".opencode"
                    | ".mcu_ai_edits"
            ) {
                return Err(refusal(
                    ErrorCode::ForbiddenRoot,
                    "Protected cache and journal directories cannot become native projects.",
                ));
            }
        }
        prefix.push(component.as_os_str());
        if matches!(component, Component::Prefix(_)) {
            continue;
        }
        let directory = native::open_directory(&prefix, directories.last())?;
        directories.push(directory);
    }
    Ok(directories)
}
fn refusal(code: ErrorCode, message: &str) -> ServiceError {
    ServiceError {
        code,
        message: message.into(),
        retryable: false,
    }
}
fn io_error(failure: io::Error) -> ServiceError {
    let code = match failure.kind() {
        io::ErrorKind::NotFound => ErrorCode::NotFound,
        io::ErrorKind::AlreadyExists => ErrorCode::AlreadyExists,
        io::ErrorKind::PermissionDenied => ErrorCode::ReadOnly,
        _ => ErrorCode::Io,
    };
    ServiceError {
        code,
        message: format!("Native source operation failed: {failure}"),
        retryable: false,
    }
}

#[cfg(target_os = "linux")]
mod native {
    use super::*;
    use std::ffi::CString;
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::OpenOptionsExt;
    pub const DISPLACED: &str = "source.tmp";
    fn c_leaf(leaf: &str) -> ServiceResult<CString> {
        CString::new(leaf).map_err(|_| refusal(ErrorCode::InvalidPath, "Invalid native leaf."))
    }
    fn from_fd(fd: i32) -> ServiceResult<File> {
        if fd < 0 {
            return Err(io_error(io::Error::last_os_error()));
        }
        // SAFETY: a nonnegative open/openat result is one newly owned fd. The
        // only conversion consumes it, and File closes it exactly once.
        Ok(unsafe { File::from_raw_fd(fd) })
    }
    pub fn open_directory(path: &Path, parent: Option<&Directory>) -> ServiceResult<Directory> {
        let file = if let Some(parent) = parent {
            let leaf = CString::new(
                path.file_name()
                    .ok_or_else(|| refusal(ErrorCode::InvalidPath, "Missing directory component."))?
                    .as_bytes(),
            )
            .map_err(|_| refusal(ErrorCode::InvalidPath, "Invalid directory component."))?;
            // SAFETY: parent owns a live directory fd; leaf is NUL terminated.
            // O_NOFOLLOW rejects redirects, O_DIRECTORY rejects other kinds.
            from_fd(unsafe {
                libc::openat(
                    parent.file.as_raw_fd(),
                    leaf.as_ptr(),
                    libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                )
            })?
        } else {
            OpenOptions::new()
                .read(true)
                .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
                .open(path)
                .map_err(io_error)?
        };
        let identity = native_identity(&file).map_err(io_error)?;
        Ok(Directory {
            path: path.to_path_buf(),
            file,
            identity,
        })
    }
    pub fn open(
        directory: &Directory,
        leaf: &str,
        write: bool,
        create: bool,
    ) -> ServiceResult<File> {
        let leaf = c_leaf(leaf)?;
        let flags = (if write { libc::O_RDWR } else { libc::O_RDONLY })
            | (if create {
                libc::O_CREAT | libc::O_EXCL
            } else {
                0
            })
            | libc::O_NOFOLLOW
            | libc::O_CLOEXEC
            | libc::O_NONBLOCK;
        // SAFETY: the live directory descriptor anchors this one leaf. Mode is
        // used only with O_CREAT; O_EXCL never truncates an existing entry.
        from_fd(unsafe { libc::openat(directory.file.as_raw_fd(), leaf.as_ptr(), flags, 0o600) })
    }
    pub fn create_directory(parent: &Directory, leaf: &str) -> ServiceResult<Directory> {
        let name = c_leaf(leaf)?;
        // SAFETY: live directory fd and NUL-terminated single component; mkdirat
        // is create-only, so another entry is never adopted or overwritten.
        if unsafe { libc::mkdirat(parent.file.as_raw_fd(), name.as_ptr(), 0o700) } != 0 {
            return Err(io_error(io::Error::last_os_error()));
        }
        open_directory(&parent.path.join(leaf), Some(parent))
    }
    pub fn replace(parent: &Directory, source: &str, transaction: &Directory) -> ServiceResult<()> {
        let source = c_leaf(source)?;
        let staged = c_leaf("source.tmp")?;
        // SAFETY: both retained directory fds are live and both names are
        // single NUL-terminated leaves. Atomic exchange preserves the exact
        // displaced original for post-commit identity/byte verification.
        let result = unsafe {
            libc::syscall(
                libc::SYS_renameat2,
                parent.file.as_raw_fd(),
                source.as_ptr(),
                transaction.file.as_raw_fd(),
                staged.as_ptr(),
                libc::RENAME_EXCHANGE,
            )
        };
        if result != 0 {
            return Err(io_error(io::Error::last_os_error()));
        }
        parent.file.sync_all().map_err(io_error)?;
        transaction.file.sync_all().map_err(io_error)
    }
    pub fn remove_file(directory: &Directory, leaf: &str) -> ServiceResult<()> {
        unlink(directory, leaf, 0)
    }
    pub fn remove_transaction_directory(
        directory: &Directory,
        _transaction: &mut Directory,
        leaf: &str,
    ) -> ServiceResult<()> {
        unlink(directory, leaf, libc::AT_REMOVEDIR)
    }
    fn unlink(directory: &Directory, leaf: &str, flags: i32) -> ServiceResult<()> {
        let leaf = c_leaf(leaf)?;
        // SAFETY: descriptor-relative unlink of a single owned entry; no link
        // or directory traversal and no recursive cleanup are performed.
        if unsafe { libc::unlinkat(directory.file.as_raw_fd(), leaf.as_ptr(), flags) } != 0 {
            return Err(io_error(io::Error::last_os_error()));
        }
        Ok(())
    }
}

#[cfg(windows)]
mod native {
    use super::*;
    use std::os::windows::ffi::OsStrExt;
    use std::os::windows::fs::OpenOptionsExt;
    use windows_sys::Win32::Storage::FileSystem::{
        ReplaceFileW, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT,
        FILE_READ_ATTRIBUTES, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
    };
    pub const DISPLACED: &str = "previous";
    pub fn open_directory(path: &Path, _parent: Option<&Directory>) -> ServiceResult<Directory> {
        let file = OpenOptions::new()
            .access_mode(FILE_READ_ATTRIBUTES)
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
            .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
            .open(path)
            .map_err(io_error)?;
        let metadata = file.metadata().map_err(io_error)?;
        if !metadata.is_dir() || is_alias(&metadata) {
            return Err(refusal(
                ErrorCode::Symlink,
                "Native directory aliases/reparse points are prohibited.",
            ));
        }
        let identity = native_identity(&file).map_err(io_error)?;
        Ok(Directory {
            path: path.to_path_buf(),
            file,
            identity,
        })
    }
    pub fn open(
        directory: &Directory,
        leaf: &str,
        write: bool,
        create: bool,
    ) -> ServiceResult<File> {
        OpenOptions::new()
            .read(true)
            .write(write)
            .create_new(create)
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .open(directory.path.join(leaf))
            .map_err(io_error)
    }
    pub fn create_directory(parent: &Directory, leaf: &str) -> ServiceResult<Directory> {
        let path = parent.path.join(leaf);
        fs::create_dir(&path).map_err(io_error)?;
        open_directory(&path, Some(parent))
    }
    pub fn replace(parent: &Directory, source: &str, transaction: &Directory) -> ServiceResult<()> {
        fn wide(path: &Path) -> Vec<u16> {
            path.as_os_str()
                .encode_wide()
                .chain(std::iter::once(0))
                .collect()
        }
        let target = wide(&parent.path.join(source));
        let staged = wide(&transaction.path.join("source.tmp"));
        let recovery = wide(&transaction.path.join(DISPLACED));
        // SAFETY: all UTF-16 arrays are NUL terminated and live for this call.
        // Root and transaction directory handles deny delete sharing, so their
        // paths cannot be redirected while ReplaceFileW uses the owned names.
        // No IGNORE_* flags permit silently losing original ACLs/attributes.
        if unsafe {
            ReplaceFileW(
                target.as_ptr(),
                staged.as_ptr(),
                recovery.as_ptr(),
                0,
                std::ptr::null(),
                std::ptr::null(),
            )
        } == 0
        {
            return Err(io_error(io::Error::last_os_error()));
        }
        Ok(())
    }
    pub fn remove_file(directory: &Directory, leaf: &str) -> ServiceResult<()> {
        fs::remove_file(directory.path.join(leaf)).map_err(io_error)
    }
    pub fn remove_transaction_directory(
        directory: &Directory,
        transaction: &mut Directory,
        leaf: &str,
    ) -> ServiceResult<()> {
        // Release only the attempt-owned child handle before removing its
        // empty directory; the retained parent still prevents path redirects.
        transaction.file = directory.file.try_clone().map_err(io_error)?;
        fs::remove_dir(directory.path.join(leaf)).map_err(io_error)
    }
}

#[cfg(not(any(target_os = "linux", windows)))]
mod native {
    use super::*;
    pub const DISPLACED: &str = "previous";
    fn unavailable<T>() -> ServiceResult<T> {
        Err(refusal(
            ErrorCode::ForbiddenRoot,
            "Native source saving is unavailable on this host.",
        ))
    }
    pub fn open_directory(_: &Path, _: Option<&Directory>) -> ServiceResult<Directory> {
        unavailable()
    }
    pub fn open(_: &Directory, _: &str, _: bool, _: bool) -> ServiceResult<File> {
        unavailable()
    }
    pub fn create_directory(_: &Directory, _: &str) -> ServiceResult<Directory> {
        unavailable()
    }
    pub fn replace(_: &Directory, _: &str, _: &Directory) -> ServiceResult<()> {
        unavailable()
    }
    pub fn remove_file(_: &Directory, _: &str) -> ServiceResult<()> {
        unavailable()
    }
    pub fn remove_transaction_directory(
        _: &Directory,
        _: &mut Directory,
        _: &str,
    ) -> ServiceResult<()> {
        unavailable()
    }
}

#[cfg(test)]
mod checks {
    use super::*;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(1);
    struct Fixture {
        parent: PathBuf,
        app: PathBuf,
        root: PathBuf,
    }
    impl Fixture {
        fn new() -> Self {
            let checkout = Path::new(env!("CARGO_MANIFEST_DIR"))
                .ancestors()
                .nth(3)
                .unwrap();
            let base = checkout.join("temp/audit/native-source-save");
            std::fs::create_dir_all(&base).unwrap();
            let parent = base.join(format!(
                "{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir(&parent).unwrap();
            let app = parent.join("application");
            let root = parent.join("sketch");
            std::fs::create_dir(&app).unwrap();
            std::fs::create_dir(&root).unwrap();
            std::fs::write(
                root.join("Sketch.ino"),
                "\u{feff}void setup() {}\r\nvoid loop() {}\n",
            )
            .unwrap();
            std::fs::write(root.join("Header.hpp"), "// café 日本語\n").unwrap();
            Self { parent, app, root }
        }
        fn open(&self) -> NativeProjectSession {
            NativeProjectSession::open(&self.app, &self.root, "native-window").unwrap()
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            std::fs::remove_dir_all(&self.parent).unwrap();
        }
    }
    fn request(session: &NativeProjectSession, content: &str) -> SaveDocumentRequest {
        let document = &session.documents()[0];
        SaveDocumentRequest {
            workspace_id: session.workspace_id().clone(),
            document_id: document.document.document_id.clone(),
            expected_revision: document.document.revision,
            expected_digest: document.document.content_digest.clone(),
            edit_revision: Revision(7),
            content: content.into(),
        }
    }
    #[test]
    fn native_save_updates_exact_snapshot_and_noop_preserves_identity() {
        let fixture = Fixture::new();
        let mut session = fixture.open();
        let original = session.documents()[0].clone();
        assert!(original.content.starts_with('\u{feff}'));
        let ack = session
            .save_document("native-window", request(&session, "// café 日本語\r\n"))
            .unwrap();
        assert_eq!(ack.edit_revision, Revision(7));
        assert_eq!(ack.revision, Revision(2));
        assert_eq!(
            std::fs::read(fixture.root.join("Sketch.ino")).unwrap(),
            "// café 日本語\r\n".as_bytes()
        );
        assert_eq!(
            session.documents()[0].document.content_digest,
            ack.content_digest
        );
        let before = session.sources[0].identity;
        let ack = session
            .save_document("native-window", request(&session, "// café 日本語\r\n"))
            .unwrap();
        assert!(ack.unchanged);
        assert_eq!(ack.bytes_written, 0);
        assert_eq!(session.sources[0].identity, before);
    }
    #[test]
    fn stale_foreign_and_oversized_requests_never_change_source() {
        let fixture = Fixture::new();
        let mut session = fixture.open();
        let original = std::fs::read(fixture.root.join("Sketch.ino")).unwrap();
        let value = request(&session, "rejected");
        assert_eq!(
            session
                .save_document("different-window", value.clone())
                .unwrap_err()
                .code,
            ErrorCode::WrongOwner
        );
        let mut foreign = value.clone();
        foreign.document_id.0.push_str("-foreign");
        assert_eq!(
            session
                .save_document("native-window", foreign)
                .unwrap_err()
                .code,
            ErrorCode::NotFound
        );
        let mut stale = value.clone();
        stale.expected_revision = Revision(9);
        assert_eq!(
            session
                .save_document("native-window", stale)
                .unwrap_err()
                .code,
            ErrorCode::StaleRevision
        );
        let mut foreign = value.clone();
        foreign.workspace_id.0.push_str("-foreign");
        assert_eq!(
            session
                .save_document("native-window", foreign)
                .unwrap_err()
                .code,
            ErrorCode::WorkspaceNotFound
        );
        let mut huge = value;
        huge.content = "x".repeat(8 * 1024 * 1024 + 1);
        assert_eq!(
            session
                .save_document("native-window", huge)
                .unwrap_err()
                .code,
            ErrorCode::TooLarge
        );
        assert_eq!(
            std::fs::read(fixture.root.join("Sketch.ino")).unwrap(),
            original
        );
    }
    #[test]
    fn same_size_external_edit_and_same_bytes_replacement_are_refused() {
        let fixture = Fixture::new();
        let mut session = fixture.open();
        let source = fixture.root.join("Sketch.ino");
        let original = std::fs::read(&source).unwrap();
        let mut changed = original.clone();
        changed[4] = b'X';
        std::fs::write(&source, &changed).unwrap();
        assert_eq!(
            session
                .save_document("native-window", request(&session, "native replacement"))
                .unwrap_err()
                .code,
            ErrorCode::ExternalChange
        );
        assert_eq!(std::fs::read(&source).unwrap(), changed);
        std::fs::rename(&source, fixture.root.join("old-source.txt")).unwrap();
        std::fs::write(&source, &original).unwrap();
        assert_eq!(
            session
                .save_document("native-window", request(&session, "native replacement"))
                .unwrap_err()
                .code,
            ErrorCode::ExternalChange
        );
        assert_eq!(std::fs::read(&source).unwrap(), original);
    }
    #[test]
    fn hardlinked_source_is_refused_at_open_and_again_before_save() {
        let fixture = Fixture::new();
        let mut session = fixture.open();
        let alias = fixture.parent.join("other-project.ino");
        std::fs::hard_link(fixture.root.join("Sketch.ino"), &alias).unwrap();
        assert_eq!(
            NativeProjectSession::open(&fixture.app, &fixture.root, "native-window")
                .err()
                .unwrap()
                .code,
            ErrorCode::Symlink
        );
        assert_eq!(
            session
                .save_document("native-window", request(&session, "rejected"))
                .unwrap_err()
                .code,
            ErrorCode::Symlink
        );
        assert!(std::fs::read_to_string(alias)
            .unwrap()
            .starts_with('\u{feff}'));
    }
    #[test]
    fn failed_staging_revalidation_preserves_external_bytes_and_removes_owned_stage() {
        let fixture = Fixture::new();
        let mut session = fixture.open();
        let root = fixture.root.clone();
        let failure = session
            .save_inner(
                "native-window",
                request(&session, "native replacement"),
                |phase, _| {
                    assert!(phase == SavePhase::Staged);
                    std::fs::write(root.join("Sketch.ino"), "external replacement").unwrap();
                    Ok(())
                },
            )
            .unwrap_err();
        assert_eq!(failure.code, ErrorCode::ExternalChange);
        assert_eq!(
            std::fs::read_to_string(fixture.root.join("Sketch.ino")).unwrap(),
            "external replacement"
        );
        assert!(!session.poisoned);
        assert!(!std::fs::read_dir(&fixture.root).unwrap().any(|entry| entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".mcu-native-save-")));
    }
    #[test]
    fn nonblocking_native_lease_refuses_busy_and_preserves_unknown_lease_data() {
        let fixture = Fixture::new();
        let mut session = fixture.open();
        let lease = session.directory.acquire_lease().unwrap();
        assert_eq!(
            session
                .save_document("native-window", request(&session, "blocked"))
                .unwrap_err()
                .code,
            ErrorCode::WorkspaceBusy
        );
        drop(lease);
        std::fs::write(fixture.root.join(LEASE_NAME), "user-owned unknown data").unwrap();
        assert_eq!(
            session
                .save_document("native-window", request(&session, "blocked"))
                .unwrap_err()
                .code,
            ErrorCode::ForbiddenRoot
        );
        assert_eq!(
            std::fs::read_to_string(fixture.root.join(LEASE_NAME)).unwrap(),
            "user-owned unknown data"
        );
    }
    #[test]
    fn final_use_race_returns_recovery_path_and_retains_dirty_baseline() {
        let fixture = Fixture::new();
        let mut session = fixture.open();
        let original = session.documents()[0].clone();
        let root = fixture.root.clone();
        let failure = session
            .save_inner(
                "native-window",
                request(&session, "native replacement"),
                |phase, _| {
                    if phase == SavePhase::Revalidated {
                        let external = root.join("external-writer.tmp");
                        std::fs::write(&external, "external final-use edit").unwrap();
                        std::fs::rename(external, root.join("Sketch.ino")).unwrap();
                    }
                    Ok(())
                },
            )
            .unwrap_err();
        assert_eq!(failure.code, ErrorCode::ExternalChange);
        assert!(failure
            .message
            .contains("Recovery material was retained at"));
        assert_eq!(session.documents()[0], original);
        assert!(session.poisoned);
        let recovery = std::fs::read_dir(&fixture.root)
            .unwrap()
            .filter_map(|entry| {
                let entry = entry.unwrap();
                entry
                    .file_name()
                    .to_string_lossy()
                    .starts_with(".mcu-native-save-")
                    .then_some(entry.path())
            })
            .collect::<Vec<_>>();
        assert_eq!(recovery.len(), 1);
        assert_eq!(
            std::fs::read(recovery[0].join(native::DISPLACED)).unwrap(),
            b"external final-use edit"
        );
        assert!(session
            .save_document("native-window", request(&session, "retry must refuse"))
            .is_err());
        assert_eq!(
            std::fs::read_dir(&fixture.root)
                .unwrap()
                .filter(|entry| entry
                    .as_ref()
                    .unwrap()
                    .file_name()
                    .to_string_lossy()
                    .starts_with(".mcu-native-save-"))
                .count(),
            1
        );
        let mut reopened = fixture.open();
        let failure = reopened
            .save_document("native-window", request(&reopened, "retry after reopening"))
            .unwrap_err();
        assert_eq!(failure.code, ErrorCode::ExternalChange);
        assert!(failure
            .message
            .contains("previous native save needs inspection"));
        assert_eq!(
            std::fs::read(recovery[0].join(native::DISPLACED)).unwrap(),
            b"external final-use edit"
        );
    }
    #[test]
    fn native_save_lease_is_exclusive_across_processes_and_released_on_close() {
        let fixture = Fixture::new();
        let session = fixture.open();
        let lease = session.directory.acquire_lease().unwrap();
        let child = |expected: &str| {
            let mut child = std::process::Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "source_save::checks::lease_child_entry",
                    "--nocapture",
                ])
                .env("MCU_NATIVE_SAVE_LEASE_CHILD_ROOT", &fixture.root)
                .env("MCU_NATIVE_SAVE_LEASE_CHILD_EXPECT", expected)
                .spawn()
                .unwrap();
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
            loop {
                if let Some(status) = child.try_wait().unwrap() {
                    assert!(status.success());
                    break;
                }
                if std::time::Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    panic!("Native lease child timed out");
                }
                std::thread::sleep(std::time::Duration::from_millis(10));
            }
        };
        child("busy");
        drop(lease);
        child("available");
    }
    #[test]
    fn lease_child_entry() {
        let Some(root) = std::env::var_os("MCU_NATIVE_SAVE_LEASE_CHILD_ROOT") else {
            return;
        };
        let root = PathBuf::from(root);
        let checkout = Path::new(env!("CARGO_MANIFEST_DIR"))
            .ancestors()
            .nth(3)
            .unwrap();
        assert!(root.starts_with(checkout.join("temp/audit/native-source-save")));
        let directories = open_ancestry(&root).unwrap();
        let result = directories.last().unwrap().acquire_lease();
        if std::env::var("MCU_NATIVE_SAVE_LEASE_CHILD_EXPECT").unwrap() == "busy" {
            assert_eq!(result.err().unwrap().code, ErrorCode::WorkspaceBusy);
        } else {
            assert!(result.is_ok());
        }
    }
    #[test]
    fn read_only_save_refuses_without_changing_permissions_or_bytes() {
        let fixture = Fixture::new();
        let mut session = fixture.open();
        let path = fixture.root.join("Sketch.ino");
        let old = std::fs::metadata(&path).unwrap().permissions();
        let mut permissions = old.clone();
        permissions.set_readonly(true);
        std::fs::set_permissions(&path, permissions).unwrap();
        let result = session.save_document("native-window", request(&session, "blocked"));
        assert_eq!(result.unwrap_err().code, ErrorCode::ReadOnly);
        assert!(std::fs::metadata(&path).unwrap().permissions().readonly());
        std::fs::set_permissions(&path, old).unwrap();
    }
    #[test]
    fn root_and_protected_path_admission_never_creates_save_entries() {
        let fixture = Fixture::new();
        let protected = fixture.root.join(".mcu_flasher_build_cache");
        std::fs::create_dir(&protected).unwrap();
        std::fs::write(protected.join("Sketch.ino"), "protected").unwrap();
        assert_eq!(
            NativeProjectSession::open(&fixture.app, &protected, "native-window")
                .err()
                .unwrap()
                .code,
            ErrorCode::ForbiddenRoot
        );
        assert_eq!(
            NativeProjectSession::open(&fixture.app, &fixture.root, "\n")
                .err()
                .unwrap()
                .code,
            ErrorCode::WrongOwner
        );
        assert!(!fixture.root.join(LEASE_NAME).exists());
        assert!(!protected.join(LEASE_NAME).exists());
    }
    #[test]
    fn empty_source_save_and_revision_bounds_preserve_exact_native_state() {
        let fixture = Fixture::new();
        let mut session = fixture.open();
        for revision in [0, EXACT_REVISION + 1] {
            let mut invalid = request(&session, "blocked");
            invalid.edit_revision = Revision(revision);
            assert_eq!(
                session
                    .save_document("native-window", invalid)
                    .unwrap_err()
                    .code,
                ErrorCode::StaleRevision
            );
        }
        let ack = session
            .save_document("native-window", request(&session, ""))
            .unwrap();
        assert!(!ack.unchanged);
        assert_eq!(ack.bytes_written, 0);
        assert_eq!(std::fs::read(fixture.root.join("Sketch.ino")).unwrap(), b"");
        let mut older = request(&session, "blocked");
        older.edit_revision = Revision(6);
        assert_eq!(
            session
                .save_document("native-window", older)
                .unwrap_err()
                .code,
            ErrorCode::StaleRevision
        );
    }
    #[cfg(target_os = "linux")]
    #[test]
    fn source_permissions_survive_atomic_native_replace() {
        use std::os::unix::fs::PermissionsExt;
        let fixture = Fixture::new();
        std::fs::set_permissions(
            fixture.root.join("Sketch.ino"),
            std::fs::Permissions::from_mode(0o640),
        )
        .unwrap();
        let mut session = fixture.open();
        session
            .save_document("native-window", request(&session, "replacement\n"))
            .unwrap();
        assert_eq!(
            std::fs::metadata(fixture.root.join("Sketch.ino"))
                .unwrap()
                .permissions()
                .mode()
                & 0o7777,
            0o640
        );
    }
    #[cfg(target_os = "linux")]
    #[test]
    fn aggregate_group_write_bit_does_not_bypass_effective_owner_write_denial() {
        use std::os::unix::fs::PermissionsExt;
        // Root deliberately bypasses ordinary DAC. Native CI runs this under
        // its standard user; a root-only run cannot qualify this denial.
        // SAFETY: geteuid has no arguments and returns the current process UID.
        if unsafe { libc::geteuid() } == 0 {
            eprintln!("SKIP effective-DAC denial: current user is root");
            return;
        }
        let fixture = Fixture::new();
        let mut session = fixture.open();
        let path = fixture.root.join("Sketch.ino");
        let old = std::fs::metadata(&path).unwrap().permissions();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o420)).unwrap();
        assert!(!std::fs::metadata(&path).unwrap().permissions().readonly());
        let failure = session
            .save_document(
                "native-window",
                request(&session, "must not replace owner-read-only source"),
            )
            .unwrap_err();
        assert_eq!(failure.code, ErrorCode::ReadOnly);
        assert!(std::fs::read(&path)
            .unwrap()
            .starts_with("\u{feff}".as_bytes()));
        assert!(!std::fs::read_dir(&fixture.root).unwrap().any(|entry| entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".mcu-native-save-")));
        std::fs::set_permissions(path, old).unwrap();
    }
    #[cfg(target_os = "linux")]
    #[test]
    fn shared_group_source_is_refused_when_new_inode_cannot_retain_its_group() {
        use std::os::fd::AsRawFd;
        use std::os::unix::fs::MetadataExt;
        let mut groups = [0_u32; 128];
        // SAFETY: groups is a writable, aligned 128-element gid_t buffer.
        let count = unsafe { libc::getgroups(groups.len() as i32, groups.as_mut_ptr()) };
        assert!(count >= 0);
        // SAFETY: getegid has no arguments and returns this process's group.
        let current = unsafe { libc::getegid() };
        let Some(group) = groups[..count as usize]
            .iter()
            .copied()
            .find(|group| *group != current)
        else {
            eprintln!("SKIP shared-group fixture: no alternate owned group");
            return;
        };
        let fixture = Fixture::new();
        let path = fixture.root.join("Sketch.ino");
        let source = OpenOptions::new()
            .read(true)
            .write(true)
            .open(&path)
            .unwrap();
        // SAFETY: source owns a live fd; -1 leaves UID unchanged, and group is
        // a current supplementary group, so only this fixture's GID changes.
        assert_eq!(unsafe { libc::fchown(source.as_raw_fd(), !0, group) }, 0);
        let mut session = fixture.open();
        let failure = session
            .save_document(
                "native-window",
                request(&session, "group must remain unchanged"),
            )
            .unwrap_err();
        assert_eq!(failure.code, ErrorCode::UnsupportedFile);
        assert_eq!(std::fs::metadata(&path).unwrap().gid(), group);
        assert!(std::fs::read(path)
            .unwrap()
            .starts_with("\u{feff}".as_bytes()));
    }
    #[cfg(target_os = "linux")]
    #[test]
    fn source_acl_and_user_xattrs_are_refused_and_preserved_byte_for_byte() {
        use std::os::fd::AsRawFd;
        // Linux's stable POSIX ACL xattr format: version header, then sorted
        // {tag:u16, permission:u16, qualifier:u32} entries. The named reader and
        // mask make this extended ACL distinct from plain Unix mode0640.
        let mut acl = 2_u32.to_le_bytes().to_vec();
        // SAFETY: geteuid has no arguments and returns this process's UID.
        let named_user = unsafe { libc::geteuid() }.saturating_add(1);
        for (tag, permission, qualifier) in [
            (1_u16, 6_u16, !0_u32),
            (2, 4, named_user),
            (4, 0, !0),
            (16, 4, !0),
            (32, 0, !0),
        ] {
            acl.extend(tag.to_le_bytes());
            acl.extend(permission.to_le_bytes());
            acl.extend(qualifier.to_le_bytes());
        }
        for (name, value) in [
            (
                "user.mcu-native-save-check",
                b"owned fixture metadata".to_vec(),
            ),
            ("system.posix_acl_access", acl),
        ] {
            let fixture = Fixture::new();
            let path = fixture.root.join("Sketch.ino");
            let source = OpenOptions::new()
                .read(true)
                .write(true)
                .open(&path)
                .unwrap();
            let name = std::ffi::CString::new(name).unwrap();
            // SAFETY: source is owned and live; name/value are valid buffers
            // for this bounded fixture-only xattr write, with no live paths.
            let result = unsafe {
                libc::fsetxattr(
                    source.as_raw_fd(),
                    name.as_ptr(),
                    value.as_ptr().cast(),
                    value.len(),
                    0,
                )
            };
            if result != 0 {
                let failure = io::Error::last_os_error();
                if failure.raw_os_error() == Some(libc::ENOTSUP) {
                    eprintln!(
                        "SKIP xattr fixture: filesystem does not support {}",
                        name.to_string_lossy()
                    );
                    continue;
                }
                panic!("Cannot set owned xattr fixture: {failure}");
            }
            let mut session = fixture.open();
            let failure = session
                .save_document(
                    "native-window",
                    request(&session, "must retain ACL and xattrs"),
                )
                .unwrap_err();
            assert_eq!(failure.code, ErrorCode::UnsupportedFile);
            let mut actual = vec![0_u8; value.len()];
            // SAFETY: the live descriptor and NUL-terminated name are valid;
            // actual is a writable buffer exactly sized to the fixture value.
            let count = unsafe {
                libc::fgetxattr(
                    source.as_raw_fd(),
                    name.as_ptr(),
                    actual.as_mut_ptr().cast(),
                    actual.len(),
                )
            };
            assert_eq!(count, value.len() as isize);
            assert_eq!(actual, value);
            assert!(std::fs::read(path)
                .unwrap()
                .starts_with("\u{feff}".as_bytes()));
        }
    }
    #[cfg(target_os = "linux")]
    #[test]
    fn root_replacement_symlink_source_and_fifo_do_not_escape_or_block() {
        use std::os::unix::fs::symlink;
        let fixture = Fixture::new();
        let mut session = fixture.open();
        let moved = fixture.parent.join("moved");
        std::fs::rename(&fixture.root, &moved).unwrap();
        std::fs::create_dir(&fixture.root).unwrap();
        std::fs::write(fixture.root.join("Sketch.ino"), "different project").unwrap();
        assert_eq!(
            session
                .save_document("native-window", request(&session, "blocked"))
                .unwrap_err()
                .code,
            ErrorCode::ExternalChange
        );
        assert_eq!(
            std::fs::read_to_string(fixture.root.join("Sketch.ino")).unwrap(),
            "different project"
        );
        std::fs::remove_file(fixture.root.join("Sketch.ino")).unwrap();
        symlink(moved.join("Sketch.ino"), fixture.root.join("Sketch.ino")).unwrap();
        assert!(NativeProjectSession::open(&fixture.app, &fixture.root, "native-window").is_err());
        std::fs::remove_file(fixture.root.join("Sketch.ino")).unwrap();
        let fifo = std::ffi::CString::new(
            fixture
                .root
                .join("Sketch.ino")
                .as_os_str()
                .as_encoded_bytes(),
        )
        .unwrap();
        // SAFETY: the fixture-owned path is NUL terminated and mkfifo only
        // creates this absent fixture entry, never a live project file.
        assert_eq!(unsafe { libc::mkfifo(fifo.as_ptr(), 0o600) }, 0);
        let directories = open_ancestry(&fixture.root).unwrap();
        assert!(directories.last().unwrap().source("Sketch.ino").is_err());
    }
    #[cfg(target_os = "linux")]
    #[test]
    fn exchange_preserves_unexpected_displaced_original_for_recovery() {
        let fixture = Fixture::new();
        let session = fixture.open();
        let mut transaction = Transaction::new(&session.directory).unwrap();
        transaction
            .write(
                b"native replacement",
                &preservation_metadata(&session.sources[0].file).unwrap(),
            )
            .unwrap();
        let original_identity = session.sources[0].identity;
        let replacement = fixture.root.join("external.tmp");
        std::fs::write(&replacement, "external edit at final use").unwrap();
        std::fs::rename(replacement, fixture.root.join("Sketch.ino")).unwrap();
        transaction.commit("Sketch.ino").unwrap();
        let (mut displaced, identity) = transaction.displaced().unwrap();
        assert_ne!(identity, original_identity);
        assert_eq!(
            read_bounded(&mut displaced, 1024).unwrap(),
            b"external edit at final use"
        );
        transaction.retain = true;
        let recovery = transaction.directory.path.clone();
        drop(transaction);
        assert_eq!(
            std::fs::read(recovery.join(native::DISPLACED)).unwrap(),
            b"external edit at final use"
        );
        assert_eq!(
            std::fs::read(fixture.root.join("Sketch.ino")).unwrap(),
            b"native replacement"
        );
    }
}
