//! Locked legacy-shaped ownership transactions in explicit migration fixtures.
//!
//! The only files this module can write are a fixed `settings.json`, its fixed
//! interoperable lock, and an owned create-new staging sibling under the
//! constructor's `audit/migration/scratch` fixture. It does not discover live
//! settings or publish production ownership. Process evidence and project keys
//! are supplied fixture inputs, not proof of fresh native identity. Metadata
//! checks and replacement still have filesystem identity/TOCTOU races and do
//! not qualify native ACL preservation or crash durability for live writes.

use crate::fixture_lock::{FixtureLockAttempt, FixtureLockScope};
use mcu_core::ownership::{plan_claim, ClaimRequest, Collision, OwnershipRefusal, ProcessEvidence};
use serde_json::Value;
use std::collections::BTreeMap;
use std::fmt;
use std::fs::{self, File, OpenOptions, Permissions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

const SETTINGS_NAME: &str = "settings.json";
const LOCK_NAME: &str = "settings.json.lock";
const MAX_SETTINGS_BYTES: u64 = 1024 * 1024;
static NEXT_STAGE: AtomicU64 = AtomicU64::new(0);

#[derive(Debug)]
pub struct FixtureRegistry {
    root: PathBuf,
    locks: FixtureLockScope,
}

#[derive(Debug, Default)]
pub struct FixtureClaimEvidence {
    pub processes: BTreeMap<String, ProcessEvidence>,
    pub project_keys: BTreeMap<String, String>,
}

#[derive(Debug, PartialEq)]
pub enum FixtureRegistryOutcome {
    Busy,
    Collision(Vec<Collision>),
    Updated { bytes_written: usize },
    Unchanged,
}

#[derive(Debug)]
pub enum FixtureRegistryRefusal {
    Io {
        operation: &'static str,
        error: io::Error,
    },
    UnsafeSettings,
    SettingsTooLarge,
    MalformedJson(serde_json::Error),
    NonObjectSettings,
    Ownership(OwnershipRefusal),
    ExternalChange,
}

impl fmt::Display for FixtureRegistryRefusal {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io { operation, error } => write!(formatter, "{operation}: {error}"),
            Self::UnsafeSettings => formatter
                .write_str("Fixture settings must be a writable regular file without aliases"),
            Self::SettingsTooLarge => formatter.write_str("Fixture settings exceed the byte limit"),
            Self::MalformedJson(error) => {
                write!(formatter, "Malformed fixture settings JSON: {error}")
            }
            Self::NonObjectSettings => {
                formatter.write_str("Fixture settings must be a JSON object")
            }
            Self::Ownership(reason) => write!(formatter, "Ownership plan refused: {reason:?}"),
            Self::ExternalChange => formatter
                .write_str("Fixture settings changed outside their lock; nothing was replaced"),
        }
    }
}

impl std::error::Error for FixtureRegistryRefusal {}

type RegistryResult<T> = Result<T, FixtureRegistryRefusal>;

struct SettingsSnapshot {
    bytes: Option<Vec<u8>>,
    permissions: Option<Permissions>,
}

impl FixtureRegistry {
    /// Both existing absolute directories must pass the fixture lock scope's
    /// alias checks. No home/config/cache path or filename can be selected.
    pub fn new(application_root: &Path, fixture_root: &Path) -> RegistryResult<Self> {
        let locks = FixtureLockScope::new(application_root, fixture_root)
            .map_err(|error| io_refusal("Validate fixture registry scope", error))?;
        let root = fs::canonicalize(fixture_root)
            .map_err(|error| io_refusal("Resolve fixture registry scope", error))?;
        Ok(Self { root, locks })
    }

    /// Acquire the legacy OS lock, freshly read its fixed sibling settings,
    /// then apply only a granted ownership plan. Busy/colliding/refused plans
    /// never publish candidate settings. Unchanged plans retain original bytes
    /// and timestamps rather than serializing or replacing them.
    pub fn claim(
        &self,
        request: ClaimRequest<'_>,
        fixture_evidence: &BTreeMap<String, ProcessEvidence>,
        fixture_project_keys: &BTreeMap<String, String>,
    ) -> RegistryResult<FixtureRegistryOutcome> {
        self.claim_with(request, |_| {
            Ok(FixtureClaimEvidence {
                processes: fixture_evidence.clone(),
                project_keys: fixture_project_keys.clone(),
            })
        })
    }

    /// Obtain controlled host evidence only after the fresh read and while the
    /// legacy OS lock remains held. Run this transaction on a worker: native
    /// process/path observations must never block the renderer/IPC thread.
    /// This fixture API still does not authorize observations of live settings.
    pub fn claim_with(
        &self,
        request: ClaimRequest<'_>,
        gather: impl FnOnce(&Value) -> RegistryResult<FixtureClaimEvidence>,
    ) -> RegistryResult<FixtureRegistryOutcome> {
        self.claim_transaction(request, gather, || Ok(()))
    }

    fn claim_transaction(
        &self,
        request: ClaimRequest<'_>,
        gather: impl FnOnce(&Value) -> RegistryResult<FixtureClaimEvidence>,
        before_replace: impl FnOnce() -> io::Result<()>,
    ) -> RegistryResult<FixtureRegistryOutcome> {
        let FixtureLockAttempt::Acquired(_lease) = self
            .locks
            .try_acquire(LOCK_NAME)
            .map_err(|error| io_refusal("Acquire fixture registry lock", error))?
        else {
            return Ok(FixtureRegistryOutcome::Busy);
        };
        let target = self.root.join(SETTINGS_NAME);
        let snapshot = read_settings(&target)?;
        let config = match &snapshot.bytes {
            Some(bytes) => {
                let value: Value =
                    serde_json::from_slice(bytes).map_err(FixtureRegistryRefusal::MalformedJson)?;
                if !value.is_object() {
                    return Err(FixtureRegistryRefusal::NonObjectSettings);
                }
                value
            }
            None => Value::Object(Default::default()),
        };
        let evidence = gather(&config)?;
        let plan = plan_claim(
            &config,
            request,
            &evidence.processes,
            &evidence.project_keys,
        )
        .map_err(FixtureRegistryRefusal::Ownership)?;
        if !plan.granted() {
            return Ok(FixtureRegistryOutcome::Collision(plan.collisions));
        }
        if plan.config == config {
            return Ok(FixtureRegistryOutcome::Unchanged);
        }
        let mut bytes = serde_json::to_vec_pretty(&plan.config)
            .map_err(FixtureRegistryRefusal::MalformedJson)?;
        bytes.push(b'\n');
        if bytes.len() as u64 > MAX_SETTINGS_BYTES {
            return Err(FixtureRegistryRefusal::SettingsTooLarge);
        }
        self.replace_settings(&target, snapshot, &bytes, before_replace)?;
        Ok(FixtureRegistryOutcome::Updated {
            bytes_written: bytes.len(),
        })
    }

    #[cfg(test)]
    fn claim_with_before_replace(
        &self,
        request: ClaimRequest<'_>,
        fixture_evidence: &BTreeMap<String, ProcessEvidence>,
        fixture_project_keys: &BTreeMap<String, String>,
        before_replace: impl FnOnce() -> io::Result<()>,
    ) -> RegistryResult<FixtureRegistryOutcome> {
        self.claim_transaction(
            request,
            |_| {
                Ok(FixtureClaimEvidence {
                    processes: fixture_evidence.clone(),
                    project_keys: fixture_project_keys.clone(),
                })
            },
            before_replace,
        )
    }

    fn replace_settings(
        &self,
        target: &Path,
        snapshot: SettingsSnapshot,
        bytes: &[u8],
        before_replace: impl FnOnce() -> io::Result<()>,
    ) -> RegistryResult<()> {
        let (pending, mut file) = create_stage(&self.root)?;
        file.write_all(bytes)
            .map_err(|error| io_refusal("Write fixture registry staging file", error))?;
        if let Some(permissions) = snapshot.permissions {
            file.set_permissions(permissions)
                .map_err(|error| io_refusal("Preserve fixture settings permissions", error))?;
        }
        file.flush()
            .and_then(|()| file.sync_all())
            .map_err(|error| io_refusal("Flush fixture registry staging file", error))?;
        drop(file);
        before_replace()
            .map_err(|error| io_refusal("Prepare fixture registry replacement", error))?;
        // A cooperating writer cannot change this file while the lock is held.
        // Detect noncooperating edits before rename; the final identity race
        // remains a documented fixture limitation rather than live authority.
        if read_settings(target)?.bytes != snapshot.bytes {
            return Err(FixtureRegistryRefusal::ExternalChange);
        }
        pending
            .replace(target)
            .map_err(|error| io_refusal("Replace fixture registry settings", error))?;
        // Rename is the commit point. Do not report a later fallible operation
        // as an unsuccessful transaction after the destination has changed.
        Ok(())
    }
}

fn read_settings(path: &Path) -> RegistryResult<SettingsSnapshot> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            return Ok(SettingsSnapshot {
                bytes: None,
                permissions: None,
            });
        }
        Err(error) => return Err(io_refusal("Inspect fixture settings", error)),
    };
    if !metadata.is_file() || is_alias(&metadata) || metadata.permissions().readonly() {
        return Err(FixtureRegistryRefusal::UnsafeSettings);
    }
    if metadata.len() > MAX_SETTINGS_BYTES {
        return Err(FixtureRegistryRefusal::SettingsTooLarge);
    }
    let file = File::open(path).map_err(|error| io_refusal("Open fixture settings", error))?;
    let opened = file
        .metadata()
        .map_err(|error| io_refusal("Inspect opened fixture settings", error))?;
    if !opened.is_file() || opened.permissions().readonly() {
        return Err(FixtureRegistryRefusal::UnsafeSettings);
    }
    let mut bytes = Vec::new();
    file.take(MAX_SETTINGS_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|error| io_refusal("Read fixture settings", error))?;
    if bytes.len() as u64 > MAX_SETTINGS_BYTES {
        return Err(FixtureRegistryRefusal::SettingsTooLarge);
    }
    Ok(SettingsSnapshot {
        bytes: Some(bytes),
        permissions: Some(opened.permissions()),
    })
}

struct PendingSettings {
    path: PathBuf,
    remove_on_drop: bool,
}

impl PendingSettings {
    fn replace(mut self, target: &Path) -> io::Result<()> {
        fs::rename(&self.path, target)?;
        // The old name becomes available to other writers after rename. Once
        // committed, never unlink that name again during cleanup.
        self.remove_on_drop = false;
        Ok(())
    }
}

impl Drop for PendingSettings {
    fn drop(&mut self) {
        // Only this transaction's create-new staging file. Unknown staging
        // entries, peer locks and settings are never deleted or swept.
        if self.remove_on_drop {
            let _ = fs::remove_file(&self.path);
        }
    }
}

fn create_stage(root: &Path) -> RegistryResult<(PendingSettings, File)> {
    for _ in 0..100 {
        let path = root.join(format!(
            ".mcu-fixture-registry-{}-{}.tmp",
            std::process::id(),
            NEXT_STAGE.fetch_add(1, Ordering::Relaxed)
        ));
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(file) => {
                return Ok((
                    PendingSettings {
                        path,
                        remove_on_drop: true,
                    },
                    file,
                ))
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(io_refusal("Create fixture registry staging file", error)),
        }
    }
    Err(io_refusal(
        "Reserve fixture registry staging file",
        io::Error::new(
            io::ErrorKind::AlreadyExists,
            "Owned staging names exhausted",
        ),
    ))
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

fn io_refusal(operation: &'static str, error: io::Error) -> FixtureRegistryRefusal {
    FixtureRegistryRefusal::Io { operation, error }
}

#[cfg(test)]
mod tests {
    use super::*;
    use mcu_core::ownership::{ClaimChange, ProcessIdentity};
    use serde_json::json;

    static NEXT_FIXTURE: AtomicU64 = AtomicU64::new(0);

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
                    "ownership-registry-{}-{}",
                    std::process::id(),
                    NEXT_FIXTURE.fetch_add(1, Ordering::Relaxed)
                ));
                match fs::create_dir(&root) {
                    Ok(()) => return Self { app, root },
                    Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
                    Err(error) => panic!("Cannot reserve registry fixture: {error}"),
                }
            }
            panic!("Cannot reserve a unique registry fixture");
        }

        fn registry(&self) -> FixtureRegistry {
            FixtureRegistry::new(&self.app, &self.root).unwrap()
        }

        fn settings(&self) -> PathBuf {
            self.root.join(SETTINGS_NAME)
        }

        fn read(&self) -> Value {
            serde_json::from_slice(&fs::read(self.settings()).unwrap()).unwrap()
        }

        fn assert_released_and_clean(&self) {
            assert!(matches!(
                FixtureLockScope::new(&self.app, &self.root)
                    .unwrap()
                    .try_acquire(LOCK_NAME)
                    .unwrap(),
                FixtureLockAttempt::Acquired(_)
            ));
            assert!(!fs::read_dir(&self.root).unwrap().any(|entry| entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with(".mcu-fixture-registry-")));
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            fs::remove_dir_all(&self.root).unwrap();
        }
    }

    fn request<'a>(project: ClaimChange<'a>, port: ClaimChange<'a>) -> ClaimRequest<'a> {
        ClaimRequest {
            process: ProcessIdentity {
                pid: 10,
                create_time: 100.0,
            },
            project,
            port,
            hwnd: None,
        }
    }

    fn keys() -> BTreeMap<String, String> {
        BTreeMap::from([
            ("fixture-sketch".into(), "canonical-fixture-sketch".into()),
            ("other-sketch".into(), "canonical-other-sketch".into()),
        ])
    }

    #[test]
    fn missing_settings_create_then_unchanged_claim_preserves_exact_bytes_and_mtime() {
        let fixture = Fixture::new();
        let registry = fixture.registry();
        let claim = request(ClaimChange::Set("fixture-sketch"), ClaimChange::Set("COM5"));
        assert!(matches!(
            registry.claim(claim, &BTreeMap::new(), &keys()).unwrap(),
            FixtureRegistryOutcome::Updated { .. }
        ));
        let original = fs::read(fixture.settings()).unwrap();
        let modified = fs::metadata(fixture.settings())
            .unwrap()
            .modified()
            .unwrap();
        assert_eq!(
            registry.claim(claim, &BTreeMap::new(), &keys()).unwrap(),
            FixtureRegistryOutcome::Unchanged
        );
        assert_eq!(fs::read(fixture.settings()).unwrap(), original);
        assert_eq!(
            fs::metadata(fixture.settings())
                .unwrap()
                .modified()
                .unwrap(),
            modified
        );
        fixture.assert_released_and_clean();
    }

    #[test]
    fn freshly_read_unrelated_fields_and_peer_records_survive_registration_and_release() {
        let fixture = Fixture::new();
        let registry = fixture.registry();
        let initial = json!({"shared":{"theme":"solarized"},"unknown":[1,2],"instances":{
            "10":{"create_time":100.0,"selected_board":"custom-board","unknown":true},
            "20":{"create_time":200.0,"active_sketch_dir":"other-sketch","selected_port":"COM8","peer":true}
        }});
        fs::write(fixture.settings(), serde_json::to_vec(&initial).unwrap()).unwrap();
        let claim = request(ClaimChange::Set("fixture-sketch"), ClaimChange::Set("COM5"));
        let evidence = BTreeMap::from([("20".into(), ProcessEvidence::InventoryUnavailable)]);
        registry.claim(claim, &evidence, &keys()).unwrap();
        let claimed = fixture.read();
        assert_eq!(claimed["instances"]["20"], initial["instances"]["20"]);
        assert_eq!(claimed["instances"]["10"]["selected_board"], "custom-board");
        assert_eq!(claimed["unknown"], initial["unknown"]);
        let mut changed = claimed;
        changed["shared"]["theme"] = json!("light");
        changed["new-peer-field"] = json!({"preserve":true});
        fs::write(fixture.settings(), serde_json::to_vec(&changed).unwrap()).unwrap();
        assert!(matches!(
            registry
                .claim(
                    request(ClaimChange::Release, ClaimChange::Release),
                    &evidence,
                    &keys()
                )
                .unwrap(),
            FixtureRegistryOutcome::Updated { .. }
        ));
        let released = fixture.read();
        assert_eq!(released["shared"], changed["shared"]);
        assert_eq!(released["new-peer-field"], changed["new-peer-field"]);
        assert_eq!(released["instances"]["20"], changed["instances"]["20"]);
        assert_eq!(released["instances"]["10"]["active_sketch_dir"], "");
        assert_eq!(released["instances"]["10"]["selected_port"], "");
        assert_eq!(
            released["instances"]["10"]["selected_board"],
            "custom-board"
        );
        fixture.assert_released_and_clean();
    }

    #[test]
    fn contending_lock_is_busy_without_creating_settings_then_release_allows_claim() {
        let fixture = Fixture::new();
        let registry = fixture.registry();
        let scope = FixtureLockScope::new(&fixture.app, &fixture.root).unwrap();
        let FixtureLockAttempt::Acquired(lease) = scope.try_acquire(LOCK_NAME).unwrap() else {
            panic!("Fresh fixture lock was busy");
        };
        let claim = request(ClaimChange::Set("fixture-sketch"), ClaimChange::Keep);
        assert_eq!(
            registry.claim(claim, &BTreeMap::new(), &keys()).unwrap(),
            FixtureRegistryOutcome::Busy
        );
        assert!(!fixture.settings().exists());
        drop(lease);
        assert!(matches!(
            registry.claim(claim, &BTreeMap::new(), &keys()).unwrap(),
            FixtureRegistryOutcome::Updated { .. }
        ));
        fixture.assert_released_and_clean();
    }

    #[test]
    fn host_evidence_is_gathered_from_the_fresh_snapshot_under_the_os_lock() {
        let fixture = Fixture::new();
        let registry = fixture.registry();
        fs::write(fixture.settings(), b"{\"shared\":{\"revision\":2}}").unwrap();
        let scope = FixtureLockScope::new(&fixture.app, &fixture.root).unwrap();
        let outcome = registry
            .claim_with(
                request(ClaimChange::Set("fixture-sketch"), ClaimChange::Keep),
                |snapshot| {
                    assert_eq!(snapshot["shared"]["revision"], 2);
                    assert!(matches!(
                        scope.try_acquire(LOCK_NAME).unwrap(),
                        FixtureLockAttempt::Busy
                    ));
                    Ok(FixtureClaimEvidence {
                        processes: BTreeMap::new(),
                        project_keys: keys(),
                    })
                },
            )
            .unwrap();
        assert!(matches!(outcome, FixtureRegistryOutcome::Updated { .. }));
        assert_eq!(fixture.read()["shared"]["revision"], 2);
        let FixtureLockAttempt::Acquired(lease) = scope.try_acquire(LOCK_NAME).unwrap() else {
            panic!("Finished transaction retained the lock");
        };
        assert_eq!(
            registry
                .claim_with(request(ClaimChange::Keep, ClaimChange::Keep), |_| panic!(
                    "Busy transaction must not gather native evidence"
                ),)
                .unwrap(),
            FixtureRegistryOutcome::Busy
        );
        drop(lease);
        fs::write(fixture.settings(), b"invalid").unwrap();
        assert!(matches!(
            registry.claim_with(request(ClaimChange::Keep, ClaimChange::Keep), |_| panic!(
                "Malformed settings must not gather native evidence"
            ),),
            Err(FixtureRegistryRefusal::MalformedJson(_))
        ));
        fixture.assert_released_and_clean();
    }

    #[test]
    fn evidence_refusal_leaves_original_bytes_and_releases_the_lock() {
        let fixture = Fixture::new();
        let original = b"{ \"shared\": {\"theme\":\"glass\"} }\n";
        fs::write(fixture.settings(), original).unwrap();
        assert!(matches!(
            fixture.registry().claim_with(
                request(ClaimChange::Set("fixture-sketch"), ClaimChange::Keep),
                |_| Err(FixtureRegistryRefusal::Ownership(
                    OwnershipRefusal::InvalidCaller
                )),
            ),
            Err(FixtureRegistryRefusal::Ownership(
                OwnershipRefusal::InvalidCaller
            ))
        ));
        assert_eq!(fs::read(fixture.settings()).unwrap(), original);
        fixture.assert_released_and_clean();
    }

    #[test]
    fn occupied_uncertain_peer_is_a_collision_without_reformatting_or_replacement() {
        let fixture = Fixture::new();
        let original = b"{ \"shared\": {\"theme\":\"glass\"}, \"instances\": {\"20\": {\"create_time\":200.0, \"selected_port\":\"COM5 UART\"}} }\n";
        fs::write(fixture.settings(), original).unwrap();
        let outcome = fixture
            .registry()
            .claim(
                request(ClaimChange::Keep, ClaimChange::Set("COM5")),
                &BTreeMap::from([("20".into(), ProcessEvidence::InventoryUnavailable)]),
                &keys(),
            )
            .unwrap();
        assert!(
            matches!(outcome, FixtureRegistryOutcome::Collision(collisions) if collisions.len() == 1)
        );
        assert_eq!(fs::read(fixture.settings()).unwrap(), original);
        fixture.assert_released_and_clean();
    }

    #[test]
    fn failed_persistence_preserves_destination_and_removes_only_owned_staging() {
        let fixture = Fixture::new();
        let original = b"{\"shared\":{\"theme\":\"dark\"}}\n";
        fs::write(fixture.settings(), original).unwrap();
        let unknown = fixture.root.join("unrelated-staging.tmp");
        fs::write(&unknown, b"retain peer artifact").unwrap();
        let result = fixture.registry().claim_with_before_replace(
            request(ClaimChange::Set("fixture-sketch"), ClaimChange::Keep),
            &BTreeMap::new(),
            &keys(),
            || {
                Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "Injected persistence refusal",
                ))
            },
        );
        assert!(matches!(
            result,
            Err(FixtureRegistryRefusal::Io {
                operation: "Prepare fixture registry replacement",
                ..
            })
        ));
        assert_eq!(fs::read(fixture.settings()).unwrap(), original);
        assert_eq!(fs::read(unknown).unwrap(), b"retain peer artifact");
        fixture.assert_released_and_clean();
    }

    #[test]
    fn a_writer_ignoring_the_lock_is_detected_before_replacement() {
        let fixture = Fixture::new();
        fs::write(fixture.settings(), b"{}").unwrap();
        let external = b"{\"shared\":{\"peer\":true}}";
        let result = fixture.registry().claim_with_before_replace(
            request(ClaimChange::Set("fixture-sketch"), ClaimChange::Keep),
            &BTreeMap::new(),
            &keys(),
            || fs::write(fixture.settings(), external),
        );
        assert!(matches!(
            result,
            Err(FixtureRegistryRefusal::ExternalChange)
        ));
        assert_eq!(fs::read(fixture.settings()).unwrap(), external);
        fixture.assert_released_and_clean();
    }

    #[test]
    fn malformed_nonobject_oversized_and_readonly_settings_are_not_adopted() {
        let fixture = Fixture::new();
        let registry = fixture.registry();
        let claim = request(ClaimChange::Keep, ClaimChange::Keep);
        for (bytes, expected) in [(b"invalid".as_slice(), "json"), (b"[]".as_slice(), "shape")] {
            fs::write(fixture.settings(), bytes).unwrap();
            let refusal = registry
                .claim(claim, &BTreeMap::new(), &keys())
                .unwrap_err();
            assert!(matches!(
                (&refusal, expected),
                (FixtureRegistryRefusal::MalformedJson(_), "json")
                    | (FixtureRegistryRefusal::NonObjectSettings, "shape")
            ));
            assert_eq!(fs::read(fixture.settings()).unwrap(), bytes);
            fixture.assert_released_and_clean();
        }
        let oversized = vec![b' '; MAX_SETTINGS_BYTES as usize + 1];
        fs::write(fixture.settings(), &oversized).unwrap();
        assert!(matches!(
            registry.claim(claim, &BTreeMap::new(), &keys()),
            Err(FixtureRegistryRefusal::SettingsTooLarge)
        ));
        assert_eq!(fs::read(fixture.settings()).unwrap(), oversized);
        fs::write(fixture.settings(), b"{}").unwrap();
        let original_permissions = fs::metadata(fixture.settings()).unwrap().permissions();
        let mut readonly = original_permissions.clone();
        readonly.set_readonly(true);
        fs::set_permissions(fixture.settings(), readonly).unwrap();
        let result = registry.claim(claim, &BTreeMap::new(), &keys());
        fs::set_permissions(fixture.settings(), original_permissions).unwrap();
        assert!(matches!(
            result,
            Err(FixtureRegistryRefusal::UnsafeSettings)
        ));
        assert_eq!(fs::read(fixture.settings()).unwrap(), b"{}");
        fixture.assert_released_and_clean();
    }

    #[test]
    fn nonfixture_scope_and_nonfile_settings_are_refused() {
        let fixture = Fixture::new();
        assert!(FixtureRegistry::new(&fixture.app, &fixture.app.join("desktop")).is_err());
        assert!(FixtureRegistry::new(&fixture.app, &fixture.root.join("..")).is_err());
        fs::create_dir(fixture.settings()).unwrap();
        assert!(matches!(
            fixture.registry().claim(
                request(ClaimChange::Keep, ClaimChange::Keep),
                &BTreeMap::new(),
                &keys()
            ),
            Err(FixtureRegistryRefusal::UnsafeSettings)
        ));
        assert!(fixture.settings().is_dir());
        fixture.assert_released_and_clean();
    }

    #[cfg(unix)]
    #[test]
    fn symlink_settings_are_refused_without_touching_the_target() {
        use std::os::unix::fs::symlink;
        let fixture = Fixture::new();
        let target = fixture.root.join("peer.json");
        fs::write(&target, b"{}").unwrap();
        symlink(&target, fixture.settings()).unwrap();
        assert!(matches!(
            fixture.registry().claim(
                request(ClaimChange::Keep, ClaimChange::Keep),
                &BTreeMap::new(),
                &keys()
            ),
            Err(FixtureRegistryRefusal::UnsafeSettings)
        ));
        assert_eq!(fs::read(target).unwrap(), b"{}");
        fixture.assert_released_and_clean();
    }
}
