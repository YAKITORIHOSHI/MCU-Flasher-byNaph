//! Versioned, GUI-independent wire contracts for the incremental desktop host.
//! IDs are scoped to one running service. They are routing identities, not access
//! tokens: the native host must supply the trusted window owner separately.

use serde::{Deserialize, Serialize};

pub const PROTOCOL_VERSION: u32 = 1;

macro_rules! string_id {
    ($name:ident) => {
        #[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
        #[serde(transparent)]
        pub struct $name(pub String);
    };
}

string_id!(WorkspaceId);
string_id!(DocumentId);
string_id!(ContentDigest);

/// Service revisions are bounded to JavaScript's exact integer range.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(transparent)]
pub struct Revision(pub u64);

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorCode {
    InvalidPath,
    ForbiddenRoot,
    NotFound,
    UnsupportedFile,
    Symlink,
    InvalidEncoding,
    TooLarge,
    LimitExceeded,
    WorkspaceNotFound,
    WorkspaceBusy,
    WrongOwner,
    StaleRevision,
    ExternalChange,
    AlreadyExists,
    ReadOnly,
    Io,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ServiceError {
    pub code: ErrorCode,
    pub message: String,
    pub retryable: bool,
}

impl std::fmt::Display for ServiceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.message)
    }
}

impl std::error::Error for ServiceError {}

pub type ServiceResult<T> = Result<T, ServiceError>;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SourceFile {
    pub document_id: DocumentId,
    pub name: String,
    /// Includes the dot and is lower case, for example `.ino`.
    pub extension: String,
    pub is_main: bool,
    pub size_bytes: u64,
    pub revision: Revision,
    /// Lower-case hex SHA-256 of the actual source bytes, including BOM/newlines.
    pub content_digest: ContentDigest,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SourceManifest {
    pub protocol_version: u32,
    pub workspace_id: WorkspaceId,
    pub revision: Revision,
    /// Main `.ino` files first, then case-insensitive filename order.
    pub files: Vec<SourceFile>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct WorkspaceRegistration {
    pub protocol_version: u32,
    pub workspace_id: WorkspaceId,
    pub root: String,
    pub name: String,
    pub manifest: SourceManifest,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DocumentRequest {
    pub workspace_id: WorkspaceId,
    pub document_id: DocumentId,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DocumentSnapshot {
    pub workspace_id: WorkspaceId,
    pub document: SourceFile,
    pub content: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SaveDocumentRequest {
    pub workspace_id: WorkspaceId,
    pub document_id: DocumentId,
    pub expected_revision: Revision,
    pub expected_digest: ContentDigest,
    /// Renderer edit revision is echoed, so an older acknowledgement cannot
    /// incorrectly clear a buffer edited while this save was in flight.
    pub edit_revision: Revision,
    pub content: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SaveAcknowledgement {
    pub workspace_id: WorkspaceId,
    pub document_id: DocumentId,
    pub revision: Revision,
    pub content_digest: ContentDigest,
    pub edit_revision: Revision,
    /// Zero for a content-equal save, whose file is never opened for writing.
    pub bytes_written: u64,
    pub unchanged: bool,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ScaffoldTemplate {
    #[default]
    Standard,
    Bare,
    Blink,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ScaffoldOptions {
    pub name: String,
    pub include_h: bool,
    pub include_cpp: bool,
    pub template: ScaffoldTemplate,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn save_contract_round_trips_and_refuses_unexpected_authority() {
        let request = SaveDocumentRequest {
            workspace_id: WorkspaceId("workspace-session-1".into()),
            document_id: DocumentId("document-1".into()),
            expected_revision: Revision(3),
            expected_digest: ContentDigest("abc".into()),
            edit_revision: Revision(7),
            content: "// café\r\n".into(),
        };
        let mut value = serde_json::to_value(&request).unwrap();
        assert_eq!(value["editRevision"], 7);
        assert_eq!(
            serde_json::from_value::<SaveDocumentRequest>(value.clone()).unwrap(),
            request
        );
        value["path"] = "outside.cpp".into();
        assert!(serde_json::from_value::<SaveDocumentRequest>(value).is_err());
    }
}
