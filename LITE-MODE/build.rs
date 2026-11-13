fn main() {
    println!("cargo:rerun-if-changed=assets/lite.ico");
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        let mut resource = winresource::WindowsResource::new();
        resource.set_icon("assets/lite.ico");
        resource.set("ProductName", "MCU Flasher Lite");
        resource.set("FileDescription", "MCU Flasher Lite — Rust workbench");
        resource
            .compile()
            .expect("compile dedicated Lite icon resource");
    }
}
