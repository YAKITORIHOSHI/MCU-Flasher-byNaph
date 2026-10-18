//! Single-outstanding-chunk flow control for the fixed native PTY fixture.
//! The native window supplies ownership; renderer requests can only acknowledge
//! the exact active stream, sequence and byte count after consuming that chunk.
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

#[derive(Clone, Default)]
pub struct FixtureAcks(Arc<Inner>);

#[derive(Default)]
struct Inner {
    state: Mutex<State>,
    changed: Condvar,
}

#[derive(Default)]
struct State {
    next: u64,
    active: Option<Active>,
}

struct Active {
    owner: String,
    id: String,
    sequence: u32,
    pending: Option<(u32, usize)>,
}

pub struct FixtureAckSession {
    gate: FixtureAcks,
    id: String,
}

impl FixtureAcks {
    pub fn begin(&self, native_owner: &str) -> Result<FixtureAckSession, String> {
        let mut state = self.0.state.lock().map_err(|_| "Stream state failed")?;
        if state.active.is_some() {
            return Err("A controlled PTY stream is already active".into());
        }
        state.next = state
            .next
            .checked_add(1)
            .ok_or("Stream identities exhausted")?;
        let id = format!("fixture-{:016x}", state.next);
        state.active = Some(Active {
            owner: native_owner.into(),
            id: id.clone(),
            sequence: 0,
            pending: None,
        });
        Ok(FixtureAckSession {
            gate: self.clone(),
            id,
        })
    }

    pub fn acknowledge(
        &self,
        native_owner: &str,
        stream_id: &str,
        sequence: u32,
        bytes: usize,
    ) -> Result<(), String> {
        let mut state = self.0.state.lock().map_err(|_| "Stream state failed")?;
        let active = state.active.as_mut().ok_or("The stream has ended")?;
        if active.owner != native_owner || active.id != stream_id {
            return Err("Acknowledgement belongs to another window or stream".into());
        }
        if active.pending != Some((sequence, bytes)) {
            return Err("Acknowledgement does not match the outstanding output chunk".into());
        }
        active.pending = None;
        self.0.changed.notify_all();
        Ok(())
    }
}

impl FixtureAckSession {
    pub fn id(&self) -> &str {
        &self.id
    }

    pub fn prepare(&self, sequence: u32, bytes: usize) -> Result<(), String> {
        let mut state = self
            .gate
            .0
            .state
            .lock()
            .map_err(|_| "Stream state failed")?;
        let active = state
            .active
            .as_mut()
            .filter(|a| a.id == self.id)
            .ok_or("The stream has ended")?;
        if active.pending.is_some()
            || active.sequence.checked_add(1) != Some(sequence)
            || bytes > 4096
        {
            return Err("Stream sequence or outstanding-byte budget exceeded".into());
        }
        active.sequence = sequence;
        active.pending = Some((sequence, bytes));
        Ok(())
    }

    /// Call only on the owned process worker, never on the UI/IPC thread. A
    /// missing/failed renderer acknowledgement aborts the fixed child; no retry
    /// or replay of process input/output is performed by this gate.
    pub fn wait_consumed(&self, timeout: Duration) -> Result<(), String> {
        let deadline = Instant::now() + timeout.min(Duration::from_secs(2));
        let mut state = self
            .gate
            .0
            .state
            .lock()
            .map_err(|_| "Stream state failed")?;
        loop {
            let active = state
                .active
                .as_ref()
                .filter(|a| a.id == self.id)
                .ok_or("The stream has ended")?;
            if active.pending.is_none() {
                return Ok(());
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err("Renderer did not acknowledge controlled PTY output in time".into());
            }
            state = self
                .gate
                .0
                .changed
                .wait_timeout(state, remaining)
                .map_err(|_| "Stream state failed")?
                .0;
        }
    }
}

impl Drop for FixtureAckSession {
    fn drop(&mut self) {
        if let Ok(mut state) = self.gate.0.state.lock() {
            if state
                .active
                .as_ref()
                .is_some_and(|active| active.id == self.id)
            {
                state.active = None;
                self.gate.0.changed.notify_all();
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_owner_and_exact_consumed_chunk_can_release_backpressure() {
        let gate = FixtureAcks::default();
        let session = gate.begin("main").unwrap();
        session.prepare(1, 4096).unwrap();
        assert!(gate.begin("main").is_err());
        assert!(gate.acknowledge("other", session.id(), 1, 4096).is_err());
        assert!(gate.acknowledge("main", "stale", 1, 4096).is_err());
        assert!(gate.acknowledge("main", session.id(), 2, 4096).is_err());
        assert!(gate.acknowledge("main", session.id(), 1, 4095).is_err());
        assert!(session.prepare(2, 1).is_err());
        gate.acknowledge("main", session.id(), 1, 4096).unwrap();
        session.wait_consumed(Duration::ZERO).unwrap();
        assert!(gate.acknowledge("main", session.id(), 1, 4096).is_err());
        session.prepare(2, 1).unwrap();
    }

    #[test]
    fn renderer_ack_wakes_worker_and_old_sessions_cannot_ack_new_output() {
        let gate = FixtureAcks::default();
        let session = gate.begin("main").unwrap();
        let old_id = session.id().to_owned();
        session.prepare(1, 10).unwrap();
        let worker = std::thread::spawn(move || {
            session.wait_consumed(Duration::from_secs(1)).unwrap();
        });
        gate.acknowledge("main", &old_id, 1, 10).unwrap();
        worker.join().unwrap();
        let next = gate.begin("main").unwrap();
        next.prepare(1, 10).unwrap();
        assert_ne!(old_id, next.id());
        assert!(gate.acknowledge("main", &old_id, 1, 10).is_err());
        gate.acknowledge("main", next.id(), 1, 10).unwrap();
    }

    #[test]
    fn failed_consumer_is_bounded_and_dropping_session_releases_ownership() {
        let gate = FixtureAcks::default();
        {
            let session = gate.begin("main").unwrap();
            assert!(session.prepare(2, 10).is_err());
            assert!(session.prepare(1, 4097).is_err());
            session.prepare(1, 0).unwrap();
            assert!(session.wait_consumed(Duration::from_millis(5)).is_err());
        }
        assert!(gate.begin("main").is_ok());
    }
}
