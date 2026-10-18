import { AdapterError, serviceError } from './contracts.ts';
import type { StreamEvent } from './contracts.ts';

export interface FixtureAcknowledgement {
  streamId: string; sequence: number; bytes: number;
}

const MAX_CHUNK_BYTES = 4096;
const MAX_STREAM_BYTES = 65536;

/** Consume the fixed native fixture without an unbounded renderer queue. */
export function createFixtureStreamConsumer(
  onEvent: (event: StreamEvent) => Promise<void>,
  acknowledge: (chunk: FixtureAcknowledgement) => Promise<void>,
): { receive(event: unknown): void; abort(error: unknown): void; finished(): Promise<void> } {
  let streamId: string | undefined;
  let nextSequence = 0;
  let totalBytes = 0;
  let displayBytes = 0;
  let phase: 'new' | 'streaming' | 'finished' = 'new';
  let pending = Promise.resolve();
  let pendingEvents = 0;
  let sealed = false;
  let failure: AdapterError | undefined;
  let terminalReceived!: () => void;
  const terminal = new Promise<void>(resolve => { terminalReceived = resolve; });
  let failureReceived!: () => void;
  const failed = new Promise<void>(resolve => { failureReceived = resolve; });
  let completion: Promise<void> | undefined;

  function invalid(message: string): never {
    throw new AdapterError('protocol', message);
  }
  function fail(error: unknown): void {
    // Keep the first normalized failure stable, including falsy rejections.
    failure ??= serviceError(error);
    terminalReceived();
    failureReceived();
  }
  function validate(value: unknown): StreamEvent {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
      return invalid('Native fixture event must be an object.');
    }
    const event = value as Record<string, unknown>;
    if (typeof event.streamId !== 'string' || !/^fixture-[0-9a-f]{16}$/.test(event.streamId)
      || !Number.isSafeInteger(event.sequence) || event.sequence !== nextSequence
      || nextSequence > 0xffffffff) {
      return invalid('Native fixture stream identity or sequence is invalid.');
    }
    if (phase === 'finished' || (streamId !== undefined && event.streamId !== streamId)) {
      return invalid('Native fixture event belongs to an ended or different stream.');
    }
    if (event.type === 'started') {
      if (phase !== 'new' || event.data !== undefined || event.bytes !== undefined || event.exitCode !== undefined) {
        return invalid('Native fixture start event is invalid.');
      }
      streamId = event.streamId;
      phase = 'streaming';
      nextSequence += 1;
      return Object.freeze({ type: 'started', streamId, sequence: event.sequence as number });
    }
    if (phase !== 'streaming') return invalid('Native fixture output arrived before its start event.');
    if (event.type === 'output') {
      if (typeof event.data !== 'string' || event.data.length > MAX_CHUNK_BYTES + 3) {
        return invalid('Native fixture output has an invalid text payload.');
      }
      const encodedBytes = new TextEncoder().encode(event.data).length;
      if (!Number.isSafeInteger(event.bytes) || (event.bytes as number) < 0
        || (event.bytes as number) > MAX_CHUNK_BYTES || event.exitCode !== undefined
        || totalBytes + (event.bytes as number) > MAX_STREAM_BYTES
        // Invalid raw bytes may expand to a three-byte UTF-8 replacement.
        // Bound actual display text independently of the reported raw count.
        || displayBytes + encodedBytes > MAX_STREAM_BYTES * 3) {
        return invalid('Native fixture output exceeds its byte budget or has invalid fields.');
      }
      totalBytes += event.bytes as number;
      displayBytes += encodedBytes;
      nextSequence += 1;
      return Object.freeze({ type: 'output', streamId, sequence: event.sequence as number,
        data: event.data, bytes: event.bytes as number });
    }
    if (event.type === 'finished') {
      if (event.data !== undefined || !Number.isSafeInteger(event.bytes)
        || (event.bytes as number) < totalBytes || (event.bytes as number) > MAX_STREAM_BYTES
        || !Number.isSafeInteger(event.exitCode) || (event.exitCode as number) < 0
        || (event.exitCode as number) > 0xffffffff) {
        return invalid('Native fixture completion fields are invalid.');
      }
      // A decoder can retain a prefix from a raw chunk that produced no text.
      // Its final raw-byte total may therefore exceed the delivered chunk sum.
      phase = 'finished';
      nextSequence += 1;
      terminalReceived();
      return Object.freeze({ type: 'finished', streamId, sequence: event.sequence as number,
        bytes: event.bytes as number, exitCode: event.exitCode as number });
    }
    return invalid('Native fixture event type is invalid.');
  }

  return {
    receive(value: unknown): void {
      if (failure) return;
      try {
        if (sealed) invalid('Native fixture event arrived after completion was awaited.');
        // The host may send its first chunk while the start event renders, or
        // finish while an ACK response returns. One queued successor suffices.
        if (pendingEvents >= 2) invalid('Native fixture exceeded its pending-event budget.');
        const event = validate(value);
        pendingEvents += 1;
        pending = pending.then(async () => {
          if (failure) return;
          await onEvent(event);
          if (failure) return;
          if (event.type === 'output') {
            await acknowledge({ streamId: event.streamId!, sequence: event.sequence, bytes: event.bytes! });
          }
        }).catch(fail).finally(() => { pendingEvents -= 1; });
      } catch (error) { fail(error); }
    },
    abort(error: unknown): void { fail(error); },
    finished(): Promise<void> {
      // The command result can race the final Channel callback. Await that
      // callback with a deadline, then drain the bounded renderer/ACK chain.
      completion ??= (async () => {
        const timeout = setTimeout(() => fail(new AdapterError('protocol', phase === 'finished'
          ? 'Native fixture renderer did not finish consuming output in time.'
          : 'Native fixture ended without a completion event.')), 2000);
        try {
          await terminal;
          sealed = true;
          // A hung renderer or ACK cannot keep completion pending after the
          // host has failed or the deadline has expired. Its attached catch
          // still observes any eventual rejection; failure prevents late ACKs.
          await Promise.race([pending, failed]);
          if (failure) throw failure;
        } finally { clearTimeout(timeout); }
      })();
      return completion;
    },
  };
}
