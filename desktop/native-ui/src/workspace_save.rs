//! Bounded save transfer and worker ownership. Renderer data supplies no paths,
//! disk baselines or window authority. Only a native command starts a transfer.
use crate::RendererMessage;
use mcu_contracts::{Revision, SaveAcknowledgement, SaveDocumentRequest};
use mcu_host::source_save::NativeProjectSession;
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use std::time::{Duration, Instant};

pub(crate) const OWNER: &str = "native-workspace-reader";

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SaveDocumentInfo {
    pub document_id: String,
    pub edit_revision: u64,
    pub bytes: usize,
}

pub(crate) struct SaveInput {
    info: SaveDocumentInfo,
    content: String,
}

pub(crate) struct SaveTransfer {
    pub id: u64,
    allowed: BTreeSet<String>,
    started: Option<Vec<SaveInput>>,
    header_complete: bool,
    next: usize,
    chunks: usize,
    deadline: Instant,
}

impl SaveTransfer {
    pub fn new(id: u64, allowed: BTreeSet<String>) -> Self {
        Self {
            id,
            allowed,
            started: None,
            header_complete: false,
            next: 0,
            chunks: 0,
            deadline: Instant::now() + Duration::from_secs(10),
        }
    }
    pub fn timed_out(&self) -> bool {
        Instant::now() >= self.deadline
    }
    pub fn accept(&mut self, message: &RendererMessage) -> Result<Option<Vec<SaveInput>>, String> {
        if self.timed_out() {
            return Err("Save transfer timed out. Your buffer remains unsaved.".into());
        }
        match message {
            RendererMessage::SaveStarted {
                request_id,
                documents,
                complete,
            } if *request_id == self.id => {
                if self.header_complete || documents.len() > 32 {
                    return Err("Duplicate or oversized save header.".into());
                }
                let mut inputs = self.started.take().unwrap_or_default();
                if inputs.len() + documents.len() > 256 {
                    return Err("Save document limit exceeded.".into());
                }
                let mut ids: BTreeSet<_> = inputs
                    .iter()
                    .map(|input| input.info.document_id.clone())
                    .collect();
                let mut bytes = inputs.iter().map(|input| input.info.bytes).sum::<usize>();
                for document in documents {
                    if !self.allowed.contains(&document.document_id)
                        || !ids.insert(document.document_id.clone())
                        || document.bytes > 8 * 1024 * 1024
                        || document.edit_revision == 0
                        || document.edit_revision >= (1 << 53)
                    {
                        return Err("Save does not match the native document selection.".into());
                    }
                    bytes = bytes
                        .checked_add(document.bytes)
                        .ok_or("Save byte budget overflow.")?;
                    if bytes > 32 * 1024 * 1024 {
                        return Err("Save exceeds the workspace byte budget.".into());
                    }
                    inputs.push(SaveInput {
                        info: SaveDocumentInfo {
                            document_id: document.document_id.clone(),
                            edit_revision: document.edit_revision,
                            bytes: document.bytes,
                        },
                        content: String::new(),
                    });
                }
                self.started = Some(inputs);
                self.header_complete = *complete;
                Ok(None)
            }
            RendererMessage::SaveChunk {
                request_id,
                document_id,
                offset,
                content,
            } if *request_id == self.id => {
                if !self.header_complete {
                    return Err("Save header is incomplete.".into());
                }
                let inputs = self
                    .started
                    .as_mut()
                    .ok_or("Save data arrived before its header.")?;
                while self.next < inputs.len()
                    && inputs[self.next].content.len() == inputs[self.next].info.bytes
                {
                    self.next += 1;
                }
                let input = inputs
                    .get_mut(self.next)
                    .ok_or("Unexpected additional save data.")?;
                self.chunks += 1;
                if self.chunks > 32768
                    || content.is_empty()
                    || content.len() > 8192
                    || input.info.document_id != *document_id
                    || input.content.len() != *offset
                    || content.len() > input.info.bytes.saturating_sub(input.content.len())
                {
                    return Err("Save chunk is stale, out of order or exceeds its budget.".into());
                }
                input.content.push_str(content);
                Ok(None)
            }
            RendererMessage::SaveFinished { request_id } if *request_id == self.id => {
                if !self.header_complete {
                    return Err("Save header is incomplete.".into());
                }
                let inputs = self
                    .started
                    .as_ref()
                    .ok_or("Save completed before its header.")?;
                if inputs
                    .iter()
                    .any(|input| input.content.len() != input.info.bytes)
                {
                    return Err("Save source bytes are incomplete.".into());
                }
                Ok(self.started.take())
            }
            _ => Err("Save response does not match the active native request.".into()),
        }
    }
}

/// One worker owns the native service exclusively until it returns its result.
pub struct SaveWork {
    pub(crate) id: u64,
    pub(crate) service: NativeProjectSession,
    pub(crate) inputs: Vec<SaveInput>,
}

pub struct SaveResult {
    pub(crate) id: u64,
    pub(crate) service: NativeProjectSession,
    acknowledgements: Vec<SaveAcknowledgement>,
    error: Option<String>,
}

impl SaveWork {
    pub fn run(mut self) -> SaveResult {
        let mut acknowledgements = Vec::new();
        let mut error = None;
        for input in self.inputs {
            let Some(baseline) = self
                .service
                .documents()
                .iter()
                .find(|document| document.document.document_id.0 == input.info.document_id)
            else {
                error = Some(
                    "The native source selection changed. Remaining buffers were not written."
                        .into(),
                );
                break;
            };
            let request = SaveDocumentRequest {
                workspace_id: self.service.workspace_id().clone(),
                document_id: baseline.document.document_id.clone(),
                expected_revision: baseline.document.revision,
                expected_digest: baseline.document.content_digest.clone(),
                edit_revision: Revision(input.info.edit_revision),
                content: input.content,
            };
            match self.service.save_document(OWNER, request) {
                Ok(acknowledgement) => acknowledgements.push(acknowledgement),
                Err(failure) => {
                    error = Some(failure.message);
                    break;
                }
            }
        }
        SaveResult {
            id: self.id,
            service: self.service,
            acknowledgements,
            error,
        }
    }
    /// Returns the service without touching the filesystem if worker creation fails.
    pub fn cancel_before_run(self, error: String) -> SaveResult {
        SaveResult {
            id: self.id,
            service: self.service,
            acknowledgements: Vec::new(),
            error: Some(error),
        }
    }
}
impl SaveResult {
    pub fn status_text(&self) -> String {
        match &self.error {
            Some(error) => format!("{} file(s) saved. {error}", self.acknowledgements.len()),
            None => format!("{} file(s) saved.", self.acknowledgements.len()),
        }
    }
    pub(crate) fn script(&self) -> Result<String, String> {
        let json = serde_json::to_string(&serde_json::json!({"requestId":self.id,
            "saved": self.acknowledgements.iter().map(|ack| serde_json::json!({"id":ack.document_id.0,"editRevision":ack.edit_revision.0})).collect::<Vec<_>>(),
            "error":self.error})).map_err(|error| error.to_string())?;
        Ok(format!("window.__mcuFinishSave({json});"))
    }
}

#[cfg(test)]
mod checks {
    use super::*;
    fn transfer() -> SaveTransfer {
        SaveTransfer::new(1, BTreeSet::from(["known".into()]))
    }
    fn start(bytes: usize) -> RendererMessage {
        RendererMessage::SaveStarted {
            request_id: 1,
            documents: vec![SaveDocumentInfo {
                document_id: "known".into(),
                edit_revision: 1,
                bytes,
            }],
            complete: true,
        }
    }
    #[test]
    fn unknown_replayed_and_incomplete_payloads_never_complete() {
        let mut state = transfer();
        assert!(state
            .accept(&RendererMessage::SaveFinished { request_id: 1 })
            .is_err());
        state.accept(&start(3)).unwrap();
        assert!(state.accept(&start(3)).is_err());
        assert!(state
            .accept(&RendererMessage::SaveFinished { request_id: 2 })
            .is_err());
        assert!(state
            .accept(&RendererMessage::SaveFinished { request_id: 1 })
            .is_err());
    }
    #[test]
    fn exact_utf8_offsets_and_empty_files_complete_without_normalizing_bytes() {
        let mut state = transfer();
        state.accept(&start(3)).unwrap();
        state
            .accept(&RendererMessage::SaveChunk {
                request_id: 1,
                document_id: "known".into(),
                offset: 0,
                content: "λ\n".into(),
            })
            .unwrap();
        let completed = state
            .accept(&RendererMessage::SaveFinished { request_id: 1 })
            .unwrap()
            .unwrap();
        assert_eq!(completed[0].content.as_bytes(), "λ\n".as_bytes());
        let mut state = transfer();
        state.accept(&start(0)).unwrap();
        assert!(state
            .accept(&RendererMessage::SaveFinished { request_id: 1 })
            .unwrap()
            .is_some());
    }
    #[test]
    fn foreign_ids_and_out_of_order_bytes_are_refused() {
        let mut state = transfer();
        let forged = RendererMessage::SaveStarted {
            request_id: 1,
            documents: vec![SaveDocumentInfo {
                document_id: "../../Sketch.ino".into(),
                edit_revision: 1,
                bytes: 1,
            }],
            complete: true,
        };
        assert!(state.accept(&forged).is_err());
        state.accept(&start(3)).unwrap();
        assert!(state
            .accept(&RendererMessage::SaveChunk {
                request_id: 1,
                document_id: "known".into(),
                offset: 1,
                content: "bad".into()
            })
            .is_err());
        assert!(state
            .accept(&RendererMessage::SaveFinished { request_id: 1 })
            .is_err());
    }
    #[test]
    fn batched_headers_support_all_256_sources_without_enlarging_ipc_frames() {
        let ids: BTreeSet<String> = (0..256).map(|index| format!("document-{index}")).collect();
        let mut state = SaveTransfer::new(1, ids);
        for batch in 0..8 {
            let message = RendererMessage::SaveStarted {
                request_id: 1,
                documents: (batch * 32..batch * 32 + 32)
                    .map(|index| SaveDocumentInfo {
                        document_id: format!("document-{index}"),
                        edit_revision: 1,
                        bytes: 0,
                    })
                    .collect(),
                complete: batch == 7,
            };
            let json = serde_json::to_string(&message).unwrap();
            assert!(crate::parse_message(crate::Island::Editor, &json).is_ok());
            state.accept(&message).unwrap();
        }
        assert_eq!(
            state
                .accept(&RendererMessage::SaveFinished { request_id: 1 })
                .unwrap()
                .unwrap()
                .len(),
            256
        );
    }
    #[test]
    fn expired_transfers_and_terminal_origin_are_refused() {
        let mut state = transfer();
        state.deadline = Instant::now();
        assert!(state.accept(&start(0)).is_err());
        let json = serde_json::to_string(&start(0)).unwrap();
        assert!(crate::parse_message(crate::Island::Terminal, &json).is_err());
    }
}
