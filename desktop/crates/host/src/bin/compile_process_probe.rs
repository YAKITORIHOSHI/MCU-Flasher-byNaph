use mcu_host::compile_process_fixture::{
    dispatch_compile_process_child, run_compile_process_fixture, CompileProcessMode,
    CompileProcessOutcome, MAX_COMPILE_PROCESS_STREAM_BYTES,
};
use std::sync::atomic::AtomicBool;

fn main() {
    if let Some(code) = dispatch_compile_process_child() {
        std::process::exit(code);
    }
    if std::env::args_os().skip(1).any(|arg| arg != "--self-check") {
        eprintln!("Only fixed child roles or --self-check are supported");
        std::process::exit(125);
    }
    match self_check() {
        Ok(receipt) => println!("{receipt}"),
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(1);
        }
    }
}

fn self_check() -> Result<serde_json::Value, String> {
    let mut checks = 0;
    let cancel = AtomicBool::new(false);
    let success = run_compile_process_fixture(CompileProcessMode::Success, &cancel)?;
    if success.outcome != CompileProcessOutcome::Success
        || success.stdout != "stdout: café Ω 日本語\r\n".as_bytes()
        || success.stderr != b"stderr: separate\n"
        || !success.cleanup_complete
        || !success.stdout_eof
        || !success.stderr_eof
    {
        return Err("Fixed success byte separation/drain/cleanup failed".into());
    }
    checks += 4;
    let nonzero = run_compile_process_fixture(CompileProcessMode::Nonzero, &cancel)?;
    if nonzero.outcome != (CompileProcessOutcome::Nonzero { code: 23 })
        || nonzero.stderr != b"fixed nonzero\n"
    {
        return Err("Fixed nonzero child was not preserved".into());
    }
    checks += 1;
    for mode in [
        CompileProcessMode::FloodStdout,
        CompileProcessMode::FloodStderr,
        CompileProcessMode::FloodBoth,
    ] {
        let report = run_compile_process_fixture(mode, &cancel)?;
        if report.outcome != CompileProcessOutcome::OutputLimit
            || report.stdout.len() > MAX_COMPILE_PROCESS_STREAM_BYTES
            || report.stderr.len() > MAX_COMPILE_PROCESS_STREAM_BYTES
            || !report.cleanup_complete
            || !report.stdout_eof
            || !report.stderr_eof
        {
            return Err(format!("Fixed {mode:?} exceeded storage/cleanup bounds"));
        }
        checks += 1;
    }
    let timeout = run_compile_process_fixture(CompileProcessMode::Hang, &cancel)?;
    if timeout.outcome != CompileProcessOutcome::TimedOut || !timeout.cleanup_complete {
        return Err("Hanging child missed its finite deadline/cleanup".into());
    }
    checks += 1;
    let cancelled = AtomicBool::new(true);
    let report = run_compile_process_fixture(CompileProcessMode::Success, &cancelled)?;
    if report.outcome != CompileProcessOutcome::Cancelled || report.child_pid.is_some() {
        return Err("Pre-cancelled fixture spawned a child".into());
    }
    checks += 1;
    let running_cancel =
        run_compile_process_fixture(CompileProcessMode::CancelAfterStart, &cancel)?;
    if running_cancel.outcome != CompileProcessOutcome::Cancelled
        || running_cancel.child_pid.is_none()
        || running_cancel.stdout != b"started\n"
        || !running_cancel.cleanup_complete
        || !running_cancel.stdout_eof
        || !running_cancel.stderr_eof
    {
        return Err("Started child did not acknowledge native cancellation and cleanup".into());
    }
    checks += 1;
    if cfg!(target_os = "linux") {
        let signal_refusal =
            run_compile_process_fixture(CompileProcessMode::SigchldRefusals, &cancel)?;
        if signal_refusal.outcome != CompileProcessOutcome::Success
            || signal_refusal.stdout
                != b"ignored SIGCHLD refused\ncustom SIGCHLD refused\nSA_NOCLDWAIT refused\n"
            || !signal_refusal.cleanup_complete
            || !signal_refusal.stdout_eof
            || !signal_refusal.stderr_eof
        {
            return Err("Non-waitable SIGCHLD did not refuse safely before any child spawn".into());
        }
        checks += 3;
    }
    let descendant =
        run_compile_process_fixture(CompileProcessMode::InheritedPipeGrandchild, &cancel)?;
    if descendant.outcome != CompileProcessOutcome::Success
        || !descendant.stdout.starts_with(b"grandchild=")
        || !descendant.cleanup_complete
        || !descendant.stdout_eof
        || !descendant.stderr_eof
        || (cfg!(target_os = "linux") && descendant.descendants_reaped != 1)
    {
        return Err("Inherited-pipe descendant was not fully terminated/drained/reaped".into());
    }
    checks += 1;
    Ok(serde_json::json!({
        "host":std::env::consts::OS,
        "architecture":std::env::consts::ARCH,
        "checks":checks,
        "fixtureOnly":true,
        "noCompilerDispatch":true,
        "threadedDrains":false,
        "descendantsReaped":descendant.descendants_reaped,
        "executableBinding":if cfg!(target_os="linux") {"running_image_procfs"} else {"named_current_executable_pinned_after_open"},
        "windowsPrePinRunningImageBindingQualified":false,
        "externalToolImageAuthority":false
    }))
}
