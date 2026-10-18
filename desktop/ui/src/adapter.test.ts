import assert from 'node:assert/strict';
import { test } from 'node:test';
import { BrowserFixtureAdapter } from './adapter.ts';

test('browser fixture rejects native paths and never exposes native disk access', async () => {
  const adapter = new BrowserFixtureAdapter();
  await assert.rejects(adapter.openProject('/home/user/LiveSketch'), /only memory:/);
  const info = await adapter.info(); assert.equal(info.mode, 'browser_fixture');
});
test('browser save checks workspace, revision, digest and returns edit acknowledgement', async () => {
  const adapter = new BrowserFixtureAdapter(); const workspace = await adapter.openProject('memory://fixtures/Welcome');
  const file = workspace.manifest.files[0];
  const request = { workspaceId: workspace.workspaceId, documentId: file.documentId,
    expectedRevision: file.revision, expectedDigest: file.contentDigest, editRevision: 7, content: '// café\n' };
  const ack = await adapter.saveDocument(request); assert.equal(ack.editRevision, 7); assert.equal(ack.bytesWritten, 9);
  await assert.rejects(adapter.saveDocument(request), /changed after/);
  await assert.rejects(adapter.readDocument({ workspaceId: 'another', documentId: file.documentId }), /no longer active/);
});
test('new fixture header is included by both generated sketch and C++ source', async () => {
  const adapter = new BrowserFixtureAdapter();
  const workspace = await adapter.createProject('memory://fixtures', { name: 'Fixture', includeH: true, includeCpp: true, template: 'standard' });
  for (const file of workspace.manifest.files.filter(file => ['.ino', '.cpp'].includes(file.extension))) {
    const snapshot = await adapter.readDocument({ workspaceId: workspace.workspaceId, documentId: file.documentId });
    assert.match(snapshot.content, /^#include "Fixture.h"/);
  }
});
test('fixture stream awaits consumption before sending successor output', async () => {
  const adapter = new BrowserFixtureAdapter(); const received: number[] = [];
  let release!: () => void;
  const pending = adapter.streamFixture(async event => {
    received.push(event.sequence);
    if (event.sequence === 0) await new Promise<void>(resolve => { release = resolve; });
  });
  await Promise.resolve(); assert.deepEqual(received, [0]); release(); await pending;
  assert.deepEqual(received, [0, 1, 2, 3]);
});

test('browser scaffold keeps template-specific behavior and exact optional source includes', async () => {
  const adapter = new BrowserFixtureAdapter();
  for (const template of ['standard', 'bare', 'blink'] as const) {
    const workspace = await adapter.createProject('memory://fixtures', { name: 'Template', includeH: true, includeCpp: true, template });
    const sketch = await adapter.readDocument({ workspaceId: workspace.workspaceId, documentId: 'Template.ino' });
    const implementation = await adapter.readDocument({ workspaceId: workspace.workspaceId, documentId: 'Template.cpp' });
    assert.equal(implementation.content, '#include "Template.h"\n');
    assert.match(sketch.content, /^#include "Template.h"\n\nvoid setup/);
    if (template === 'blink') {
      assert.match(sketch.content, /pinMode\(LED_BUILTIN, OUTPUT\)/);
      assert.match(sketch.content, /digitalWrite\(LED_BUILTIN, HIGH\)/);
      assert.match(sketch.content, /digitalWrite\(LED_BUILTIN, LOW\)/);
    } else if (template === 'bare') assert.match(sketch.content, /void setup\(\) \{\n  \n\}/);
    else assert.match(sketch.content, /void setup\(\) \{\n\n\}/);
  }
});

test('browser scaffold omits unchecked header and C++ source and refuses unknown templates', async () => {
  const adapter = new BrowserFixtureAdapter();
  const workspace = await adapter.createProject('memory://fixtures', { name: 'NoHeader', includeH: false, includeCpp: false, template: 'blink' });
  assert.deepEqual(workspace.manifest.files.map(file => file.name), ['NoHeader.ino']);
  const snapshot = await adapter.readDocument({ workspaceId: workspace.workspaceId, documentId: 'NoHeader.ino' });
  assert.doesNotMatch(snapshot.content, /#include/);
  await assert.rejects(adapter.createProject('memory://fixtures', { name: 'Invalid', includeH: false, includeCpp: false, template: 'unexpected' as 'standard' }), /Unknown fixture project template/);
  assert.equal((await adapter.manifest(workspace.workspaceId)).files[0].name, 'NoHeader.ino');
});
