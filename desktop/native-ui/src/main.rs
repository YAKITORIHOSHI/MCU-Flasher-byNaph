#[cfg(target_os = "linux")]
mod linux;

fn main() {
    #[cfg(target_os = "linux")]
    if let Err(error) = linux::run() {
        eprintln!("Native Rust GUI fixture failed: {error}");
        std::process::exit(1);
    }
    #[cfg(not(target_os = "linux"))]
    {
        eprintln!("Native Rust GUI fixture: this milestone implements only the Linux GTK3 adapter. A native Windows Win32/WebView2 adapter and desktop qualification remain required.");
        std::process::exit(78);
    }
}
