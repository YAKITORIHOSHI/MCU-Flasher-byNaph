//! Bounded renderer reports for the native-widget feasibility fixture.

use serde::{Deserialize, Serialize};
pub mod workspace_design;
pub mod workspace_project;

pub const MAX_MESSAGE_BYTES: usize = 16 * 1024;
pub const SOURCE: &str = "void setup() {}\nvoid loop() {}\n";
pub const CHANGED_SOURCE: &str =
    "void setup() {}\nvoid loop() {}\n// Native Rust fixture: café 日本語 λ\n";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Island {
    Editor,
    Terminal,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum RendererMessage {
    WorkspaceSnapshot {
        phase: u8,
        documents: u16,
        same_models: bool,
        dirty_retained: bool,
        history_retained: bool,
        selection_retained: bool,
        theme: String,
        background: String,
    },
    DocumentsOpened {
        documents: Vec<String>,
    },
    WorkspaceReady {},
    DocumentChanged {
        document_id: String,
        edit_revision: u64,
        dirty: bool,
    },
    EditorSnapshot {
        phase: u8,
        model_id: String,
        text: String,
        dirty: bool,
        same_model: bool,
        can_undo: bool,
        history_retained: bool,
        selection: [u32; 4],
        worker_messages: u16,
        cpp_loaded: bool,
    },
    TerminalSnapshot {
        text: String,
        columns: u16,
        rows: u16,
    },
    Error {
        message: String,
    },
}

pub fn parse_message(island: Island, body: &str) -> Result<RendererMessage, String> {
    if body.len() > MAX_MESSAGE_BYTES {
        return Err("Renderer report exceeds the 16 KiB fixture limit.".into());
    }
    let message: RendererMessage = serde_json::from_str(body)
        .map_err(|_| "Unknown or malformed renderer report.".to_owned())?;
    let valid = match &message {
        RendererMessage::WorkspaceSnapshot {
            phase,
            documents,
            theme,
            background,
            ..
        } => {
            island == Island::Editor
                && *phase <= 3
                && *documents <= 256
                && theme.len() <= 32
                && background.len() <= 64
        }
        RendererMessage::DocumentsOpened { documents } => {
            island == Island::Editor
                && documents.len() <= 256
                && documents.iter().all(|id| !id.is_empty() && id.len() <= 256)
        }
        RendererMessage::WorkspaceReady {} => island == Island::Editor,
        RendererMessage::DocumentChanged {
            document_id,
            edit_revision,
            ..
        } => {
            island == Island::Editor
                && !document_id.is_empty()
                && document_id.len() <= 256
                && *edit_revision > 0
                && *edit_revision < (1u64 << 53)
        }
        RendererMessage::EditorSnapshot {
            phase,
            model_id,
            text,
            ..
        } => {
            island == Island::Editor
                && *phase <= 7
                && !model_id.is_empty()
                && model_id.len() <= 256
                && text.len() <= 8192
        }
        RendererMessage::TerminalSnapshot {
            text,
            columns,
            rows,
        } => {
            island == Island::Terminal
                && text.len() <= 4096
                && (1..=512).contains(columns)
                && (1..=256).contains(rows)
        }
        RendererMessage::Error { message } => message.len() <= 1024,
    };
    if valid {
        Ok(message)
    } else {
        Err("Report does not match its native-bound island or budget.".into())
    }
}

pub fn asset_path(uri: &str) -> Option<&'static str> {
    match uri.strip_prefix("mcu-probe://localhost/")? {
        "editor.html" => Some("editor.html"),
        "terminal.html" => Some("terminal.html"),
        "probe-editor.js" => Some("probe-editor.js"),
        "probe-terminal.js" => Some("probe-terminal.js"),
        "probe.css" => Some("probe.css"),
        "bundle.js" => Some("bundle.js"),
        "18.bundle.js" => Some("18.bundle.js"),
        "editor.worker.js" => Some("editor.worker.js"),
        "8f3abbcbc983396e1f13.ttf" => Some("8f3abbcbc983396e1f13.ttf"),
        "xterm.js" => Some("xterm.js"),
        "xterm.css" => Some("xterm.css"),
        "xterm-addon-fit.js" => Some("xterm-addon-fit.js"),
        _ => None,
    }
}

/// WebView2 maps the custom scheme to this fixed HTTPS origin. Navigation is
/// bound to the native island and never accepts another page or a remote URL.
pub fn windows_navigation_allowed(island: Island, uri: &str) -> bool {
    match island {
        Island::Editor => matches!(
            uri,
            "mcu-probe://localhost/editor.html" | "https://mcu-probe.localhost/editor.html"
        ),
        Island::Terminal => matches!(
            uri,
            "mcu-probe://localhost/terminal.html" | "https://mcu-probe.localhost/terminal.html"
        ),
    }
}

pub fn checked_scratch_root(repo: &std::path::Path) -> std::io::Result<std::path::PathBuf> {
    let canonical_repo = repo.canonicalize()?;
    let mut candidate = canonical_repo.clone();
    for part in ["audit", "migration", "scratch"] {
        candidate.push(part);
        let metadata = std::fs::symlink_metadata(&candidate)?;
        if metadata.file_type().is_symlink() || !metadata.is_dir() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "Fixture scratch ancestors must be real checkout directories.",
            ));
        }
    }
    let canonical_scratch = candidate.canonicalize()?;
    if canonical_scratch != candidate || !canonical_scratch.starts_with(&canonical_repo) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "Fixture scratch root escapes the checkout.",
        ));
    }
    Ok(canonical_scratch)
}

#[cfg(test)]
mod checks {
    use super::*;
    #[test]
    fn native_document_reports_have_no_path_or_owner_authority() {
        let body = r#"{"kind":"document_changed","document_id":"native-id","edit_revision":1,"dirty":true}"#;
        assert!(parse_message(Island::Editor, body).is_ok());
        assert!(parse_message(Island::Terminal, body).is_err());
        assert!(parse_message(
            Island::Editor,
            &body.replace("\"dirty\":true", "\"dirty\":true,\"path\":\"outside.ino\"")
        )
        .is_err());
        assert!(parse_message(
            Island::Editor,
            &body.replace("\"dirty\":true", "\"dirty\":true,\"owner\":\"forged\"")
        )
        .is_err());
        for revision in ["0", "9007199254740992"] {
            assert!(parse_message(
                Island::Editor,
                &body.replace(
                    "\"edit_revision\":1",
                    &format!("\"edit_revision\":{revision}")
                )
            )
            .is_err());
        }
    }

    #[test]
    fn accepts_only_the_native_bound_island() {
        let body = r#"{"kind":"terminal_snapshot","text":"café 日本語 λ","columns":80,"rows":10}"#;
        assert!(matches!(
            parse_message(Island::Terminal, body),
            Ok(RendererMessage::TerminalSnapshot { .. })
        ));
        assert!(parse_message(Island::Editor, body).is_err());
    }

    #[test]
    fn renderer_cannot_claim_native_ownership() {
        let body =
            r#"{"kind":"terminal_snapshot","text":"x","columns":80,"rows":10,"owner":"main"}"#;
        assert!(parse_message(Island::Terminal, body).is_err());
    }

    #[test]
    fn refuses_oversized_or_unknown_messages() {
        assert!(parse_message(Island::Editor, &"x".repeat(MAX_MESSAGE_BYTES + 1)).is_err());
        assert!(parse_message(Island::Editor, r#"{"kind":"save","path":"/etc/passwd"}"#).is_err());
    }

    #[test]
    fn refuses_out_of_range_terminal_geometry() {
        assert!(parse_message(
            Island::Terminal,
            r#"{"kind":"terminal_snapshot","text":"x","columns":0,"rows":10}"#
        )
        .is_err());
    }

    #[test]
    fn assets_have_a_finite_local_origin_allowlist() {
        assert_eq!(
            asset_path("mcu-probe://localhost/editor.html"),
            Some("editor.html")
        );
        assert_eq!(
            asset_path("mcu-probe://localhost/bundle.js"),
            Some("bundle.js")
        );
        for uri in [
            "https://localhost/bundle.js",
            "mcu-probe://evil/bundle.js",
            "mcu-probe://localhost/../Cargo.toml",
            "mcu-probe://localhost/%2e%2e/Cargo.toml",
            "mcu-probe://localhost/bundle.js?path=../secret",
        ] {
            assert_eq!(asset_path(uri), None, "{uri}");
        }
    }

    #[test]
    fn windows_navigation_accepts_only_the_native_bound_fixed_page() {
        assert!(windows_navigation_allowed(
            Island::Editor,
            "mcu-probe://localhost/editor.html"
        ));
        assert!(windows_navigation_allowed(
            Island::Editor,
            "https://mcu-probe.localhost/editor.html"
        ));
        assert!(windows_navigation_allowed(
            Island::Terminal,
            "https://mcu-probe.localhost/terminal.html"
        ));
        for uri in [
            "http://mcu-probe.localhost/editor.html",
            "https://mcu-probe.localhost/terminal.html",
            "https://evil/editor.html",
            "https://mcu-probe.localhost/editor.html?path=/etc/passwd",
            "https://mcu-probe.localhost/%65ditor.html",
            "mcu-probe://evil/editor.html",
        ] {
            assert!(!windows_navigation_allowed(Island::Editor, uri), "{uri}");
        }
    }

    #[cfg(unix)]
    #[test]
    fn scratch_root_refuses_selected_directory_symlink_escape() {
        use std::{
            fs,
            os::unix::fs::symlink,
            path::PathBuf,
            time::{SystemTime, UNIX_EPOCH},
        };
        struct OwnedTree(PathBuf);
        impl Drop for OwnedTree {
            fn drop(&mut self) {
                fs::remove_dir_all(&self.0).expect("remove owned scratch-root canary tree");
            }
        }
        let parent = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../audit/migration/scratch")
            .canonicalize()
            .expect("existing audit scratch");
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let name = format!("native-ui-root-check-{}-{nanos}", std::process::id());
        let tree = OwnedTree(parent.join(name));
        let checkout = tree.0.join("checkout");
        let outside = tree.0.join("outside-checkout");
        fs::create_dir_all(checkout.join("audit/migration")).unwrap();
        fs::create_dir(&outside).unwrap();
        fs::write(outside.join("canary"), b"KEEP").unwrap();
        symlink(&outside, checkout.join("audit/migration/scratch")).unwrap();
        assert!(
            checked_scratch_root(&checkout).is_err(),
            "A selected scratch symlink must not redirect profile writes outside the checkout."
        );
        assert_eq!(fs::read(outside.join("canary")).unwrap(), b"KEEP");
        assert_eq!(fs::read_dir(&outside).unwrap().count(), 1);
    }
}
