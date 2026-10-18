//! Native compatibility probes. No serial or arbitrary process command is exposed.
pub mod fixture_lock;
pub mod stream_ack;
use portable_pty::{native_pty_system, CommandBuilder, PtySize};
use serde::Serialize;
use std::io::{Read, Write};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, OnceLock};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

pub const FIXTURE_CHILD_ARGUMENT: &str = "--mcu-fixture-child";
pub const MAX_FIXTURE_BYTES: usize = 64 * 1024;
const FIXTURE_TIMEOUT: Duration = Duration::from_secs(10);
const CLEANUP_TIMEOUT: Duration = Duration::from_secs(3);

static FIXTURE_BUSY: OnceLock<Arc<AtomicBool>> = OnceLock::new();

struct ProbeLease {
    busy: Arc<AtomicBool>,
}

impl ProbeLease {
    fn acquire(busy: Arc<AtomicBool>) -> Result<Arc<Self>, String> {
        busy.compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| {
                "The previous controlled PTY probe is still running or cleaning up".to_string()
            })?;
        Ok(Arc::new(Self { busy }))
    }
}

impl Drop for ProbeLease {
    fn drop(&mut self) {
        self.busy.store(false, Ordering::Release);
    }
}

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
/// Each reader message and the complete output are bounded. `deliver` must
/// return promptly; arbitrary callback blocking cannot be cancelled by this
/// synchronous API. Errors/panics stop delivery, kill the child and keep draining
/// through native teardown. An OS worker that fails to finish within the cleanup
/// deadline retains the single-probe lease, so repeated calls cannot accumulate
/// stuck processes/threads. It is reported as incomplete cleanup, never success.
pub fn stream_fixture(
    executable: &Path,
    mut deliver: impl FnMut(String, usize) -> Result<(), String>,
) -> Result<StreamSummary, String> {
    let lease = ProbeLease::acquire(
        FIXTURE_BUSY
            .get_or_init(|| Arc::new(AtomicBool::new(false)))
            .clone(),
    )?;
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
    let reader = pair
        .master
        .try_clone_reader()
        .map_err(|error| format!("Cannot read the native PTY: {error}"))?;
    let writer = pair
        .master
        .take_writer()
        .map_err(|error| format!("Cannot answer native PTY capability queries: {error}"))?;
    let (sender, receiver) = mpsc::sync_channel(2);
    let reader_lease = lease.clone();
    let reader_thread = std::thread::spawn(move || {
        let _lease = reader_lease;
        drain_pty(reader, writer, sender);
    });
    // Start draining before CreateProcess. Failed launch must still answer any
    // pending inherit-cursor query while ClosePseudoConsole tears down.
    let child_result = pair.slave.spawn_command(command);
    drop(pair.slave);
    let mut child = match child_result {
        Ok(child) => child,
        Err(error) => {
            drop(receiver);
            let closer_lease = lease.clone();
            let closer = std::thread::spawn(move || {
                let _lease = closer_lease;
                drop(pair.master);
            });
            let deadline = Instant::now() + CLEANUP_TIMEOUT;
            let mut result = Err(format!("Cannot launch the controlled PTY child: {error}"));
            append_cleanup_error(
                &mut result,
                finish_worker(reader_thread, deadline, "PTY reader"),
            );
            append_cleanup_error(&mut result, finish_worker(closer, deadline, "PTY teardown"));
            return result;
        }
    };
    let mut bytes = 0;
    let mut decoder = Utf8Stream::default();
    let deadline = Instant::now() + FIXTURE_TIMEOUT;
    let mut master = Some(pair.master);
    let mut closer = None;
    let mut exit_status = None;
    let mut result = (|| {
        loop {
            if Instant::now() >= deadline {
                return Err("Controlled PTY child timed out".into());
            }
            if exit_status.is_none() {
                exit_status = child
                    .try_wait()
                    .map_err(|error| format!("Cannot poll the PTY child: {error}"))?;
                if exit_status.is_some() {
                    // ConPTY can keep its output pipe open after child exit.
                    // Close it off-thread while the reader keeps draining the
                    // final frame and answering the inherited cursor query.
                    closer = master.take().map(|master| {
                        let closer_lease = lease.clone();
                        std::thread::spawn(move || {
                            let _lease = closer_lease;
                            drop(master);
                        })
                    });
                }
            }
            match receiver.recv_timeout(Duration::from_millis(25)) {
                Ok(Ok(chunk)) => {
                    bytes += chunk.len();
                    if bytes > MAX_FIXTURE_BYTES {
                        return Err("Controlled child exceeded its output budget".into());
                    }
                    let text = decoder.push(&chunk, false);
                    if !text.is_empty() {
                        deliver_safely(&mut deliver, text, chunk.len())?;
                    }
                }
                Ok(Err(error)) => return Err(format!("PTY read failed: {error}")),
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
                Err(mpsc::RecvTimeoutError::Timeout) => continue,
            }
        }
        let trailing = decoder.push(&[], true);
        if !trailing.is_empty() {
            deliver_safely(&mut deliver, trailing, 0)?;
        }
        // EOF can precede process exit on Unix. Keep this wait bounded too.
        while exit_status.is_none() {
            if Instant::now() >= deadline {
                return Err("Controlled PTY child timed out after output closed".into());
            }
            exit_status = child
                .try_wait()
                .map_err(|error| format!("Cannot poll the PTY child: {error}"))?;
            if exit_status.is_none() {
                std::thread::sleep(Duration::from_millis(10));
            }
        }
        let status = exit_status.take().expect("child exit observed");
        Ok(StreamSummary {
            bytes,
            exit_code: status.exit_code(),
        })
    })();
    let mut needs_reap = result.is_err() && exit_status.is_none();
    if needs_reap {
        match child.try_wait() {
            Ok(Some(_)) => needs_reap = false,
            Ok(None) => {}
            Err(error) => append_cleanup_error(
                &mut result,
                Err(format!("Cannot poll the PTY child during cleanup: {error}")),
            ),
        }
        // Unix try_wait reaps the child; portable-pty's kill implementation
        // sends SIGHUP by numeric PID. Never signal an already-observed exit,
        // whose old PID could have been reused by an unrelated process.
        if needs_reap {
            if let Err(error) = child.kill() {
                append_cleanup_error(
                    &mut result,
                    Err(format!("Cannot terminate the PTY child: {error}")),
                );
            }
        }
    }
    // Release a blocked bounded-channel send before waiting for process exit or
    // ClosePseudoConsole. The disconnected reader still drains native output.
    drop(receiver);
    if let Some(master) = master.take() {
        let closer_lease = lease.clone();
        closer = Some(std::thread::spawn(move || {
            let _lease = closer_lease;
            drop(master);
        }));
    }
    let cleanup_deadline = Instant::now() + CLEANUP_TIMEOUT;
    if needs_reap {
        // portable-pty's wait() can block indefinitely. Keep a reaper owner in
        // that case, retaining the busy lease until the OS actually releases it.
        let reaper_lease = lease.clone();
        let reaper = std::thread::spawn(move || {
            let _lease = reaper_lease;
            child
                .wait()
                .map(|_| ())
                .map_err(|error| format!("Cannot reap the PTY child: {error}"))
        });
        append_cleanup_error(&mut result, finish_reaper(reaper, cleanup_deadline));
    }
    append_cleanup_error(
        &mut result,
        finish_worker(reader_thread, cleanup_deadline, "PTY reader"),
    );
    if let Some(closer) = closer {
        append_cleanup_error(
            &mut result,
            finish_worker(closer, cleanup_deadline, "PTY teardown"),
        );
    }
    result
}

fn drain_pty(
    mut reader: impl Read,
    mut writer: impl Write,
    sender: mpsc::SyncSender<Result<Vec<u8>, String>>,
) {
    let mut cursor_query = CursorQuery::default();
    let mut consumer_connected = true;
    let mut writer_failed = false;
    loop {
        let mut chunk = [0u8; 4096];
        match reader.read(&mut chunk) {
            Ok(0) => break,
            Ok(count) => {
                // portable-pty enables ConPTY's INHERIT_CURSOR flag. This fixed
                // probe starts at1;1; real terminal sessions must query their
                // actual renderer cursor instead of reusing this fixture reply.
                for _ in 0..cursor_query.push(&chunk[..count]) {
                    if !writer_failed {
                        if let Err(error) =
                            writer.write_all(b"\x1b[1;1R").and_then(|()| writer.flush())
                        {
                            writer_failed = true;
                            if consumer_connected {
                                consumer_connected = sender
                                    .send(Err(format!("PTY cursor reply failed: {error}")))
                                    .is_ok();
                            }
                            // Closing input may fail during teardown. Continue
                            // draining output so ClosePseudoConsole can finish.
                        }
                    }
                }
                if consumer_connected {
                    consumer_connected = sender.send(Ok(chunk[..count].to_vec())).is_ok();
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            // A closed Linux PTY can report EIO after its final output.
            Err(error) if cfg!(unix) && error.raw_os_error() == Some(5) => break,
            Err(error) => {
                if consumer_connected {
                    let _ = sender.send(Err(error.to_string()));
                }
                break;
            }
        }
    }
}

fn deliver_safely(
    deliver: &mut impl FnMut(String, usize) -> Result<(), String>,
    text: String,
    bytes: usize,
) -> Result<(), String> {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| deliver(text, bytes)))
        .unwrap_or_else(|_| Err("Controlled PTY output consumer panicked".into()))
}

fn append_cleanup_error<T>(result: &mut Result<T, String>, cleanup: Result<(), String>) {
    if let Err(error) = cleanup {
        match result {
            Ok(_) => *result = Err(error),
            Err(original) => {
                original.push_str("; ");
                original.push_str(&error);
            }
        }
    }
}

fn wait_worker<T>(worker: &JoinHandle<T>, deadline: Instant) -> Result<(), String> {
    while !worker.is_finished() {
        if Instant::now() >= deadline {
            return Err("cleanup deadline expired; native worker remains supervised and another probe is blocked".into());
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    Ok(())
}

fn finish_worker(worker: JoinHandle<()>, deadline: Instant, name: &str) -> Result<(), String> {
    wait_worker(&worker, deadline).map_err(|error| format!("{name}: {error}"))?;
    worker.join().map_err(|_| format!("{name} panicked"))
}

fn finish_reaper(worker: JoinHandle<Result<(), String>>, deadline: Instant) -> Result<(), String> {
    wait_worker(&worker, deadline).map_err(|error| format!("PTY reaper: {error}"))?;
    worker
        .join()
        .map_err(|_| "PTY reaper panicked".to_string())?
}

#[derive(Default)]
struct CursorQuery {
    matched: usize,
}

impl CursorQuery {
    fn push(&mut self, chunk: &[u8]) -> usize {
        const QUERY: &[u8] = b"\x1b[6n";
        let mut count = 0;
        for &byte in chunk {
            if byte == QUERY[self.matched] {
                self.matched += 1;
                if self.matched == QUERY.len() {
                    count += 1;
                    self.matched = 0;
                }
            } else {
                self.matched = usize::from(byte == QUERY[0]);
            }
        }
        count
    }
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
    use std::collections::VecDeque;
    use std::sync::atomic::AtomicUsize;
    use std::sync::Mutex;

    struct ScriptedReader {
        chunks: VecDeque<Result<Vec<u8>, std::io::ErrorKind>>,
        bytes_read: Arc<AtomicUsize>,
    }

    impl Read for ScriptedReader {
        fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
            match self.chunks.pop_front() {
                Some(Ok(chunk)) => {
                    assert!(chunk.len() <= buffer.len());
                    buffer[..chunk.len()].copy_from_slice(&chunk);
                    self.bytes_read.fetch_add(chunk.len(), Ordering::SeqCst);
                    Ok(chunk.len())
                }
                Some(Err(kind)) => Err(kind.into()),
                None => Ok(0),
            }
        }
    }

    struct RecordedWriter(Arc<Mutex<Vec<u8>>>);

    impl Write for RecordedWriter {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            self.0.lock().unwrap().extend_from_slice(bytes);
            Ok(bytes.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    struct FailedWriter;

    impl Write for FailedWriter {
        fn write(&mut self, _: &[u8]) -> std::io::Result<usize> {
            Err(std::io::ErrorKind::BrokenPipe.into())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn failed_native_child_launch_closes_pty_and_releases_probe_lease() {
        let checkout = Path::new(env!("CARGO_MANIFEST_DIR"))
            .ancestors()
            .nth(3)
            .unwrap();
        let missing = checkout.join("audit/migration/scratch").join(format!(
            "missing-pty-fixture-{}-no-executable",
            std::process::id()
        ));
        assert!(!missing.exists());
        let started = Instant::now();
        let error = stream_fixture(&missing, |_, _| {
            panic!("a failed launch cannot deliver child output")
        })
        .unwrap_err();
        assert!(
            error.starts_with("Cannot launch the controlled PTY child:"),
            "{error}"
        );
        assert!(!error.contains("cleanup deadline"), "{error}");
        assert!(started.elapsed() < CLEANUP_TIMEOUT + Duration::from_secs(1));
        assert!(!FIXTURE_BUSY.get().unwrap().load(Ordering::Acquire));
    }

    #[test]
    fn disconnected_consumer_still_drains_and_answers_split_cursor_query() {
        let bytes_read = Arc::new(AtomicUsize::new(0));
        let reader = ScriptedReader {
            chunks: [
                Ok(b"first".to_vec()),
                Ok(b"\x1b[".to_vec()),
                Err(std::io::ErrorKind::Interrupted),
                Ok(b"6nlast".to_vec()),
            ]
            .into(),
            bytes_read: bytes_read.clone(),
        };
        let writes = Arc::new(Mutex::new(Vec::new()));
        let (sender, receiver) = mpsc::sync_channel(1);
        drop(receiver);
        drain_pty(reader, RecordedWriter(writes.clone()), sender);
        assert_eq!(bytes_read.load(Ordering::SeqCst), 13);
        assert_eq!(*writes.lock().unwrap(), b"\x1b[1;1R");
    }

    #[test]
    fn cursor_reply_failure_reports_error_and_drains_remaining_output() {
        let bytes_read = Arc::new(AtomicUsize::new(0));
        let reader = ScriptedReader {
            chunks: [Ok(b"\x1b[6n".to_vec()), Ok(b"final frame".to_vec())].into(),
            bytes_read: bytes_read.clone(),
        };
        let (sender, receiver) = mpsc::sync_channel(8);
        drain_pty(reader, FailedWriter, sender);
        let messages: Vec<_> = receiver.into_iter().collect();
        assert!(messages[0]
            .as_ref()
            .unwrap_err()
            .contains("cursor reply failed"));
        assert_eq!(messages[2].as_ref().unwrap(), b"final frame");
        assert_eq!(bytes_read.load(Ordering::SeqCst), 15);
    }

    #[test]
    fn dropping_full_channel_releases_reader_to_drain_final_frame() {
        let bytes_read = Arc::new(AtomicUsize::new(0));
        let reader = ScriptedReader {
            chunks: [Ok(vec![1; 4096]), Ok(vec![2; 4096]), Ok(vec![3; 4096])].into(),
            bytes_read: bytes_read.clone(),
        };
        let (sender, receiver) = mpsc::sync_channel(1);
        let worker = std::thread::spawn(move || drain_pty(reader, std::io::sink(), sender));
        let deadline = Instant::now() + Duration::from_secs(1);
        while bytes_read.load(Ordering::SeqCst) < 8192 {
            assert!(
                Instant::now() < deadline,
                "reader did not reach bounded backpressure"
            );
            std::thread::yield_now();
        }
        drop(receiver);
        finish_worker(worker, deadline, "test reader").unwrap();
        assert_eq!(bytes_read.load(Ordering::SeqCst), 3 * 4096);
    }

    #[test]
    fn timed_out_cleanup_returns_promptly_and_keeps_probe_lease_until_worker_finishes() {
        let busy = Arc::new(AtomicBool::new(false));
        let lease = ProbeLease::acquire(busy.clone()).unwrap();
        let worker_lease = lease.clone();
        let (release, wait) = mpsc::channel();
        let (completed, done) = mpsc::channel();
        let worker = std::thread::spawn(move || {
            let _lease = worker_lease;
            wait.recv().unwrap();
            drop(_lease);
            completed.send(()).unwrap();
        });
        drop(lease);
        let started = Instant::now();
        assert!(finish_worker(worker, started, "test worker")
            .unwrap_err()
            .contains("cleanup deadline"));
        assert!(started.elapsed() < Duration::from_secs(1));
        assert!(ProbeLease::acquire(busy.clone()).is_err());
        release.send(()).unwrap();
        done.recv_timeout(Duration::from_secs(1)).unwrap();
        assert!(ProbeLease::acquire(busy).is_ok());
    }

    #[test]
    fn consumer_panic_and_cleanup_failure_preserve_original_delivery_error() {
        let mut deliver =
            |_: String, _: usize| -> Result<(), String> { panic!("controlled consumer panic") };
        let mut result: Result<(), String> = deliver_safely(&mut deliver, "fixture".into(), 7);
        assert!(result.as_ref().unwrap_err().contains("consumer panicked"));
        append_cleanup_error(&mut result, Err("native cleanup pending".into()));
        assert!(result.unwrap_err().ends_with("; native cleanup pending"));
    }

    #[test]
    fn cursor_query_is_recognized_across_every_chunk_boundary() {
        let bytes = b"unrelated\x1b[36m\x1b[6n\x1b[6nend";
        for split in 0..=bytes.len() {
            let mut query = CursorQuery::default();
            assert_eq!(query.push(&bytes[..split]) + query.push(&bytes[split..]), 2);
        }
        let mut query = CursorQuery::default();
        assert_eq!(query.push(b"\x1b\x1b[6n\x1b[?6n\x1b[1;1R"), 1);
        assert_eq!(query.matched, 0);
    }
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
