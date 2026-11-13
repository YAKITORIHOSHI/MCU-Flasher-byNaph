use std::path::Path;
use std::process::{Command, Stdio};

/// Permit our owned portal to activate only while Lite owns the foreground.
/// Windows policy: https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-allowsetforegroundwindow
pub fn allow_portal_activation(pid: u32) {
    #[cfg(windows)]
    if pid != 0 && pid != u32::MAX {
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
                if owner == std::process::id() {
                    // The PID comes from our worker's live child, never user
                    // input. Never grant ASFW_ANY or take focus from another app.
                    AllowSetForegroundWindow(pid);
                }
            }
        }
    }
    #[cfg(not(windows))]
    let _ = pid;
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

#[cfg(test)]
mod verification {
    use super::*;

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
    fn unavailable_directory_is_reported_before_launching() {
        assert!(
            open_directory(Path::new("\0"))
                .unwrap_err()
                .contains("no longer available")
        );
    }
}
