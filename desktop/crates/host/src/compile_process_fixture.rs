//! Standalone fixed-self process-lifetime fixture, never a tool/build runner.
//! Only `compile_process_probe` may call this module. It accepts no executable,
//! command, environment, directory, hook or caller callback. Linux temporarily
//! makes this dedicated probe a subreaper; this is not a live GUI primitive.
//! No immutable external executable or compiler lifetime has been qualified.
//! Windows qualifies only the fixed named-current-executable fixture: a file
//! pin prevents later write/delete, but a rename/replacement before that pin
//! is not independently bound to the already-running image. Do not promote
//! this Windows fixture selection into byte/image authority or general adoption.
//! Linux requires default waitable SIGCHLD and exclusive child waiting in this
//! standalone probe; custom/ignored/no-wait dispositions refuse before spawn.

use serde::Serialize;
use std::io::{self, Write};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

pub const MAX_COMPILE_PROCESS_STREAM_BYTES: usize = 64 * 1024;
const CHILD_ARGUMENT: &str = "--compile-process-child";
const GRANDCHILD_ARGUMENT: &str = "--compile-process-grandchild";
const RUN_TIMEOUT: Duration = Duration::from_secs(5);
const HANG_TIMEOUT: Duration = Duration::from_millis(150);
const CLEANUP_TIMEOUT: Duration = Duration::from_secs(3);
const POLL_INTERVAL: Duration = Duration::from_millis(5);
const READ_CHUNK: usize = 4096;
const READS_PER_TICK: usize = 4;
static PROCESS_BUSY: AtomicBool = AtomicBool::new(false);

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum CompileProcessMode {
    Success,
    Nonzero,
    FloodStdout,
    FloodStderr,
    FloodBoth,
    Hang,
    CancelAfterStart,
    SigchldRefusals,
    InheritedPipeGrandchild,
}

impl CompileProcessMode {
    fn literal(self) -> &'static str {
        match self {
            Self::Success => "success",
            Self::Nonzero => "nonzero",
            Self::FloodStdout => "flood_stdout",
            Self::FloodStderr => "flood_stderr",
            Self::FloodBoth => "flood_both",
            Self::Hang => "hang",
            Self::CancelAfterStart => "cancel_after_start",
            Self::SigchldRefusals => "sigchld_refusals",
            Self::InheritedPipeGrandchild => "inherited_pipe_grandchild",
        }
    }

    fn parse(value: &str) -> Option<Self> {
        match value {
            "success" => Some(Self::Success),
            "nonzero" => Some(Self::Nonzero),
            "flood_stdout" => Some(Self::FloodStdout),
            "flood_stderr" => Some(Self::FloodStderr),
            "flood_both" => Some(Self::FloodBoth),
            "hang" => Some(Self::Hang),
            "cancel_after_start" => Some(Self::CancelAfterStart),
            "sigchld_refusals" => Some(Self::SigchldRefusals),
            "inherited_pipe_grandchild" => Some(Self::InheritedPipeGrandchild),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum CompileProcessOutcome {
    Success,
    Nonzero { code: i32 },
    OutputLimit,
    TimedOut,
    Cancelled,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CompileProcessReport {
    pub outcome: CompileProcessOutcome,
    pub child_pid: Option<u32>,
    #[serde(skip)]
    pub stdout: Vec<u8>,
    #[serde(skip)]
    pub stderr: Vec<u8>,
    pub cleanup_complete: bool,
    pub stdout_eof: bool,
    pub stderr_eof: bool,
    pub descendants_reaped: usize,
}

/// `Ok` means the owner completed cleanup/drain; inspect `outcome` separately
/// for child success. Incomplete cleanup refuses and blocks further probes.
pub fn run_compile_process_fixture(
    mode: CompileProcessMode,
    cancel: &AtomicBool,
) -> Result<CompileProcessReport, String> {
    let executable = verified_probe_executable().map_err(|error| error.to_string())?;
    #[cfg(any(target_os = "linux", windows))]
    {
        run_native(&executable, mode, cancel)
    }
    #[cfg(not(any(target_os = "linux", windows)))]
    {
        let _ = (executable, mode, cancel);
        Err("This fixed process fixture supports native Linux and Windows only".into())
    }
}

struct ProbeLease {
    release: bool,
}

impl Drop for ProbeLease {
    fn drop(&mut self) {
        if self.release {
            PROCESS_BUSY.store(false, Ordering::Release);
        }
    }
}

#[cfg(any(target_os = "linux", windows))]
fn run_native(
    executable: &std::path::Path,
    mode: CompileProcessMode,
    cancel: &AtomicBool,
) -> Result<CompileProcessReport, String> {
    native::Owner::preflight()?;
    PROCESS_BUSY
        .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
        .map_err(|_| "The prior process fixture is still active or cleanup was incomplete")?;
    let mut lease = ProbeLease { release: true };
    if cancel.load(Ordering::Acquire) {
        return Ok(CompileProcessReport {
            outcome: CompileProcessOutcome::Cancelled,
            child_pid: None,
            stdout: Vec::new(),
            stderr: Vec::new(),
            cleanup_complete: true,
            stdout_eof: true,
            stderr_eof: true,
            descendants_reaped: 0,
        });
    }
    // A failed/incomplete native spawn or cleanup poisons this one-probe lease.
    // With no worker threads and no retries, calls cannot accumulate owners.
    lease.release = false;
    let mut owner = native::Owner::spawn(executable, mode)?;
    let mut stdout = Capture::default();
    let mut stderr = Capture::default();
    let deadline = Instant::now()
        + if mode == CompileProcessMode::Hang {
            HANG_TIMEOUT
        } else {
            RUN_TIMEOUT
        };
    let mut error = None;
    let mut outcome = loop {
        if let Err(issue) = stdout.pump(|buffer| owner.read_stdout(buffer)) {
            error = Some(format!("stdout drain failed: {issue}"));
            break CompileProcessOutcome::Cancelled;
        }
        if let Err(issue) = stderr.pump(|buffer| owner.read_stderr(buffer)) {
            error = Some(format!("stderr drain failed: {issue}"));
            break CompileProcessOutcome::Cancelled;
        }
        // This fixed mode exercises the ordinary cancellation path only after
        // an observed child-start marker, without scheduling/timer assumptions.
        let after_start =
            mode == CompileProcessMode::CancelAfterStart && stdout.bytes.starts_with(b"started\n");
        if cancel.load(Ordering::Acquire) || after_start {
            break CompileProcessOutcome::Cancelled;
        }
        if stdout.exceeded || stderr.exceeded {
            break CompileProcessOutcome::OutputLimit;
        }
        if Instant::now() >= deadline {
            break CompileProcessOutcome::TimedOut;
        }
        match owner.poll_exit() {
            Ok(Some(0)) => break CompileProcessOutcome::Success,
            Ok(Some(code)) => break CompileProcessOutcome::Nonzero { code },
            Ok(None) => {}
            Err(issue) => {
                error = Some(format!("Cannot observe fixed child exit: {issue}"));
                break CompileProcessOutcome::Cancelled;
            }
        }
        std::thread::sleep(POLL_INTERVAL);
    };
    if let Err(issue) = owner.terminate() {
        error.get_or_insert_with(|| format!("Cannot terminate the owned group/job: {issue}"));
    }
    let cleanup_deadline = Instant::now() + CLEANUP_TIMEOUT;
    let mut native_complete = false;
    loop {
        if let Err(issue) = stdout.pump(|buffer| owner.read_stdout(buffer)) {
            error.get_or_insert_with(|| format!("stdout cleanup drain failed: {issue}"));
        }
        if let Err(issue) = stderr.pump(|buffer| owner.read_stderr(buffer)) {
            error.get_or_insert_with(|| format!("stderr cleanup drain failed: {issue}"));
        }
        if !native_complete {
            match owner.cleanup_step() {
                Ok(complete) => native_complete = complete,
                Err(issue) => {
                    error.get_or_insert_with(|| format!("Native cleanup failed: {issue}"));
                    break;
                }
            }
        }
        if native_complete && (stdout.eof || stdout.failed) && (stderr.eof || stderr.failed) {
            break;
        }
        if Instant::now() >= cleanup_deadline {
            error.get_or_insert_with(|| "Finite process cleanup/drain deadline expired".into());
            break;
        }
        std::thread::sleep(POLL_INTERVAL);
    }
    if !native_complete || !stdout.eof || !stderr.eof {
        // Retain at most this one poisoned owner rather than dropping its PID
        // reservation/subreaper or letting another call accumulate resources.
        // This explicit incomplete result is never a successful fixture.
        std::mem::forget(owner);
        return Err(
            error.unwrap_or_else(|| "Process cleanup or stream drain was incomplete".into())
        );
    }
    owner.finish_scope()?;
    lease.release = true;
    if let Some(error) = error {
        return Err(error);
    }
    if (stdout.exceeded || stderr.exceeded)
        && matches!(
            outcome,
            CompileProcessOutcome::Success | CompileProcessOutcome::Nonzero { .. }
        )
    {
        outcome = CompileProcessOutcome::OutputLimit;
    }
    Ok(CompileProcessReport {
        outcome,
        child_pid: Some(owner.pid()),
        stdout: stdout.bytes,
        stderr: stderr.bytes,
        cleanup_complete: true,
        stdout_eof: stdout.eof,
        stderr_eof: stderr.eof,
        descendants_reaped: owner.descendants_reaped(),
    })
}

/// Recognize only fixed fixture child roles for the dedicated probe binary.
/// None means this invocation is the native parent/self-check role.
pub fn dispatch_compile_process_child() -> Option<i32> {
    let mut args = std::env::args_os().skip(1);
    let argument = args.next()?;
    if argument == GRANDCHILD_ARGUMENT {
        if args.next().is_some() || verified_probe_executable().is_err() {
            return Some(125);
        }
        std::thread::sleep(Duration::from_secs(20));
        return Some(0);
    }
    if argument != CHILD_ARGUMENT {
        return None;
    }
    let mode_argument = args.next();
    let mode = mode_argument
        .as_deref()
        .and_then(std::ffi::OsStr::to_str)
        .and_then(CompileProcessMode::parse);
    if args.next().is_some() || verified_probe_executable().is_err() {
        return Some(125);
    }
    Some(match mode {
        Some(mode) => child_body(mode).unwrap_or(124),
        None => 125,
    })
}

fn verified_probe_executable() -> io::Result<PathBuf> {
    let executable = std::env::current_exe()?;
    let expected = if cfg!(windows) {
        "compile_process_probe.exe"
    } else {
        "compile_process_probe"
    };
    if executable.file_name() != Some(std::ffi::OsStr::new(expected))
        || !std::fs::metadata(&executable)?.is_file()
    {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "Only the dedicated compile_process_probe executable may run this fixture",
        ));
    }
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::fs::MetadataExt;
        let running = std::fs::metadata("/proc/self/exe")?;
        let named = std::fs::metadata(&executable)?;
        if (running.dev(), running.ino()) != (named.dev(), named.ino()) {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "Named probe executable no longer identifies the running image",
            ));
        }
        // exec of this kernel-owned link selects the calling process's image,
        // closing the final pathname-replacement gap for this self-only role.
        Ok(PathBuf::from("/proc/self/exe"))
    }
    #[cfg(not(target_os = "linux"))]
    Ok(executable)
}

fn child_body(mode: CompileProcessMode) -> io::Result<i32> {
    match mode {
        CompileProcessMode::Success => {
            io::stdout().write_all(
                b"stdout: caf\xc3\xa9 \xce\xa9 \xe6\x97\xa5\xe6\x9c\xac\xe8\xaa\x9e\r\n",
            )?;
            io::stderr().write_all(b"stderr: separate\n")?;
            Ok(0)
        }
        CompileProcessMode::Nonzero => {
            io::stderr().write_all(b"fixed nonzero\n")?;
            Ok(23)
        }
        CompileProcessMode::FloodStdout
        | CompileProcessMode::FloodStderr
        | CompileProcessMode::FloodBoth => {
            for _ in 0..48 {
                if mode != CompileProcessMode::FloodStderr {
                    io::stdout().write_all(&[b'O'; READ_CHUNK])?;
                }
                if mode != CompileProcessMode::FloodStdout {
                    io::stderr().write_all(&[b'E'; READ_CHUNK])?;
                }
            }
            Ok(0)
        }
        CompileProcessMode::Hang => {
            std::thread::sleep(Duration::from_secs(20));
            Ok(0)
        }
        CompileProcessMode::CancelAfterStart => {
            io::stdout().write_all(b"started\n")?;
            io::stdout().flush()?;
            std::thread::sleep(Duration::from_secs(20));
            Ok(0)
        }
        CompileProcessMode::SigchldRefusals => {
            #[cfg(target_os = "linux")]
            {
                linux::verify_sigchld_refusals()?;
                io::stdout().write_all(
                    b"ignored SIGCHLD refused\ncustom SIGCHLD refused\nSA_NOCLDWAIT refused\n",
                )?;
                Ok(0)
            }
            #[cfg(not(target_os = "linux"))]
            Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "SIGCHLD is a Linux-only fixture",
            ))
        }
        CompileProcessMode::InheritedPipeGrandchild => {
            let mut grandchild = std::process::Command::new(verified_probe_executable()?)
                .arg(GRANDCHILD_ARGUMENT)
                .env_clear()
                .stdin(std::process::Stdio::null())
                .stdout(std::process::Stdio::inherit())
                .stderr(std::process::Stdio::inherit())
                .spawn()?;
            // Deliberately leave the fixed grandchild's inherited output open.
            // The owning group/job must terminate it after this leader exits.
            writeln!(io::stdout(), "grandchild={}", grandchild.id())?;
            let _ = grandchild.try_wait()?;
            Ok(0)
        }
    }
}

#[derive(Default)]
struct Capture {
    bytes: Vec<u8>,
    eof: bool,
    exceeded: bool,
    failed: bool,
}

impl Capture {
    fn append(&mut self, bytes: &[u8]) {
        let remaining = MAX_COMPILE_PROCESS_STREAM_BYTES.saturating_sub(self.bytes.len());
        self.exceeded |= bytes.len() > remaining;
        self.bytes
            .extend_from_slice(&bytes[..bytes.len().min(remaining)]);
    }

    fn pump(
        &mut self,
        mut read: impl FnMut(&mut [u8]) -> io::Result<Option<usize>>,
    ) -> io::Result<()> {
        if self.eof || self.failed {
            return Ok(());
        }
        for _ in 0..READS_PER_TICK {
            let mut buffer = [0u8; READ_CHUNK];
            match read(&mut buffer) {
                Ok(Some(0)) => {
                    self.eof = true;
                    break;
                }
                Ok(Some(count)) if count <= buffer.len() => self.append(&buffer[..count]),
                Ok(None) => break,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                result => {
                    self.failed = true;
                    return Err(match result {
                        Err(error) => error,
                        _ => io::Error::new(
                            io::ErrorKind::InvalidData,
                            "Pipe reader exceeded its chunk",
                        ),
                    });
                }
            }
        }
        Ok(())
    }
}

#[cfg(target_os = "linux")]
use linux as native;
#[cfg(windows)]
use windows as native;

#[cfg(target_os = "linux")]
mod linux {
    use super::*;
    use std::io::Read;
    use std::os::fd::AsRawFd;
    use std::os::unix::process::CommandExt;
    use std::process::{Child, Command, Stdio};

    fn waitable_sigchld(handler: usize, flags: libc::c_int) -> bool {
        handler == libc::SIG_DFL && flags & libc::SA_NOCLDWAIT == 0
    }

    fn require_waitable_children() -> Result<(), String> {
        // SAFETY: sigaction is a C output record valid when zeroed. A null
        // replacement pointer queries disposition without changing any signal.
        let mut disposition: libc::sigaction = unsafe { std::mem::zeroed() };
        // SAFETY: the initialized output record is writable for this query.
        if unsafe { libc::sigaction(libc::SIGCHLD, std::ptr::null(), &mut disposition) } != 0 {
            return Err(format!(
                "Cannot query SIGCHLD disposition: {}",
                io::Error::last_os_error()
            ));
        }
        if !waitable_sigchld(disposition.sa_sigaction, disposition.sa_flags) {
            return Err("Refusing non-waitable SIGCHLD disposition before fixture spawn".into());
        }
        Ok(())
    }

    pub(super) fn verify_sigchld_refusals() -> io::Result<()> {
        // This fixed child-only role has no children or worker threads of its
        // own. Change only its signal disposition, then restore on every path.
        struct SignalGuard(libc::sigaction);
        impl Drop for SignalGuard {
            fn drop(&mut self) {
                // SAFETY: restore the previously queried valid signal action
                // in this isolated single-threaded fixture child process.
                unsafe {
                    libc::sigaction(libc::SIGCHLD, &self.0, std::ptr::null_mut());
                }
            }
        }
        // SAFETY: sigaction output records and an empty sigset are zeroable C
        // structures; all FFI pointers below reference initialized live records.
        let mut original: libc::sigaction = unsafe { std::mem::zeroed() };
        // SAFETY: query only the writable original action; no disposition changes.
        if unsafe { libc::sigaction(libc::SIGCHLD, std::ptr::null(), &mut original) } != 0 {
            return Err(io::Error::last_os_error());
        }
        let _restore = SignalGuard(original);
        // The empty native handler performs no operation if a signal arrives.
        extern "C" fn custom_handler(_signal: libc::c_int) {}
        for (handler, flags) in [
            (libc::SIG_IGN, 0),
            (custom_handler as *const () as usize, 0),
            (libc::SIG_DFL, libc::SA_NOCLDWAIT),
        ] {
            // SAFETY: the C input record is zeroable and then initialized with
            // a valid handler, flags and an explicitly empty signal mask.
            let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
            action.sa_sigaction = handler;
            action.sa_flags = flags;
            // SAFETY: both calls operate only on this fixed child process's
            // SIGCHLD action, before it has spawned any children.
            if unsafe { libc::sigemptyset(&mut action.sa_mask) } != 0
                || unsafe { libc::sigaction(libc::SIGCHLD, &action, std::ptr::null_mut()) } != 0
            {
                return Err(io::Error::last_os_error());
            }
            match run_compile_process_fixture(CompileProcessMode::Success, &AtomicBool::new(false))
            {
                Err(issue) if issue.starts_with("Refusing non-waitable SIGCHLD") => {}
                _ => {
                    return Err(io::Error::other(
                        "Non-waitable SIGCHLD did not refuse before spawn",
                    ))
                }
            }
        }
        Ok(())
    }

    struct Subreaper {
        previous: libc::c_int,
        restored: bool,
    }

    impl Subreaper {
        fn acquire() -> io::Result<Self> {
            let mut previous = 0;
            // SAFETY: PR_GET_CHILD_SUBREAPER writes one initialized c_int;
            // PR_SET_CHILD_SUBREAPER changes only this dedicated probe process.
            unsafe {
                if libc::prctl(libc::PR_GET_CHILD_SUBREAPER, &mut previous, 0, 0, 0) != 0
                    || libc::prctl(libc::PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0
                {
                    return Err(io::Error::last_os_error());
                }
            }
            Ok(Self {
                previous,
                restored: false,
            })
        }

        fn restore(&mut self) -> io::Result<()> {
            if !self.restored {
                // SAFETY: restoring the observed subreaper flag changes only
                // this standalone process, after every owned child is reaped.
                if unsafe { libc::prctl(libc::PR_SET_CHILD_SUBREAPER, self.previous, 0, 0, 0) } != 0
                {
                    return Err(io::Error::last_os_error());
                }
                self.restored = true;
            }
            Ok(())
        }
    }

    impl Drop for Subreaper {
        fn drop(&mut self) {
            let _ = self.restore();
        }
    }

    pub(super) struct Owner {
        child: Child,
        group: libc::pid_t,
        subreaper: Subreaper,
        terminated: bool,
        leader_reaped: bool,
        reservation_lost: bool,
        reaped: usize,
        complete: bool,
    }

    impl Owner {
        pub(super) fn preflight() -> Result<(), String> {
            require_waitable_children()
        }

        pub(super) fn spawn(
            executable: &std::path::Path,
            mode: CompileProcessMode,
        ) -> Result<Self, String> {
            let subreaper =
                Subreaper::acquire().map_err(|e| format!("Cannot set fixture subreaper: {e}"))?;
            let mut command = Command::new(executable);
            command
                .arg(CHILD_ARGUMENT)
                .arg(mode.literal())
                .env_clear()
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped());
            // SAFETY: the fork child performs only async-signal-safe setsid;
            // it starts a session/group before executing any fixture role.
            unsafe {
                command.pre_exec(|| {
                    if libc::setsid() < 0 {
                        Err(io::Error::last_os_error())
                    } else {
                        Ok(())
                    }
                });
            }
            let child = command
                .spawn()
                .map_err(|e| format!("Cannot spawn fixed self child: {e}"))?;
            let group = i32::try_from(child.id()).map_err(|_| "Invalid native child PID")?;
            let mut owner = Self {
                child,
                group,
                subreaper,
                terminated: false,
                leader_reaped: false,
                reservation_lost: false,
                reaped: 0,
                complete: false,
            };
            let configured = (|| {
                nonblocking(
                    owner
                        .child
                        .stdout
                        .as_ref()
                        .ok_or_else(|| io::Error::other("Missing stdout pipe"))?
                        .as_raw_fd(),
                )?;
                nonblocking(
                    owner
                        .child
                        .stderr
                        .as_ref()
                        .ok_or_else(|| io::Error::other("Missing stderr pipe"))?
                        .as_raw_fd(),
                )
            })();
            if let Err(error) = configured {
                let _ = owner.terminate();
                let deadline = Instant::now() + CLEANUP_TIMEOUT;
                while Instant::now() < deadline && !owner.cleanup_step().unwrap_or(false) {
                    std::thread::sleep(POLL_INTERVAL);
                }
                if !owner.complete {
                    std::mem::forget(owner);
                }
                return Err(format!(
                    "Cannot configure nonblocking fixture pipes: {error}"
                ));
            }
            Ok(owner)
        }

        pub(super) fn pid(&self) -> u32 {
            self.group as u32
        }
        pub(super) fn descendants_reaped(&self) -> usize {
            self.reaped
        }
        pub(super) fn read_stdout(&mut self, buffer: &mut [u8]) -> io::Result<Option<usize>> {
            read(
                self.child
                    .stdout
                    .as_mut()
                    .ok_or_else(|| io::Error::other("Missing stdout pipe"))?,
                buffer,
            )
        }
        pub(super) fn read_stderr(&mut self, buffer: &mut [u8]) -> io::Result<Option<usize>> {
            read(
                self.child
                    .stderr
                    .as_mut()
                    .ok_or_else(|| io::Error::other("Missing stderr pipe"))?,
                buffer,
            )
        }

        pub(super) fn poll_exit(&mut self) -> Result<Option<i32>, String> {
            // SAFETY: siginfo_t is a C output record valid when zeroed; waitid
            // is restricted to the unreaped child PID owned by this instance.
            let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
            // SAFETY: the output record is writable and WNOWAIT deliberately
            // retains the session leader to prevent PID/PGID reuse before kill.
            if unsafe {
                libc::waitid(
                    libc::P_PID,
                    self.group as libc::id_t,
                    &mut info,
                    libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
                )
            } != 0
            {
                let error = io::Error::last_os_error();
                if error.raw_os_error() == Some(libc::ECHILD) {
                    self.reservation_lost = true;
                }
                return Err(error.to_string());
            }
            // SAFETY: successful waitid initializes the SIGCHLD-compatible
            // union fields; a zero si_pid means no child exit was observed.
            let pid = unsafe { info.si_pid() };
            if pid == 0 {
                return Ok(None);
            }
            // SAFETY: this is the same initialized SIGCHLD output record.
            let status = unsafe { info.si_status() };
            Ok(Some(if info.si_code == libc::CLD_EXITED {
                status
            } else {
                128 + status
            }))
        }

        pub(super) fn terminate(&mut self) -> Result<(), String> {
            if !self.terminated {
                if self.leader_reaped || self.reservation_lost {
                    return Err("Refusing to signal a group without its reserved leader".into());
                }
                // Re-observe waitability before the sole signal too, including
                // deadline/cancellation paths which need not have polled exit.
                // ECHILD marks reservation lost and no group signal follows.
                require_waitable_children()?;
                self.poll_exit()?;
                // SAFETY: positive group is the unique child-created session,
                // whose unreaped leader reserves its numeric identity. No
                // external PID/list/renderer value can reach this signal.
                let result = unsafe { libc::kill(-self.group, libc::SIGKILL) };
                if result != 0 && io::Error::last_os_error().raw_os_error() != Some(libc::ESRCH) {
                    return Err(io::Error::last_os_error().to_string());
                }
                self.terminated = true;
            }
            Ok(())
        }

        pub(super) fn cleanup_step(&mut self) -> Result<bool, String> {
            if self.complete {
                return Ok(true);
            }
            if !self.terminated {
                return Err("Owned group was not terminated before reaping".into());
            }
            if !self.leader_reaped {
                self.leader_reaped = self.child.try_wait().map_err(|e| e.to_string())?.is_some();
                if !self.leader_reaped {
                    return Ok(false);
                }
            }
            for _ in 0..16 {
                let mut status = 0;
                // SAFETY: negative group restricts waitpid to this owner's
                // direct/adopted children; never waitpid(-1) or unrelated PIDs.
                let reaped = unsafe { libc::waitpid(-self.group, &mut status, libc::WNOHANG) };
                if reaped > 0 {
                    self.reaped += 1;
                    continue;
                }
                if reaped == 0 {
                    return Ok(false);
                }
                let issue = io::Error::last_os_error();
                if issue.raw_os_error() == Some(libc::EINTR) {
                    continue;
                }
                if issue.raw_os_error() != Some(libc::ECHILD) {
                    return Err(issue.to_string());
                }
                // SAFETY: signal zero observes only group existence and never
                // changes state, even if a numeric group later gets reused.
                let exists = unsafe { libc::kill(-self.group, 0) };
                if exists == -1 && io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH) {
                    self.complete = true;
                    return Ok(true);
                }
                return Ok(false);
            }
            Ok(false)
        }

        pub(super) fn finish_scope(&mut self) -> Result<(), String> {
            if !self.complete {
                return Err("Cannot release incomplete native fixture scope".into());
            }
            self.subreaper
                .restore()
                .map_err(|e| format!("Cannot restore subreaper state: {e}"))
        }
    }

    impl Drop for Owner {
        fn drop(&mut self) {
            if !self.complete {
                let _ = self.terminate();
                let _ = self.cleanup_step();
            }
        }
    }

    fn nonblocking(fd: libc::c_int) -> io::Result<()> {
        // SAFETY: fd is a live, exclusively owned parent-side pipe descriptor.
        let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
        // SAFETY: preserving existing flags and setting O_NONBLOCK does not
        // transfer descriptor ownership or affect any unrelated descriptor.
        if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }

    fn read(reader: &mut impl Read, buffer: &mut [u8]) -> io::Result<Option<usize>> {
        match reader.read(buffer) {
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => Ok(None),
            result => result.map(Some),
        }
    }

    #[cfg(test)]
    mod disposition_tests {
        use super::*;

        #[test]
        fn ignored_custom_and_no_child_wait_dispositions_cannot_reserve_leader_pids() {
            assert!(waitable_sigchld(libc::SIG_DFL, 0));
            assert!(!waitable_sigchld(libc::SIG_IGN, 0));
            assert!(!waitable_sigchld(12345, 0));
            assert!(!waitable_sigchld(libc::SIG_DFL, libc::SA_NOCLDWAIT));
            assert!(!waitable_sigchld(libc::SIG_IGN, libc::SA_NOCLDWAIT));
        }
    }
}

#[cfg(windows)]
mod windows {
    use super::*;
    use std::fs::{File, OpenOptions};
    use std::io::Read;
    use std::os::windows::ffi::OsStrExt;
    use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
    use std::os::windows::io::{AsRawHandle, FromRawHandle};
    use windows_sys::Win32::Foundation::{
        CloseHandle, SetHandleInformation, ERROR_BROKEN_PIPE, ERROR_INSUFFICIENT_BUFFER,
        ERROR_PIPE_NOT_CONNECTED, HANDLE, HANDLE_FLAG_INHERIT, WAIT_OBJECT_0, WAIT_TIMEOUT,
    };
    use windows_sys::Win32::Security::SECURITY_ATTRIBUTES;
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_READ,
    };
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectBasicAccountingInformation,
        JobObjectExtendedLimitInformation, QueryInformationJobObject, SetInformationJobObject,
        TerminateJobObject, JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    use windows_sys::Win32::System::Pipes::{CreatePipe, PeekNamedPipe};
    use windows_sys::Win32::System::Threading::{
        CreateProcessW, DeleteProcThreadAttributeList, GetExitCodeProcess,
        InitializeProcThreadAttributeList, ResumeThread, TerminateProcess,
        UpdateProcThreadAttribute, WaitForSingleObject, CREATE_NO_WINDOW, CREATE_SUSPENDED,
        CREATE_UNICODE_ENVIRONMENT, EXTENDED_STARTUPINFO_PRESENT, LPPROC_THREAD_ATTRIBUTE_LIST,
        PROCESS_INFORMATION, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, STARTF_USESTDHANDLES,
        STARTUPINFOEXW,
    };

    struct Handle(HANDLE);

    impl Handle {
        fn new(handle: HANDLE) -> io::Result<Self> {
            if handle.is_null() || handle == windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE {
                Err(io::Error::last_os_error())
            } else {
                Ok(Self(handle))
            }
        }

        fn into_file(self) -> File {
            let handle = self.0;
            std::mem::forget(self);
            // SAFETY: this transfers the one owned real pipe handle to File;
            // the Handle destructor is suppressed, avoiding a double close.
            unsafe { File::from_raw_handle(handle) }
        }
    }

    impl Drop for Handle {
        fn drop(&mut self) {
            // SAFETY: this wrapper owns one valid real handle and never clones
            // or exposes it outside the synchronous fixture owner.
            unsafe {
                CloseHandle(self.0);
            }
        }
    }

    struct Attributes<'a> {
        storage: Vec<usize>,
        initialized: bool,
        _handles: std::marker::PhantomData<&'a [HANDLE]>,
    }

    impl<'a> Attributes<'a> {
        fn new(handles: &'a [HANDLE; 3]) -> io::Result<Self> {
            let mut size = 0;
            // SAFETY: the initial size query takes no buffer and writes one
            // usize. Windows specifies ERROR_INSUFFICIENT_BUFFER for it.
            let initial =
                unsafe { InitializeProcThreadAttributeList(std::ptr::null_mut(), 1, 0, &mut size) };
            if initial != 0
                || io::Error::last_os_error().raw_os_error()
                    != Some(ERROR_INSUFFICIENT_BUFFER as i32)
                || size == 0
                || size > 64 * 1024
            {
                return Err(io::Error::other(
                    "Unexpected process attribute-list size result",
                ));
            }
            let words = size.div_ceil(std::mem::size_of::<usize>());
            let mut result = Self {
                storage: vec![0; words],
                initialized: false,
                _handles: std::marker::PhantomData,
            };
            // SAFETY: usize storage is aligned for this opaque native record,
            // at least size bytes long, and remains allocated until deletion.
            if unsafe { InitializeProcThreadAttributeList(result.pointer(), 1, 0, &mut size) } == 0
            {
                return Err(io::Error::last_os_error());
            }
            result.initialized = true;
            // SAFETY: all three listed pipe handles are real, inheritable and
            // live through CreateProcessW. No other parent handles are listed.
            let updated = unsafe {
                UpdateProcThreadAttribute(
                    result.pointer(),
                    0,
                    PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
                    handles.as_ptr().cast(),
                    std::mem::size_of_val(handles),
                    std::ptr::null_mut(),
                    std::ptr::null(),
                )
            };
            if updated == 0 {
                return Err(io::Error::last_os_error());
            }
            Ok(result)
        }

        fn pointer(&mut self) -> LPPROC_THREAD_ATTRIBUTE_LIST {
            self.storage.as_mut_ptr().cast()
        }
    }

    impl Drop for Attributes<'_> {
        fn drop(&mut self) {
            // SAFETY: this is the one initialized opaque attribute list, and
            // its backing aligned storage outlives this native deletion.
            if self.initialized {
                unsafe {
                    DeleteProcThreadAttributeList(self.pointer());
                }
            }
        }
    }

    fn pipe() -> io::Result<(Handle, Handle)> {
        let attributes = SECURITY_ATTRIBUTES {
            nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: std::ptr::null_mut(),
            bInheritHandle: 1,
        };
        let mut read = std::ptr::null_mut();
        let mut write = std::ptr::null_mut();
        // SAFETY: initialized security attributes and two writable handle
        // outputs are valid for the call. Success transfers both real handles.
        if unsafe { CreatePipe(&mut read, &mut write, &attributes, READ_CHUNK as u32) } == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok((Handle::new(read)?, Handle::new(write)?))
    }

    fn parent_only(handle: &Handle) -> io::Result<()> {
        // SAFETY: the flag is changed only on this exclusively owned pipe end.
        if unsafe { SetHandleInformation(handle.0, HANDLE_FLAG_INHERIT, 0) } == 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(())
        }
    }

    pub(super) struct Owner {
        job: Handle,
        process: Handle,
        stdout: File,
        stderr: File,
        _executable: File,
        pid: u32,
        assigned: bool,
        terminated: bool,
        complete: bool,
    }

    impl Owner {
        pub(super) fn preflight() -> Result<(), String> {
            Ok(())
        }

        pub(super) fn spawn(
            executable: &std::path::Path,
            mode: CompileProcessMode,
        ) -> Result<Self, String> {
            let executable_pin = OpenOptions::new()
                .read(true)
                .share_mode(FILE_SHARE_READ)
                .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
                .open(executable)
                .map_err(|e| e.to_string())?;
            let metadata = executable_pin.metadata().map_err(|e| e.to_string())?;
            if !metadata.is_file() || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
            {
                return Err("The fixed current executable is not a regular unreparsed file".into());
            }
            // This sharing mode denies write/delete of the selected current
            // executable during this fixture; it does not pin DLL dependencies
            // or certify a later arbitrary tool's executable identity/lifetime.
            // Unlike Linux /proc/self/exe, it also does not independently bind
            // a path replaced before this open to the already-running image.
            // This remains fixed-name fixture/job-lifetime evidence only.
            // SAFETY: no name/security pointer is supplied; a successful call
            // returns one private, noninheritable job handle owned below.
            let job = Handle::new(unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) })
                .map_err(|e| e.to_string())?;
            let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            // SAFETY: limits is a valid initialized native input structure;
            // its exact size is passed, and job is a live owned handle.
            if unsafe {
                SetInformationJobObject(
                    job.0,
                    JobObjectExtendedLimitInformation,
                    (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                    std::mem::size_of_val(&limits) as u32,
                )
            } == 0
            {
                return Err(format!(
                    "Cannot set kill-on-close fixture job: {}",
                    io::Error::last_os_error()
                ));
            }

            let (stdout_read, stdout_write) = pipe().map_err(|e| e.to_string())?;
            let (stderr_read, stderr_write) = pipe().map_err(|e| e.to_string())?;
            let (stdin_read, stdin_write) = pipe().map_err(|e| e.to_string())?;
            parent_only(&stdout_read).map_err(|e| e.to_string())?;
            parent_only(&stderr_read).map_err(|e| e.to_string())?;
            parent_only(&stdin_write).map_err(|e| e.to_string())?;
            drop(stdin_write); // The fixed child receives EOF, never commands.
            let inherited = [stdin_read.0, stdout_write.0, stderr_write.0];
            let mut attributes = Attributes::new(&inherited).map_err(|e| e.to_string())?;
            let mut startup = STARTUPINFOEXW::default();
            startup.StartupInfo.cb = std::mem::size_of::<STARTUPINFOEXW>() as u32;
            startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
            startup.StartupInfo.hStdInput = stdin_read.0;
            startup.StartupInfo.hStdOutput = stdout_write.0;
            startup.StartupInfo.hStdError = stderr_write.0;
            startup.lpAttributeList = attributes.pointer();
            let application: Vec<u16> = executable
                .as_os_str()
                .encode_wide()
                .chain(Some(0))
                .collect();
            // argv[0] is a fixed label; lpApplicationName alone selects the
            // literal verified absolute image, preserving Unicode/space paths.
            let mut command: Vec<u16> =
                format!("compile_process_probe {CHILD_ARGUMENT} {}", mode.literal())
                    .encode_utf16()
                    .chain(Some(0))
                    .collect();
            let environment = [0u16; 2];
            let mut info = PROCESS_INFORMATION::default();
            // SAFETY: all UTF-16 strings are terminated; command is writable;
            // environment is an empty double-NUL UTF-16 block; startup and the
            // explicit three-handle list remain live. CREATE_SUSPENDED forbids
            // child user code until successful job assignment below.
            let created = unsafe {
                CreateProcessW(
                    application.as_ptr(),
                    command.as_mut_ptr(),
                    std::ptr::null(),
                    std::ptr::null(),
                    1,
                    CREATE_SUSPENDED
                        | CREATE_NO_WINDOW
                        | CREATE_UNICODE_ENVIRONMENT
                        | EXTENDED_STARTUPINFO_PRESENT,
                    environment.as_ptr().cast(),
                    std::ptr::null(),
                    &startup.StartupInfo,
                    &mut info,
                )
            };
            if created == 0 {
                return Err(format!(
                    "Cannot create suspended fixture child: {}",
                    io::Error::last_os_error()
                ));
            }
            let process = Handle::new(info.hProcess).map_err(|e| e.to_string())?;
            let thread = Handle::new(info.hThread).map_err(|e| e.to_string())?;
            let mut owner = Self {
                job,
                process,
                stdout: stdout_read.into_file(),
                stderr: stderr_read.into_file(),
                _executable: executable_pin,
                pid: info.dwProcessId,
                assigned: false,
                terminated: false,
                complete: false,
            };
            // SAFETY: this live private job and still-suspended owned child are
            // valid. No breakaway flags are requested. Unsupported/nested job
            // assignment refuses; the child is killed without ever resuming.
            if unsafe { AssignProcessToJobObject(owner.job.0, owner.process.0) } == 0 {
                let issue = format!(
                    "Cannot contain suspended fixture child in its job: {}",
                    io::Error::last_os_error()
                );
                owner.abort_start();
                return Err(issue);
            }
            owner.assigned = true;
            // SAFETY: the returned primary thread still belongs to the owned
            // child and the child is already inside the kill-on-close job.
            if unsafe { ResumeThread(thread.0) } != 1 {
                let issue = format!(
                    "Cannot resume contained fixture child: {}",
                    io::Error::last_os_error()
                );
                owner.abort_start();
                return Err(issue);
            }
            drop(thread);
            drop(attributes);
            drop(stdin_read);
            drop(stdout_write);
            drop(stderr_write);
            Ok(owner)
        }

        fn abort_start(mut self) {
            let _ = self.terminate();
            let deadline = Instant::now() + CLEANUP_TIMEOUT;
            while Instant::now() < deadline && !self.cleanup_step().unwrap_or(false) {
                std::thread::sleep(POLL_INTERVAL);
            }
            if !self.complete {
                std::mem::forget(self);
            }
        }

        pub(super) fn pid(&self) -> u32 {
            self.pid
        }
        pub(super) fn descendants_reaped(&self) -> usize {
            0
        }
        pub(super) fn read_stdout(&mut self, buffer: &mut [u8]) -> io::Result<Option<usize>> {
            read_pipe(&mut self.stdout, buffer)
        }
        pub(super) fn read_stderr(&mut self, buffer: &mut [u8]) -> io::Result<Option<usize>> {
            read_pipe(&mut self.stderr, buffer)
        }

        pub(super) fn poll_exit(&mut self) -> Result<Option<i32>, String> {
            // SAFETY: this is an owned process handle; zero timeout never waits.
            match unsafe { WaitForSingleObject(self.process.0, 0) } {
                WAIT_TIMEOUT => Ok(None),
                WAIT_OBJECT_0 => {
                    let mut code = 0;
                    // SAFETY: the live process handle and writable DWORD output
                    // remain valid. A signalled process has a final exit code.
                    if unsafe { GetExitCodeProcess(self.process.0, &mut code) } == 0 {
                        return Err(io::Error::last_os_error().to_string());
                    }
                    Ok(Some(code as i32))
                }
                _ => Err(io::Error::last_os_error().to_string()),
            }
        }

        pub(super) fn terminate(&mut self) -> Result<(), String> {
            if !self.terminated {
                // SAFETY: an assigned child and all descendants belong to this
                // private job. Before assignment, this process is still fully
                // suspended, so terminating its handle cannot miss descendants.
                let result = unsafe {
                    if self.assigned {
                        TerminateJobObject(self.job.0, 137)
                    } else {
                        TerminateProcess(self.process.0, 137)
                    }
                };
                if result == 0 {
                    return Err(io::Error::last_os_error().to_string());
                }
                self.terminated = true;
            }
            Ok(())
        }

        pub(super) fn cleanup_step(&mut self) -> Result<bool, String> {
            if self.complete {
                return Ok(true);
            }
            if !self.terminated {
                return Err("Fixture job was not terminated before cleanup".into());
            }
            if self.poll_exit()?.is_none() {
                return Ok(false);
            }
            let mut accounting = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
            // SAFETY: job is live, output is initialized/aligned and its exact
            // size is passed. ActiveProcesses includes inherited descendants.
            if unsafe {
                QueryInformationJobObject(
                    self.job.0,
                    JobObjectBasicAccountingInformation,
                    (&mut accounting as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
                    std::mem::size_of_val(&accounting) as u32,
                    std::ptr::null_mut(),
                )
            } == 0
            {
                return Err(io::Error::last_os_error().to_string());
            }
            self.complete = accounting.ActiveProcesses == 0;
            Ok(self.complete)
        }

        pub(super) fn finish_scope(&mut self) -> Result<(), String> {
            if self.complete {
                Ok(())
            } else {
                Err("Cannot release an active fixture job".into())
            }
        }
    }

    impl Drop for Owner {
        fn drop(&mut self) {
            if !self.complete {
                let _ = self.terminate();
            }
            // The one private job handle additionally kills any assigned
            // processes on close; no job/child handles reach another process.
        }
    }

    fn read_pipe(pipe: &mut File, buffer: &mut [u8]) -> io::Result<Option<usize>> {
        let mut available = 0;
        // SAFETY: File owns this real pipe read handle exclusively. This
        // zero-byte peek cannot block and writes one initialized DWORD.
        let peeked = unsafe {
            PeekNamedPipe(
                pipe.as_raw_handle(),
                std::ptr::null_mut(),
                0,
                std::ptr::null_mut(),
                &mut available,
                std::ptr::null_mut(),
            )
        };
        if peeked == 0 {
            let issue = io::Error::last_os_error();
            if matches!(issue.raw_os_error(), Some(code) if code == ERROR_BROKEN_PIPE as i32 || code == ERROR_PIPE_NOT_CONNECTED as i32)
            {
                return Ok(Some(0));
            }
            return Err(issue);
        }
        if available == 0 {
            return Ok(None);
        }
        // No competing reader exists. Reading at most the already-buffered
        // amount cannot wait for another writer or descendant to produce data.
        let count = buffer.len().min(available as usize);
        pipe.read(&mut buffer[..count]).map(Some)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stream_capture_preserves_binary_utf8_and_mixed_newlines() {
        let mut capture = Capture::default();
        capture.append(b"\xef\xbb\xbf\xff\r\n\xc3");
        capture.append(b"\xa9\n");
        assert_eq!(capture.bytes, b"\xef\xbb\xbf\xff\r\n\xc3\xa9\n");
        assert!(!capture.exceeded);
    }

    #[test]
    fn stream_cap_retains_at_most_64_kib_while_marking_excess_for_refusal() {
        let mut capture = Capture::default();
        capture.append(&vec![b'x'; 64 * 1024]);
        assert_eq!(capture.bytes.len(), 64 * 1024);
        assert!(!capture.exceeded);
        capture.append(b"more");
        capture.append(&vec![b'y'; 64 * 1024]);
        assert_eq!(capture.bytes.len(), 64 * 1024);
        assert!(capture.exceeded);
        assert!(capture.bytes.iter().all(|&b| b == b'x'));
    }

    #[test]
    fn stdout_and_stderr_budgets_are_separate() {
        let mut stdout = Capture::default();
        let mut stderr = Capture::default();
        stdout.append(&vec![b'o'; 64 * 1024 + 1]);
        stderr.append(b"error\n");
        assert!(stdout.exceeded);
        assert!(!stderr.exceeded);
        assert_eq!(stderr.bytes, b"error\n");
    }

    #[test]
    fn fixed_modes_cannot_parse_commands_or_extra_arguments() {
        for input in [
            "",
            "success --extra",
            "hang;command",
            "../success",
            "${ENV}",
            "Success",
            "unknown",
        ] {
            assert_eq!(CompileProcessMode::parse(input), None);
        }
        for mode in [
            CompileProcessMode::Success,
            CompileProcessMode::Nonzero,
            CompileProcessMode::FloodStdout,
            CompileProcessMode::FloodStderr,
            CompileProcessMode::FloodBoth,
            CompileProcessMode::Hang,
            CompileProcessMode::CancelAfterStart,
            CompileProcessMode::SigchldRefusals,
            CompileProcessMode::InheritedPipeGrandchild,
        ] {
            assert_eq!(CompileProcessMode::parse(mode.literal()), Some(mode));
        }
    }

    #[test]
    fn library_test_executable_cannot_be_adopted_as_a_probe() {
        assert!(verified_probe_executable().is_err());
    }
}
