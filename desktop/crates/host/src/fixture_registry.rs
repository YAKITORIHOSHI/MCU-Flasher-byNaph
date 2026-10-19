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
use serde::de::{Deserialize, IgnoredAny};
use serde_json::{Map, Value};
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
    UnexpectedOwnershipPlan,
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
            Self::UnexpectedOwnershipPlan => formatter
                .write_str("Ownership plan changed fields outside the caller's ownership record"),
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

type RawObject = BTreeMap<String, JsonFragment>;
const OWNERSHIP_FIELDS: [&str; 4] = ["create_time", "active_sketch_dir", "selected_port", "hwnd"];

/// A validated, single JSON value whose spelling is retained without numeric
/// conversion. This stays private instead of enabling serde_json's raw_value
/// feature, which changes how every Value consumer interprets marker objects.
#[derive(Clone)]
struct JsonFragment(String);

fn json_error(message: &str) -> FixtureRegistryRefusal {
    FixtureRegistryRefusal::MalformedJson(<serde_json::Error as serde::de::Error>::custom(message))
}

fn json_whitespace(byte: u8) -> bool {
    matches!(byte, b' ' | b'\n' | b'\r' | b'\t')
}

fn parsed_prefix<'a, T: Deserialize<'a>>(text: &'a str) -> RegistryResult<(T, usize)> {
    let mut stream = serde_json::Deserializer::from_str(text).into_iter::<T>();
    let value = stream
        .next()
        .ok_or_else(|| json_error("Expected a JSON value"))?
        .map_err(FixtureRegistryRefusal::MalformedJson)?;
    Ok((value, stream.byte_offset()))
}

impl JsonFragment {
    fn parse(bytes: &[u8]) -> RegistryResult<Self> {
        if bytes.len() as u64 > MAX_SETTINGS_BYTES {
            return Err(FixtureRegistryRefusal::SettingsTooLarge);
        }
        let text = std::str::from_utf8(bytes).map_err(|_| json_error("Invalid UTF-8 JSON"))?;
        // IgnoredAny validates JSON syntax without parsing integers/floats into
        // machine numbers or adopting serde_json's private marker objects.
        let (_, end) = parsed_prefix::<IgnoredAny>(text)?;
        if !text.as_bytes()[end..].iter().copied().all(json_whitespace) {
            return Err(json_error("Trailing characters after JSON value"));
        }
        let start = text
            .as_bytes()
            .iter()
            .copied()
            .take_while(|byte| json_whitespace(*byte))
            .count();
        Ok(Self(text[start..end].into()))
    }

    fn get(&self) -> &str {
        &self.0
    }
}

/// Keep untouched JSON as raw values. Python integers can exceed u64 and valid
/// user objects may contain serde_json's private Number/RawValue marker keys.
/// Neither may be passed through Value during a read/modify/write transaction.
struct SettingsProjection {
    root: RawObject,
    instances: RawObject,
    own: RawObject,
    policy: Value,
}

fn raw_object(value: &JsonFragment) -> RegistryResult<Option<RawObject>> {
    let text = value.get();
    if !text.starts_with('{') {
        return Ok(None);
    }
    let bytes = text.as_bytes();
    let mut position = 1;
    let mut fields = RawObject::new();
    loop {
        while bytes.get(position).copied().is_some_and(json_whitespace) {
            position += 1;
        }
        if bytes.get(position) == Some(&b'}') {
            break;
        }
        let (key, consumed) = parsed_prefix::<String>(&text[position..])?;
        position += consumed;
        while bytes.get(position).copied().is_some_and(json_whitespace) {
            position += 1;
        }
        if bytes.get(position) != Some(&b':') {
            return Err(json_error("Expected colon after JSON object key"));
        }
        position += 1;
        while bytes.get(position).copied().is_some_and(json_whitespace) {
            position += 1;
        }
        let (_, consumed) = parsed_prefix::<IgnoredAny>(&text[position..])?;
        // The parent fragment and this value boundary have both been validated.
        fields.insert(
            key,
            JsonFragment(text[position..position + consumed].into()),
        );
        position += consumed;
        while bytes.get(position).copied().is_some_and(json_whitespace) {
            position += 1;
        }
        match bytes.get(position) {
            Some(b',') => position += 1,
            Some(b'}') => break,
            _ => return Err(json_error("Expected JSON object delimiter")),
        }
    }
    Ok(Some(fields))
}

fn policy_field(field: &str, raw: &JsonFragment) -> Value {
    if raw.get().trim() == "null" {
        return Value::Null;
    }
    match field {
        "create_time" => serde_json::from_str::<f64>(raw.get())
            .ok()
            .and_then(serde_json::Number::from_f64)
            .map(Value::Number)
            // Invalid non-null identity must not look absent/stale. The core
            // rejects this non-number if that record needs identity evidence.
            .unwrap_or(Value::Bool(false)),
        "active_sketch_dir" | "selected_port" => serde_json::from_str::<String>(raw.get())
            .map(Value::String)
            .unwrap_or(Value::Bool(false)),
        // A repeated native handle must remain an unchanged claim. Invalid or
        // future handle shapes stay opaque unless this caller replaces them.
        "hwnd" => serde_json::from_str::<u64>(raw.get())
            .map(Value::from)
            .unwrap_or(Value::Null),
        _ => Value::Null,
    }
}

impl SettingsProjection {
    fn read(bytes: Option<&[u8]>, pid: &str) -> RegistryResult<Self> {
        let root = match bytes {
            Some(bytes) => {
                let raw = JsonFragment::parse(bytes)?;
                raw_object(&raw)?.ok_or(FixtureRegistryRefusal::NonObjectSettings)?
            }
            None => RawObject::new(),
        };
        let mut policy: Map<String, Value> =
            root.keys().map(|key| (key.clone(), Value::Null)).collect();
        let mut instances = RawObject::new();
        let mut own = RawObject::new();
        if let Some(raw) = root.get("instances") {
            if let Some(records) = raw_object(raw)? {
                let mut projected = Map::new();
                for (owner, record) in &records {
                    let value = if let Some(fields) = raw_object(record)? {
                        let projection: Map<String, Value> = fields
                            .iter()
                            .map(|(field, raw)| (field.clone(), policy_field(field, raw)))
                            .collect();
                        if owner == pid {
                            own = fields;
                        }
                        Value::Object(projection)
                    } else {
                        Value::Bool(false)
                    };
                    projected.insert(owner.clone(), value);
                }
                instances = records;
                policy.insert("instances".into(), Value::Object(projected));
            } else {
                // Even null instances are malformed under the legacy policy.
                policy.insert("instances".into(), Value::Bool(false));
            }
        }
        Ok(Self {
            root,
            instances,
            own,
            policy: Value::Object(policy),
        })
    }

    fn compose(mut self, next: &Value, request: ClaimRequest<'_>) -> RegistryResult<Vec<u8>> {
        let invalid = || FixtureRegistryRefusal::UnexpectedOwnershipPlan;
        let before = self.policy.as_object().ok_or_else(invalid)?;
        let after = next.as_object().ok_or_else(invalid)?;
        if !before
            .iter()
            .filter(|(key, _)| key.as_str() != "instances")
            .eq(after.iter().filter(|(key, _)| key.as_str() != "instances"))
        {
            return Err(invalid());
        }
        let empty = Map::new();
        let before_instances = before
            .get("instances")
            .and_then(Value::as_object)
            .unwrap_or(&empty);
        let after_instances = after
            .get("instances")
            .and_then(Value::as_object)
            .ok_or_else(invalid)?;
        let pid = request.process.pid.to_string();
        if !before_instances
            .iter()
            .filter(|(key, _)| **key != pid)
            .eq(after_instances.iter().filter(|(key, _)| **key != pid))
        {
            return Err(invalid());
        }
        let before_own = before_instances
            .get(&pid)
            .and_then(Value::as_object)
            .unwrap_or(&empty);
        let after_own = after_instances
            .get(&pid)
            .and_then(Value::as_object)
            .ok_or_else(invalid)?;
        // The core deliberately clears a proven reused PID's entire old block.
        let reused_pid = before_own
            .get("create_time")
            .and_then(Value::as_f64)
            .is_some_and(|time| (time - request.process.create_time).abs() >= 2.0);
        let mut own = if reused_pid {
            RawObject::new()
        } else {
            self.own.clone()
        };
        if !reused_pid
            && before_own
                .keys()
                .any(|field| !after_own.contains_key(field))
        {
            return Err(invalid());
        }
        for (field, value) in after_own {
            if !reused_pid && before_own.get(field) == Some(value) {
                if !own.contains_key(field) {
                    return Err(invalid());
                }
                continue;
            }
            if !OWNERSHIP_FIELDS.contains(&field.as_str()) {
                return Err(invalid());
            }
            own.insert(field.clone(), raw_from_serializable(value)?);
        }
        self.instances.insert(pid, raw_from_object(&own)?);
        self.root
            .insert("instances".into(), raw_from_object(&self.instances)?);
        let mut bytes = raw_from_object(&self.root)?.0.into_bytes();
        bytes.push(b'\n');
        if bytes.len() as u64 > MAX_SETTINGS_BYTES {
            return Err(FixtureRegistryRefusal::SettingsTooLarge);
        }
        Ok(bytes)
    }
}

fn raw_from_serializable(value: &impl serde::Serialize) -> RegistryResult<JsonFragment> {
    let text = serde_json::to_string(value).map_err(FixtureRegistryRefusal::MalformedJson)?;
    JsonFragment::parse(text.as_bytes())
}

fn raw_from_object(fields: &RawObject) -> RegistryResult<JsonFragment> {
    let mut text = String::from("{");
    for (index, (key, value)) in fields.iter().enumerate() {
        if index != 0 {
            text.push(',');
        }
        text.push_str(&serde_json::to_string(key).map_err(FixtureRegistryRefusal::MalformedJson)?);
        text.push(':');
        text.push_str(value.get());
        if text.len() as u64 >= MAX_SETTINGS_BYTES {
            return Err(FixtureRegistryRefusal::SettingsTooLarge);
        }
    }
    text.push('}');
    // Keys were serialized by serde_json and every value is an immutable,
    // validated fragment; composing those with fixed delimiters is valid JSON.
    Ok(JsonFragment(text))
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

    /// Obtain controlled host evidence from the ownership policy projection
    /// only after the fresh read and while the
    /// legacy OS lock remains held. Run this transaction on a worker: native
    /// process/path observations must never block the renderer/IPC thread.
    /// Unrelated fields are opaque nulls; only creation time, project/port and
    /// native window ownership are exposed. This does not authorize live access.
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
        let projection =
            SettingsProjection::read(snapshot.bytes.as_deref(), &request.process.pid.to_string())?;
        let evidence = gather(&projection.policy)?;
        let plan = plan_claim(
            &projection.policy,
            request,
            &evidence.processes,
            &evidence.project_keys,
        )
        .map_err(FixtureRegistryRefusal::Ownership)?;
        if !plan.granted() {
            return Ok(FixtureRegistryOutcome::Collision(plan.collisions));
        }
        if plan.config == projection.policy {
            return Ok(FixtureRegistryOutcome::Unchanged);
        }
        let bytes = projection.compose(&plan.config, request)?;
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

        fn read_raw(&self) -> RawObject {
            raw_object(&JsonFragment::parse(&fs::read(self.settings()).unwrap()).unwrap())
                .unwrap()
                .unwrap()
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
    fn opaque_object_boundaries_preserve_escaped_keys_literals_and_python_duplicate_keys() {
        let original = r#"
            {"caf\u00e9 \"key\"": ["comma, colon: braces{}", {"nested": "日本語"}],
             "duplicate": 0, "\u0064uplicate": 18446744073709551617,
             "extremeExponent": 1e999999999999999999999999999999,
             "marker": {"$serde_json::private::RawValue":"not JSON"}}	 "#;
        let fragment = JsonFragment::parse(original.as_bytes()).unwrap();
        let fields = raw_object(&fragment).unwrap().unwrap();
        assert_eq!(fields.len(), 4);
        assert_eq!(fields["duplicate"].get(), "18446744073709551617");
        assert_eq!(
            fields["extremeExponent"].get(),
            "1e999999999999999999999999999999"
        );
        assert_eq!(
            fields["café \"key\""].get(),
            r#"["comma, colon: braces{}", {"nested": "日本語"}]"#
        );
        let recomposed = raw_from_object(&fields).unwrap();
        let reparsed = raw_object(&recomposed).unwrap().unwrap();
        for (key, original) in &fields {
            assert_eq!(reparsed[key].get(), original.get());
        }
    }

    #[test]
    fn opaque_value_validation_rejects_malformed_and_trailing_json_before_scanning() {
        for invalid in [
            "",
            "{} {}",
            "{\"x\":[1,]}",
            "{\"x\":1,}",
            "{\"x\":01}",
            "{\"x\":1.e2}",
            "{\"x\":1e+}",
            "{\"x\":tru}",
            r#"{"x":"\u00xz"}"#,
            "{\"x\":\"unescaped\nline\"}",
        ] {
            assert!(
                JsonFragment::parse(invalid.as_bytes()).is_err(),
                "Accepted {invalid:?}"
            );
        }
        assert!(JsonFragment::parse(b"{\"x\":\"\xff\"}").is_err());
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
    fn repeated_native_window_handle_claim_does_not_rewrite_original_bytes() {
        let fixture = Fixture::new();
        let original = b"{ \"instances\": {\"10\": {\"create_time\":100.0,\"hwnd\":42}} }\n";
        fs::write(fixture.settings(), original).unwrap();
        let mut claim = request(ClaimChange::Keep, ClaimChange::Keep);
        claim.hwnd = Some(42);
        assert_eq!(
            fixture
                .registry()
                .claim(claim, &BTreeMap::new(), &keys())
                .unwrap(),
            FixtureRegistryOutcome::Unchanged
        );
        assert_eq!(fs::read(fixture.settings()).unwrap(), original);
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
    fn python_float_timestamps_survive_claim_and_release_without_rounding() {
        let fixture = Fixture::new();
        let registry = fixture.registry();
        // Python's json.dumps produces this decimal for a possible Windows
        // psutil timestamp. The default serde_json parser rounds it down by one
        // ULP; parsing the expected JSON with that same parser would hide loss.
        let initial = br#"{"shared":{"precise":1792340000.1227617},"instances":{
            "20":{"create_time":1792340000.1227617,"selected_port":"COM8"}
        }}"#;
        fs::write(fixture.settings(), initial).unwrap();
        let expected_bits = 1792340000.1227617_f64.to_bits();
        for (project, port) in [
            (ClaimChange::Set("fixture-sketch"), ClaimChange::Set("COM5")),
            (ClaimChange::Release, ClaimChange::Release),
        ] {
            assert!(matches!(
                registry
                    .claim(request(project, port), &BTreeMap::new(), &keys())
                    .unwrap(),
                FixtureRegistryOutcome::Updated { .. }
            ));
            let saved = fixture.read();
            for number in [
                &saved["shared"]["precise"],
                &saved["instances"]["20"]["create_time"],
            ] {
                assert_eq!(number.as_f64().unwrap().to_bits(), expected_bits);
            }
            let bytes = fs::read_to_string(fixture.settings()).unwrap();
            assert_eq!(bytes.matches("1792340000.1227617").count(), 2);
        }
        fixture.assert_released_and_clean();
    }

    #[test]
    fn unknown_json_objects_are_not_reinterpreted_as_number_markers() {
        let fixture = Fixture::new();
        let initial = br#"{"shared":{"$serde_json::private::Number":"123"}}"#;
        fs::write(fixture.settings(), initial).unwrap();
        assert!(matches!(
            fixture
                .registry()
                .claim(
                    request(ClaimChange::Set("fixture-sketch"), ClaimChange::Keep),
                    &BTreeMap::new(),
                    &keys()
                )
                .unwrap(),
            FixtureRegistryOutcome::Updated { .. }
        ));
        let saved = fixture.read_raw();
        let shared: BTreeMap<String, String> = serde_json::from_str(saved["shared"].get()).unwrap();
        assert_eq!(shared["$serde_json::private::Number"], "123");
        fixture.assert_released_and_clean();
    }

    #[test]
    fn unknown_numbers_and_marker_objects_remain_raw_through_claim_and_release() {
        let fixture = Fixture::new();
        let registry = fixture.registry();
        let opaque = r#"{"positive":18446744073709551617,"negative":-18446744073709551617,"precise":1792340000.1227617,"nested":[{"$serde_json::private::Number":"not a number"},{"$serde_json::private::RawValue":"not JSON"}]}"#;
        let peer = format!(
            r#"{{"create_time":1792340000.1227617,"selected_port":"COM8","future":{opaque}}}"#
        );
        let original = format!(
            r#"{{"shared":{opaque},"futureInteger":18446744073709551617,"instances":{{"10":{{"create_time":100.0,"future":{opaque},"futureInteger":-18446744073709551617}},"20":{peer}}}}}"#
        );
        fs::write(fixture.settings(), original).unwrap();
        for (project, port) in [
            (ClaimChange::Set("fixture-sketch"), ClaimChange::Set("COM5")),
            (ClaimChange::Release, ClaimChange::Release),
        ] {
            assert!(matches!(
                registry
                    .claim(request(project, port), &BTreeMap::new(), &keys())
                    .unwrap(),
                FixtureRegistryOutcome::Updated { .. }
            ));
            let root = fixture.read_raw();
            assert_eq!(root["shared"].get(), opaque);
            assert_eq!(root["futureInteger"].get(), "18446744073709551617");
            let instances = raw_object(&root["instances"]).unwrap().unwrap();
            assert_eq!(instances["20"].get(), peer);
            let own = raw_object(&instances["10"]).unwrap().unwrap();
            assert_eq!(own["future"].get(), opaque);
            assert_eq!(own["futureInteger"].get(), "-18446744073709551617");
            assert_eq!(own["create_time"].get(), "100.0");
        }
        fixture.assert_released_and_clean();
    }

    #[test]
    fn proven_reused_pid_discards_only_its_old_raw_record() {
        let fixture = Fixture::new();
        let peer = r#"{"future":18446744073709551617}"#;
        let original = format!(
            r#"{{"future":-18446744073709551617,"instances":{{"10":{{"create_time":1.0,"future":18446744073709551617}},"20":{peer}}}}}"#
        );
        fs::write(fixture.settings(), original).unwrap();
        fixture
            .registry()
            .claim(
                request(ClaimChange::Set("fixture-sketch"), ClaimChange::Keep),
                &BTreeMap::new(),
                &keys(),
            )
            .unwrap();
        let root = fixture.read_raw();
        assert_eq!(root["future"].get(), "-18446744073709551617");
        let instances = raw_object(&root["instances"]).unwrap().unwrap();
        assert_eq!(instances["20"].get(), peer);
        let own = raw_object(&instances["10"]).unwrap().unwrap();
        assert!(!own.contains_key("future"));
        assert_eq!(own["create_time"].get(), "100.0");
        fixture.assert_released_and_clean();
    }

    #[test]
    fn malformed_known_identity_fields_cannot_be_adopted_through_private_markers() {
        for creation_time in [
            r#"{"$serde_json::private::Number":"100.0"}"#,
            r#"{"$serde_json::private::RawValue":"100.0"}"#,
            "[]",
            "true",
            r#""100.0""#,
            "1e400",
        ] {
            let fixture = Fixture::new();
            let original = format!(r#"{{"instances":{{"10":{{"create_time":{creation_time}}}}}}}"#);
            fs::write(fixture.settings(), &original).unwrap();
            assert!(matches!(
                fixture.registry().claim(
                    request(ClaimChange::Set("fixture-sketch"), ClaimChange::Keep),
                    &BTreeMap::new(),
                    &keys()
                ),
                Err(FixtureRegistryRefusal::Ownership(
                    OwnershipRefusal::InvalidCreationTime
                ))
            ));
            assert_eq!(fs::read_to_string(fixture.settings()).unwrap(), original);
            fixture.assert_released_and_clean();
        }
    }

    #[test]
    fn releasing_ignores_and_preserves_malformed_unrelated_peer_identity() {
        let fixture = Fixture::new();
        let peer = r#"{"create_time":{"$serde_json::private::RawValue":"100.0"},"selected_port":"COM5","future":18446744073709551617}"#;
        let original = format!(r#"{{"instances":{{"20":{peer}}}}}"#);
        fs::write(fixture.settings(), original).unwrap();
        fixture
            .registry()
            .claim(
                request(ClaimChange::Release, ClaimChange::Release),
                &BTreeMap::new(),
                &keys(),
            )
            .unwrap();
        let root = fixture.read_raw();
        let instances = raw_object(&root["instances"]).unwrap().unwrap();
        assert_eq!(instances["20"].get(), peer);
        fixture.assert_released_and_clean();
    }

    #[test]
    fn raw_composer_rejects_unrelated_root_peer_and_own_field_changes() {
        let initial = br#"{"shared":{"future":18446744073709551617},"instances":{"10":{"create_time":100.0,"future":123},"20":{"future":true}}}"#;
        for alteration in 0..3 {
            let projection = SettingsProjection::read(Some(initial), "10").unwrap();
            let mut next = projection.policy.clone();
            next["instances"]["10"]["selected_port"] = json!("COM5");
            match alteration {
                0 => next["shared"] = json!("changed"),
                1 => next["instances"]["20"]["future"] = json!("changed"),
                _ => next["instances"]["10"]["future"] = json!("changed"),
            }
            assert!(matches!(
                projection.compose(&next, request(ClaimChange::Keep, ClaimChange::Set("COM5"))),
                Err(FixtureRegistryRefusal::UnexpectedOwnershipPlan)
            ));
        }
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
        fs::write(
            fixture.settings(),
            b"{\"shared\":{\"revision\":2},\"instances\":{\"20\":{\"create_time\":200.0}}}",
        )
        .unwrap();
        let scope = FixtureLockScope::new(&fixture.app, &fixture.root).unwrap();
        let outcome = registry
            .claim_with(
                request(ClaimChange::Set("fixture-sketch"), ClaimChange::Keep),
                |snapshot| {
                    assert_eq!(snapshot["instances"]["20"]["create_time"], 200.0);
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
