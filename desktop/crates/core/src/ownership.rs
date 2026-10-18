//! Pure ownership-policy fixtures for the legacy PID-keyed settings schema.
//!
//! Behavioral references: `main/core/config.py::_instance_is_alive` (305–333),
//! `port_occupied_owner` (840–861), `find_project_window` (940–978), and the
//! locked claim functions (888–900, 1105–1125). This module does not discover
//! processes, resolve paths, acquire locks, or read/write settings. The host must
//! supply native process evidence and resolved project comparison keys inside
//! the *same* future transaction. Renderer-supplied evidence is never sufficient.
//!
//! Unlike the old direct replacement API, a process with one active workspace
//! cannot silently claim a different workspace. Release is explicit. The legacy
//! schema has no per-window identifier; multiple workspaces in one Tauri process
//! remain unsupported. These fixture plans do not enable live registration.

use serde_json::{Map, Value};
use std::collections::BTreeMap;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ProcessIdentity {
    pub pid: u32,
    /// Seconds since the Unix epoch, matching psutil's `create_time()`.
    pub create_time: f64,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum DirectProcessEvidence {
    NotRunning,
    AccessDenied,
    Present { create_time: Option<f64> },
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum ProcessEvidence {
    InventoryUnavailable,
    Present {
        create_time: Option<f64>,
    },
    /// A cached inventory can miss a newly started process. Absence alone is
    /// insufficient: the original implementation performs a direct PID probe.
    MissingFromInventory {
        direct: Option<DirectProcessEvidence>,
    },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Liveness {
    Live,
    Stale,
    /// Reserve the resource: uncertainty is never permission to evict a peer.
    Uncertain,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum OwnershipRefusal {
    MalformedConfig,
    MalformedInstance { pid: String, field: &'static str },
    InvalidCreationTime,
    InvalidCaller,
    MissingDirectProbe,
    MissingProcessEvidence { pid: String },
    UnresolvedProject { path: String },
    InvalidPort,
    UnsupportedPortDigits,
    CallerIdentityUncertain,
    MultipleWorkspacesForProcess,
}

/// `Uncertain` matches the legacy conservative `True`; malformed numeric
/// fields and an omitted mandatory direct probe are explicit fixture refusals.
pub fn liveness(instance: &Value, evidence: ProcessEvidence) -> Result<Liveness, OwnershipRefusal> {
    let instance = instance
        .as_object()
        .ok_or(OwnershipRefusal::MalformedConfig)?;
    let observed = match evidence {
        ProcessEvidence::InventoryUnavailable => return Ok(Liveness::Uncertain),
        ProcessEvidence::Present { create_time } => create_time,
        ProcessEvidence::MissingFromInventory { direct } => match direct {
            Some(DirectProcessEvidence::NotRunning) => return Ok(Liveness::Stale),
            Some(DirectProcessEvidence::AccessDenied) => return Ok(Liveness::Uncertain),
            Some(DirectProcessEvidence::Present { create_time }) => create_time,
            None => return Err(OwnershipRefusal::MissingDirectProbe),
        },
    };
    let Some(observed) = observed else {
        return Ok(Liveness::Uncertain);
    };
    if !observed.is_finite() || observed < 0.0 {
        return Err(OwnershipRefusal::InvalidCreationTime);
    }
    let Some(stored) = creation_time(instance)? else {
        return Ok(Liveness::Stale);
    };
    // Deliberately strict `< 2`, including both boundary directions, as in the
    // Python reference. This detects PID reuse rather than treating PID as ID.
    Ok(if (observed - stored).abs() < 2.0 {
        Liveness::Live
    } else {
        Liveness::Stale
    })
}

fn creation_time(instance: &Map<String, Value>) -> Result<Option<f64>, OwnershipRefusal> {
    let Some(value) = instance.get("create_time").filter(|v| !v.is_null()) else {
        return Ok(None);
    };
    let value = value
        .as_f64()
        .filter(|v| v.is_finite() && *v >= 0.0)
        .ok_or(OwnershipRefusal::InvalidCreationTime)?;
    Ok(Some(value))
}

fn python_whitespace(character: char) -> bool {
    character.is_whitespace() || matches!(character, '\u{1c}'..='\u{1f}')
}

/// Match the original display-label normalization (case-sensitive `COM\d+`
/// prefix, `/dev/` token, otherwise first whitespace-delimited token, then
/// Unicode uppercasing). Empty/placeholder choices do not reserve a device.
/// Non-ASCII numeric COM suffixes need Python's Unicode `Nd` table, so this
/// small dependency-free port refuses that ambiguous boundary explicitly.
pub fn normalize_port(label: &str) -> Result<Option<String>, OwnershipRefusal> {
    if label.is_empty() || label.starts_with('─') {
        return Ok(None);
    }
    let token = label
        .split(python_whitespace)
        .find(|part| !part.is_empty())
        .ok_or(OwnershipRefusal::InvalidPort)?;
    if let Some(tail) = label.strip_prefix("COM") {
        let digits = tail.chars().take_while(char::is_ascii_digit).count();
        if tail
            .chars()
            .nth(digits)
            .is_some_and(|character| !character.is_ascii() && character.is_numeric())
        {
            return Err(OwnershipRefusal::UnsupportedPortDigits);
        }
        if digits > 0 {
            return Ok(Some(label[..3 + digits].to_uppercase()));
        }
    }
    Ok(Some(token.to_uppercase()))
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ClaimChange<'a> {
    Keep,
    Set(&'a str),
    Release,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ClaimRequest<'a> {
    pub process: ProcessIdentity,
    pub project: ClaimChange<'a>,
    pub port: ClaimChange<'a>,
    pub hwnd: Option<u64>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Resource {
    Project,
    Port,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Collision {
    pub resource: Resource,
    pub owner_pid: String,
    pub liveness: Liveness,
}

#[derive(Clone, Debug, PartialEq)]
pub struct ClaimPlan {
    /// A blocked plan contains the exact original JSON value. A successful plan
    /// changes only this PID's ownership fields; unrelated records/keys survive.
    pub config: Value,
    pub collisions: Vec<Collision>,
}

impl ClaimPlan {
    pub fn granted(&self) -> bool {
        self.collisions.is_empty()
    }
}

fn optional_string<'a>(
    instance: &'a Map<String, Value>,
    pid: &str,
    field: &'static str,
) -> Result<Option<&'a str>, OwnershipRefusal> {
    match instance.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) => Ok((!value.is_empty()).then_some(value)),
        Some(_) => Err(OwnershipRefusal::MalformedInstance {
            pid: pid.into(),
            field,
        }),
    }
}

fn project_key<'a>(
    path: &str,
    keys: &'a BTreeMap<String, String>,
) -> Result<&'a str, OwnershipRefusal> {
    keys.get(path)
        .map(String::as_str)
        .filter(|key| !key.is_empty())
        .ok_or_else(|| OwnershipRefusal::UnresolvedProject { path: path.into() })
}

fn changed_value<'a>(change: ClaimChange<'a>, previous: Option<&'a str>) -> Option<&'a str> {
    match change {
        ClaimChange::Keep => previous,
        ClaimChange::Set(value) => (!value.is_empty()).then_some(value),
        ClaimChange::Release => None,
    }
}

/// Plan one fixture transaction against a supplied snapshot. Project keys must
/// come from native canonical resolution (file→parent, Windows case-insensitive
/// comparison), not from lexical normalization in this pure module. A future
/// host must hold the legacy store lock while obtaining/revalidating this
/// snapshot, process evidence, identities and applying an approved plan.
///
/// No stale-peer pruning occurs: the reference's `> 5` cleanup is a separate
/// save policy. This planner also does not import preferences or reset boards.
pub fn plan_claim(
    config: &Value,
    request: ClaimRequest<'_>,
    evidence: &BTreeMap<String, ProcessEvidence>,
    project_keys: &BTreeMap<String, String>,
) -> Result<ClaimPlan, OwnershipRefusal> {
    if request.process.pid == 0
        || !request.process.create_time.is_finite()
        || request.process.create_time <= 0.0
    {
        return Err(OwnershipRefusal::InvalidCaller);
    }
    let config_object = config
        .as_object()
        .ok_or(OwnershipRefusal::MalformedConfig)?;
    let empty = Map::new();
    let instances = match config_object.get("instances") {
        None => &empty,
        Some(Value::Object(instances)) => instances,
        Some(_) => return Err(OwnershipRefusal::MalformedConfig),
    };
    let pid = request.process.pid.to_string();
    let mut own = match instances.get(&pid) {
        None => Map::new(),
        Some(Value::Object(instance)) => instance.clone(),
        Some(_) => {
            return Err(OwnershipRefusal::MalformedInstance {
                pid,
                field: "instance",
            })
        }
    };
    if let Some(stored) = creation_time(&own)? {
        if (request.process.create_time - stored).abs() >= 2.0 {
            // `load_gui_config` discards a proven reused-PID record as well.
            // Keep/Release below apply to this fresh process record; they must
            // never retain the previous lifetime's project, port or settings.
            own.clear();
        }
    } else if optional_string(&own, &pid, "active_sketch_dir")?.is_some()
        || optional_string(&own, &pid, "selected_port")?.is_some()
    {
        // Legacy backfills this identity. It is unsafe to inherit ownership
        // when the old record cannot be tied to this native process lifetime.
        return Err(OwnershipRefusal::CallerIdentityUncertain);
    }
    let old_project = optional_string(&own, &pid, "active_sketch_dir")?;
    let old_port = optional_string(&own, &pid, "selected_port")?;
    let project = changed_value(request.project, old_project);
    let port = changed_value(request.port, old_port);
    let desired_project = project
        .map(|path| project_key(path, project_keys))
        .transpose()?;
    let desired_port = port.map(normalize_port).transpose()?.flatten();
    if let (Some(old), Some(new)) = (old_project, desired_project) {
        if matches!(request.project, ClaimChange::Set(_)) && project_key(old, project_keys)? != new
        {
            return Err(OwnershipRefusal::MultipleWorkspacesForProcess);
        }
    }
    let mut collisions = Vec::new();
    // Releasing all resources cannot collide. Preserve even malformed peer
    // records verbatim instead of making unrelated shape errors prevent release.
    if desired_project.is_some() || desired_port.is_some() {
        for (owner_pid, value) in instances {
            if *owner_pid == pid {
                continue;
            }
            if owner_pid
                .parse::<u32>()
                .ok()
                .filter(|value| *value != 0)
                .is_none_or(|value| value.to_string() != *owner_pid)
            {
                return Err(OwnershipRefusal::MalformedInstance {
                    pid: owner_pid.clone(),
                    field: "pid",
                });
            }
            let instance =
                value
                    .as_object()
                    .ok_or_else(|| OwnershipRefusal::MalformedInstance {
                        pid: owner_pid.clone(),
                        field: "instance",
                    })?;
            let peer_project = optional_string(instance, owner_pid, "active_sketch_dir")?;
            let peer_port = optional_string(instance, owner_pid, "selected_port")?;
            let port_collision = match (&desired_port, peer_port) {
                (Some(desired), Some(label)) => normalize_port(label)?.as_ref() == Some(desired),
                _ => false,
            };
            let possible_project_collision = desired_project.is_some() && peer_project.is_some();
            if !possible_project_collision && !port_collision {
                continue;
            }
            let observed = evidence.get(owner_pid).copied().ok_or_else(|| {
                OwnershipRefusal::MissingProcessEvidence {
                    pid: owner_pid.clone(),
                }
            })?;
            let state = liveness(value, observed)?;
            if state == Liveness::Stale {
                continue;
            }
            // The original liveness check precedes path resolution. Do not demand
            // a resolvable path from a peer whose recorded process is provably dead.
            let project_collision = match (desired_project, peer_project) {
                (Some(desired), Some(path)) => project_key(path, project_keys)? == desired,
                _ => false,
            };
            for (collides, resource) in [
                (project_collision, Resource::Project),
                (port_collision, Resource::Port),
            ] {
                if collides {
                    collisions.push(Collision {
                        resource,
                        owner_pid: owner_pid.clone(),
                        liveness: state,
                    });
                }
            }
        }
    }
    if !collisions.is_empty() {
        return Ok(ClaimPlan {
            config: config.clone(),
            collisions,
        });
    }
    for (field, change) in [
        ("active_sketch_dir", request.project),
        ("selected_port", request.port),
    ] {
        match change {
            ClaimChange::Keep => (),
            ClaimChange::Set(value) => {
                own.insert(field.into(), Value::String(value.into()));
            }
            ClaimChange::Release => {
                own.insert(field.into(), Value::String(String::new()));
            }
        }
    }
    // Match legacy registration: an existing same-lifetime timestamp survives;
    // a fresh/backfilled PID block records the positive native creation time.
    if creation_time(&own)?.is_none() {
        own.insert(
            "create_time".into(),
            Value::from(request.process.create_time),
        );
    }
    if let Some(hwnd) = request.hwnd.filter(|value| *value != 0) {
        own.insert("hwnd".into(), Value::from(hwnd));
    }
    let mut next = config_object.clone();
    let mut next_instances = instances.clone();
    next_instances.insert(pid, Value::Object(own));
    next.insert("instances".into(), Value::Object(next_instances));
    Ok(ClaimPlan {
        config: Value::Object(next),
        collisions,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

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
            ("/fixture/sketch".into(), "canonical-sketch".into()),
            ("/fixture/alias/main.ino".into(), "canonical-sketch".into()),
            ("C:\\SKETCH".into(), "canonical-sketch".into()),
            ("/fixture/other".into(), "other-sketch".into()),
        ])
    }

    #[test]
    fn pid_reuse_boundary_and_uncertainty_match_the_reference() {
        let instance = json!({"create_time":100.0});
        for (observed, expected) in [
            (98.0, Liveness::Stale),
            (98.001, Liveness::Live),
            (100.0, Liveness::Live),
            (101.999, Liveness::Live),
            (102.0, Liveness::Stale),
        ] {
            assert_eq!(
                liveness(
                    &instance,
                    ProcessEvidence::Present {
                        create_time: Some(observed)
                    }
                ),
                Ok(expected)
            );
        }
        assert_eq!(
            liveness(&instance, ProcessEvidence::InventoryUnavailable),
            Ok(Liveness::Uncertain)
        );
        assert_eq!(
            liveness(
                &json!({}),
                ProcessEvidence::Present {
                    create_time: Some(100.0)
                }
            ),
            Ok(Liveness::Stale)
        );
        assert_eq!(
            liveness(&json!({}), ProcessEvidence::Present { create_time: None }),
            Ok(Liveness::Uncertain)
        );
    }

    #[test]
    fn cached_inventory_miss_requires_direct_process_evidence() {
        let instance = json!({"create_time":100.0});
        for (direct, expected) in [
            (DirectProcessEvidence::NotRunning, Liveness::Stale),
            (DirectProcessEvidence::AccessDenied, Liveness::Uncertain),
            (
                DirectProcessEvidence::Present { create_time: None },
                Liveness::Uncertain,
            ),
            (
                DirectProcessEvidence::Present {
                    create_time: Some(100.0),
                },
                Liveness::Live,
            ),
        ] {
            assert_eq!(
                liveness(
                    &instance,
                    ProcessEvidence::MissingFromInventory {
                        direct: Some(direct)
                    }
                ),
                Ok(expected)
            );
        }
        assert_eq!(
            liveness(
                &instance,
                ProcessEvidence::MissingFromInventory { direct: None }
            ),
            Err(OwnershipRefusal::MissingDirectProbe)
        );
    }

    #[test]
    fn port_tokens_keep_legacy_case_prefix_and_whitespace_rules() {
        for (label, expected) in [
            ("COM12 (USB)", Some("COM12")),
            ("COM3suffix", Some("COM3")),
            ("com3suffix", Some("COM3SUFFIX")),
            ("/dev/ttyUSB0 - UART", Some("/DEV/TTYUSB0")),
            ("  COM4 label", Some("COM4")),
            ("tty\u{1c}label", Some("TTY")),
            ("", None),
            ("─ Select port ─", None),
        ] {
            assert_eq!(normalize_port(label).unwrap().as_deref(), expected);
        }
        assert_eq!(normalize_port(" \t"), Err(OwnershipRefusal::InvalidPort));
        assert_eq!(
            normalize_port("COM١suffix"),
            Err(OwnershipRefusal::UnsupportedPortDigits)
        );
    }

    #[test]
    fn occupied_claim_is_atomic_and_unknown_process_keeps_ownership() {
        let original = json!({"shared":{"theme":"dark"},"instances":{
            "20":{"create_time":200.0,"active_sketch_dir":"/fixture/alias/main.ino","selected_port":"COM5 UART","unknown":true}
        }});
        let evidence = BTreeMap::from([("20".into(), ProcessEvidence::InventoryUnavailable)]);
        let plan = plan_claim(
            &original,
            request(
                ClaimChange::Set("/fixture/sketch"),
                ClaimChange::Set("COM5"),
            ),
            &evidence,
            &keys(),
        )
        .unwrap();
        assert!(!plan.granted());
        assert_eq!(plan.config, original);
        assert_eq!(plan.collisions.len(), 2);
        assert!(plan
            .collisions
            .iter()
            .all(|collision| collision.liveness == Liveness::Uncertain));
    }

    #[test]
    fn known_stale_peer_is_retained_and_unrelated_settings_survive() {
        let original = json!({"shared":{"theme":"dark"},"unknown":[1,2],"instances":{
            "20":{"create_time":200.0,"active_sketch_dir":"/fixture/sketch","selected_port":"COM5","unknown":true}
        }});
        let evidence = BTreeMap::from([(
            "20".into(),
            ProcessEvidence::Present {
                create_time: Some(300.0),
            },
        )]);
        let plan = plan_claim(
            &original,
            request(
                ClaimChange::Set("/fixture/sketch"),
                ClaimChange::Set("COM5"),
            ),
            &evidence,
            &keys(),
        )
        .unwrap();
        assert!(plan.granted());
        assert_eq!(plan.config["instances"]["20"], original["instances"]["20"]);
        assert_eq!(plan.config["shared"], original["shared"]);
        assert_eq!(plan.config["unknown"], original["unknown"]);
        assert_eq!(plan.config["instances"]["10"]["create_time"], 100.0);
        assert!(original["instances"].get("10").is_none());
    }

    #[test]
    fn reused_self_pid_replaces_only_that_record() {
        let original = json!({"instances":{"10":{"create_time":1.0,"active_sketch_dir":"unresolvable-old-path","selected_board":"old","unknown":"old"},"20":{"custom":true}}});
        let plan = plan_claim(
            &original,
            request(ClaimChange::Set("/fixture/sketch"), ClaimChange::Release),
            &BTreeMap::new(),
            &keys(),
        )
        .unwrap();
        assert!(plan.granted());
        assert_eq!(plan.config["instances"]["20"], original["instances"]["20"]);
        assert!(plan.config["instances"]["10"].get("unknown").is_none());
        assert!(plan.config["instances"]["10"]
            .get("selected_board")
            .is_none());
        assert_eq!(plan.config["instances"]["10"]["selected_port"], "");
    }

    #[test]
    fn active_self_record_cannot_be_adopted_without_lifetime_identity() {
        let original = json!({"instances":{"10":{"active_sketch_dir":"/fixture/sketch"}}});
        assert_eq!(
            plan_claim(
                &original,
                request(ClaimChange::Keep, ClaimChange::Release),
                &BTreeMap::new(),
                &keys()
            ),
            Err(OwnershipRefusal::CallerIdentityUncertain)
        );
        let no_resources = json!({"instances":{"10":{"custom":true}}});
        let plan = plan_claim(
            &no_resources,
            request(ClaimChange::Keep, ClaimChange::Keep),
            &BTreeMap::new(),
            &keys(),
        )
        .unwrap();
        assert_eq!(plan.config["instances"]["10"]["create_time"], 100.0);
        assert_eq!(plan.config["instances"]["10"]["custom"], true);
        let null_time = json!({"instances":{"10":{"create_time":null,"custom":true}}});
        let plan = plan_claim(
            &null_time,
            request(ClaimChange::Set("/fixture/sketch"), ClaimChange::Keep),
            &BTreeMap::new(),
            &keys(),
        )
        .unwrap();
        assert_eq!(plan.config["instances"]["10"]["create_time"], 100.0);
        assert_eq!(plan.config["instances"]["10"]["custom"], true);
        assert_eq!(
            liveness(
                &plan.config["instances"]["10"],
                ProcessEvidence::Present {
                    create_time: Some(100.0)
                }
            ),
            Ok(Liveness::Live)
        );
        let owned_null = json!({"instances":{"10":{"create_time":null,"selected_port":"COM3"}}});
        assert_eq!(
            plan_claim(
                &owned_null,
                request(ClaimChange::Release, ClaimChange::Release),
                &BTreeMap::new(),
                &keys()
            ),
            Err(OwnershipRefusal::CallerIdentityUncertain)
        );
    }

    #[test]
    fn stale_unresolvable_peers_and_unrelated_release_do_not_block() {
        let original = json!({"instances":{"20":{"create_time":200.0,"active_sketch_dir":"missing-old-volume"}}});
        let evidence = BTreeMap::from([(
            "20".into(),
            ProcessEvidence::MissingFromInventory {
                direct: Some(DirectProcessEvidence::NotRunning),
            },
        )]);
        let plan = plan_claim(
            &original,
            request(ClaimChange::Set("/fixture/sketch"), ClaimChange::Keep),
            &evidence,
            &keys(),
        )
        .unwrap();
        assert!(plan.granted());
        assert_eq!(plan.config["instances"]["20"], original["instances"]["20"]);
        let releasing = json!({"instances":{"10":{"create_time":100.0,"active_sketch_dir":"missing-old-volume","selected_port":"COM4"},"malformed-peer":false}});
        let plan = plan_claim(
            &releasing,
            request(ClaimChange::Release, ClaimChange::Release),
            &BTreeMap::new(),
            &BTreeMap::new(),
        )
        .unwrap();
        assert!(plan.granted());
        assert_eq!(plan.config["instances"]["10"]["active_sketch_dir"], "");
        assert_eq!(plan.config["instances"]["10"]["selected_port"], "");
        assert_eq!(plan.config["instances"]["malformed-peer"], false);
    }

    #[test]
    fn stale_self_keep_does_not_inherit_resources_and_noop_is_unchanged() {
        let original = json!({"instances":{"10":{"create_time":1.0,"active_sketch_dir":"missing-old-volume","selected_port":"COM4"}}});
        let plan = plan_claim(
            &original,
            request(ClaimChange::Keep, ClaimChange::Keep),
            &BTreeMap::new(),
            &BTreeMap::new(),
        )
        .unwrap();
        assert_eq!(plan.config["instances"]["10"], json!({"create_time":100.0}));
        let again = plan_claim(
            &plan.config,
            request(ClaimChange::Keep, ClaimChange::Keep),
            &BTreeMap::new(),
            &BTreeMap::new(),
        )
        .unwrap();
        assert_eq!(again.config, plan.config);
    }

    #[test]
    fn one_workspace_per_process_requires_explicit_release() {
        let original = json!({"instances":{"10":{"create_time":100.0,"active_sketch_dir":"/fixture/sketch","dirty_preserved":true}}});
        assert_eq!(
            plan_claim(
                &original,
                request(ClaimChange::Set("/fixture/other"), ClaimChange::Keep),
                &BTreeMap::new(),
                &keys()
            ),
            Err(OwnershipRefusal::MultipleWorkspacesForProcess)
        );
        let release = plan_claim(
            &original,
            request(ClaimChange::Release, ClaimChange::Keep),
            &BTreeMap::new(),
            &keys(),
        )
        .unwrap();
        let next = plan_claim(
            &release.config,
            request(ClaimChange::Set("/fixture/other"), ClaimChange::Keep),
            &BTreeMap::new(),
            &keys(),
        )
        .unwrap();
        assert_eq!(
            next.config["instances"]["10"]["active_sketch_dir"],
            "/fixture/other"
        );
        assert_eq!(next.config["instances"]["10"]["dirty_preserved"], true);
        assert!(plan_claim(
            &original,
            request(ClaimChange::Set("C:\\SKETCH"), ClaimChange::Keep),
            &BTreeMap::new(),
            &keys()
        )
        .unwrap()
        .granted());
    }

    #[test]
    fn malformed_or_unresolved_inputs_never_change_the_snapshot() {
        for original in [
            json!([]),
            json!({"instances":[]}),
            json!({"instances":{"20":false}}),
        ] {
            assert!(plan_claim(
                &original,
                request(ClaimChange::Set("/fixture/sketch"), ClaimChange::Keep),
                &BTreeMap::new(),
                &keys()
            )
            .is_err());
        }
        let original =
            json!({"instances":{"20":{"create_time":200,"active_sketch_dir":"/fixture/sketch"}}});
        assert_eq!(
            plan_claim(
                &original,
                request(ClaimChange::Set("/fixture/sketch"), ClaimChange::Keep),
                &BTreeMap::new(),
                &keys()
            ),
            Err(OwnershipRefusal::MissingProcessEvidence { pid: "20".into() })
        );
        assert_eq!(
            plan_claim(
                &json!({}),
                request(ClaimChange::Set("unresolved"), ClaimChange::Keep),
                &BTreeMap::new(),
                &keys()
            ),
            Err(OwnershipRefusal::UnresolvedProject {
                path: "unresolved".into()
            })
        );
        assert_eq!(
            liveness(
                &json!({"create_time":"100"}),
                ProcessEvidence::Present {
                    create_time: Some(100.0)
                }
            ),
            Err(OwnershipRefusal::InvalidCreationTime)
        );
        let mut invalid = request(ClaimChange::Keep, ClaimChange::Keep);
        invalid.process.create_time = f64::NAN;
        assert_eq!(
            plan_claim(&json!({}), invalid, &BTreeMap::new(), &keys()),
            Err(OwnershipRefusal::InvalidCaller)
        );
    }
}
