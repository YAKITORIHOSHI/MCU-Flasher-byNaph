//! Fixed metadata-only identity role for a Python-owned disposable project.
//! No source bytes, settings, tool stores or device handles are opened here.
use mcu_host::file_identity::{FixtureIdentityScope, FixtureSourceHandle};
use serde::Serialize;
use serde_json::{json, Value};
use std::io::{self, BufRead, Read, Write};
use std::path::Path;
use std::sync::mpsc;
use std::time::{Duration, Instant};

const DEADLINE: Duration = Duration::from_secs(10);
const MAX_COMMANDS: usize = 12;
const MAX_COMMAND_BYTES: u64 = 32;

fn report(value: &Value) -> io::Result<()> {
    let mut output = io::stdout().lock();
    writeln!(output, "{value}")?;
    output.flush()
}

fn observed<T: Serialize>(result: io::Result<T>) -> Value {
    match result {
        Ok(value) => json!(value),
        Err(error) => json!({"error":{
            "kind":format!("{:?}",error.kind()),
            "message":error.to_string().chars().take(256).collect::<String>()
        }}),
    }
}

fn snapshot(scope: &FixtureIdentityScope, source: &FixtureSourceHandle, ready: bool) -> Value {
    json!({
        "schemaVersion":1,"fixtureOnly":true,"ready":ready,
        "rootIdentity":observed(scope.root_identity()),
        "sourceIdentity":observed(source.identity()),
        "rootValid":observed(scope.verify_root().map(|()| true)),
        "sourceMatches":observed(scope.source_still_matches(source)),
        "currentIdentity":observed(scope.open_source("Main.ino").and_then(|file| file.identity()))
    })
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let arguments: Vec<_> = std::env::args_os().skip(1).collect();
    let [application_root, fixture_root] = arguments.as_slice() else {
        return Err("Expected explicit application and disposable fixture roots".into());
    };
    let scope = FixtureIdentityScope::new(Path::new(application_root), Path::new(fixture_root))?;
    let source = scope.open_source("Main.ino")?;
    let deadline = Instant::now() + DEADLINE;
    let (sender, receiver) = mpsc::sync_channel(1);
    // A silent input controller cannot retain these handles indefinitely while
    // output is consumed. The parent separately bounds process/pipe cleanup;
    // this receive deadline cannot interrupt a blocked stdout write.
    // Limit the bytes read before accepting a complete fixed command, rather
    // than reading an unbounded line and checking its size afterward.
    std::thread::spawn(move || {
        let input = io::stdin();
        let mut input = input.lock();
        for _ in 0..MAX_COMMANDS {
            let mut line = String::new();
            let result = input
                .by_ref()
                .take(MAX_COMMAND_BYTES + 1)
                .read_line(&mut line)
                .and_then(|_| {
                    if line.len() as u64 > MAX_COMMAND_BYTES || !line.ends_with('\n') {
                        return Err(io::Error::new(
                            io::ErrorKind::InvalidInput,
                            "Incomplete or oversized fixed fixture command",
                        ));
                    }
                    Ok(line)
                });
            let failed = result.is_err();
            if sender.send(result).is_err() || failed {
                break;
            }
        }
    });
    report(&snapshot(&scope, &source, true))?;
    for _ in 0..MAX_COMMANDS {
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .ok_or("Metadata fixture controller exceeded its deadline")?;
        let line = receiver
            .recv_timeout(remaining)
            .map_err(|error| match error {
                mpsc::RecvTimeoutError::Timeout => {
                    "Metadata fixture controller exceeded its deadline"
                }
                mpsc::RecvTimeoutError::Disconnected => "Metadata fixture command channel closed",
            })??;
        match line.as_str() {
            "inspect\n" | "inspect\r\n" => report(&snapshot(&scope, &source, false))?,
            "release\n" | "release\r\n" => {
                report(&json!({"schemaVersion":1,"fixtureOnly":true,"released":true}))?;
                return Ok(());
            }
            _ => return Err("Only fixed inspect/release fixture commands are allowed".into()),
        }
    }
    Err("Metadata fixture command budget exhausted".into())
}

fn main() {
    if let Err(error) = run() {
        let _ = report(&json!({
            "schemaVersion":1,"fixtureOnly":true,"passed":false,
            "error":error.to_string().chars().take(512).collect::<String>()
        }));
        std::process::exit(1);
    }
}
