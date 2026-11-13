use eframe::egui;
use serde::Deserialize;
use serde_json::{Value, json};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc::{self, Receiver, SyncSender};
use std::thread;
use std::time::{Duration, Instant};

const MAX_FRAME: usize = 1024 * 1024;

#[derive(Debug, Deserialize)]
pub struct Event {
    pub event: String,
    #[serde(default)]
    pub data: Value,
}

impl Event {
    pub fn error(message: impl Into<String>) -> Self {
        Self {
            event: "fatal".into(),
            data: json!({"message": message.into()}),
        }
    }
}

pub struct Bridge {
    pub events: Receiver<Event>,
    commands: Option<SyncSender<Value>>,
}

pub fn lite_root() -> Result<PathBuf, String> {
    if let Some(root) = std::env::var_os("MCU_LITE_ROOT") {
        let root = PathBuf::from(root);
        if root.join("python/worker.py").is_file() {
            return Ok(root);
        }
    }
    let executable = std::env::current_exe().map_err(|e| e.to_string())?;
    // Native launchers, bin/ bundles, and cargo target/{debug,release} all work
    // after moving the whole project. No compiled-in developer path is used.
    for parent in executable.ancestors().skip(1).take(6) {
        if parent.join("python/worker.py").is_file() {
            return Ok(parent.to_path_buf());
        }
    }
    Err("Keep this executable in LITE-MODE/bin beside python/worker.py".into())
}

fn python(root: &Path) -> Result<PathBuf, String> {
    if let Some(path) = std::env::var_os("MCU_LITE_PYTHON") {
        let path = PathBuf::from(path);
        if path.is_file() {
            return Ok(path);
        }
        return Err("MCU_LITE_PYTHON does not name a Python executable".into());
    }
    let original = root.parent().ok_or("Missing main application folder")?;
    #[cfg(windows)]
    let candidates = [
        root.join(".venv/Scripts/python.exe"),
        original.join("src/_python/python.exe"),
        original.join("env/Scripts/python.exe"),
    ];
    #[cfg(not(windows))]
    let candidates = [
        root.join(".venv/bin/python"),
        original.join(".venv-linux/bin/python"),
    ];
    candidates.into_iter().find(|p| p.is_file()).ok_or_else(||
        "Python tools are missing. Run LITE-MODE/tools/setup.py with Python 3.11+ or prepare the main app's private runtime.".into())
}

impl Bridge {
    pub fn start(ctx: egui::Context) -> Self {
        let (command_tx, command_rx) = mpsc::sync_channel::<Value>(64);
        let (event_tx, event_rx) = mpsc::sync_channel::<Event>(512);
        thread::spawn(move || {
            if let Err(error) = run(command_rx, &event_tx, &ctx) {
                let _ = event_tx.send(Event::error(error));
                ctx.request_repaint();
            }
        });
        Self {
            events: event_rx,
            commands: Some(command_tx),
        }
    }

    pub fn send(&self, value: Value) -> Result<(), String> {
        self.commands
            .as_ref()
            .ok_or("Tool worker closed")?
            .try_send(value)
            .map_err(|error| format!("Tool worker unavailable: {error}"))
    }
}

impl Drop for Bridge {
    fn drop(&mut self) {
        // Disconnecting the command queue asks the owner thread for graceful
        // shutdown. The Python worker never kills an active flash write.
        self.commands.take();
    }
}

fn run(
    commands: Receiver<Value>,
    events: &SyncSender<Event>,
    ctx: &egui::Context,
) -> Result<(), String> {
    let root = lite_root()?;
    let mut command = Command::new(python(&root)?);
    command
        .args(["-B", "-u"])
        .arg(root.join("python/worker.py"))
        .current_dir(&root)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONDONTWRITEBYTECODE", "1");
    // The isolated worker owns its environment. Discard inherited application
    // and import policies before even starting the borrowed interpreter.
    for (key, _) in std::env::vars_os() {
        let key_text = key.to_string_lossy();
        if key_text.starts_with("MCU_FLASHER_")
            || key_text.starts_with("PLATFORMIO_")
            || ["PYTHONHOME", "PYTHONPATH", "PYTHONPYCACHEPREFIX"].contains(&key_text.as_ref())
        {
            command.env_remove(key);
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000);
    }
    let mut child = command
        .spawn()
        .map_err(|e| format!("Cannot start Python tools: {e}"))?;
    let mut input = child.stdin.take().ok_or("Missing tool input pipe")?;
    let output = child.stdout.take().ok_or("Missing tool output pipe")?;
    let errors = child.stderr.take().ok_or("Missing tool diagnostics pipe")?;
    let tx = events.clone();
    let repaint = ctx.clone();
    thread::spawn(move || {
        let mut reader = BufReader::new(output);
        loop {
            let mut frame = Vec::new();
            match reader
                .by_ref()
                .take((MAX_FRAME + 1) as u64)
                .read_until(b'\n', &mut frame)
            {
                Ok(0) => break,
                Ok(_) if frame.len() > MAX_FRAME => {
                    let _ = tx.send(Event::error("Tool response exceeded the 1 MiB limit"));
                    break;
                }
                Ok(_) => {
                    let event = serde_json::from_slice::<Event>(&frame)
                        .unwrap_or_else(|e| Event::error(format!("Invalid tool response: {e}")));
                    if tx.send(event).is_err() {
                        break;
                    }
                    repaint.request_repaint();
                }
                Err(error) => {
                    let _ = tx.send(Event::error(format!("Tool output: {error}")));
                    break;
                }
            }
        }
        let _ = tx.send(Event::error(
            "Tool worker disconnected. Reopen Lite to reconnect.",
        ));
        repaint.request_repaint();
    });
    let tx = events.clone();
    let repaint = ctx.clone();
    thread::spawn(move || {
        // Bound individual diagnostics as well as the receiving event queue.
        let mut reader = BufReader::new(errors);
        loop {
            let mut line = Vec::new();
            match reader.by_ref().take(8192).read_until(b'\n', &mut line) {
                Ok(0) | Err(_) => break,
                Ok(_) => {
                    if tx
                        .send(Event {
                            event: "diagnostic".into(),
                            data: json!({"text": String::from_utf8_lossy(&line)}),
                        })
                        .is_err()
                    {
                        break;
                    }
                    repaint.request_repaint();
                }
            }
        }
    });
    // Poll exit while idle so a dead worker cannot leave a live command sender.
    loop {
        match commands.recv_timeout(Duration::from_millis(200)) {
            Ok(value) => {
                serde_json::to_writer(&mut input, &value).map_err(|e| e.to_string())?;
                input
                    .write_all(b"\n")
                    .and_then(|_| input.flush())
                    .map_err(|e| e.to_string())?;
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {
                if let Some(status) = child.try_wait().map_err(|e| e.to_string())? {
                    return Err(format!(
                        "Python tool worker exited ({status}). See Build monitor."
                    ));
                }
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => break,
        }
    }
    let _ = input.write_all(b"{\"cmd\":\"shutdown\"}\n");
    let _ = input.flush();
    drop(input);
    // Do not forcibly terminate a programmer. This owner remains alive until
    // the tool backend has finished its guarded shutdown after UI loss.
    let started = Instant::now();
    loop {
        if child.try_wait().map_err(|e| e.to_string())?.is_some() {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(if started.elapsed().as_secs() < 10 {
            100
        } else {
            500
        }));
    }
}
