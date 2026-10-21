//! Owned compile-input fixtures. No subprocess, live cache or execution grant.

use crate::file_identity::{native_identity, single_link, NativeFileIdentity};
use crate::recipe_snapshot::{alias, open_native, pin_chain, verify_directories, PinnedDirectory};
use mcu_core::compile_inputs::{CompileInputPlan, CompileSource, ExactCompileTarget};
use std::collections::BTreeSet;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

const MAX_TREE_FILES: usize = 4096;
const MAX_TREE_DIRECTORIES: usize = 512;
const MAX_TREE_FILE_BYTES: usize = 64 * 1024 * 1024;
const MAX_TREE_BYTES: usize = 256 * 1024 * 1024;
const MAX_MANIFEST_BYTES: usize = 20 * 1024 * 1024;
const MAX_ROOT_ENTRIES: usize = 4096;
static NEXT_STAGE: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PreparedRole {
    Platform,
    Board,
    Framework,
    Compiler,
    PrivateRuntime,
    PlatformIo,
    ToolAdapter,
}
pub struct PreparedInput {
    pub path: String,
    pub digest: String,
    pub role: PreparedRole,
}
pub struct PreparedFixtureSpec {
    pub target: ExactCompileTarget,
    pub host_os: String,
    pub host_arch: String,
    pub inputs: Vec<PreparedInput>,
}
/// Trusted, native fixture inputs; never deserialize these from the editor.
/// Role labels/hash equality alone do not certify third-party tool semantics.
pub struct OwnedCompileScope {
    source: PathBuf,
    tools: PathBuf,
    stages: PathBuf,
    prefixes: Vec<PinnedDirectory>,
    spec: PreparedFixtureSpec,
    owner: Arc<()>,
    attempted: bool,
    valid: bool,
    #[cfg(test)]
    before_stage_capture: Option<fn(&Path)>,
}

/// Opaque process-local preflight evidence. No spawn method or serializable grant.
/// Retained handles pin object identities, not immutable contents. Drop releases
/// handles only; partial/full owned staging is retained for explicit cleanup.
pub struct StagedCompileInputs {
    owner: Arc<()>,
    target: ExactCompileTarget,
    generation: u64,
    source: InputSnapshot,
    tools: InputSnapshot,
    staged: InputSnapshot,
    project_root: PathBuf,
    prepared_root: PathBuf,
    output_root: PathBuf,
    prefixes: Vec<PinnedDirectory>,
}
impl StagedCompileInputs {
    pub fn stage_root(&self) -> &Path {
        &self.project_root
    }
    /// Writable outputs are separate from all captured inputs. This path alone
    /// grants no process execution or successful-artifact reuse authority.
    pub fn output_root(&self) -> &Path {
        &self.output_root
    }
    pub fn prepared_root(&self) -> &Path {
        &self.prepared_root
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CompileRefusal {
    InvalidScope,
    UnsafePath,
    Changed,
    TooLarge,
    Io,
    InvalidProvenance,
    InvalidSources,
    SelectionChanged,
    UsedScope,
    ForeignScope,
    Revoked,
}
type CompileResult<T> = Result<T, CompileRefusal>;
fn io<T>(value: std::io::Result<T>) -> CompileResult<T> {
    value.map_err(|_| CompileRefusal::Io)
}
fn native<T>(value: Result<T, mcu_core::board_compatibility::SnapshotRefusal>) -> CompileResult<T> {
    value.map_err(|_| CompileRefusal::UnsafePath)
}

impl OwnedCompileScope {
    /// All roots must already exist, be disjoint and strictly below the owned
    /// temp/audit/native-compile-inputs sandbox. No live project/store is adopted.
    pub fn new(
        app: &Path,
        source: &Path,
        tools: &Path,
        stages: &Path,
        spec: PreparedFixtureSpec,
    ) -> CompileResult<Self> {
        let mut prefixes = native(pin_chain(app))?;
        let sandbox = io(app.canonicalize())?.join("temp/audit/native-compile-inputs");
        prefixes.extend(native(pin_chain(&sandbox))?);
        let sandbox = io(sandbox.canonicalize())?;
        let mut roots = Vec::new();
        for path in [source, tools, stages] {
            if path.components().any(|p| matches!(p,std::path::Component::Normal(n) if n.to_str().is_some_and(protected_component))) { return Err(CompileRefusal::InvalidScope); }
            prefixes.extend(native(pin_chain(path))?);
            let root = io(path.canonicalize())?;
            if root == sandbox || !root.starts_with(&sandbox) {
                return Err(CompileRefusal::InvalidScope);
            }
            roots.push(root);
        }
        for (i, a) in roots.iter().enumerate() {
            for b in &roots[i + 1..] {
                if a.starts_with(b) || b.starts_with(a) {
                    return Err(CompileRefusal::InvalidScope);
                }
            }
        }
        native(verify_directories(&prefixes))?;
        Ok(Self {
            source: roots[0].clone(),
            tools: roots[1].clone(),
            stages: roots[2].clone(),
            prefixes,
            spec,
            owner: Arc::new(()),
            attempted: false,
            valid: false,
            #[cfg(test)]
            before_stage_capture: None,
        })
    }

    /// Synchronous bounded worker operation. One attempt per scope; failed
    /// captures cannot publish receipts or retry into accumulating partial work.
    pub fn capture_inputs(
        &mut self,
        target: &ExactCompileTarget,
        generation: u64,
    ) -> CompileResult<StagedCompileInputs> {
        self.valid = false;
        if self.attempted {
            return Err(CompileRefusal::UsedScope);
        }
        self.attempted = true;
        if generation == 0 || *target != self.spec.target {
            return Err(CompileRefusal::SelectionChanged);
        }
        native(verify_directories(&self.prefixes))?;
        let source = InputSnapshot::capture(&self.source, true)?;
        let plan = CompileInputPlan::new(
            target.clone(),
            source
                .files
                .iter()
                .map(|f| CompileSource {
                    name: f.name.clone(),
                    bytes: f.bytes.clone(),
                })
                .collect(),
        )
        .map_err(|_| CompileRefusal::InvalidSources)?;
        let tools = InputSnapshot::capture(&self.tools, false)?;
        self.admit_prepared(&tools)?;
        source.verify()?;
        tools.verify()?;
        let manifest = self.manifest(&plan, &tools);
        if manifest.len() > MAX_MANIFEST_BYTES {
            return Err(CompileRefusal::TooLarge);
        }
        native(verify_directories(&self.prefixes))?;
        let bundle = reserve_stage(&self.stages)?;
        let stage = bundle.join("inputs");
        let output_root = bundle.join("outputs");
        io(fs::create_dir(&stage))?;
        io(fs::create_dir(&output_root))?;
        io(fs::create_dir(output_root.join("core")))?;
        let mut prefixes = native(pin_chain(&stage))?;
        prefixes.extend(native(pin_chain(&output_root))?);
        let project_root = stage.join("project");
        io(fs::create_dir(&project_root))?;
        let sources_dir = project_root.join("sources");
        let prepared_dir = stage.join("prepared");
        io(fs::create_dir(&sources_dir))?;
        io(fs::create_dir(&prepared_dir))?;
        for directory in tools.directories.iter().skip(1) {
            io(fs::create_dir(prepared_dir.join(&directory.name)))?;
        }
        for source in plan.sources() {
            write_new(&sources_dir.join(&source.name), &source.bytes, None)?;
        }
        for input in &tools.files {
            write_new(
                &prepared_dir.join(&input.name),
                &input.bytes,
                Some(input.mode),
            )?;
        }
        write_new(
            &project_root.join("platformio.ini"),
            plan.config_bytes(),
            None,
        )?;
        write_new(&stage.join("compile-inputs.json"), &manifest, None)?;
        #[cfg(test)]
        if let Some(callback) = self.before_stage_capture.take() {
            callback(&stage);
        }
        let staged = InputSnapshot::capture_kind(&stage, false, true)?;
        let expected_files: BTreeSet<_> = source
            .files
            .iter()
            .map(|f| format!("project/sources/{}", f.name))
            .chain(tools.files.iter().map(|f| format!("prepared/{}", f.name)))
            .chain([
                "project/platformio.ini".into(),
                "compile-inputs.json".into(),
            ])
            .collect();
        let expected_directories: BTreeSet<_> = tools
            .directories
            .iter()
            .skip(1)
            .map(|d| format!("prepared/{}", d.name))
            .chain([
                "".into(),
                "prepared".into(),
                "project".into(),
                "project/sources".into(),
            ])
            .collect();
        if staged
            .files
            .iter()
            .map(|f| f.name.clone())
            .collect::<BTreeSet<_>>()
            != expected_files
            || staged
                .directories
                .iter()
                .map(|d| d.name.clone())
                .collect::<BTreeSet<_>>()
                != expected_directories
        {
            return Err(CompileRefusal::Changed);
        }
        // Verify every final copy against its captured bytes before issuing a receipt.
        for input in &source.files {
            staged.expect(&format!("project/sources/{}", input.name), &input.bytes)?;
        }
        for input in &tools.files {
            staged.expect(&format!("prepared/{}", input.name), &input.bytes)?;
        }
        staged.expect("project/platformio.ini", plan.config_bytes())?;
        staged.expect("compile-inputs.json", &manifest)?;
        source.verify()?;
        tools.verify()?;
        staged.verify()?;
        native(verify_directories(&self.prefixes))?;
        native(verify_directories(&prefixes))?;
        self.valid = true;
        Ok(StagedCompileInputs {
            owner: self.owner.clone(),
            target: target.clone(),
            generation,
            source,
            tools,
            staged,
            project_root,
            prepared_root: prepared_dir,
            output_root,
            prefixes,
        })
    }

    /// Re-read actual bytes and complete inventories. Any failed observation
    /// revokes this scope, even if the external writer later restores content.
    /// Active ABA, non-atomic reads, OS stalls and final-use races remain open.
    pub fn revalidate_inputs(
        &mut self,
        receipt: &StagedCompileInputs,
        target: &ExactCompileTarget,
        generation: u64,
    ) -> CompileResult<()> {
        let result = (|| {
            if !self.valid {
                return Err(CompileRefusal::Revoked);
            }
            if !Arc::ptr_eq(&self.owner, &receipt.owner) {
                return Err(CompileRefusal::ForeignScope);
            }
            if *target != receipt.target || generation != receipt.generation {
                return Err(CompileRefusal::SelectionChanged);
            }
            native(verify_directories(&self.prefixes))?;
            native(verify_directories(&receipt.prefixes))?;
            receipt.source.verify()?;
            receipt.tools.verify()?;
            receipt.staged.verify()?;
            native(verify_directories(&self.prefixes))?;
            native(verify_directories(&receipt.prefixes))?;
            Ok(())
        })();
        if result.is_err() {
            self.valid = false;
        }
        result
    }

    fn admit_prepared(&self, tools: &InputSnapshot) -> CompileResult<()> {
        use PreparedRole::*;
        if self.spec.host_os != std::env::consts::OS
            || self.spec.host_arch != std::env::consts::ARCH
            || self.spec.inputs.len() != tools.files.len()
            || self.spec.inputs.len() > MAX_TREE_FILES
        {
            return Err(CompileRefusal::InvalidProvenance);
        }
        let mut names = BTreeSet::new();
        for entry in &self.spec.inputs {
            portable_relative(&entry.path)?;
            if !names.insert(entry.path.clone())
                || entry.digest.len() != 64
                || !entry
                    .digest
                    .bytes()
                    .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
            {
                return Err(CompileRefusal::InvalidProvenance);
            }
            let file = tools
                .files
                .iter()
                .find(|f| f.name == entry.path)
                .ok_or(CompileRefusal::InvalidProvenance)?;
            if file.digest != entry.digest {
                return Err(CompileRefusal::InvalidProvenance);
            }
        }
        for role in [
            Platform,
            Board,
            Framework,
            Compiler,
            PrivateRuntime,
            PlatformIo,
            ToolAdapter,
        ] {
            if !self.spec.inputs.iter().any(|i| i.role == role) {
                return Err(CompileRefusal::InvalidProvenance);
            }
        }
        let descriptor = |role| -> CompileResult<&InputFile> {
            let mut entries = self.spec.inputs.iter().filter(|i| i.role == role);
            let entry = entries.next().ok_or(CompileRefusal::InvalidProvenance)?;
            if entries.next().is_some() {
                return Err(CompileRefusal::InvalidProvenance);
            }
            tools
                .files
                .iter()
                .find(|f| f.name == entry.path)
                .ok_or(CompileRefusal::InvalidProvenance)
        };
        let platform = descriptor(Platform)?;
        let board = descriptor(Board)?;
        mcu_core::prepared_target::validate_prepared_descriptors(
            &self.spec.target,
            &platform.name,
            &platform.bytes,
            &board.name,
            &board.bytes,
        )
        .map_err(|_| CompileRefusal::InvalidProvenance)?;
        Ok(())
    }

    fn manifest(&self, plan: &CompileInputPlan, tools: &InputSnapshot) -> Vec<u8> {
        let sources: Vec<_> = plan.sources().iter().map(|s| serde_json::json!({"path":s.name,"sha256":mcu_core::source_digest(&s.bytes).0,"bytes":s.bytes.len()})).collect();
        let inputs: Vec<_> = tools.files.iter().map(|s| serde_json::json!({"path":s.name,"sha256":s.digest,"bytes":s.bytes.len(),"role":format!("{:?}",self.spec.inputs.iter().find(|i| i.path == s.name).expect("admitted input").role)})).collect();
        serde_json::to_vec_pretty(&serde_json::json!({"schema":1,"scope":"fixture-only-no-dispatch","platform":plan.target().platform_spec(),"board":plan.target().board_id(),"framework":plan.target().framework(),"host_os":self.spec.host_os,"host_arch":self.spec.host_arch,"config_sha256":mcu_core::source_digest(plan.config_bytes()).0,"sources":sources,"prepared":inputs})).expect("finite JSON values")
    }
}

struct InputFile {
    name: String,
    handle: File,
    identity: NativeFileIdentity,
    bytes: Vec<u8>,
    digest: String,
    mode: u32,
}
struct InputDirectory {
    name: String,
    handle: File,
    identity: NativeFileIdentity,
}
struct InputSnapshot {
    root: PathBuf,
    source_only: bool,
    staged: bool,
    files: Vec<InputFile>,
    directories: Vec<InputDirectory>,
}

impl InputSnapshot {
    fn capture(root: &Path, source_only: bool) -> CompileResult<Self> {
        Self::capture_kind(root, source_only, false)
    }
    fn capture_kind(root: &Path, source_only: bool, staged: bool) -> CompileResult<Self> {
        let mut snapshot = Self {
            root: root.into(),
            source_only,
            staged,
            files: Vec::new(),
            directories: Vec::new(),
        };
        snapshot.scan("", &mut 0)?;
        snapshot.verify_handles()?;
        Ok(snapshot)
    }
    fn scan(&mut self, relative: &str, total: &mut usize) -> CompileResult<()> {
        let path = self.root.join(relative);
        let handle = native(open_native(&path, true))?;
        let metadata = io(handle.metadata())?;
        if alias(&metadata) || !metadata.is_dir() {
            return Err(CompileRefusal::UnsafePath);
        }
        let identity = io(native_identity(&handle))?;
        if self.directories.len() >= MAX_TREE_DIRECTORIES + if self.staged { 3 } else { 0 } {
            return Err(CompileRefusal::TooLarge);
        }
        self.directories.push(InputDirectory {
            name: relative.into(),
            handle,
            identity,
        });
        let mut entries = Vec::new();
        for entry in io(fs::read_dir(&path))? {
            if entries.len() >= MAX_ROOT_ENTRIES {
                return Err(CompileRefusal::TooLarge);
            }
            entries.push(io(entry)?);
        }
        entries.sort_by_key(|e| e.file_name());
        let mut names = BTreeSet::new();
        for entry in entries {
            let name = entry
                .file_name()
                .into_string()
                .map_err(|_| CompileRefusal::UnsafePath)?;
            if self.source_only
                && !matches!(
                    name.rsplit('.')
                        .next()
                        .unwrap_or("")
                        .to_ascii_lowercase()
                        .as_str(),
                    "ino" | "c" | "cpp" | "h" | "hpp"
                )
            {
                continue;
            }
            let relative_name = if relative.is_empty() {
                name
            } else {
                format!("{relative}/{name}")
            };
            portable_relative_kind(&relative_name, self.staged)?;
            if !names.insert(relative_name.to_uppercase().to_lowercase()) {
                return Err(CompileRefusal::UnsafePath);
            }
            let metadata = io(fs::symlink_metadata(entry.path()))?;
            if alias(&metadata) {
                return Err(CompileRefusal::UnsafePath);
            }
            if metadata.is_dir() {
                if self.source_only {
                    return Err(CompileRefusal::InvalidSources);
                }
                self.scan(&relative_name, total)?;
            } else {
                if !metadata.is_file() {
                    return Err(CompileRefusal::UnsafePath);
                }
                if self.files.len()
                    >= if self.source_only {
                        128
                    } else {
                        MAX_TREE_FILES
                            + if self.staged {
                                mcu_core::compile_inputs::MAX_COMPILE_SOURCES + 2
                            } else {
                                0
                            }
                    }
                {
                    return Err(CompileRefusal::TooLarge);
                }
                let limit = if self.source_only {
                    mcu_core::compile_inputs::MAX_COMPILE_SOURCE_BYTES
                } else {
                    MAX_TREE_FILE_BYTES
                };
                let file = read_file(&self.root, &relative_name, limit)?;
                *total = total
                    .checked_add(file.bytes.len())
                    .ok_or(CompileRefusal::TooLarge)?;
                if *total
                    > if self.source_only {
                        mcu_core::compile_inputs::MAX_COMPILE_TOTAL_BYTES
                    } else {
                        MAX_TREE_BYTES
                            + if self.staged {
                                mcu_core::compile_inputs::MAX_COMPILE_TOTAL_BYTES
                                    + MAX_MANIFEST_BYTES
                            } else {
                                0
                            }
                    }
                {
                    return Err(CompileRefusal::TooLarge);
                }
                self.files.push(file);
            }
        }
        self.files.sort_by(|a, b| a.name.cmp(&b.name));
        self.directories.sort_by(|a, b| a.name.cmp(&b.name));
        Ok(())
    }
    fn verify_handles(&self) -> CompileResult<()> {
        for d in &self.directories {
            let current = native(open_native(&self.root.join(&d.name), true))?;
            if !io(current.metadata())?.is_dir()
                || alias(&io(current.metadata())?)
                || io(native_identity(&current))? != d.identity
                || io(native_identity(&d.handle))? != d.identity
            {
                return Err(CompileRefusal::Changed);
            }
        }
        for f in &self.files {
            let current = native(open_native(&self.root.join(&f.name), true))?;
            if io(native_identity(&current))? != f.identity
                || io(native_identity(&f.handle))? != f.identity
                || !io(single_link(&current))?
            {
                return Err(CompileRefusal::Changed);
            }
        }
        Ok(())
    }
    fn verify(&self) -> CompileResult<()> {
        self.verify_handles()?;
        let current = Self::capture_kind(&self.root, self.source_only, self.staged)?;
        if self.directories.len() != current.directories.len()
            || self.files.len() != current.files.len()
            || self
                .directories
                .iter()
                .zip(&current.directories)
                .any(|(a, b)| a.name != b.name || a.identity != b.identity)
            || self.files.iter().zip(&current.files).any(|(a, b)| {
                a.name != b.name
                    || a.identity != b.identity
                    || a.digest != b.digest
                    || a.mode != b.mode
            })
        {
            return Err(CompileRefusal::Changed);
        }
        self.verify_handles()
    }
    fn expect(&self, name: &str, bytes: &[u8]) -> CompileResult<()> {
        if !self
            .files
            .iter()
            .any(|f| f.name == name && f.bytes == bytes)
        {
            return Err(CompileRefusal::Changed);
        }
        Ok(())
    }
}

fn portable_relative(name: &str) -> CompileResult<()> {
    portable_relative_kind(name, false)
}
fn protected_component(name: &str) -> bool {
    matches!(
        name.to_ascii_lowercase().as_str(),
        ".mcu_flasher_build_cache" | ".pio" | ".vscode" | ".clangd" | ".opencode"
    )
}
fn portable_relative_kind(name: &str, staged: bool) -> CompileResult<()> {
    if name.is_empty()
        || name.len() > 4096 + if staged { 9 } else { 0 }
        || name.split('/').count() > if staged { 25 } else { 24 }
    {
        return Err(CompileRefusal::UnsafePath);
    }
    for part in name.split('/') {
        if part.is_empty()
            || protected_component(part)
            || part == "."
            || part == ".."
            || part.len() > 255
            || part.ends_with([' ', '.'])
            || part
                .chars()
                .any(|c| c.is_control() || ":\\<>\"|?*".contains(c))
            || mcu_core::source_names::is_windows_reserved_device_name(part)
        {
            return Err(CompileRefusal::UnsafePath);
        }
    }
    Ok(())
}
fn read_file(root: &Path, name: &str, limit: usize) -> CompileResult<InputFile> {
    let path = root.join(name);
    let handle = native(open_native(&path, true))?;
    let metadata = io(handle.metadata())?;
    if !metadata.is_file() || alias(&metadata) || !io(single_link(&handle))? {
        return Err(CompileRefusal::UnsafePath);
    }
    if metadata.len() > limit as u64 {
        return Err(CompileRefusal::TooLarge);
    }
    let identity = io(native_identity(&handle))?;
    let mut content = native(open_native(&path, false))?;
    let before = io(content.metadata())?;
    if !before.is_file()
        || alias(&before)
        || !io(single_link(&content))?
        || io(native_identity(&content))? != identity
    {
        return Err(CompileRefusal::Changed);
    }
    let mut bytes = Vec::new();
    io((&mut content)
        .take(limit as u64 + 1)
        .read_to_end(&mut bytes))?;
    let after = io(content.metadata())?;
    if bytes.len() > limit {
        return Err(CompileRefusal::TooLarge);
    }
    if bytes.len() as u64 != before.len()
        || before.len() != after.len()
        || before.modified().ok() != after.modified().ok()
        || file_mode(&before) != file_mode(&after)
        || !io(single_link(&content))?
    {
        return Err(CompileRefusal::Changed);
    }
    let current = native(open_native(&path, true))?;
    if io(native_identity(&current))? != identity {
        return Err(CompileRefusal::Changed);
    }
    Ok(InputFile {
        name: name.into(),
        handle,
        identity,
        digest: mcu_core::source_digest(&bytes).0,
        bytes,
        mode: file_mode(&before),
    })
}
fn file_mode(metadata: &fs::Metadata) -> u32 {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        metadata.permissions().mode() & 0o777
    }
    #[cfg(not(unix))]
    {
        u32::from(metadata.permissions().readonly())
    }
}
fn write_new(path: &Path, bytes: &[u8], mode: Option<u32>) -> CompileResult<()> {
    let mut file = io(OpenOptions::new().create_new(true).write(true).open(path))?;
    io(file.write_all(bytes))?;
    #[cfg(unix)]
    if let Some(mode) = mode {
        use std::os::unix::fs::PermissionsExt;
        io(file.set_permissions(fs::Permissions::from_mode(mode)))?;
    }
    #[cfg(not(unix))]
    let _ = mode;
    io(file.sync_all())
}
fn reserve_stage(parent: &Path) -> CompileResult<PathBuf> {
    // create_dir is the create-new ownership boundary; occupied names are never
    // inspected or overwritten. At most 32 collisions are attempted.
    for _ in 0..32 {
        let path = parent.join(format!(
            "compile-{}-{}",
            std::process::id(),
            NEXT_STAGE.fetch_add(1, Ordering::Relaxed)
        ));
        match fs::create_dir(&path) {
            Ok(()) => return Ok(path),
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(_) => return Err(CompileRefusal::Io),
        }
    }
    Err(CompileRefusal::UsedScope)
}

#[cfg(test)]
mod tests {
    use super::*;
    use mcu_core::compile_inputs::ExactCompileTarget;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(0);
    struct Fixture {
        app: PathBuf,
        root: PathBuf,
        source: PathBuf,
        tools: PathBuf,
        stages: PathBuf,
    }
    impl Fixture {
        fn new() -> Self {
            let app = Path::new(env!("CARGO_MANIFEST_DIR"))
                .ancestors()
                .nth(3)
                .unwrap()
                .to_path_buf();
            let base = app.join("temp/audit/native-compile-inputs");
            fs::create_dir_all(&base).unwrap();
            let root = base.join(format!(
                "unit-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&root).unwrap();
            let source = root.join("project");
            let tools = root.join("prepared");
            let stages = root.join("stages");
            for dir in [&source, &tools, &stages] {
                fs::create_dir(dir).unwrap();
            }
            fs::write(
                source.join("main.cpp"),
                b"\xef\xbb\xbf#include \"pins.h\"\r\nvoid setup() {}\r\nvoid loop() {}\r\n",
            )
            .unwrap();
            fs::write(source.join("pins.h"), b"#define PIN 13\r\n").unwrap();
            fs::write(source.join("helper.c"), b"int helper(void) { return 7; }\n").unwrap();
            for (name, role) in Self::roles() {
                fs::create_dir_all(tools.join(name).parent().unwrap()).unwrap();
                let bytes: &[u8] = match role {
                    PreparedRole::Platform => {
                        br#"{"name":"atmelavr","version":"5.1.0","frameworks":{"arduino":{}}}"#
                    }
                    PreparedRole::Board => br#"{"frameworks":["arduino"]}"#,
                    _ => name.as_bytes(),
                };
                fs::write(tools.join(name), bytes).unwrap();
            }
            Self {
                app,
                root,
                source,
                tools,
                stages,
            }
        }
        fn target() -> ExactCompileTarget {
            ExactCompileTarget::new("platformio/atmelavr@5.1.0", "uno", "arduino").unwrap()
        }
        fn roles() -> Vec<(&'static str, PreparedRole)> {
            vec![
                (
                    "core/platforms/atmelavr/platform.json",
                    PreparedRole::Platform,
                ),
                (
                    "core/platforms/atmelavr/boards/uno.json",
                    PreparedRole::Board,
                ),
                ("framework.bin", PreparedRole::Framework),
                ("compiler.bin", PreparedRole::Compiler),
                ("python.bin", PreparedRole::PrivateRuntime),
                ("platformio.bin", PreparedRole::PlatformIo),
                ("adapter.py", PreparedRole::ToolAdapter),
            ]
        }
        fn spec(&self) -> PreparedFixtureSpec {
            PreparedFixtureSpec {
                target: Self::target(),
                host_os: std::env::consts::OS.into(),
                host_arch: std::env::consts::ARCH.into(),
                inputs: Self::roles()
                    .into_iter()
                    .map(|(name, role)| PreparedInput {
                        path: name.into(),
                        digest: mcu_core::source_digest(&fs::read(self.tools.join(name)).unwrap())
                            .0,
                        role,
                    })
                    .collect(),
            }
        }
        fn scope(&self) -> OwnedCompileScope {
            OwnedCompileScope::new(
                &self.app,
                &self.source,
                &self.tools,
                &self.stages,
                self.spec(),
            )
            .unwrap()
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            fs::remove_dir_all(&self.root).unwrap();
        }
    }

    #[test]
    fn stages_all_root_source_bytes_and_complete_prepared_tree_in_fresh_files() {
        let f = Fixture::new();
        let mut scope = f.scope();
        let receipt = scope.capture_inputs(&Fixture::target(), 1).unwrap();
        for name in ["main.cpp", "pins.h", "helper.c"] {
            assert_eq!(
                fs::read(receipt.stage_root().join("sources").join(name)).unwrap(),
                fs::read(f.source.join(name)).unwrap()
            );
            assert_ne!(
                native_identity(&open_native(&f.source.join(name), true).unwrap()).unwrap(),
                native_identity(
                    &open_native(&receipt.stage_root().join("sources").join(name), true).unwrap()
                )
                .unwrap()
            );
        }
        assert!(
            fs::read_to_string(receipt.stage_root().join("platformio.ini"))
                .unwrap()
                .contains("board = uno\n")
        );
        for (name, _) in Fixture::roles() {
            assert_eq!(
                fs::read(receipt.prepared_root().join(name)).unwrap(),
                fs::read(f.tools.join(name)).unwrap()
            );
        }
        assert!(scope
            .revalidate_inputs(&receipt, &Fixture::target(), 1)
            .is_ok());
        assert!(scope.capture_inputs(&Fixture::target(), 1).is_err());
    }

    #[test]
    fn source_bytes_inventory_and_staged_mutation_revoke_receipt() {
        for change in ["bytes", "add", "stage", "tools", "tool-add", "config"] {
            let f = Fixture::new();
            let mut scope = f.scope();
            let receipt = scope.capture_inputs(&Fixture::target(), 9).unwrap();
            match change {
                "bytes" => fs::write(f.source.join("pins.h"), b"#define PIN 14\r\n").unwrap(),
                "add" => fs::write(f.source.join("extra.hpp"), b"// extra").unwrap(),
                "stage" => {
                    fs::write(receipt.stage_root().join("sources/main.cpp"), b"different").unwrap()
                }
                "tools" => fs::write(f.tools.join("compiler.bin"), b"different").unwrap(),
                "tool-add" => fs::write(f.tools.join("extra.bin"), b"extra").unwrap(),
                _ => fs::write(
                    receipt.stage_root().join("platformio.ini"),
                    b"[env:changed]",
                )
                .unwrap(),
            }
            assert!(
                scope
                    .revalidate_inputs(&receipt, &Fixture::target(), 9)
                    .is_err(),
                "{change}"
            );
            assert!(scope
                .revalidate_inputs(&receipt, &Fixture::target(), 9)
                .is_err());
        }
    }

    #[test]
    fn foreign_receipts_and_stale_native_selection_are_refused() {
        let a = Fixture::new();
        let b = Fixture::new();
        let mut first = a.scope();
        let mut second = b.scope();
        let receipt = first.capture_inputs(&Fixture::target(), 1).unwrap();
        assert!(second
            .revalidate_inputs(&receipt, &Fixture::target(), 1)
            .is_err());
        assert!(first
            .revalidate_inputs(&receipt, &Fixture::target(), 2)
            .is_err());
        assert!(first
            .revalidate_inputs(&receipt, &Fixture::target(), 1)
            .is_err());
    }

    #[test]
    fn attempted_replacement_capture_revokes_prior_receipt() {
        let f = Fixture::new();
        let mut scope = f.scope();
        let receipt = scope.capture_inputs(&Fixture::target(), 1).unwrap();
        assert!(scope.capture_inputs(&Fixture::target(), 2).is_err());
        assert!(scope
            .revalidate_inputs(&receipt, &Fixture::target(), 1)
            .is_err());
    }

    #[test]
    fn incomplete_wrong_host_or_changed_prepared_provenance_never_stages() {
        let f = Fixture::new();
        for mode in ["missing-role", "host", "digest", "missing-file", "extra"] {
            let mut spec = f.spec();
            match mode {
                "missing-role" => {
                    spec.inputs.pop();
                }
                "host" => spec.host_arch = "foreign".into(),
                "digest" => spec.inputs[0].digest = "0".repeat(64),
                "missing-file" => spec.inputs[0].path = "absent.bin".into(),
                _ => {
                    fs::write(f.tools.join("extra.bin"), b"extra").unwrap();
                }
            }
            let mut scope =
                OwnedCompileScope::new(&f.app, &f.source, &f.tools, &f.stages, spec).unwrap();
            assert!(
                scope.capture_inputs(&Fixture::target(), 1).is_err(),
                "{mode}"
            );
        }
    }

    #[test]
    fn linked_sources_and_tool_entries_are_refused_and_originals_preserved() {
        let f = Fixture::new();
        let original = fs::read(f.source.join("main.cpp")).unwrap();
        fs::hard_link(f.source.join("main.cpp"), f.root.join("external.cpp")).unwrap();
        assert!(f.scope().capture_inputs(&Fixture::target(), 1).is_err());
        assert_eq!(fs::read(f.source.join("main.cpp")).unwrap(), original);
        let g = Fixture::new();
        fs::hard_link(g.tools.join("compiler.bin"), g.root.join("external.bin")).unwrap();
        assert!(g.scope().capture_inputs(&Fixture::target(), 1).is_err());
    }

    #[test]
    fn unsupported_sketch_conversion_and_unlisted_source_directories_are_not_copied() {
        let f = Fixture::new();
        fs::create_dir(f.source.join("nested")).unwrap();
        fs::write(f.source.join("nested/secret.cpp"), b"untouched").unwrap();
        fs::write(f.source.join("NOTE.txt"), b"notes stay in the project").unwrap();
        let mut scope = f.scope();
        let receipt = scope.capture_inputs(&Fixture::target(), 1).unwrap();
        assert!(!receipt.stage_root().join("sources/nested").exists());
        assert!(!receipt.stage_root().join("sources/NOTE.txt").exists());
        fs::write(
            f.source.join("Sketch.ino"),
            b"void setup() {} void loop() {}",
        )
        .unwrap();
        assert!(f.scope().capture_inputs(&Fixture::target(), 1).is_err());
    }

    #[test]
    fn identical_byte_leaf_replacement_and_stage_additions_are_refused() {
        for change in ["source-replace", "tool-replace", "stage-add", "empty-dir"] {
            let f = Fixture::new();
            let mut scope = f.scope();
            let receipt = scope.capture_inputs(&Fixture::target(), 1).unwrap();
            match change {
                "source-replace" | "tool-replace" => {
                    let path = if change == "source-replace" {
                        f.source.join("pins.h")
                    } else {
                        f.tools.join("compiler.bin")
                    };
                    let bytes = fs::read(&path).unwrap();
                    fs::rename(&path, f.root.join("retained.bin")).unwrap();
                    fs::write(path, bytes).unwrap();
                }
                "stage-add" => fs::write(receipt.stage_root().join("extra.py"), b"extra").unwrap(),
                _ => fs::create_dir(f.tools.join("extra-directory")).unwrap(),
            }
            assert!(
                scope
                    .revalidate_inputs(&receipt, &Fixture::target(), 1)
                    .is_err(),
                "{change}"
            );
        }
    }

    #[test]
    fn scope_rejects_live_and_overlapping_roots_before_creating_output() {
        let f = Fixture::new();
        assert!(OwnedCompileScope::new(&f.app, &f.app, &f.tools, &f.stages, f.spec()).is_err());
        assert!(OwnedCompileScope::new(&f.app, &f.source, &f.tools, &f.source, f.spec()).is_err());
        assert!(OwnedCompileScope::new(&f.app, &f.source, &f.tools, &f.tools, f.spec()).is_err());
        assert_eq!(fs::read_dir(&f.stages).unwrap().count(), 0);
    }

    #[test]
    fn protected_tree_components_are_refused_before_staging() {
        let f = Fixture::new();
        fs::create_dir(f.tools.join(".PIO")).unwrap();
        assert!(f.scope().capture_inputs(&Fixture::target(), 1).is_err());
        assert_eq!(fs::read_dir(&f.stages).unwrap().count(), 0);
    }

    #[test]
    fn matching_source_names_and_tree_entry_limits_are_portable() {
        #[cfg(unix)]
        {
            let f = Fixture::new();
            fs::write(f.source.join("PINS.H"), b"collision").unwrap();
            assert!(f.scope().capture_inputs(&Fixture::target(), 1).is_err());
        }
        let g = Fixture::new();
        for index in 0..MAX_TREE_DIRECTORIES {
            fs::create_dir(g.tools.join(format!("dir{index:04}"))).unwrap();
        }
        assert!(g.scope().capture_inputs(&Fixture::target(), 1).is_err());
        assert_eq!(fs::read_dir(&g.stages).unwrap().count(), 0);
    }

    #[test]
    fn nested_tool_paths_and_empty_directories_are_copied_and_bound() {
        let f = Fixture::new();
        fs::create_dir_all(f.tools.join("nested/empty")).unwrap();
        fs::write(f.tools.join("nested/builder.py"), b"# literal tool bytes\n").unwrap();
        let mut spec = f.spec();
        spec.inputs.push(PreparedInput {
            path: "nested/builder.py".into(),
            digest: mcu_core::source_digest(b"# literal tool bytes\n").0,
            role: PreparedRole::ToolAdapter,
        });
        let mut scope =
            OwnedCompileScope::new(&f.app, &f.source, &f.tools, &f.stages, spec).unwrap();
        let receipt = scope.capture_inputs(&Fixture::target(), 1).unwrap();
        assert_eq!(
            fs::read(receipt.prepared_root().join("nested/builder.py")).unwrap(),
            b"# literal tool bytes\n"
        );
        assert!(receipt.prepared_root().join("nested/empty").is_dir());
        assert!(scope
            .revalidate_inputs(&receipt, &Fixture::target(), 1)
            .is_ok());
    }

    #[test]
    fn semantic_prepared_target_mismatch_refuses_even_with_correct_hashes() {
        for mode in ["platform", "board", "duplicate", "path"] {
            let f = Fixture::new();
            match mode {
                "platform" => fs::write(
                    f.tools.join("core/platforms/atmelavr/platform.json"),
                    br#"{"name":"atmelavr","version":"6.0.0","frameworks":{"arduino":{}}}"#,
                )
                .unwrap(),
                "board" => fs::write(
                    f.tools.join("core/platforms/atmelavr/boards/uno.json"),
                    br#"{"frameworks":["espidf"]}"#,
                )
                .unwrap(),
                _ => (),
            }
            let mut spec = f.spec();
            if mode == "duplicate" {
                fs::write(f.tools.join("other.json"), b"{}").unwrap();
                spec.inputs.push(PreparedInput {
                    path: "other.json".into(),
                    digest: mcu_core::source_digest(b"{}").0,
                    role: PreparedRole::Platform,
                });
            }
            if mode == "path" {
                fs::rename(
                    f.tools.join("core/platforms/atmelavr/boards/uno.json"),
                    f.tools.join("wrong.json"),
                )
                .unwrap();
                spec.inputs
                    .iter_mut()
                    .find(|i| i.role == PreparedRole::Board)
                    .unwrap()
                    .path = "wrong.json".into();
            }
            let mut scope =
                OwnedCompileScope::new(&f.app, &f.source, &f.tools, &f.stages, spec).unwrap();
            assert!(
                scope.capture_inputs(&Fixture::target(), 1).is_err(),
                "{mode}"
            );
            assert_eq!(fs::read_dir(&f.stages).unwrap().count(), 0);
        }
    }

    #[test]
    fn full_prepared_file_and_directory_budgets_allow_generated_stage_overhead() {
        for files in [true, false] {
            let f = Fixture::new();
            let mut spec = f.spec();
            if files {
                for index in spec.inputs.len()..MAX_TREE_FILES {
                    let name = format!("f{index:04}.bin");
                    fs::write(f.tools.join(&name), []).unwrap();
                    spec.inputs.push(PreparedInput {
                        path: name,
                        digest: mcu_core::source_digest(b"").0,
                        role: PreparedRole::ToolAdapter,
                    });
                }
            } else {
                let present = InputSnapshot::capture(&f.tools, false)
                    .unwrap()
                    .directories
                    .len();
                for index in present..MAX_TREE_DIRECTORIES {
                    fs::create_dir(f.tools.join(format!("d{index:04}"))).unwrap();
                }
            }
            let mut scope =
                OwnedCompileScope::new(&f.app, &f.source, &f.tools, &f.stages, spec).unwrap();
            let receipt = scope.capture_inputs(&Fixture::target(), 1).unwrap();
            assert!(scope
                .revalidate_inputs(&receipt, &Fixture::target(), 1)
                .is_ok());
        }
    }

    #[test]
    fn preexisting_undeclared_staged_entries_cannot_become_receipt_inputs() {
        for add_directory in [false, true] {
            let f = Fixture::new();
            let mut scope = f.scope();
            scope.before_stage_capture = Some(if add_directory {
                |stage| fs::create_dir(stage.join("injected")).unwrap()
            } else {
                |stage| fs::write(stage.join("injected.py"), b"not declared").unwrap()
            });
            assert!(scope.capture_inputs(&Fixture::target(), 1).is_err());
        }
    }

    #[test]
    fn writable_outputs_are_disjoint_from_captured_compile_inputs() {
        let f = Fixture::new();
        let mut scope = f.scope();
        let receipt = scope.capture_inputs(&Fixture::target(), 1).unwrap();
        assert!(!receipt.output_root().starts_with(receipt.stage_root()));
        assert!(!receipt.stage_root().starts_with(receipt.output_root()));
        fs::write(receipt.output_root().join("firmware.hex"), b":00000001FF\n").unwrap();
        assert!(scope
            .revalidate_inputs(&receipt, &Fixture::target(), 1)
            .is_ok());
    }

    #[test]
    fn project_and_prepared_roots_match_the_explicit_tool_boundary() {
        let f = Fixture::new();
        let mut scope = f.scope();
        let receipt = scope.capture_inputs(&Fixture::target(), 1).unwrap();
        assert!(!receipt.stage_root().join("prepared").exists());
        assert!(receipt.stage_root().join("sources/main.cpp").is_file());
        let roots = [
            receipt.stage_root(),
            receipt.prepared_root(),
            receipt.output_root(),
        ];
        for (i, a) in roots.iter().enumerate() {
            for b in &roots[i + 1..] {
                assert!(!a.starts_with(b) && !b.starts_with(a));
            }
        }
        assert!(receipt
            .prepared_root()
            .join("core/platforms/atmelavr/boards/uno.json")
            .is_file());
    }

    #[cfg(unix)]
    #[test]
    fn executable_tool_bits_are_preserved_without_special_mode_bits() {
        use std::os::unix::fs::PermissionsExt;
        let f = Fixture::new();
        fs::set_permissions(
            f.tools.join("compiler.bin"),
            fs::Permissions::from_mode(0o4755),
        )
        .unwrap();
        let mut scope = f.scope();
        let receipt = scope.capture_inputs(&Fixture::target(), 1).unwrap();
        assert_eq!(
            fs::metadata(receipt.prepared_root().join("compiler.bin"))
                .unwrap()
                .permissions()
                .mode()
                & 0o7777,
            0o755
        );
        fs::set_permissions(
            f.tools.join("compiler.bin"),
            fs::Permissions::from_mode(0o644),
        )
        .unwrap();
        assert!(scope
            .revalidate_inputs(&receipt, &Fixture::target(), 1)
            .is_err());
    }

    #[cfg(unix)]
    #[test]
    fn symlink_roots_and_nested_tool_aliases_are_refused() {
        use std::os::unix::fs::symlink;
        let f = Fixture::new();
        symlink(&f.source, f.root.join("alias")).unwrap();
        assert!(OwnedCompileScope::new(
            &f.app,
            &f.root.join("alias"),
            &f.tools,
            &f.stages,
            f.spec()
        )
        .is_err());
        symlink("compiler.bin", f.tools.join("extra.bin")).unwrap();
        assert!(f.scope().capture_inputs(&Fixture::target(), 1).is_err());
    }
}
