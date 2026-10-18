//! Native compatibility probes. No serial or arbitrary process command is exposed.
use portable_pty::{native_pty_system, CommandBuilder, PtySize};
use serde::Serialize;
use std::io::{Read, Write};
use std::path::Path;
use std::sync::mpsc;
use std::time::Duration;

pub const FIXTURE_CHILD_ARGUMENT: &str = "--mcu-fixture-child";
pub const MAX_FIXTURE_BYTES: usize = 64 * 1024;

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct StreamSummary {
    pub bytes: usize,
    pub exit_code: u32,
}

/// Deterministic child used only by the prototype PTY probe. No shell, filesystem
/// project, inherited CLI command, credentials or device is involved.
pub fn write_fixture(mut output: impl Write) -> std::io::Result<()> {
    output.write_all(b"\x1b[36mMCU Flasher native PTY probe\x1b[0m\r\n")?;
    output.write_all("UTF-8: café · Ω · 日本語\r\n".as_bytes())?;
    for index in 1..=24 {
        writeln!(output, "fixture output {index:02}/24")?;
    }
    output.write_all(b"\x1b[32mControlled child complete\x1b[0m\r\n")?;
    output.flush()
}

/// Launch only this verified application executable in its fixed fixture role.
/// Each reader message and the complete output are bounded; stalled or failed
/// consumers kill and reap the owned child before returning an error.
pub fn stream_fixture(
    executable: &Path,
    mut deliver: impl FnMut(String, usize) -> Result<(), String>,
) -> Result<StreamSummary, String> {
    let pair = native_pty_system()
        .openpty(PtySize {
            rows: 24,
            cols: 80,
            pixel_width: 0,
            pixel_height: 0,
        })
        .map_err(|error| format!("Cannot create the native PTY: {error}"))?;
    let mut command = CommandBuilder::new(executable);
    command.arg(FIXTURE_CHILD_ARGUMENT);
    let mut reader = pair
        .master
        .try_clone_reader()
        .map_err(|error| format!("Cannot read the native PTY: {error}"))?;
    let mut child = pair
        .slave
        .spawn_command(command)
        .map_err(|error| format!("Cannot launch the controlled PTY child: {error}"))?;
    drop(pair.slave);
    let (sender, receiver) = mpsc::sync_channel(2);
    let reader_thread = std::thread::spawn(move || {
        loop {
            let mut chunk = [0u8; 4096];
            match reader.read(&mut chunk) {
                Ok(0) => break,
                Ok(count) => {
                    if sender.send(Ok(chunk[..count].to_vec())).is_err() {
                        return;
                    }
                }
                // A closed Linux PTY can report EIO after its final output.
                Err(error) if cfg!(unix) && error.raw_os_error() == Some(5) => break,
                Err(error) => {
                    let _ = sender.send(Err(error.to_string()));
                    break;
                }
            }
        }
    });
    let mut bytes = 0;
    let mut decoder = Utf8Stream::default();
    let result = (|| {
        loop {
            match receiver.recv_timeout(Duration::from_secs(10)) {
                Ok(Ok(chunk)) => {
                    bytes += chunk.len();
                    if bytes > MAX_FIXTURE_BYTES {
                        return Err("Controlled child exceeded its output budget".into());
                    }
                    let text = decoder.push(&chunk, false);
                    if !text.is_empty() {
                        deliver(text, chunk.len())?;
                    }
                }
                Ok(Err(error)) => return Err(format!("PTY read failed: {error}")),
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    return Err("Controlled PTY child timed out".into())
                }
            }
        }
        let trailing = decoder.push(&[], true);
        if !trailing.is_empty() {
            deliver(trailing, 0)?;
        }
        let status = child
            .wait()
            .map_err(|error| format!("Cannot reap the PTY child: {error}"))?;
        Ok(StreamSummary {
            bytes,
            exit_code: status.exit_code(),
        })
    })();
    if result.is_err() {
        let _ = child.kill();
        let _ = child.wait();
    }
    drop(receiver);
    drop(pair.master);
    // Reader observes EOF after the owned child's slave closes. Do not detach
    // blocked workers or retain a per-probe background poller.
    reader_thread
        .join()
        .map_err(|_| "PTY reader panicked".to_string())?;
    result
}

#[derive(Default)]
struct Utf8Stream {
    pending: Vec<u8>,
}

impl Utf8Stream {
    fn push(&mut self, chunk: &[u8], final_chunk: bool) -> String {
        self.pending.extend_from_slice(chunk);
        let mut result = String::new();
        loop {
            match std::str::from_utf8(&self.pending) {
                Ok(text) => {
                    result.push_str(text);
                    self.pending.clear();
                    break;
                }
                Err(error) => {
                    let valid = error.valid_up_to();
                    result.push_str(
                        std::str::from_utf8(&self.pending[..valid]).expect("validated prefix"),
                    );
                    self.pending.drain(..valid);
                    match error.error_len() {
                        Some(invalid) => {
                            result.push('\u{fffd}');
                            self.pending.drain(..invalid);
                        }
                        None if final_chunk => {
                            result.push('\u{fffd}');
                            self.pending.clear();
                            break;
                        }
                        None => break,
                    }
                }
            }
        }
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn utf8_is_preserved_across_every_chunk_boundary() {
        let text = "PTY café Ω 日本語\r\n";
        for split in 0..=text.len() {
            let mut decoder = Utf8Stream::default();
            let mut output = decoder.push(&text.as_bytes()[..split], false);
            output.push_str(&decoder.push(&text.as_bytes()[split..], true));
            assert_eq!(output, text);
        }
    }
    #[test]
    fn invalid_or_incomplete_bytes_have_bounded_replacement() {
        let mut decoder = Utf8Stream::default();
        assert_eq!(decoder.push(&[b'A', 0xff, 0xe2], false), "A�");
        assert_eq!(decoder.push(&[], true), "�");
        assert!(decoder.pending.is_empty());
    }
    #[test]
    fn fixture_is_bounded_and_contains_real_unicode_and_ansi() {
        let mut bytes = Vec::new();
        write_fixture(&mut bytes).unwrap();
        assert!(bytes.len() < MAX_FIXTURE_BYTES);
        let text = String::from_utf8(bytes).unwrap();
        assert!(text.contains("日本語"));
        assert!(text.contains("\x1b[36m"));
        assert!(text.contains("24/24"));
    }
}
