//! Native read-only source snapshots shared by GTK and Win32.
//! The selected path reaches ProjectService unchanged so aliases fail closed.
use serde::Serialize;
use std::path::Path;
#[cfg(test)]
use std::path::PathBuf;

#[derive(Serialize)]
struct SourceDocument {
    id: String,
    name: String,
    content: String,
}
pub type LoadedProject = (Vec<(String, String)>, String);
pub const CHECK_SOURCE: &str = "\u{feff}void setup() {}\r\nvoid loop() {}\n";
pub const CHECK_HEADER: &str = "#pragma once\n// café 日本語\n";

pub fn read_project(selected: &Path) -> Result<LoadedProject, String> {
    let application = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .ok_or("Application root unavailable")?;
    // Only read APIs are reachable from the native workspace. Source saves and
    // live-write/ownership activation remain outside this adapter.
    let mut service = if selected.starts_with(application.join("audit/migration/scratch")) {
        mcu_core::ProjectService::fixture(application, selected)
    } else {
        mcu_core::ProjectService::new(application)
    }
    .map_err(|error| error.message)?;
    let workspace = service
        .register_project("native-workspace-reader", selected)
        .map_err(|error| error.message)?;
    let mut documents = Vec::new();
    for file in workspace.manifest.files {
        let document = service
            .read_document(
                "native-workspace-reader",
                &workspace.workspace_id,
                &file.document_id,
            )
            .map_err(|error| error.message)?;
        documents.push(SourceDocument {
            id: file.document_id.0,
            name: file.name,
            content: document.content,
        });
    }
    let entries = documents
        .iter()
        .map(|file| (file.id.clone(), file.name.clone()))
        .collect();
    let json = serde_json::to_string(&documents).map_err(|error| error.to_string())?;
    Ok((entries, format!("window.__mcuOpenDocuments({json});")))
}

#[cfg(test)]
mod reader_checks {
    use super::*;
    fn fixture() -> PathBuf {
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let application = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(Path::parent)
            .expect("application root");
        let root = application.join(format!(
            "audit/migration/scratch/source-reader-{}-{nonce}",
            std::process::id()
        ));
        std::fs::create_dir(&root).expect("owned source fixture");
        root
    }
    #[test]
    fn root_sources_keep_bytes_and_nested_sources_stay_outside_manifest() {
        let root = fixture();
        std::fs::write(root.join("Sketch.ino"), CHECK_SOURCE).expect("source");
        std::fs::create_dir(root.join("user-folder")).expect("nested fixture");
        std::fs::write(root.join("user-folder/Nested.ino"), "never staged").expect("nested source");
        let (entries, script) = read_project(&root).expect("native source reader");
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].1, "Sketch.ino");
        assert!(script.contains("void setup()"));
        assert!(!script.contains("never staged"));
        let documents: serde_json::Value = serde_json::from_str(
            script
                .strip_prefix("window.__mcuOpenDocuments(")
                .unwrap()
                .strip_suffix(");")
                .unwrap(),
        )
        .expect("native JSON snapshot");
        assert_eq!(documents[0]["content"].as_str(), Some(CHECK_SOURCE));
        assert_eq!(documents[0]["id"].as_str(), Some(entries[0].0.as_str()));
        assert_eq!(
            std::fs::read(root.join("Sketch.ino")).expect("original"),
            CHECK_SOURCE.as_bytes()
        );
        std::fs::remove_dir_all(root).expect("owned fixture cleanup");
    }
    #[cfg(unix)]
    #[test]
    fn native_adapter_preserves_core_alias_refusal_before_canonicalization() {
        let root = fixture();
        let project = root.join("project");
        std::fs::create_dir(&project).expect("project");
        std::fs::write(project.join("Sketch.ino"), CHECK_SOURCE).expect("source");
        assert!(read_project(&project).is_ok());
        std::os::unix::fs::symlink(&project, root.join("alias")).expect("owned alias");
        assert!(read_project(&root.join("alias")).is_err());
        std::fs::remove_dir_all(root).expect("owned fixture cleanup");
    }
}
