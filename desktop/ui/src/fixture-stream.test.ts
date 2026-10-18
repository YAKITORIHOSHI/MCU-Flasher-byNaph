import assert from 'node:assert/strict';
import { test } from 'node:test';
import { AdapterError } from './contracts.ts';
import type { StreamEvent } from './contracts.ts';
import { createFixtureStreamConsumer } from './fixture-stream.ts';
import type { FixtureAcknowledgement } from './fixture-stream.ts';

const streamId = 'fixture-0000000000000001';
const started = { type: 'started', streamId, sequence: 0 } as const;
const output = { type: 'output', streamId, sequence: 1, data: 'café', bytes: 5 } as const;
const finished = { type: 'finished', streamId, sequence: 2, bytes: 5, exitCode: 0 } as const;
const settle = () => new Promise<void>(resolve => setImmediate(resolve));
function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>(done => { resolve = done; });
  return { promise, resolve };
}

test('native output ACK follows renderer consumption and echoes the exact chunk', async () => {
  const rendered: number[] = []; const acknowledgements: FixtureAcknowledgement[] = [];
  const renderer = deferred();
  const consumer = createFixtureStreamConsumer(async event => {
    rendered.push(event.sequence);
    if (event.type === 'output') await renderer.promise;
  }, async chunk => { acknowledgements.push(chunk); });
  consumer.receive(started);
  consumer.receive(output);
  await settle();
  assert.deepEqual(rendered, [0, 1]);
  assert.deepEqual(acknowledgements, [], 'Rendering an outstanding chunk must hold backpressure.');
  renderer.resolve(); await settle();
  assert.deepEqual(acknowledgements, [{ streamId, sequence: 1, bytes: 5 }]);
  consumer.receive(finished); await consumer.finished();
  assert.deepEqual(rendered, [0, 1, 2]);
  assert.equal(acknowledgements.length, 1, 'Start and completion must never acknowledge output.');
});

test('native completion can arrive before the final ACK response resolves', async () => {
  const ack = deferred(); const rendered: number[] = [];
  const consumer = createFixtureStreamConsumer(async event => { rendered.push(event.sequence); }, async () => ack.promise);
  consumer.receive(started); consumer.receive(output); await settle();
  consumer.receive(finished);
  const completion = consumer.finished();
  await settle(); assert.deepEqual(rendered, [0, 1]);
  ack.resolve(); await completion;
  assert.deepEqual(rendered, [0, 1, 2]);
});

test('waiting for the command result tolerates its final Channel callback arriving later', async () => {
  const rendered: number[] = [];
  const consumer = createFixtureStreamConsumer(async event => { rendered.push(event.sequence); }, async () => {});
  consumer.receive(started); consumer.receive(output); await settle();
  let completed = false;
  const completion = consumer.finished().then(() => { completed = true; });
  await settle(); assert.equal(completed, false);
  consumer.receive(finished); await completion;
  assert.deepEqual(rendered, [0, 1, 2]);
});

test('native events are snapshotted before awaiting renderer or acknowledging', async () => {
  const renderer = deferred(); const rendered: StreamEvent[] = []; const acknowledgements: FixtureAcknowledgement[] = [];
  const consumer = createFixtureStreamConsumer(async event => {
    rendered.push(event);
    if (event.type === 'output') await renderer.promise;
  }, async chunk => { acknowledgements.push(chunk); });
  consumer.receive(started); await settle();
  const mutable = { ...output }; consumer.receive(mutable);
  mutable.sequence = 99; mutable.bytes = 4096; mutable.data = 'changed';
  await settle();
  assert.equal(rendered[1].data, 'café'); assert.equal(Object.isFrozen(rendered[1]), true);
  renderer.resolve(); await settle(); consumer.receive(finished); await consumer.finished();
  assert.deepEqual(acknowledgements, [{ streamId, sequence: 1, bytes: 5 }]);
});

test('invalid, stale and out-of-order native events are rejected before rendering', async () => {
  for (const [label, value, hasStarted] of [
    ['non-object', null, false],
    ['array', [], false],
    ['missing start', output, false],
    ['wrong initial sequence', { ...started, sequence: 1 }, false],
    ['invalid identity', { ...started, streamId: '' }, false],
    ['duplicate start', { ...started, sequence: 1 }, true],
    ['different stream', { ...output, streamId: 'fixture-0000000000000002' }, true],
    ['skipped sequence', { ...output, sequence: 2 }, true],
    ['negative bytes', { ...output, bytes: -1 }, true],
    ['fractional bytes', { ...output, bytes: .5 }, true],
    ['oversize chunk', { ...output, bytes: 4097 }, true],
    ['missing data', { ...output, data: undefined }, true],
    ['oversize payload', { ...output, data: 'a'.repeat(4100) }, true],
    ['wrong event type', { ...output, type: 'input' }, true],
    ['start with output fields', { ...started, bytes: 0 }, false],
    ['negative exit', { ...finished, sequence: 1, exitCode: -1 }, true],
    ['oversize completion', { ...finished, sequence: 1, bytes: 65537 }, true],
  ] as const) {
    const rendered: number[] = []; const acknowledgements: FixtureAcknowledgement[] = [];
    const consumer = createFixtureStreamConsumer(async event => { rendered.push(event.sequence); }, async chunk => { acknowledgements.push(chunk); });
    if (hasStarted) { consumer.receive(started); await settle(); }
    consumer.receive(value);
    await assert.rejects(consumer.finished(), error => error instanceof AdapterError && error.code === 'protocol', label);
    assert.deepEqual(rendered, hasStarted ? [0] : [], label);
    assert.deepEqual(acknowledgements, [], label);
  }
});

test('ACK failure stops successor events and preserves the first stable failure', async () => {
  const rendered: number[] = []; const acknowledgements: FixtureAcknowledgement[] = [];
  const consumer = createFixtureStreamConsumer(async event => { rendered.push(event.sequence); }, async chunk => {
    acknowledgements.push(chunk); throw new Error('Fixture ACK failed');
  });
  consumer.receive(started); consumer.receive(output); await settle();
  consumer.receive({ ...output, sequence: 2 }); consumer.receive({ ...finished, sequence: 3 });
  let first: unknown;
  await assert.rejects(consumer.finished(), error => {
    first = error; return error instanceof AdapterError && error.code === 'transport' && error.message === 'Fixture ACK failed';
  });
  await assert.rejects(consumer.finished(), error => error === first);
  assert.deepEqual(rendered, [0, 1]); assert.equal(acknowledgements.length, 1);
});

test('renderer failure is handled before any ACK and falsy rejection remains a failure', async () => {
  const rendered: number[] = []; const acknowledgements: FixtureAcknowledgement[] = [];
  const consumer = createFixtureStreamConsumer(async event => {
    rendered.push(event.sequence); if (event.type === 'output') throw 0;
  }, async chunk => { acknowledgements.push(chunk); });
  consumer.receive(started); consumer.receive(output); await settle();
  consumer.receive(finished);
  await assert.rejects(consumer.finished(), error => error instanceof AdapterError && error.code === 'transport' && error.message === '0');
  assert.deepEqual(rendered, [0, 1]); assert.deepEqual(acknowledgements, []);
});

test('a native event flood cannot accumulate an unbounded pending renderer queue', async () => {
  const renderer = deferred(); const rendered: number[] = []; const acknowledgements: FixtureAcknowledgement[] = [];
  const consumer = createFixtureStreamConsumer(async event => { rendered.push(event.sequence); await renderer.promise; },
    async chunk => { acknowledgements.push(chunk); });
  consumer.receive(started); await settle();
  consumer.receive(output);
  for (let sequence = 2; sequence < 1000; sequence += 1) consumer.receive({ ...output, sequence });
  renderer.resolve();
  await assert.rejects(consumer.finished(), /pending-event budget/);
  assert.deepEqual(rendered, [0]); assert.deepEqual(acknowledgements, []);
});

test('the total native byte budget is enforced before the exceeding chunk renders', async () => {
  const rendered: number[] = []; const acknowledgements: FixtureAcknowledgement[] = [];
  const consumer = createFixtureStreamConsumer(async event => { rendered.push(event.sequence); }, async chunk => { acknowledgements.push(chunk); });
  consumer.receive(started); await settle();
  for (let sequence = 1; sequence <= 16; sequence += 1) {
    consumer.receive({ ...output, sequence, data: 'a'.repeat(4096), bytes: 4096 }); await settle();
  }
  consumer.receive({ ...output, sequence: 17, data: 'a', bytes: 1 });
  await assert.rejects(consumer.finished(), /byte budget/);
  assert.deepEqual(rendered, Array.from({ length: 17 }, (_, index) => index));
  assert.equal(acknowledgements.length, 16);
});

test('completion cannot report fewer raw bytes than consumed chunks', async () => {
  const rendered: number[] = [];
  const consumer = createFixtureStreamConsumer(async event => { rendered.push(event.sequence); }, async () => {});
  consumer.receive(started); consumer.receive(output); await settle();
  consumer.receive({ ...finished, bytes: 4 });
  await assert.rejects(consumer.finished(), /completion fields/);
  assert.deepEqual(rendered, [0, 1]);
});

test('display text has an independent bound even when raw byte counts are zero', async () => {
  let outputs = 0;
  const consumer = createFixtureStreamConsumer(async event => { if (event.type === 'output') outputs += 1; }, async () => {});
  consumer.receive(started); await settle();
  for (let sequence = 1; sequence <= 48; sequence += 1) {
    consumer.receive({ ...output, sequence, data: 'a'.repeat(4096), bytes: 0 }); await settle();
  }
  consumer.receive({ ...output, sequence: 49, data: 'a', bytes: 0 });
  await assert.rejects(consumer.finished(), /byte budget/);
  assert.equal(outputs, 48);
});

test('a truncated stream fails completion without acknowledging start', async () => {
  const acknowledgements: FixtureAcknowledgement[] = [];
  const consumer = createFixtureStreamConsumer(async () => {}, async chunk => { acknowledgements.push(chunk); });
  consumer.receive(started);
  await assert.rejects(consumer.finished(), /without a completion event/);
  assert.deepEqual(acknowledgements, []);
});

test('completion deadline also bounds a renderer that never resolves', { timeout: 4000 }, async () => {
  const acknowledgements: FixtureAcknowledgement[] = [];
  const consumer = createFixtureStreamConsumer(async () => new Promise<void>(() => {}),
    async chunk => { acknowledgements.push(chunk); });
  consumer.receive(started); await settle();
  consumer.receive({ ...finished, sequence: 1, bytes: 0 });
  await assert.rejects(consumer.finished(), /renderer did not finish/);
  assert.deepEqual(acknowledgements, []);
});

test('host abort preserves its real failure and releases a hung ACK immediately', async () => {
  const rendered: number[] = []; const acknowledgements: FixtureAcknowledgement[] = [];
  const consumer = createFixtureStreamConsumer(async event => { rendered.push(event.sequence); }, async chunk => {
    acknowledgements.push(chunk); await new Promise<void>(() => {});
  });
  consumer.receive(started); consumer.receive(output); await settle();
  const hostError = new AdapterError('host_failed', 'Owned fixture process could not complete.');
  consumer.abort(hostError);
  consumer.receive({ ...output, sequence: 2 });
  await assert.rejects(consumer.finished(), error => error === hostError);
  await assert.rejects(consumer.finished(), error => error === hostError);
  assert.deepEqual(rendered, [0, 1]); assert.equal(acknowledgements.length, 1);
});

test('a renderer released after host abort cannot send a late ACK', async () => {
  const renderer = deferred(); const acknowledgements: FixtureAcknowledgement[] = [];
  const consumer = createFixtureStreamConsumer(async event => {
    if (event.type === 'output') await renderer.promise;
  }, async chunk => { acknowledgements.push(chunk); });
  consumer.receive(started); consumer.receive(output); await settle();
  consumer.abort(new Error('Host stopped'));
  await assert.rejects(consumer.finished(), /Host stopped/);
  renderer.resolve(); await settle();
  assert.deepEqual(acknowledgements, []);
});
