#[cfg(target_os = "linux")]
mod linux;
#[cfg(target_os = "linux")]
mod linux_workspace;
#[cfg(target_os = "windows")]
mod windows;

fn main() {
    #[cfg(target_os = "linux")]
    if let Err(error) = linux::run() {
        eprintln!("Native Rust GUI fixture failed: {error}");
        std::process::exit(1);
    }
    #[cfg(target_os = "windows")]
    if let Err(error) = windows::run() {
        eprintln!("Native Rust GUI fixture failed: {error}");
        std::process::exit(1);
    }
    #[cfg(not(any(target_os = "linux", target_os = "windows")))]
    {
        eprintln!("Native Rust GUI fixture: this host has no native adapter.");
        std::process::exit(78);
    }
}
