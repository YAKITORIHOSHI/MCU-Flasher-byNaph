//! Native read-only project sessions shared by GTK and Win32.
//! The selected path reaches ProjectService unchanged so aliases fail closed.
//! All filesystem work runs in read_project on the existing reader worker.
//! Acknowledgement and session retention are memory-only on the window thread.
//! No mutable service accessor, source save, live registry or write lease exists.
use mcu_contracts::DocumentSnapshot;
use serde::Serialize;
use std::path::Path;
#[cfg(test)]
use std::path::PathBuf;

#[derive(Serialize)]
struct SourceDocument<'a> {
    id: &'a str,
    name: &'a str,
    content: &'a str,
}

/// One native window retains the original service and exact read baselines.
/// This is process-local read state, not interprocess project ownership or
/// proof that paths/bytes are unchanged after the worker's reads.
pub struct ProjectSession {
    _service: mcu_core::ProjectService,
    workspace_id: String,
    documents: Vec<DocumentSnapshot>,
}

impl ProjectSession {
    pub fn workspace_id(&self) -> &str {
        &self.workspace_id
    }
    pub fn documents(&self) -> &[DocumentSnapshot] {
        &self.documents
    }
}

/// A pending read cannot be cloned or constructed from renderer input. The
/// serialized open script is released when the window accepts its exact IDs.
pub struct LoadedProject {
    session: ProjectSession,
    script: Option<String>,
}

impl LoadedProject {
    pub fn entries(&self) -> Vec<(String, String)> {
        self.session
            .documents
            .iter()
            .map(|snapshot| {
                (
                    snapshot.document.document_id.0.clone(),
                    snapshot.document.name.clone(),
                )
            })
            .collect()
    }
    fn matches_ids(&self, ids: &[String]) -> bool {
        self.session
            .documents
            .iter()
            .map(|snapshot| &snapshot.document.document_id.0)
            .eq(ids.iter())
    }
}

/// At most one pending or active project belongs to this native window.
/// Project selection opens another window; it cannot replace this session.
#[derive(Default)]
pub struct ProjectSessions {
    pending: Option<LoadedProject>,
    active: Option<ProjectSession>,
}

impl ProjectSessions {
    pub fn stage(&mut self, project: LoadedProject) -> Result<(), String> {
        if self.pending.is_some() || self.active.is_some() {
            return Err("This window already owns a project read. Open another project in an independent window.".into());
        }
        self.pending = Some(project);
        Ok(())
    }
    pub fn pending_script(&self) -> Option<&str> {
        self.pending
            .as_ref()
            .and_then(|pending| pending.script.as_deref())
    }
    /// Move the script out before calling the browser engine, whose dispatch
    /// can reenter native IPC. No session borrow crosses that external call.
    pub fn take_pending_script(&mut self) -> Option<String> {
        self.pending
            .as_mut()
            .and_then(|pending| pending.script.take())
    }
    pub fn active(&self) -> Option<&ProjectSession> {
        self.active.as_ref()
    }
    pub fn cancel_pending(&mut self) {
        self.pending = None;
    }
    pub fn acknowledge(&mut self, ids: &[String]) -> Result<Option<Vec<(String, String)>>, String> {
        let Some(pending) = self.pending.as_ref() else {
            return Ok(None);
        };
        if !pending.matches_ids(ids) {
            return Err(
                "The editor did not acknowledge the exact ordered native source IDs.".into(),
            );
        }
        let entries = pending.entries();
        let pending = self.pending.take().expect("pending project checked above");
        self.active = Some(pending.session);
        Ok(Some(entries))
    }
}
pub const CHECK_SOURCE: &str = "\u{feff}void setup() {}\r\nvoid loop() {}\n";
pub const CHECK_HEADER: &str = "#pragma once\n// café 日本語\n";

pub fn read_project(selected: &Path) -> Result<LoadedProject, String> {
    let application_spelling = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .ok_or("Application root unavailable")?;
    // Normalize only our trusted application root. Windows canonical paths
    // have a verbatim prefix, unlike Cargo's path and ordinary folder choices.
    // Recognize both trusted spellings; ProjectService still validates the
    // literal selection before resolving it, retaining alias/traversal refusal.
    let application = application_spelling
        .canonicalize()
        .map_err(|error| error.to_string())?;
    // Only read APIs are reachable from the native workspace. Source saves and
    // live-write/ownership activation remain outside this adapter.
    let mut service = if selected.starts_with(application.join("audit/migration/scratch"))
        || selected.starts_with(application_spelling.join("audit/migration/scratch"))
    {
        mcu_core::ProjectService::fixture(&application, selected)
    } else {
        mcu_core::ProjectService::new(&application)
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
        documents.push(document);
    }
    let payload = documents
        .iter()
        .map(|snapshot| SourceDocument {
            id: &snapshot.document.document_id.0,
            name: &snapshot.document.name,
            content: &snapshot.content,
        })
        .collect::<Vec<_>>();
    let json = serde_json::to_string(&payload).map_err(|error| error.to_string())?;
    Ok(LoadedProject {
        session: ProjectSession {
            _service: service,
            workspace_id: workspace.workspace_id.0,
            documents,
        },
        script: Some(format!("window.__mcuOpenDocuments({json});")),
    })
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
        let loaded = read_project(&root).expect("native source reader");
        let entries = loaded.entries();
        let script = loaded.script.as_deref().unwrap();
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
    #[test]
    fn canonical_owned_fixture_path_uses_the_same_read_only_service() {
        let root = fixture();
        std::fs::write(root.join("Sketch.ino"), CHECK_SOURCE).expect("source");
        // Native profiles start at checked_scratch_root's canonical checkout.
        // On Windows this spelling has a verbatim prefix; Cargo's compile-time
        // manifest path and ordinary user selections usually do not.
        let selected = root.canonicalize().expect("owned canonical fixture");
        let loaded = read_project(&selected).expect("canonical native source reader");
        let entries = loaded.entries();
        let script = loaded.script.as_deref().unwrap();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].1, "Sketch.ino");
        assert!(script.contains("void setup()"));
        assert_eq!(
            std::fs::read(root.join("Sketch.ino")).expect("unchanged source"),
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

    #[test]
    fn acknowledged_project_retains_original_native_document_baselines() {
        let root = fixture();
        std::fs::write(root.join("Sketch.ino"), CHECK_SOURCE).unwrap();
        let loaded = read_project(&root).unwrap();
        let ids = loaded
            .entries()
            .into_iter()
            .map(|(id, _)| id)
            .collect::<Vec<_>>();
        let mut sessions = ProjectSessions::default();
        sessions.stage(loaded).unwrap();
        assert!(sessions.active().is_none());
        assert!(sessions.pending_script().unwrap().contains("void setup()"));
        let entries = sessions.acknowledge(&ids).unwrap().unwrap();
        assert_eq!(entries[0].0, ids[0]);
        assert!(sessions.pending_script().is_none());
        let active = sessions.active().unwrap();
        assert!(!active.workspace_id().is_empty());
        assert_eq!(active.documents().len(), 1);
        let original = &active.documents()[0];
        assert_eq!(original.content, CHECK_SOURCE);
        assert_eq!(original.document.revision.0, 1);
        assert!(!original.document.content_digest.0.is_empty());
        std::fs::write(root.join("Sketch.ino"), b"external owned fixture change").unwrap();
        assert_eq!(
            sessions.active().unwrap().documents()[0].content,
            CHECK_SOURCE
        );
        sessions.cancel_pending();
        assert_eq!(
            sessions.active().unwrap().documents()[0].content,
            CHECK_SOURCE
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn foreign_reordered_duplicate_or_partial_editor_acknowledgements_cannot_activate() {
        let root = fixture();
        std::fs::write(root.join("Sketch.ino"), CHECK_SOURCE).unwrap();
        std::fs::write(root.join("Config.h"), CHECK_HEADER).unwrap();
        let loaded = read_project(&root).unwrap();
        let ids = loaded
            .entries()
            .into_iter()
            .map(|(id, _)| id)
            .collect::<Vec<_>>();
        let foreign = read_project(&root)
            .unwrap()
            .entries()
            .into_iter()
            .map(|(id, _)| id)
            .collect::<Vec<_>>();
        let mut sessions = ProjectSessions::default();
        sessions.stage(loaded).unwrap();
        for bad in [
            vec![],
            vec![ids[0].clone()],
            vec![ids[1].clone(), ids[0].clone()],
            vec![ids[0].clone(), ids[0].clone()],
            foreign,
        ] {
            assert!(sessions.acknowledge(&bad).is_err());
            assert!(sessions.active().is_none());
            assert!(sessions.pending_script().is_some());
        }
        assert!(sessions.acknowledge(&ids).unwrap().is_some());
        assert!(sessions.acknowledge(&ids).unwrap().is_none());
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn one_project_per_window_and_cancelled_pending_session_preserve_sources() {
        let root = fixture();
        std::fs::write(root.join("Sketch.ino"), CHECK_SOURCE).unwrap();
        let loaded = read_project(&root).unwrap();
        let ids = loaded
            .entries()
            .into_iter()
            .map(|(id, _)| id)
            .collect::<Vec<_>>();
        let mut sessions = ProjectSessions::default();
        sessions.stage(loaded).unwrap();
        assert!(sessions.stage(read_project(&root).unwrap()).is_err());
        sessions.cancel_pending();
        assert!(sessions.active().is_none());
        assert!(sessions.acknowledge(&ids).unwrap().is_none());
        let loaded = read_project(&root).unwrap();
        let ids = loaded
            .entries()
            .into_iter()
            .map(|(id, _)| id)
            .collect::<Vec<_>>();
        sessions.stage(loaded).unwrap();
        sessions.acknowledge(&ids).unwrap();
        let active_id = sessions.active().unwrap().workspace_id().to_owned();
        assert!(sessions.stage(read_project(&root).unwrap()).is_err());
        assert_eq!(sessions.active().unwrap().workspace_id(), active_id);
        assert_eq!(
            std::fs::read(root.join("Sketch.ino")).unwrap(),
            CHECK_SOURCE.as_bytes()
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn loaded_native_session_can_move_from_worker_to_window_without_filesystem_calls() {
        fn assert_send<T: Send>() {}
        assert_send::<LoadedProject>();
        let root = fixture();
        std::fs::write(root.join("Sketch.ino"), CHECK_SOURCE).unwrap();
        let selected = root.clone();
        let loaded = std::thread::spawn(move || read_project(&selected))
            .join()
            .unwrap()
            .unwrap();
        let ids = loaded
            .entries()
            .into_iter()
            .map(|(id, _)| id)
            .collect::<Vec<_>>();
        // Once the worker has captured the project, native acknowledgement is
        // memory-only and remains valid after its owned fixture is removed.
        std::fs::remove_dir_all(root).unwrap();
        let mut sessions = ProjectSessions::default();
        sessions.stage(loaded).unwrap();
        sessions.acknowledge(&ids).unwrap();
        assert_eq!(
            sessions.active().unwrap().documents()[0].content,
            CHECK_SOURCE
        );
    }

    #[test]
    fn renderer_script_moves_once_without_borrowing_pending_state_during_dispatch() {
        let root = fixture();
        std::fs::write(root.join("Sketch.ino"), CHECK_SOURCE).unwrap();
        let loaded = read_project(&root).unwrap();
        let ids = loaded
            .entries()
            .into_iter()
            .map(|(id, _)| id)
            .collect::<Vec<_>>();
        let mut sessions = ProjectSessions::default();
        sessions.stage(loaded).unwrap();
        let script = sessions.take_pending_script().unwrap();
        assert!(script.contains("void setup()"));
        assert!(sessions.take_pending_script().is_none());
        // Dispatch can reenter native IPC without a borrow of the session.
        assert!(sessions.acknowledge(&ids).unwrap().is_some());
        assert_eq!(
            sessions.active().unwrap().documents()[0].content,
            CHECK_SOURCE
        );
        std::fs::remove_dir_all(root).unwrap();
    }
}
