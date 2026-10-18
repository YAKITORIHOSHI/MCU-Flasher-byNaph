//! Fixed fixture registration roles. No live settings, shell, or serial access.
use mcu_core::ownership::{
    ClaimChange, ClaimRequest, DirectProcessEvidence, ProcessEvidence as PolicyEvidence,
    ProcessIdentity,
};
use mcu_host::fixture_registry::{
    FixtureClaimEvidence, FixtureRegistry, FixtureRegistryOutcome, FixtureRegistryRefusal,
};
use mcu_host::process_identity::{own_process_identity, query_process, ProcessEvidence};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::io::{self, BufRead, Read, Write};
use std::path::Path;
use std::sync::mpsc;
use std::time::Duration;

fn report(value: Value) -> io::Result<()> {
    let mut output = io::stdout().lock();
    writeln!(output, "{value}")?;
    output.flush()
}

fn gather(config: &Value, keys: &BTreeMap<String, String>) -> FixtureClaimEvidence {
    let processes = config["instances"]
        .as_object()
        .into_iter()
        .flatten()
        .filter_map(|(pid, _)| {
            let native_pid: u32 = pid.parse().ok()?;
            if native_pid == 0 || native_pid.to_string() != *pid {
                return None;
            }
            let observed = match query_process(native_pid) {
                ProcessEvidence::Running { identity } => PolicyEvidence::Present {
                    create_time: Some(identity.create_time),
                },
                ProcessEvidence::Exited { .. } => PolicyEvidence::MissingFromInventory {
                    direct: Some(DirectProcessEvidence::NotRunning),
                },
                // Every uncertain native reason reserves resources. In
                // particular, a zombie's Unknown(ZombieOrDead) is not Exited.
                ProcessEvidence::Unknown { .. } => PolicyEvidence::Present { create_time: None },
            };
            Some((pid.clone(), observed))
        })
        .collect();
    FixtureClaimEvidence {
        processes,
        project_keys: keys.clone(),
    }
}

fn value(outcome: &FixtureRegistryOutcome) -> Value {
    match outcome {
        FixtureRegistryOutcome::Busy => json!({"status":"busy"}),
        FixtureRegistryOutcome::Collision(collisions) => json!({
            "status":"collision",
            "owners":collisions.iter().map(|collision| collision.owner_pid.clone()).collect::<Vec<_>>()
        }),
        FixtureRegistryOutcome::Updated { .. } => json!({"status":"updated"}),
        FixtureRegistryOutcome::Unchanged => json!({"status":"unchanged"}),
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let arguments: Vec<_> = std::env::args_os().skip(1).collect();
    if arguments.len() != 4 {
        return Err("Expected application root, fixture root, a|b and claim|hold".into());
    }
    let project = arguments[2].to_str().ok_or("Invalid fixed project role")?;
    let role = arguments[3].to_str().ok_or("Invalid fixed claim role")?;
    if !matches!(project, "a" | "b") || !matches!(role, "claim" | "hold") {
        return Err("Only fixed project a|b and claim|hold roles are allowed".into());
    }
    let fixture = Path::new(&arguments[1]);
    let registry = FixtureRegistry::new(Path::new(&arguments[0]), fixture)?;
    let fixture = std::fs::canonicalize(fixture)?;
    let keys: BTreeMap<String, String> = ["project-a", "project-b"]
        .into_iter()
        .map(|leaf| -> Result<_, Box<dyn std::error::Error>> {
            let path = std::fs::canonicalize(fixture.join(leaf))?;
            if path.parent() != Some(fixture.as_path()) {
                return Err("Fixed fixture project cannot escape its parent".into());
            }
            let text = path
                .to_str()
                .ok_or("Fixture requires UTF-8 paths")?
                .to_string();
            Ok((text.clone(), text))
        })
        .collect::<Result<_, _>>()?;
    let path = std::fs::canonicalize(fixture.join(format!("project-{project}")))?;
    let project_path = path.to_str().ok_or("Fixture requires UTF-8 paths")?;
    let ProcessEvidence::Running { identity } = own_process_identity() else {
        return Err("Positive native caller identity is required".into());
    };
    let process = ProcessIdentity {
        pid: identity.pid,
        create_time: identity.create_time,
    };
    let request = ClaimRequest {
        process,
        project: ClaimChange::Set(project_path),
        port: ClaimChange::Set(if project == "a" {
            "FIXTURE-PORT-A"
        } else {
            "FIXTURE-PORT-B"
        }),
        hwnd: None,
    };
    let collect =
        |config: &Value| -> Result<_, FixtureRegistryRefusal> { Ok(gather(config, &keys)) };
    let outcome = registry.claim_with(request, collect)?;
    report(value(&outcome))?;
    if role == "claim"
        || !matches!(
            outcome,
            FixtureRegistryOutcome::Updated { .. } | FixtureRegistryOutcome::Unchanged
        )
    {
        return Ok(());
    }
    let (sender, receiver) = mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut line = String::new();
        let result = io::stdin()
            .lock()
            .take(33)
            .read_line(&mut line)
            .map(|_| line);
        let _ = sender.send(result);
    });
    let line = receiver.recv_timeout(Duration::from_secs(15))??;
    if line != "release\n" && line != "release\r\n" {
        return Err("Expected the fixed release token".into());
    }
    let release = ClaimRequest {
        project: ClaimChange::Release,
        port: ClaimChange::Release,
        ..request
    };
    let outcome = registry.claim_with(release, collect)?;
    if !matches!(
        outcome,
        FixtureRegistryOutcome::Updated { .. } | FixtureRegistryOutcome::Unchanged
    ) {
        return Err("Fixture registration release was not acknowledged".into());
    }
    report(json!({"status":"released"}))?;
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        let message: String = error.to_string().chars().take(512).collect();
        let _ = report(json!({"status":"error","message":message}));
        std::process::exit(1);
    }
}
