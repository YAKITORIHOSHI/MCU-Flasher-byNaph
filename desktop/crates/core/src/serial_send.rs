//! Deterministic policy port of `main/core/serial_send.py::SerialSendQueue`.
//!
//! No threads, serial drivers, hardware handles, locks or IPC are opened here.
//! A future native adapter must serialize queue transitions, capture connection
//! evidence under its own lock without blocking GUI admission, and perform at
//! most one finite-timeout driver write for each `BeginOutcome::Write`. Tokens,
//! generations, port, baud, busy and worker-start evidence must come from native
//! state, never renderer claims. No partial/error completion is requeued.

use std::collections::VecDeque;

pub const MAX_MESSAGE_BYTES: usize = 64 * 1024;
pub const MAX_QUEUED_BYTES: usize = 256 * 1024;
pub const MAX_MESSAGES: usize = 16;

/// Native connection-instance identity, independent of port spelling.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ConnectionToken(pub u64);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SendId(pub u64);

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SerialConnection {
    pub generation: u64,
    pub token: ConnectionToken,
    pub port: String,
    pub baud: Option<u32>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OperationState {
    Idle,
    Compile,
    Reserved,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SerialSnapshot {
    pub connection: Option<SerialConnection>,
    pub is_open: bool,
    pub operation: OperationState,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SendRequest {
    id: SendId,
    connection: SerialConnection,
    text: String,
    data: Vec<u8>,
}

impl SendRequest {
    pub fn id(&self) -> SendId {
        self.id
    }
    pub fn connection(&self) -> &SerialConnection {
        &self.connection
    }
    pub fn text(&self) -> &str {
        &self.text
    }
    pub fn data(&self) -> &[u8] {
        &self.data
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SubmitRefusal {
    MessageTooLarge,
    ConnectionChanging,
    Unavailable,
    Stopped,
    QueueFull,
    WorkerStartFailed,
    RequestIdExhausted,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BeginOutcome {
    Idle,
    Stopped,
    AlreadyActive,
    Skipped(SendId),
    Write(SendId),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum WriteResult {
    Written(usize),
    Failed,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FinishRefusal {
    NoActiveRequest,
    WrongActiveRequest,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DisconnectEffect {
    AdvanceGeneration(u64),
    /// Python generations are unbounded integers. At the typed native limit,
    /// disconnect and retire the session; never wrap or reuse its generation.
    GenerationExhausted,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SendEcho {
    pub generation: u64,
    pub text: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct FailureEffect {
    /// Close only the captured connection, even when its failure is stale.
    pub close_connection: ConnectionToken,
    pub disconnect: Option<DisconnectEffect>,
    pub discarded_messages: usize,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct FinishEffect {
    pub id: SendId,
    pub echo: Option<SendEcho>,
    pub failure: Option<FailureEffect>,
}

#[derive(Debug)]
pub struct SerialSendQueue {
    pending: VecDeque<SendRequest>,
    active: Option<SendRequest>,
    queued_bytes: usize,
    stopped: bool,
    worker_started: bool,
    next_id: Option<u64>,
}

impl Default for SerialSendQueue {
    fn default() -> Self {
        Self {
            pending: VecDeque::new(),
            active: None,
            queued_bytes: 0,
            stopped: false,
            worker_started: false,
            next_id: Some(1),
        }
    }
}

impl SerialSendQueue {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn pending(&self) -> impl Iterator<Item = &SendRequest> {
        self.pending.iter()
    }

    pub fn active(&self) -> Option<&SendRequest> {
        self.active.as_ref()
    }

    pub fn queued_bytes(&self) -> usize {
        self.queued_bytes
    }

    pub fn is_stopped(&self) -> bool {
        self.stopped
    }

    pub fn worker_started(&self) -> bool {
        self.worker_started
    }

    /// `lock_available` models nonblocking native connection-lock admission.
    /// `worker_start_fails` records a native startup failure only when the
    /// worker has not started. Refusal leaves input and the queue unchanged.
    pub fn submit(
        &mut self,
        text: &str,
        ending: &str,
        snapshot: &SerialSnapshot,
        lock_available: bool,
        worker_start_fails: bool,
    ) -> Result<SendId, SubmitRefusal> {
        let suffix = match ending {
            "nl" => "\n",
            "cr" => "\r",
            "none" => "",
            _ => "\r\n",
        };
        // Rust strings are already valid UTF-8. Measuring bytes before copying
        // enforces the same final payload limit without an expansion allocation.
        let length = text
            .len()
            .checked_add(suffix.len())
            .filter(|length| *length <= MAX_MESSAGE_BYTES)
            .ok_or(SubmitRefusal::MessageTooLarge)?;
        if !lock_available {
            return Err(SubmitRefusal::ConnectionChanging);
        }
        let connection = snapshot
            .connection
            .as_ref()
            .filter(|_| snapshot.is_open && snapshot.operation != OperationState::Reserved)
            .ok_or(SubmitRefusal::Unavailable)?;
        if self.stopped {
            return Err(SubmitRefusal::Stopped);
        }
        if self.pending.len() + usize::from(self.active.is_some()) >= MAX_MESSAGES
            || self.queued_bytes + length > MAX_QUEUED_BYTES
        {
            return Err(SubmitRefusal::QueueFull);
        }
        let id = self.next_id.ok_or(SubmitRefusal::RequestIdExhausted)?;
        if !self.worker_started && worker_start_fails {
            return Err(SubmitRefusal::WorkerStartFailed);
        }
        let mut data = Vec::with_capacity(length);
        data.extend_from_slice(text.as_bytes());
        data.extend_from_slice(suffix.as_bytes());
        self.pending.push_back(SendRequest {
            id: SendId(id),
            connection: connection.clone(),
            text: text.into(),
            data,
        });
        self.queued_bytes += length;
        self.worker_started = true;
        self.next_id = id.checked_add(1);
        Ok(SendId(id))
    }

    /// One worker iteration. A stale queued request is removed without exposing
    /// a write; a begun write remains active until its exact completion arrives.
    pub fn begin(&mut self, snapshot: &SerialSnapshot) -> BeginOutcome {
        if self.active.is_some() {
            return BeginOutcome::AlreadyActive;
        }
        if self.stopped {
            return BeginOutcome::Stopped;
        }
        let Some(request) = self.pending.pop_front() else {
            return BeginOutcome::Idle;
        };
        let id = request.id;
        if !self.matches(&request, snapshot) {
            self.queued_bytes -= request.data.len();
            return BeginOutcome::Skipped(id);
        }
        self.active = Some(request);
        BeginOutcome::Write(id)
    }

    /// No retry, flush or remainder-write action exists. Wrong/duplicate
    /// completion IDs leave the current request and accounting untouched.
    pub fn finish(
        &mut self,
        id: SendId,
        result: WriteResult,
        snapshot: &SerialSnapshot,
    ) -> Result<FinishEffect, FinishRefusal> {
        let active = self.active.as_ref().ok_or(FinishRefusal::NoActiveRequest)?;
        if active.id != id {
            return Err(FinishRefusal::WrongActiveRequest);
        }
        let succeeded = result == WriteResult::Written(active.data.len());
        let echo = succeeded && self.matches(active, snapshot);
        let request = self.active.take().expect("active request checked above");
        self.queued_bytes -= request.data.len();
        if succeeded {
            return Ok(FinishEffect {
                id,
                echo: echo.then_some(SendEcho {
                    generation: request.connection.generation,
                    text: request.text,
                }),
                failure: None,
            });
        }
        let discarded_messages = self.discard(
            Some(request.connection.generation),
            Some(request.connection.token),
        );
        // The legacy failure path deliberately tests only generation and native
        // object identity here. Changed baud/port/open/busy state suppresses a
        // successful echo but does not save this same failed connection.
        let disconnect = snapshot
            .connection
            .as_ref()
            .filter(|current| {
                current.generation == request.connection.generation
                    && current.token == request.connection.token
            })
            .map(|current| match current.generation.checked_add(1) {
                Some(next) => DisconnectEffect::AdvanceGeneration(next),
                None => DisconnectEffect::GenerationExhausted,
            });
        Ok(FinishEffect {
            id,
            echo: None,
            failure: Some(FailureEffect {
                close_connection: request.connection.token,
                disconnect,
                discarded_messages,
            }),
        })
    }

    /// Drop only unsent requests matching both optional native identity filters.
    pub fn discard(
        &mut self,
        generation: Option<u64>,
        connection: Option<ConnectionToken>,
    ) -> usize {
        let before = self.pending.len();
        let mut removed_bytes = 0;
        self.pending.retain(|request| {
            let remove = generation.is_none_or(|value| request.connection.generation == value)
                && connection.is_none_or(|value| request.connection.token == value);
            if remove {
                removed_bytes += request.data.len();
            }
            !remove
        });
        self.queued_bytes -= removed_bytes;
        before - self.pending.len()
    }

    pub fn stop(&mut self) -> usize {
        self.stopped = true;
        self.discard(None, None)
    }

    fn matches(&self, request: &SendRequest, snapshot: &SerialSnapshot) -> bool {
        !self.stopped
            && snapshot.is_open
            && snapshot.operation != OperationState::Reserved
            && snapshot.connection.as_ref() == Some(&request.connection)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn snapshot() -> SerialSnapshot {
        SerialSnapshot {
            connection: Some(SerialConnection {
                generation: 1,
                token: ConnectionToken(10),
                port: "FIXTURE".into(),
                baud: Some(115_200),
            }),
            is_open: true,
            operation: OperationState::Idle,
        }
    }

    fn accepted(queue: &mut SerialSendQueue, text: &str, state: &SerialSnapshot) -> SendId {
        queue.submit(text, "both", state, true, false).unwrap()
    }

    #[test]
    fn unicode_suffixes_and_unknown_ending_preserve_exact_bytes_once() {
        let state = snapshot();
        let mut queue = SerialSendQueue::new();
        for (ending, expected) in [
            ("both", "héllo 漢字\r\n"),
            ("nl", "héllo 漢字\n"),
            ("cr", "héllo 漢字\r"),
            ("none", "héllo 漢字"),
            ("unexpected", "héllo 漢字\r\n"),
        ] {
            let id = queue
                .submit("héllo 漢字", ending, &state, true, false)
                .unwrap();
            assert_eq!(queue.begin(&state), BeginOutcome::Write(id));
            assert_eq!(queue.active().unwrap().data(), expected.as_bytes());
            let effect = queue
                .finish(id, WriteResult::Written(expected.len()), &state)
                .unwrap();
            assert_eq!(
                effect.echo,
                Some(SendEcho {
                    generation: 1,
                    text: "héllo 漢字".into()
                })
            );
            assert_eq!(effect.failure, None);
            assert_eq!(queue.begin(&state), BeginOutcome::Idle);
            assert_eq!(queue.queued_bytes(), 0);
        }
    }

    #[test]
    fn utf8_payload_and_suffix_are_bounded_before_capture() {
        let state = snapshot();
        let mut queue = SerialSendQueue::new();
        for (text, ending) in [
            ("x".repeat(65_537), "none"),
            ("x".repeat(65_535), "both"),
            ("漢".repeat(21_846), "none"),
        ] {
            assert_eq!(
                queue.submit(&text, ending, &state, true, false),
                Err(SubmitRefusal::MessageTooLarge)
            );
            assert_eq!(queue.queued_bytes(), 0);
        }
        assert_eq!(
            queue.submit(&"x".repeat(65_536), "none", &state, true, false),
            Ok(SendId(1))
        );
        assert_eq!(queue.queued_bytes(), 65_536);
    }

    #[test]
    fn admission_refusals_preserve_ids_and_compile_is_allowed() {
        let mut state = snapshot();
        let mut queue = SerialSendQueue::new();
        assert_eq!(
            queue.submit("input", "none", &state, false, false),
            Err(SubmitRefusal::ConnectionChanging)
        );
        state.operation = OperationState::Reserved;
        assert_eq!(
            queue.submit("input", "none", &state, true, false),
            Err(SubmitRefusal::Unavailable)
        );
        state.operation = OperationState::Compile;
        state.is_open = false;
        assert_eq!(
            queue.submit("input", "none", &state, true, false),
            Err(SubmitRefusal::Unavailable)
        );
        state.is_open = true;
        assert_eq!(
            queue.submit("input", "none", &state, true, true),
            Err(SubmitRefusal::WorkerStartFailed)
        );
        assert!(!queue.worker_started());
        assert_eq!(queue.queued_bytes(), 0);
        assert_eq!(accepted(&mut queue, "compile", &state), SendId(1));
        assert!(queue.worker_started());
        // A later thread-start failure flag is irrelevant to an existing worker.
        assert_eq!(
            queue.submit("next", "none", &state, true, true),
            Ok(SendId(2))
        );
        state.connection = None;
        assert_eq!(
            queue.submit("input", "none", &state, true, false),
            Err(SubmitRefusal::Unavailable)
        );
    }

    #[test]
    fn message_and_byte_budgets_include_the_active_write() {
        let state = snapshot();
        let mut queue = SerialSendQueue::new();
        let first = accepted(&mut queue, "active", &state);
        assert_eq!(queue.begin(&state), BeginOutcome::Write(first));
        for _ in 0..15 {
            accepted(&mut queue, "pending", &state);
        }
        assert_eq!(
            queue.submit("rejected", "none", &state, true, false),
            Err(SubmitRefusal::QueueFull)
        );
        let mut queue = SerialSendQueue::new();
        let text = "x".repeat(65_534);
        let first = accepted(&mut queue, &text, &state);
        queue.begin(&state);
        for _ in 0..3 {
            accepted(&mut queue, &text, &state);
        }
        assert_eq!(queue.queued_bytes(), 262_144);
        assert_eq!(
            queue.submit("x", "none", &state, true, false),
            Err(SubmitRefusal::QueueFull)
        );
        queue
            .finish(first, WriteResult::Written(65_536), &state)
            .unwrap();
        assert_eq!(queue.queued_bytes(), 196_608);
        assert_eq!(
            queue.submit(&text, "both", &state, true, false),
            Ok(SendId(5))
        );
    }

    #[test]
    fn each_changed_connection_field_skips_pending_without_a_write() {
        let original = snapshot();
        let mut changed = Vec::new();
        let mut state = original.clone();
        state.connection.as_mut().unwrap().generation += 1;
        changed.push(state);
        let mut state = original.clone();
        state.connection.as_mut().unwrap().token = ConnectionToken(11);
        changed.push(state);
        let mut state = original.clone();
        state.connection.as_mut().unwrap().port = "OTHER".into();
        changed.push(state);
        let mut state = original.clone();
        state.connection.as_mut().unwrap().baud = Some(9_600);
        changed.push(state);
        let mut state = original.clone();
        state.is_open = false;
        changed.push(state);
        let mut state = original.clone();
        state.operation = OperationState::Reserved;
        changed.push(state);
        let mut state = original.clone();
        state.connection = None;
        changed.push(state);
        for state in changed {
            let mut queue = SerialSendQueue::new();
            let id = accepted(&mut queue, "must not replay", &original);
            assert_eq!(queue.begin(&state), BeginOutcome::Skipped(id));
            assert!(queue.active().is_none());
            assert_eq!(queue.queued_bytes(), 0);
            assert_eq!(queue.begin(&original), BeginOutcome::Idle);
        }
    }

    #[test]
    fn completed_write_is_not_echoed_after_port_baud_or_connection_changes() {
        let original = snapshot();
        for change in [
            |state: &mut SerialSnapshot| {
                state.connection.as_mut().unwrap().port = "NEW FIXTURE".into();
            },
            |state: &mut SerialSnapshot| {
                state.connection.as_mut().unwrap().baud = Some(9_600);
            },
            |state: &mut SerialSnapshot| {
                state.connection.as_mut().unwrap().generation += 1;
            },
            |state: &mut SerialSnapshot| {
                state.connection.as_mut().unwrap().token = ConnectionToken(20);
            },
            |state: &mut SerialSnapshot| {
                state.is_open = false;
            },
            |state: &mut SerialSnapshot| {
                state.operation = OperationState::Reserved;
            },
            |state: &mut SerialSnapshot| {
                state.connection = None;
            },
        ] {
            let mut queue = SerialSendQueue::new();
            let id = accepted(&mut queue, "written", &original);
            queue.begin(&original);
            let mut changed = original.clone();
            change(&mut changed);
            let effect = queue.finish(id, WriteResult::Written(9), &changed).unwrap();
            assert_eq!(effect.echo, None);
            assert_eq!(effect.failure, None);
            assert_eq!(queue.queued_bytes(), 0);
        }
    }

    #[test]
    fn partial_failure_is_terminal_and_discards_only_its_native_identity() {
        let original = snapshot();
        let mut replacement = original.clone();
        replacement.connection.as_mut().unwrap().generation = 2;
        replacement.connection.as_mut().unwrap().token = ConnectionToken(20);
        replacement.connection.as_mut().unwrap().port = "NEW FIXTURE".into();
        for result in [WriteResult::Written(2), WriteResult::Failed] {
            let mut queue = SerialSendQueue::new();
            let id = accepted(&mut queue, "active", &original);
            queue.begin(&original);
            accepted(&mut queue, "old pending", &original);
            let next = accepted(&mut queue, "new pending", &replacement);
            let effect = queue.finish(id, result, &replacement).unwrap();
            assert_eq!(effect.echo, None);
            assert_eq!(
                effect.failure,
                Some(FailureEffect {
                    close_connection: ConnectionToken(10),
                    disconnect: None,
                    discarded_messages: 1,
                })
            );
            assert_eq!(queue.begin(&replacement), BeginOutcome::Write(next));
            assert_eq!(queue.active().unwrap().text(), "new pending");
            assert_eq!(queue.queued_bytes(), 13);
        }
    }

    #[test]
    fn failure_disconnect_uses_generation_and_token_even_after_other_state_changes() {
        let original = snapshot();
        let mut changed = original.clone();
        changed.connection.as_mut().unwrap().port = "changed spelling".into();
        changed.connection.as_mut().unwrap().baud = None;
        changed.is_open = false;
        changed.operation = OperationState::Reserved;
        let mut queue = SerialSendQueue::new();
        let id = accepted(&mut queue, "active", &original);
        queue.begin(&original);
        accepted(&mut queue, "pending", &original);
        let effect = queue.finish(id, WriteResult::Failed, &changed).unwrap();
        assert_eq!(
            effect.failure,
            Some(FailureEffect {
                close_connection: ConnectionToken(10),
                disconnect: Some(DisconnectEffect::AdvanceGeneration(2)),
                discarded_messages: 1,
            })
        );
        assert_eq!(queue.queued_bytes(), 0);
        assert_eq!(queue.begin(&original), BeginOutcome::Idle);
    }

    #[test]
    fn stop_retains_started_bytes_until_completion_and_prevents_echo_or_replay() {
        let state = snapshot();
        let mut queue = SerialSendQueue::new();
        let id = accepted(&mut queue, "active", &state);
        queue.begin(&state);
        accepted(&mut queue, "pending", &state);
        assert_eq!(queue.stop(), 1);
        assert_eq!(queue.queued_bytes(), 8);
        assert_eq!(queue.stop(), 0);
        assert_eq!(
            queue.submit("next", "none", &state, true, false),
            Err(SubmitRefusal::Stopped)
        );
        let effect = queue.finish(id, WriteResult::Written(8), &state).unwrap();
        assert_eq!(effect.echo, None);
        assert_eq!(effect.failure, None);
        assert_eq!(queue.queued_bytes(), 0);
        assert_eq!(queue.begin(&state), BeginOutcome::Stopped);
    }

    #[test]
    fn incorrect_or_duplicate_completions_cannot_consume_another_write() {
        let state = snapshot();
        let mut queue = SerialSendQueue::new();
        let first = accepted(&mut queue, "one", &state);
        let second = accepted(&mut queue, "two", &state);
        assert_eq!(queue.begin(&state), BeginOutcome::Write(first));
        assert_eq!(queue.begin(&state), BeginOutcome::AlreadyActive);
        assert_eq!(
            queue.finish(second, WriteResult::Written(5), &state),
            Err(FinishRefusal::WrongActiveRequest)
        );
        assert_eq!(queue.queued_bytes(), 10);
        assert_eq!(queue.active().unwrap().id(), first);
        queue
            .finish(first, WriteResult::Written(5), &state)
            .unwrap();
        assert_eq!(queue.begin(&state), BeginOutcome::Write(second));
        assert_eq!(
            queue.finish(first, WriteResult::Failed, &state),
            Err(FinishRefusal::WrongActiveRequest)
        );
        assert_eq!(queue.active().unwrap().id(), second);
        queue
            .finish(second, WriteResult::Written(5), &state)
            .unwrap();
        assert_eq!(
            queue.finish(second, WriteResult::Failed, &state),
            Err(FinishRefusal::NoActiveRequest)
        );
    }

    #[test]
    fn discard_filters_are_conjunctive_and_never_remove_active() {
        let original = snapshot();
        let mut same_token = original.clone();
        same_token.connection.as_mut().unwrap().generation = 2;
        let mut same_generation = original.clone();
        same_generation.connection.as_mut().unwrap().token = ConnectionToken(20);
        let mut queue = SerialSendQueue::new();
        let active = accepted(&mut queue, "active", &original);
        queue.begin(&original);
        accepted(&mut queue, "original", &original);
        accepted(&mut queue, "generation2", &same_token);
        accepted(&mut queue, "token20", &same_generation);
        assert_eq!(queue.discard(Some(1), Some(ConnectionToken(10))), 1);
        assert_eq!(queue.discard(Some(1), None), 1);
        assert_eq!(queue.discard(None, Some(ConnectionToken(10))), 1);
        assert_eq!(queue.discard(None, None), 0);
        assert_eq!(queue.active().unwrap().id(), active);
        assert_eq!(queue.queued_bytes(), 8);
    }

    #[test]
    fn typed_generation_exhaustion_never_wraps_and_request_ids_never_reuse() {
        let mut state = snapshot();
        state.connection.as_mut().unwrap().generation = u64::MAX;
        let mut queue = SerialSendQueue::new();
        queue.next_id = Some(u64::MAX);
        let id = accepted(&mut queue, "active", &state);
        assert_eq!(id, SendId(u64::MAX));
        assert_eq!(
            queue.submit("next", "none", &state, true, false),
            Err(SubmitRefusal::RequestIdExhausted)
        );
        queue.begin(&state);
        assert_eq!(
            queue
                .finish(id, WriteResult::Failed, &state)
                .unwrap()
                .failure
                .unwrap()
                .disconnect,
            Some(DisconnectEffect::GenerationExhausted)
        );
        assert_eq!(queue.queued_bytes(), 0);
    }
}
