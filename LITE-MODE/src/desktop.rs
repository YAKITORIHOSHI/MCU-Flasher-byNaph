use std::path::Path;
use std::process::{Command, Stdio};

/// Permit our owned portal to activate only while Lite owns the foreground.
/// Windows policy: https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-allowsetforegroundwindow
pub fn allow_portal_activation(pid: u32) -> bool {
    if pid == 0 || pid == u32::MAX {
        return false;
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::UI::WindowsAndMessaging::{
            AllowSetForegroundWindow, GetForegroundWindow, GetWindowThreadProcessId,
        };
        // SAFETY: Win32 supplies the borrowed HWND; the output pointer refers
        // to an initialized live u32. No HWND is retained or dereferenced by us.
        unsafe {
            let window = GetForegroundWindow();
            let mut owner = 0;
            if !window.is_null() {
                GetWindowThreadProcessId(window, &mut owner);
                return portal_focus_allowed(pid, owner, |pid| AllowSetForegroundWindow(pid) != 0);
            }
            false
        }
    }
    #[cfg(not(windows))]
    {
        true
    }
}

#[cfg(any(windows, test))]
fn portal_focus_allowed(pid: u32, foreground_owner: u32, grant: impl FnOnce(u32) -> bool) -> bool {
    pid != 0 && pid != u32::MAX && foreground_owner == std::process::id() && grant(pid)
}

fn directory_command(directory: &Path) -> Command {
    #[cfg(target_os = "windows")]
    let mut command = {
        let windows = std::env::var_os("WINDIR").unwrap_or_else(|| r"C:\Windows".into());
        Command::new(Path::new(&windows).join("explorer.exe"))
    };
    #[cfg(not(target_os = "windows"))]
    let mut command = Command::new("xdg-open");
    command
        .arg(directory)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    command
}

/// Open the original folder without passing its name through a shell.
pub fn open_directory(directory: &Path) -> Result<(), String> {
    if !directory.is_dir() {
        return Err(format!(
            "Sketch folder is no longer available: {}",
            directory.display()
        ));
    }
    let mut command = directory_command(directory);
    #[cfg(target_os = "windows")]
    {
        command
            .spawn()
            .map(|_| ())
            .map_err(|error| format!("Could not open the sketch folder: {error}"))
    }
    #[cfg(not(target_os = "windows"))]
    {
        let status = command
            .status()
            .map_err(|error| format!("Could not open the sketch folder: {error}"))?;
        if status.success() {
            Ok(())
        } else {
            Err(format!(
                "The file manager could not open {} ({status})",
                directory.display()
            ))
        }
    }
}

fn window_command(executable: &Path, project: &str) -> Command {
    let mut command = Command::new(executable);
    command
        .arg("--from-bootstrap")
        .arg("--project")
        .arg(project)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    command
}

/// The first Lite launch uses the same setup/repair path as the main app.
/// Bootstrap returns to Lite with a literal marker, preventing a launch loop.
pub fn bootstrap(arguments: &[String]) -> Result<bool, String> {
    if arguments
        .iter()
        .any(|argument| matches!(argument.as_str(), "--preview" | "--from-bootstrap"))
    {
        return Ok(false);
    }
    let lite = crate::bridge::lite_root()?;
    let root = lite
        .parent()
        .ok_or("Missing MCU Flasher application folder")?;
    #[cfg(windows)]
    let mut command = Command::new(root.join("LAUNCHER/WINDOWS.exe"));
    #[cfg(not(windows))]
    let mut command = {
        let mut command = Command::new("/bin/bash");
        command
            .arg(root.join("direct/ubuntu/run.sh"))
            .arg("--desktop");
        command
    };
    command.arg("--lite");
    if let Some(project) = startup_project(arguments) {
        command.arg("--project").arg(project);
    }
    command.current_dir(root).stdin(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000);
    }
    command
        .spawn()
        .map_err(|error| format!("Could not start Bootstrap: {error}"))?;
    Ok(true)
}

/// Open another sketch in its own independent Lite window, like the main
/// app's Project. Each window runs its own tool worker; port and sketch
/// ownership is shared through the instance registries.
pub fn open_window(project: &str) -> Result<(), String> {
    let executable = std::env::current_exe()
        .map_err(|error| format!("Could not find Lite to open another window: {error}"))?;
    let mut child = window_command(&executable, project)
        .spawn()
        .map_err(|error| format!("Could not open another Lite window: {error}"))?;
    // Reap the independent window when it closes; it never ends with this one.
    std::thread::Builder::new()
        .name("lite-window".into())
        .spawn(move || {
            let _ = child.wait();
        })
        .map(|_| ())
        .map_err(|error| format!("Could not watch the new Lite window: {error}"))
}

/// The sketch a window was opened for: `--project <path>`.
pub fn startup_project(args: &[String]) -> Option<String> {
    args.windows(2)
        .find(|pair| pair[0] == "--project")
        .map(|pair| pair[1].clone())
        .filter(|path| !path.trim().is_empty())
}

#[cfg(test)]
mod verification {
    use super::*;

    #[test]
    fn portal_focus_permission_never_grants_another_foreground_app() {
        let own = std::process::id();
        for (pid, owner) in [
            (0, own),
            (u32::MAX, own),
            (1234, own.wrapping_add(1)),
            (1234, 0),
        ] {
            assert!(!portal_focus_allowed(pid, owner, |_| panic!(
                "Must not grant focus"
            )));
        }
        assert!(!portal_focus_allowed(1234, own, |_| false));
        assert!(portal_focus_allowed(1234, own, |pid| {
            assert_eq!(pid, 1234);
            true
        }));
    }

    #[test]
    fn source_directory_stays_one_literal_argument() {
        let folder = Path::new("/original/My Sketch & $HOME [1]");
        let command = directory_command(folder);
        assert_eq!(
            command.get_args().collect::<Vec<_>>(),
            vec![folder.as_os_str()]
        );
    }

    #[test]
    fn another_window_receives_the_sketch_as_one_literal_argument() {
        let project = "/original/My Sketch & $HOME [1]";
        let command = window_command(Path::new("/lite/UBUNTU"), project);
        assert_eq!(
            command.get_args().collect::<Vec<_>>(),
            vec![
                std::ffi::OsStr::new("--from-bootstrap"),
                std::ffi::OsStr::new("--project"),
                std::ffi::OsStr::new(project)
            ]
        );
        let args = ["lite", "--project", project].map(String::from);
        assert_eq!(startup_project(&args).as_deref(), Some(project));
        assert_eq!(startup_project(&["lite".into(), "--project".into()]), None);
    }

    #[test]
    fn unavailable_directory_is_reported_before_launching() {
        assert!(
            open_directory(Path::new("\0"))
                .unwrap_err()
                .contains("no longer available")
        );
    }
}
