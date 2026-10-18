//! Fixed self/child process identity probe; no arbitrary executable or PID input.
use mcu_host::process_identity::{own_process_identity, query_process, ProcessEvidence};
use serde_json::{json, Value};
use std::io::{self, BufRead, Read, Write};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant};

const HOLD_TIMEOUT: Duration = Duration::from_secs(5);
const CHILD_TIMEOUT: Duration = Duration::from_secs(3);
const MAX_LINE: u64 = 4096;

fn report(value: &Value) -> io::Result<()> {
    let mut output = io::stdout().lock();
    writeln!(output, "{value}")?;
    output.flush()
}

fn hold() -> Result<(), Box<dyn std::error::Error>> {
    report(&json!({"schemaVersion":1,"native":true,"identity":own_process_identity()}))?;
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
    let line = receiver.recv_timeout(HOLD_TIMEOUT)??;
    if line != "release\n" && line != "release\r\n" {
        return Err("Expected the fixed release token".into());
    }
    Ok(())
}

struct OwnedFixtureChild(Child);

impl OwnedFixtureChild {
    fn await_exit(&mut self) -> Result<(), Box<dyn std::error::Error>> {
        let deadline = Instant::now() + CHILD_TIMEOUT;
        loop {
            if let Some(status) = self.0.try_wait()? {
                return if status.success() {
                    Ok(())
                } else {
                    Err("Controlled child failed".into())
                };
            }
            if Instant::now() >= deadline {
                return Err("Controlled child exceeded its exit deadline".into());
            }
            std::thread::sleep(Duration::from_millis(5));
        }
    }
}

impl Drop for OwnedFixtureChild {
    fn drop(&mut self) {
        if !matches!(self.0.try_wait(), Ok(Some(_))) {
            // Only the exact Child handle this probe spawned can be killed.
            // The identity query API never terminates or signals a process.
            let _ = self.0.kill();
            let deadline = Instant::now() + Duration::from_millis(250);
            while Instant::now() < deadline {
                if !matches!(self.0.try_wait(), Ok(None)) {
                    break;
                }
                std::thread::sleep(Duration::from_millis(5));
            }
        }
    }
}

fn lifecycle() -> Result<Value, Box<dyn std::error::Error>> {
    let own = own_process_identity();
    let ProcessEvidence::Running {
        identity: own_identity,
    } = own
    else {
        return Err("Native self identity unavailable".into());
    };
    let mut child = OwnedFixtureChild(
        Command::new(std::env::current_exe()?)
            .arg("--hold")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()?,
    );
    let pid = child.0.id();
    let stdout = child
        .0
        .stdout
        .take()
        .ok_or("Missing controlled child stdout")?;
    let (sender, receiver) = mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut line = String::new();
        let result = io::BufReader::new(stdout)
            .take(MAX_LINE + 1)
            .read_line(&mut line)
            .map(|_| line);
        let _ = sender.send(result);
    });
    let ready = receiver.recv_timeout(CHILD_TIMEOUT)??;
    if ready.len() as u64 > MAX_LINE || !ready.ends_with('\n') {
        return Err("Controlled child ready frame exceeded its bound".into());
    }
    let ready: Value = serde_json::from_str(&ready)?;
    let child_reported = &ready["identity"];
    let observed = query_process(pid);
    let ProcessEvidence::Running {
        identity: observed_identity,
    } = observed
    else {
        return Err("Controlled child identity unavailable while running".into());
    };
    let reported_pid = child_reported["identity"]["pid"].as_u64();
    let reported_time = child_reported["identity"]["createTime"]
        .as_f64()
        .ok_or("Child start time missing")?;
    if ready["schemaVersion"] != 1
        || ready["native"] != true
        || child_reported["state"] != "running"
        || reported_pid != Some(u64::from(pid))
        || (reported_time - observed_identity.create_time).abs() >= 0.001
    {
        return Err("Child and parent native identity evidence differ".into());
    }
    child
        .0
        .stdin
        .take()
        .ok_or("Missing controlled child stdin")?
        .write_all(b"release\n")?;
    child.await_exit()?;
    let after = query_process(pid);
    if after != (ProcessEvidence::Exited { pid }) {
        return Err("Reaped controlled child was not observed as exited".into());
    }
    Ok(json!({
        "schemaVersion":1,"native":true,"passed":true,
        "host":{"os":std::env::consts::OS,"architecture":std::env::consts::ARCH},
        "selfIdentity":own,"childReported":child_reported,"childObserved":observed,"childAfterExit":after,
        "checks":{"selfIdentityAvailable":own_identity.pid==std::process::id(),"childIdentityMatches":true,"reapedChildIsExited":true},
        "inventoryScans":false,"liveSettingsAccess":false,"productionOwnershipQualified":false
    }))
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let arguments: Vec<_> = std::env::args_os().skip(1).collect();
    match arguments.as_slice() {
        [] => report(&lifecycle()?)?,
        [role] if role == "--hold" => hold()?,
        _ => return Err("Only the fixed no-argument lifecycle or --hold role is allowed".into()),
    }
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        let message: String = error.to_string().chars().take(512).collect();
        let _ = report(&json!({"schemaVersion":1,"native":true,"passed":false,"error":message}));
        std::process::exit(1);
    }
}
