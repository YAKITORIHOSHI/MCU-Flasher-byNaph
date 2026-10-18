fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "prototype_info",
            "open_project",
            "project_manifest",
            "read_document",
            "save_document",
            "create_project",
            "stream_fixture",
            "acknowledge_fixture",
            "report_probe",
            "verify_webview_transfer",
        ]),
    ))
    .expect("Tauri command permission generation failed");
}
