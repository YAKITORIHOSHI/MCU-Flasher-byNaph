import assert from 'node:assert/strict';
import { test } from 'node:test';
import { DocumentStore } from './document-store.ts';
import type { TextModel } from './document-store.ts';
import type { DocumentSnapshot, SourceManifest, WorkspaceRegistration } from './contracts.ts';

class Model implements TextModel {
  value: string; disposed = false; listeners = new Set<() => void>();
  constructor(content: string) { this.value = content; }
  getValue() { return this.value; }
  setValue(value: string) { this.value = value; for (const callback of this.listeners) callback(); }
  onDidChangeContent(callback: () => void) { this.listeners.add(callback); return { dispose: () => { this.listeners.delete(callback); } }; }
  dispose() { this.disposed = true; this.listeners.clear(); }
}
function fixture() {
  const source = { documentId: 'main', name: 'Sketch.ino', extension: '.ino', isMain: true, sizeBytes: 3, revision: 1, contentDigest: 'old' };
  const manifest: SourceManifest = { protocolVersion: 1, workspaceId: 'workspace', revision: 1, files: [source] };
  const registration: WorkspaceRegistration = { protocolVersion: 1, workspaceId: 'workspace', root: '/fixture', name: 'Sketch', manifest };
  const store = new DocumentStore(snapshot => new Model(snapshot.content));
  store.reset(registration);
  const record = store.add({ workspaceId: 'workspace', document: source, content: 'old' });
  return { store, record, source, manifest, registration };
}

test('save acknowledgement preserves edits made while saving and advances disk identity', () => {
  const { store, record } = fixture(); record.model.setValue('first');
  const request = store.saveRequest('main'); record.model.setValue('newer');
  assert.equal(store.acknowledge(request, { workspaceId: 'workspace', documentId: 'main', editRevision: request.editRevision,
    revision: 2, contentDigest: 'first-digest', bytesWritten: 5, unchanged: false }), true);
  assert.equal(record.dirty, true); assert.equal(record.model.getValue(), 'newer');
  assert.equal(store.saveRequest('main').expectedDigest, 'first-digest');
});
test('foreign acknowledgement cannot clear dirty buffer', () => {
  const { store, record } = fixture(); record.model.setValue('new'); const request = store.saveRequest('main');
  assert.equal(store.acknowledge(request, { workspaceId: 'another', documentId: 'main', editRevision: request.editRevision,
    revision: 2, contentDigest: 'new', bytesWritten: 3, unchanged: false }), false);
  assert.equal(record.dirty, true);
});
test('unchanged manifest keeps model identity and does not read source', async () => {
  const { store, record, manifest } = fixture(); record.model.setValue('dirty');
  await store.reconcile(manifest, async () => { throw new Error('must not read'); });
  assert.equal(store.records.get('main')?.model, record.model); assert.equal(record.dirty, true);
});
test('external change and deletion retain dirty models with explicit conflict', async () => {
  const { store, record, source, manifest } = fixture(); record.model.setValue('local edit');
  await store.reconcile({ ...manifest, revision: 2, files: [{ ...source, revision: 2, contentDigest: 'external' }] }, async () => { throw new Error('must not replace dirty model'); });
  assert.equal(record.conflict, true); assert.throws(() => store.saveRequest('main'), /Disk source changed/);
  await store.reconcile({ ...manifest, revision: 3, files: [] }, async () => { throw new Error('no read'); });
  assert.equal(record.missing, true); assert.equal(record.model.getValue(), 'local edit');
  assert.equal((record.model as Model).disposed, false);
});
test('editing during refresh preserves buffer and reports conflict', async () => {
  const { store, record, source, manifest } = fixture();
  await store.reconcile({ ...manifest, revision: 2, files: [{ ...source, contentDigest: 'external' }] }, async () => {
    record.model.setValue('new edit'); return { workspaceId: 'workspace', document: { ...source, contentDigest: 'external' }, content: 'external' };
  });
  assert.equal(record.conflict, true); assert.equal(record.model.getValue(), 'new edit');
});
test('workspace switch invalidates pending refresh reply', async () => {
  const { store, record, source, manifest, registration } = fixture();
  let finish!: (snapshot: DocumentSnapshot) => void;
  const pending = store.reconcile({ ...manifest, revision: 2, files: [{ ...source, contentDigest: 'external' }] }, () => new Promise(resolve => { finish = resolve; }));
  store.reset({ ...registration, workspaceId: 'next', manifest: { ...manifest, workspaceId: 'next', files: [] } });
  finish({ workspaceId: 'workspace', document: source, content: 'stale' }); await pending;
  assert.equal(store.workspaceId, 'next'); assert.equal(store.records.size, 0); assert.equal((record.model as Model).disposed, true);
});
test('clean removed document disposes exactly its own model', async () => {
  const { store, record, manifest } = fixture();
  await store.reconcile({ ...manifest, revision: 2, files: [] }, async () => { throw new Error('no read'); });
  assert.equal(store.records.size, 0); assert.equal((record.model as Model).disposed, true);
});

test('invalid successor registration preserves the current dirty buffers', () => {
  const { store, record, registration, manifest } = fixture(); record.model.setValue('keep my edits');
  assert.throws(() => store.reset({ ...registration, workspaceId: 'next',
    manifest: { ...manifest, workspaceId: 'wrong-owner' } }), /another workspace/);
  assert.equal(store.workspaceId, 'workspace'); assert.equal(record.dirty, true);
  assert.equal(record.model.getValue(), 'keep my edits'); assert.equal((record.model as Model).disposed, false);
});

test('out-of-order manifest cannot regress source set or model content', async () => {
  const { store, record, source, manifest } = fixture();
  const latest = { ...manifest, revision: 3, files: [{ ...source, revision: 3, contentDigest: 'newest' }] };
  await store.reconcile(latest, async () => ({ workspaceId: 'workspace', document: latest.files[0], content: 'newest' }));
  await store.reconcile({ ...manifest, revision: 2, files: [] }, async () => { throw new Error('stale manifest must be ignored'); });
  assert.equal(store.records.get('main')?.model, record.model); assert.equal(record.model.getValue(), 'newest');
  assert.equal(store.registration?.manifest.revision, 3);
});

test('newer refresh invalidates an older pending read without losing model identity', async () => {
  const { store, record, source, manifest } = fixture();
  let finish!: (snapshot: DocumentSnapshot) => void;
  const older = store.reconcile({ ...manifest, revision: 2, files: [{ ...source, revision: 2, contentDigest: 'older' }] },
    () => new Promise(resolve => { finish = resolve; }));
  const newest = { ...source, revision: 3, contentDigest: 'newest' };
  await store.reconcile({ ...manifest, revision: 3, files: [newest] },
    async () => ({ workspaceId: 'workspace', document: newest, content: 'newest' }));
  finish({ workspaceId: 'workspace', document: { ...source, revision: 2, contentDigest: 'older' }, content: 'older' });
  await older;
  assert.equal(store.records.get('main')?.model, record.model); assert.equal(record.model.getValue(), 'newest');
  assert.equal(store.registration?.manifest.revision, 3);
});
