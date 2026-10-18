//! Controlled lock fixture only. No live settings or arbitrary process role.
use mcu_host::fixture_lock::{FixtureLockAttempt, FixtureLockScope};
use std::io::{self, BufRead, Read, Write};
use std::path::Path;
use std::sync::mpsc;
use std::time::Duration;

fn report(value: serde_json::Value) -> io::Result<()> {
    let mut output = io::stdout().lock();
    writeln!(output, "{value}")?;
    output.flush()
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let arguments: Vec<_> = std::env::args_os().skip(1).collect();
    if arguments.len() != 3 {
        return Err("Expected application root, fixture root and try|hold role".into());
    }
    let role = arguments[2].to_str().ok_or("Invalid fixture role")?;
    if !matches!(role, "try" | "hold") {
        return Err("Only try and hold fixture roles are allowed".into());
    }
    let scope = FixtureLockScope::new(Path::new(&arguments[0]), Path::new(&arguments[1]))?;
    let FixtureLockAttempt::Acquired(lease) = scope.try_acquire("settings.json.lock")? else {
        return Ok(report(serde_json::json!({"status":"busy"}))?);
    };
    if !matches!(
        scope.try_acquire("settings.json.lock")?,
        FixtureLockAttempt::Busy
    ) {
        return Err("An independent handle bypassed the fixture lock".into());
    }
    if role == "try" {
        report(serde_json::json!({"status":"acquired","independentBusy":true}))?;
    } else {
        report(serde_json::json!({"status":"ready","independentBusy":true}))?;
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
        drop(lease);
        report(serde_json::json!({"status":"released"}))?;
    }
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        let message: String = error.to_string().chars().take(512).collect();
        let _ = report(serde_json::json!({"status":"error","message":message}));
        std::process::exit(1);
    }
}
