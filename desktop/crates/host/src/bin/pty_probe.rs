fn main() {
    if std::env::args().nth(1).as_deref() == Some(mcu_host::FIXTURE_CHILD_ARGUMENT) {
        if let Err(error) = mcu_host::write_fixture(std::io::stdout()) {
            eprintln!("{error}");
            std::process::exit(1);
        }
        return;
    }
    let executable = std::env::current_exe().expect("current native executable");
    let mut output = String::new();
    match mcu_host::stream_fixture(&executable, |chunk, _bytes| {
        output.push_str(&chunk);
        Ok(())
    }) {
        Ok(summary) => {
            if summary.exit_code != 0 || !output.contains("日本語") || !output.contains("24/24")
            {
                eprintln!("Native PTY output failed validation");
                std::process::exit(1);
            }
            println!(
                "{}",
                serde_json::json!({"host":std::env::consts::OS,"architecture":std::env::consts::ARCH,
                "nativePty":true,"summary":summary,"unicode":true,"ansi":output.contains("\u{1b}[36m")})
            );
        }
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(1);
        }
    }
}
