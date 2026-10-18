//! Read-only evidence for one explicitly requested native PID.
//!
//! Creation time uses the same Unix epoch seconds as legacy psutil ownership
//! records. An inaccessible process is never reported as exited. Evidence is a
//! point-in-time observation, not a lease: registration still needs the shared
//! settings transaction and source writes need independent ownership checks.

use serde::Serialize;

#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ProcessIdentity {
    pub pid: u32,
    pub create_time: f64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProcessQueryFailure {
    InvalidPid,
    PermissionDenied,
    Unavailable,
    Malformed,
    IdentityChanged,
    /// A zombie/dead PID remains present in psutil. Preserve the claim on this
    /// uncertain boundary instead of treating its registration as free.
    ZombieOrDead,
    UnsupportedPlatform,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum ProcessEvidence {
    Running {
        identity: ProcessIdentity,
    },
    Exited {
        pid: u32,
    },
    Unknown {
        pid: u32,
        reason: ProcessQueryFailure,
    },
}

/// Query only this PID; no inventory, shell, signal, or settings access occurs.
pub fn query_process(pid: u32) -> ProcessEvidence {
    if pid == 0 {
        return unknown(pid, ProcessQueryFailure::InvalidPid);
    }
    #[cfg(target_os = "linux")]
    {
        linux::query(pid)
    }
    #[cfg(windows)]
    {
        windows::query(pid)
    }
    #[cfg(not(any(target_os = "linux", windows)))]
    {
        unknown(pid, ProcessQueryFailure::UnsupportedPlatform)
    }
}

pub fn own_process_identity() -> ProcessEvidence {
    query_process(std::process::id())
}

fn unknown(pid: u32, reason: ProcessQueryFailure) -> ProcessEvidence {
    ProcessEvidence::Unknown { pid, reason }
}

#[cfg(any(target_os = "linux", test))]
mod linux {
    use super::*;
    use std::io::{self, Read};

    const MAX_PROCESS_STAT: usize = 8192;
    const MAX_BOOT_STAT: usize = 1024 * 1024;

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    struct ProcStat {
        pid: u32,
        state: u8,
        start_ticks: u64,
    }

    fn bounded_read(reader: impl Read, limit: usize) -> io::Result<Vec<u8>> {
        let mut bytes = Vec::new();
        reader.take((limit + 1) as u64).read_to_end(&mut bytes)?;
        if bytes.len() > limit {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "Native process evidence exceeds its bound",
            ));
        }
        Ok(bytes)
    }

    fn number(bytes: &[u8]) -> Option<u64> {
        if bytes.is_empty() || !bytes.iter().all(u8::is_ascii_digit) {
            return None;
        }
        std::str::from_utf8(bytes).ok()?.parse().ok()
    }

    fn process_stat(bytes: &[u8], pid: u32) -> Option<ProcStat> {
        if bytes.len() > MAX_PROCESS_STAT {
            return None;
        }
        // comm is arbitrary bytes and may contain spaces, parentheses, or a
        // newline. The last ')' ends it; splitting the whole row is incorrect.
        let open = bytes.iter().position(|&byte| byte == b'(')?;
        let close = bytes.iter().rposition(|&byte| byte == b')')?;
        if close <= open || bytes.get(open.wrapping_sub(1)) != Some(&b' ') {
            return None;
        }
        let actual_pid = number(&bytes[..open - 1])?;
        if actual_pid != u64::from(pid) || bytes.get(close + 1) != Some(&b' ') {
            return None;
        }
        let mut fields = bytes[close + 2..]
            .split(u8::is_ascii_whitespace)
            .filter(|field| !field.is_empty());
        let state = fields.next()?;
        if state.len() != 1 || !b"RSDZTtXxKWPI".contains(&state[0]) {
            return None;
        }
        // The remaining fields start at field 4; starttime is field 22.
        let start_ticks = number(fields.nth(18)?)?;
        Some(ProcStat {
            pid,
            state: state[0],
            start_ticks,
        })
    }

    fn boot_time(bytes: &[u8]) -> Option<u64> {
        if bytes.len() > MAX_BOOT_STAT {
            return None;
        }
        let mut result = None;
        for line in bytes.split(|&byte| byte == b'\n') {
            let mut fields = line
                .split(u8::is_ascii_whitespace)
                .filter(|field| !field.is_empty());
            if fields.next() == Some(b"btime".as_slice()) {
                if result.is_some() {
                    return None;
                }
                let value = number(fields.next()?)?;
                if value == 0 || fields.next().is_some() {
                    return None;
                }
                result = Some(value);
            }
        }
        result
    }

    fn combine(pid: u32, first: ProcStat, second: ProcStat, boot: u64, hz: u64) -> ProcessEvidence {
        if first.pid != pid || second.pid != pid || first.start_ticks != second.start_ticks {
            return unknown(pid, ProcessQueryFailure::IdentityChanged);
        }
        if b"ZXx".contains(&first.state) || b"ZXx".contains(&second.state) {
            return unknown(pid, ProcessQueryFailure::ZombieOrDead);
        }
        if hz == 0 || boot == 0 {
            return unknown(pid, ProcessQueryFailure::Malformed);
        }
        // Match psutil: process start ticks / SC_CLK_TCK + /proc/stat btime.
        let create_time = first.start_ticks as f64 / hz as f64 + boot as f64;
        if !create_time.is_finite() || create_time <= 0.0 {
            return unknown(pid, ProcessQueryFailure::Malformed);
        }
        ProcessEvidence::Running {
            identity: ProcessIdentity { pid, create_time },
        }
    }

    #[cfg(target_os = "linux")]
    fn read_stat(pid: u32) -> Result<ProcStat, ProcessEvidence> {
        let result = std::fs::File::open(format!("/proc/{pid}/stat"))
            .and_then(|file| bounded_read(file, MAX_PROCESS_STAT));
        match result {
            Ok(bytes) => process_stat(&bytes, pid)
                .ok_or_else(|| unknown(pid, ProcessQueryFailure::Malformed)),
            // hidepid can conceal a present process with ENOENT. Signal zero
            // does not send a signal; only ESRCH proves absence of this PID.
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                // SAFETY: pid was validated positive and within pid_t range;
                // signal zero performs only existence/permission checks.
                let result = unsafe { libc::kill(pid as libc::pid_t, 0) };
                let error = io::Error::last_os_error();
                if result == -1 && error.raw_os_error() == Some(libc::ESRCH) {
                    Err(ProcessEvidence::Exited { pid })
                } else {
                    let reason = if result == -1 && error.raw_os_error() == Some(libc::EPERM) {
                        ProcessQueryFailure::PermissionDenied
                    } else {
                        ProcessQueryFailure::Unavailable
                    };
                    Err(unknown(pid, reason))
                }
            }
            Err(error) => Err(unknown(pid, io_failure(&error))),
        }
    }

    fn io_failure(error: &io::Error) -> ProcessQueryFailure {
        match error.kind() {
            io::ErrorKind::PermissionDenied => ProcessQueryFailure::PermissionDenied,
            io::ErrorKind::InvalidData => ProcessQueryFailure::Malformed,
            _ => ProcessQueryFailure::Unavailable,
        }
    }

    #[cfg(target_os = "linux")]
    pub(super) fn query(pid: u32) -> ProcessEvidence {
        if pid > i32::MAX as u32 {
            return unknown(pid, ProcessQueryFailure::InvalidPid);
        }
        let first = match read_stat(pid) {
            Ok(stat) => stat,
            Err(evidence) => return evidence,
        };
        let bytes = match std::fs::File::open("/proc/stat")
            .and_then(|file| bounded_read(file, MAX_BOOT_STAT))
        {
            Ok(bytes) => bytes,
            Err(error) => return unknown(pid, io_failure(&error)),
        };
        let Some(boot) = boot_time(&bytes) else {
            return unknown(pid, ProcessQueryFailure::Malformed);
        };
        // SAFETY: sysconf has no pointer arguments and reads a fixed process
        // configuration constant. Never assume 100Hz across architectures.
        let hz = unsafe { libc::sysconf(libc::_SC_CLK_TCK) };
        if hz <= 0 {
            return unknown(pid, ProcessQueryFailure::Unavailable);
        }
        match read_stat(pid) {
            Ok(second) => combine(pid, first, second, boot, hz as u64),
            Err(evidence) => evidence,
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use std::io::Cursor;

        fn fixture(pid: u32, comm: &[u8], state: u8, ticks: &str) -> Vec<u8> {
            let mut bytes = format!("{pid} (").into_bytes();
            bytes.extend_from_slice(comm);
            bytes.extend_from_slice(
                format!(") {} {} {ticks} 0 0\n", state as char, ["0"; 18].join(" ")).as_bytes(),
            );
            bytes
        }

        #[test]
        fn comm_bytes_do_not_shift_the_start_time_field() {
            for comm in [b"plain".as_slice(), b"a ) ( name\n", b"\xff) byte name"] {
                let stat = process_stat(&fixture(42, comm, b'S', "12345"), 42).unwrap();
                assert_eq!(stat.start_ticks, 12345);
                assert_eq!(stat.pid, 42);
            }
        }

        #[test]
        fn malformed_truncated_or_wrong_pid_stat_is_unknown() {
            for bytes in [
                b"".as_slice(),
                b"42 no parentheses",
                b"42 (a) S 0",
                b"42 (a) SS 0",
            ] {
                assert!(process_stat(bytes, 42).is_none());
            }
            assert!(process_stat(&fixture(43, b"name", b'S', "123"), 42).is_none());
            for invalid in ["-1", "+3", "18446744073709551616", "NaN"] {
                assert!(process_stat(&fixture(42, b"name", b'S', invalid), 42).is_none());
            }
            assert!(process_stat(&vec![b')'; MAX_PROCESS_STAT + 1], 42).is_none());
        }

        #[test]
        fn boot_time_requires_exact_single_positive_record() {
            assert_eq!(
                boot_time(b"cpu 1 2 3\nbtime 1700000000\nprocesses 9\n"),
                Some(1_700_000_000)
            );
            for bytes in [
                b"btime 0\n".as_slice(),
                b"btime -1\n",
                b"btime 12 3\n",
                b"btime 123\nbtime 123\n",
                b"other 123\n",
            ] {
                assert_eq!(boot_time(bytes), None);
            }
        }

        #[test]
        fn clock_rate_and_epoch_conversion_match_known_psutil_values() {
            let first = ProcStat {
                pid: 42,
                state: b'S',
                start_ticks: 12345,
            };
            assert_eq!(
                combine(42, first, first, 1_700_000_000, 100),
                ProcessEvidence::Running {
                    identity: ProcessIdentity {
                        pid: 42,
                        create_time: 1_700_000_123.45
                    }
                }
            );
            assert_eq!(
                combine(42, first, first, 1_700_000_000, 250),
                ProcessEvidence::Running {
                    identity: ProcessIdentity {
                        pid: 42,
                        create_time: 1_700_000_049.38
                    }
                }
            );
            assert_eq!(
                combine(42, first, first, 1_700_000_000, 0),
                unknown(42, ProcessQueryFailure::Malformed)
            );
        }

        #[test]
        fn raced_identity_and_zombies_remain_conservative() {
            let first = ProcStat {
                pid: 42,
                state: b'S',
                start_ticks: 12345,
            };
            let recycled = ProcStat {
                start_ticks: 12346,
                ..first
            };
            assert_eq!(
                combine(42, first, recycled, 1_700_000_000, 100),
                unknown(42, ProcessQueryFailure::IdentityChanged)
            );
            for state in b"ZXx" {
                let zombie = ProcStat {
                    state: *state,
                    ..first
                };
                assert_eq!(
                    combine(42, first, zombie, 1_700_000_000, 100),
                    unknown(42, ProcessQueryFailure::ZombieOrDead)
                );
            }
        }

        #[test]
        fn proc_reads_and_permission_errors_are_bounded_and_conservative() {
            assert_eq!(bounded_read(Cursor::new(b"1234"), 4).unwrap(), b"1234");
            assert_eq!(
                bounded_read(Cursor::new(b"12345"), 4).unwrap_err().kind(),
                io::ErrorKind::InvalidData
            );
            assert_eq!(
                io_failure(&io::ErrorKind::PermissionDenied.into()),
                ProcessQueryFailure::PermissionDenied
            );
            assert_eq!(
                io_failure(&io::ErrorKind::NotFound.into()),
                ProcessQueryFailure::Unavailable
            );
        }
    }
}

#[cfg(any(windows, test))]
fn filetime_epoch_seconds(ticks: u64) -> Option<f64> {
    // Windows FILETIME: 100ns ticks since 1601-01-01; Unix epoch starts 1970.
    let unix_ticks = ticks.checked_sub(116_444_736_000_000_000)?;
    Some(unix_ticks as f64 / 10_000_000.0)
}

#[cfg(windows)]
mod windows {
    use super::*;
    use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
    use windows_sys::Win32::Foundation::{
        GetLastError, ERROR_ACCESS_DENIED, ERROR_INVALID_PARAMETER, FILETIME, WAIT_FAILED,
        WAIT_OBJECT_0, WAIT_TIMEOUT,
    };
    use windows_sys::Win32::System::Threading::{
        GetProcessTimes, OpenProcess, WaitForSingleObject, PROCESS_QUERY_LIMITED_INFORMATION,
        PROCESS_SYNCHRONIZE,
    };

    fn wait_state(handle: &OwnedHandle, pid: u32) -> Option<ProcessEvidence> {
        // SAFETY: owned process handle remains live for this nonblocking call.
        match unsafe { WaitForSingleObject(handle.as_raw_handle(), 0) } {
            WAIT_TIMEOUT => None,
            WAIT_OBJECT_0 => Some(ProcessEvidence::Exited { pid }),
            WAIT_FAILED => Some(unknown(pid, ProcessQueryFailure::Unavailable)),
            _ => Some(unknown(pid, ProcessQueryFailure::Malformed)),
        }
    }

    pub(super) fn query(pid: u32) -> ProcessEvidence {
        // SAFETY: no inheritance or privilege changes; only identity query and
        // nonblocking wait rights are requested for one explicit positive PID.
        let raw = unsafe {
            OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE,
                0,
                pid,
            )
        };
        if raw.is_null() {
            // SAFETY: this thread reads the immediate OpenProcess failure.
            return match unsafe { GetLastError() } {
                ERROR_INVALID_PARAMETER => ProcessEvidence::Exited { pid },
                ERROR_ACCESS_DENIED => unknown(pid, ProcessQueryFailure::PermissionDenied),
                _ => unknown(pid, ProcessQueryFailure::Unavailable),
            };
        }
        // SAFETY: OpenProcess returned a new owned handle; exactly this owner
        // closes it on every return path. PID reuse cannot redirect the handle.
        let handle = unsafe { OwnedHandle::from_raw_handle(raw) };
        if let Some(evidence) = wait_state(&handle, pid) {
            return evidence;
        }
        let mut creation = FILETIME {
            dwLowDateTime: 0,
            dwHighDateTime: 0,
        };
        let mut exit = creation;
        let mut kernel = creation;
        let mut user = creation;
        // SAFETY: owned handle and four distinct valid FILETIME output slots.
        if unsafe {
            GetProcessTimes(
                handle.as_raw_handle(),
                &mut creation,
                &mut exit,
                &mut kernel,
                &mut user,
            )
        } == 0
        {
            // SAFETY: capture the immediate native query error on this thread.
            let reason = if unsafe { GetLastError() } == ERROR_ACCESS_DENIED {
                ProcessQueryFailure::PermissionDenied
            } else {
                ProcessQueryFailure::Unavailable
            };
            return unknown(pid, reason);
        }
        if let Some(evidence) = wait_state(&handle, pid) {
            return evidence;
        }
        let ticks = u64::from(creation.dwHighDateTime) << 32 | u64::from(creation.dwLowDateTime);
        match filetime_epoch_seconds(ticks).filter(|seconds| *seconds > 0.0) {
            Some(create_time) => ProcessEvidence::Running {
                identity: ProcessIdentity { pid, create_time },
            },
            None => unknown(pid, ProcessQueryFailure::Malformed),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn windows_filetime_epoch_conversion_preserves_subseconds() {
        const EPOCH: u64 = 116_444_736_000_000_000;
        assert_eq!(filetime_epoch_seconds(EPOCH), Some(0.0));
        assert_eq!(filetime_epoch_seconds(EPOCH + 10_000_000), Some(1.0));
        assert_eq!(
            filetime_epoch_seconds(EPOCH + 17_000_000_001_234_560),
            Some(1_700_000_000.123456)
        );
        assert_eq!(filetime_epoch_seconds(EPOCH - 1), None);
        assert_eq!(filetime_epoch_seconds(0), None);
    }

    #[test]
    fn invalid_pid_is_unknown_without_process_group_queries() {
        assert_eq!(
            query_process(0),
            unknown(0, ProcessQueryFailure::InvalidPid)
        );
        #[cfg(target_os = "linux")]
        assert_eq!(
            query_process(u32::MAX),
            unknown(u32::MAX, ProcessQueryFailure::InvalidPid)
        );
    }

    #[cfg(any(target_os = "linux", windows))]
    #[test]
    fn native_self_creation_time_is_stable_and_precedes_current_epoch() {
        let ProcessEvidence::Running { identity } = own_process_identity() else {
            panic!("native self identity must be available");
        };
        assert_eq!(identity.pid, std::process::id());
        assert_eq!(
            query_process(identity.pid),
            ProcessEvidence::Running { identity }
        );
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs_f64();
        assert!(identity.create_time > 0.0 && identity.create_time <= now + 2.0);
    }
}
