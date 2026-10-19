//! Bounded, memory-only traces for the serial Send policy. No serial API,
//! application settings, native process execution or filesystem access exists.
use mcu_core::serial_send::{
    BeginOutcome, ConnectionToken, DisconnectEffect, OperationState, SendId, SendRequest,
    SerialConnection, SerialSendQueue, SerialSnapshot, SubmitRefusal, WriteResult,
};
use mcu_core::source_digest;
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::io::{self, Read, Write};

const MAX_INPUT_BYTES: u64 = 1_000_000;
const MAX_OUTPUT_BYTES: usize = 16 * 1024 * 1024;

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Batch {
    schema_version: u32,
    traces: Vec<Trace>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Trace {
    label: String,
    initial_state: NativeState,
    actions: Vec<Action>,
}

#[derive(Clone, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct NativeState {
    generation: u64,
    connection: Option<u64>,
    port: String,
    baud: Option<u32>,
    is_open: bool,
    busy: bool,
    operation: Option<String>,
    lock_available: bool,
}

#[derive(Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
enum Action {
    SetState {
        state: NativeState,
    },
    Submit {
        text: String,
        #[serde(default = "default_ending")]
        ending: String,
        #[serde(default, rename = "workerStartFails")]
        worker_start_fails: bool,
    },
    Begin {},
    Finish {
        written: Option<usize>,
        #[serde(default)]
        error: bool,
        #[serde(rename = "requestId")]
        request_id: Option<u64>,
    },
    Discard {
        generation: Option<u64>,
        connection: Option<u64>,
    },
    Stop {},
}

fn default_ending() -> String {
    "both".into()
}

fn validate_state(state: &NativeState) -> Result<(), &'static str> {
    if state.port.len() > 4096 || state.operation.as_ref().is_some_and(|s| s.len() > 128) {
        return Err("Fixture native state exceeds its text budget");
    }
    Ok(())
}

struct Controller {
    state: NativeState,
    // Python requests retain connection object identity, including its current
    // is_open flag. A close changes only that token, even after replacement.
    connections: BTreeMap<u64, bool>,
    queue: SerialSendQueue,
}

impl Controller {
    fn new(state: NativeState) -> Self {
        let mut controller = Self {
            state: state.clone(),
            connections: BTreeMap::new(),
            queue: SerialSendQueue::new(),
        };
        controller.set_state(state);
        controller
    }

    fn set_state(&mut self, state: NativeState) {
        if let Some(token) = state.connection {
            self.connections.insert(token, state.is_open);
        }
        self.state = state;
    }

    fn snapshot(&self) -> SerialSnapshot {
        SerialSnapshot {
            connection: self.state.connection.map(|token| SerialConnection {
                generation: self.state.generation,
                token: ConnectionToken(token),
                port: self.state.port.clone(),
                baud: self.state.baud,
            }),
            is_open: self.is_open(),
            operation: if !self.state.busy {
                OperationState::Idle
            } else if self.state.operation.as_deref() == Some("compile") {
                OperationState::Compile
            } else {
                OperationState::Reserved
            },
        }
    }

    fn is_open(&self) -> bool {
        self.state
            .connection
            .and_then(|token| self.connections.get(&token))
            .copied()
            .unwrap_or(false)
    }

    fn state_json(&self) -> Value {
        json!({"generation":self.state.generation,"connection":self.state.connection,
            "port":self.state.port,"baud":self.state.baud,"isOpen":self.is_open(),
            "busy":self.state.busy,"operation":self.state.operation,
            "lockAvailable":self.state.lock_available})
    }

    fn step(&mut self, action: Action) -> Result<Value, &'static str> {
        let mut effects = json!({"write":null,"close":null,"disconnect":null,"echo":null});
        let snapshot = self.snapshot();
        let result = match action {
            Action::SetState { state } => {
                self.set_state(state);
                json!({"kind":"stateChanged"})
            }
            Action::Submit {
                text,
                ending,
                worker_start_fails,
            } => {
                match self.queue.submit(
                    &text,
                    &ending,
                    &snapshot,
                    self.state.lock_available,
                    worker_start_fails,
                ) {
                    Ok(id) => json!({"kind":"submit","accepted":true,"id":id.0,"reason":null}),
                    Err(reason) => {
                        json!({"kind":"submit","accepted":false,"id":null,"reason":match reason {
                            SubmitRefusal::MessageTooLarge => "messageTooLarge",
                            SubmitRefusal::ConnectionChanging => "connectionChanging",
                            SubmitRefusal::Unavailable => "unavailable",
                            SubmitRefusal::Stopped => "stopped",
                            SubmitRefusal::QueueFull => "queueFull",
                            SubmitRefusal::WorkerStartFailed => "workerStartFailed",
                            SubmitRefusal::RequestIdExhausted => "requestIdExhausted",
                        }})
                    }
                }
            }
            Action::Begin {} => match self.queue.begin(&snapshot) {
                BeginOutcome::Idle => json!({"kind":"idle"}),
                BeginOutcome::Stopped => json!({"kind":"stopped"}),
                BeginOutcome::AlreadyActive => json!({"kind":"alreadyActive"}),
                BeginOutcome::Skipped(id) => json!({"kind":"skipped","id":id.0}),
                BeginOutcome::Write(id) => {
                    effects["write"] =
                        request_json(self.queue.active().ok_or("Missing begun request")?);
                    json!({"kind":"write","id":id.0})
                }
            },
            Action::Finish {
                written,
                error,
                request_id,
            } => {
                let active_id = self.queue.active().ok_or("No active fixture Send")?.id();
                let result = if error {
                    WriteResult::Failed
                } else {
                    WriteResult::Written(written.ok_or("Missing fixture write result")?)
                };
                let id = request_id.map(SendId).unwrap_or(active_id);
                let completion = self
                    .queue
                    .finish(id, result, &snapshot)
                    .map_err(|_| "Stale or missing fixture completion identity")?;
                if completion.echo.is_some() {
                    effects["echo"] = json!(id.0);
                }
                if let Some(failure) = completion.failure {
                    let token = failure.close_connection.0;
                    self.connections.insert(token, false);
                    effects["close"] = json!(token);
                    if let Some(disconnect) = failure.disconnect {
                        let DisconnectEffect::AdvanceGeneration(generation) = disconnect else {
                            return Err(
                                "Typed generation exhausted; session must be retired without reuse",
                            );
                        };
                        self.state.generation = generation;
                        self.state.connection = None;
                        effects["disconnect"] = json!(generation);
                    }
                }
                json!({"kind":"finished"})
            }
            Action::Discard {
                generation,
                connection,
            } => {
                self.queue
                    .discard(generation, connection.map(ConnectionToken));
                json!({"kind":"discarded"})
            }
            Action::Stop {} => {
                self.queue.stop();
                json!({"kind":"stopped"})
            }
        };
        let pending: Vec<_> = self.queue.pending().map(request_json).collect();
        Ok(json!({"result":result,"snapshot":{
            "stopped":self.queue.is_stopped(),"queuedBytes":self.queue.queued_bytes(),
            "pending":pending,"active":self.queue.active().map(request_json)},
            "state":self.state_json(),"effects":effects}))
    }
}

fn request_json(request: &SendRequest) -> Value {
    let connection = request.connection();
    json!({"id":request.id().0,"generation":connection.generation,
        "connection":connection.token.0,"port":connection.port,"baud":connection.baud,
        "bytes":request.data().len(),"digest":source_digest(request.data()).0})
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    if std::env::args_os().len() != 1 {
        return Err("This memory-only fixture accepts no path or device arguments".into());
    }
    let mut input = Vec::new();
    io::stdin()
        .take(MAX_INPUT_BYTES + 1)
        .read_to_end(&mut input)?;
    if input.len() as u64 > MAX_INPUT_BYTES {
        return Err("Fixture input byte budget exceeded".into());
    }
    let batch: Batch = serde_json::from_slice(&input)?;
    if batch.schema_version != 1
        || batch.traces.len() > 128
        || batch
            .traces
            .iter()
            .map(|trace| trace.actions.len())
            .sum::<usize>()
            > 4096
    {
        return Err("Unsupported fixture schema or trace/action budget exceeded".into());
    }
    // Validate the entire batch before interpreting any action. These identities
    // are fabricated fixture inputs, never an application IPC authority grant.
    for trace in &batch.traces {
        if trace.label.len() > 256 {
            return Err("Fixture label budget exceeded".into());
        }
        validate_state(&trace.initial_state)?;
        for action in &trace.actions {
            if let Action::SetState { state } = action {
                validate_state(state)?;
            }
        }
    }
    let mut traces = Vec::new();
    for trace in batch.traces {
        let mut controller = Controller::new(trace.initial_state);
        let events = trace
            .actions
            .into_iter()
            .map(|action| controller.step(action))
            .collect::<Result<Vec<_>, _>>()?;
        traces.push(json!({"events":events}));
    }
    let output =
        serde_json::to_vec(&json!({"schemaVersion":1,"fixtureOnly":true,"traces":traces}))?;
    if output.len() > MAX_OUTPUT_BYTES {
        return Err("Fixture output byte budget exceeded".into());
    }
    let mut stdout = io::stdout().lock();
    stdout.write_all(&output)?;
    stdout.write_all(b"\n")?;
    stdout.flush()?;
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        let message: String = error.to_string().chars().take(256).collect();
        let _ = writeln!(
            io::stdout(),
            "{}",
            json!({"schemaVersion":1,"fixtureOnly":true,"passed":false,"error":message})
        );
        std::process::exit(1);
    }
}
