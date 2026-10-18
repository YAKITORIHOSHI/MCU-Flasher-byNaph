import { AdapterError, PROTOCOL_VERSION } from './contracts.ts';
import type { DocumentSnapshot, SaveAcknowledgement, SaveDocumentRequest,
  SourceFile, SourceManifest, WorkspaceRegistration } from './contracts.ts';

export interface TextModel {
  getValue(): string;
  setValue(value: string): void;
  onDidChangeContent(callback: () => void): { dispose(): void };
  dispose(): void;
}
export interface DocumentRecord {
  source: SourceFile; model: TextModel; dirty: boolean; editRevision: number;
  savedContent: string; conflict: boolean; missing: boolean; viewState: unknown;
  listener: { dispose(): void }; suppressChanges: boolean;
}
/** One model per document, retained through tab/theme/geometry/manifest changes. */
export class DocumentStore {
  readonly records = new Map<string, DocumentRecord>();
  private workspace: WorkspaceRegistration | null = null;
  private generation = 0;
  private refreshGeneration = 0;
  private readonly createModel: (snapshot: DocumentSnapshot) => TextModel;
  private readonly changed: () => void;
  constructor(createModel: (snapshot: DocumentSnapshot) => TextModel, changed: () => void = () => {}) {
    this.createModel = createModel; this.changed = changed;
  }
  get workspaceId(): string | null { return this.workspace?.workspaceId ?? null; }
  get registration(): WorkspaceRegistration | null { return this.workspace; }
  get dirty(): boolean { return [...this.records.values()].some(record => record.dirty); }
  private validate(manifest: SourceManifest, workspaceId: string | null = this.workspaceId): void {
    if (manifest.protocolVersion !== PROTOCOL_VERSION) {
      throw new AdapterError('protocol', 'The desktop service protocol does not match this interface.');
    }
    if (manifest.workspaceId !== workspaceId) throw new AdapterError('wrong_owner', 'Reply belongs to another workspace.');
    const ids = new Set(manifest.files.map(file => file.documentId));
    if (ids.size !== manifest.files.length) throw new AdapterError('protocol', 'Manifest contains duplicate document identities.');
  }
  reset(registration: WorkspaceRegistration): void {
    if (registration.protocolVersion !== PROTOCOL_VERSION) throw new AdapterError('protocol', 'Unsupported desktop service protocol.');
    // A failed/malformed reply must never dispose the currently edited buffers.
    this.validate(registration.manifest, registration.workspaceId);
    this.generation += 1;
    for (const record of this.records.values()) { record.listener.dispose(); record.model.dispose(); }
    this.records.clear(); this.workspace = registration;
    this.changed();
  }
  add(snapshot: DocumentSnapshot): DocumentRecord {
    if (snapshot.workspaceId !== this.workspaceId) throw new AdapterError('wrong_owner', 'Document belongs to another workspace.');
    const existing = this.records.get(snapshot.document.documentId);
    if (existing) return existing;
    const model = this.createModel(snapshot);
    const record: DocumentRecord = { source: { ...snapshot.document }, model, dirty: false,
      editRevision: 0, savedContent: snapshot.content, conflict: false, missing: false,
      viewState: null, listener: { dispose() {} }, suppressChanges: false };
    record.listener = model.onDidChangeContent(() => {
      if (record.suppressChanges) return;
      record.editRevision += 1;
      record.dirty = model.getValue() !== record.savedContent;
      this.changed();
    });
    this.records.set(snapshot.document.documentId, record); this.changed(); return record;
  }
  saveRequest(documentId: string): SaveDocumentRequest {
    const record = this.records.get(documentId);
    if (!record || !this.workspaceId) throw new AdapterError('not_found', 'Open a source file first.');
    if (record.missing || record.conflict) throw new AdapterError('external_change', 'Disk source changed. Keep the buffer and resolve the conflict before saving.');
    return { workspaceId: this.workspaceId, documentId, expectedRevision: record.source.revision,
      expectedDigest: record.source.contentDigest, editRevision: record.editRevision,
      content: record.model.getValue() };
  }
  acknowledge(request: SaveDocumentRequest, ack: SaveAcknowledgement): boolean {
    if (request.workspaceId !== this.workspaceId || ack.workspaceId !== this.workspaceId) return false;
    const record = this.records.get(request.documentId);
    if (!record || ack.documentId !== request.documentId || ack.editRevision !== request.editRevision) return false;
    // An older transport reply must never overwrite a newer accepted disk version.
    if (ack.revision < record.source.revision) return false;
    record.source = { ...record.source, revision: ack.revision, contentDigest: ack.contentDigest,
      sizeBytes: new TextEncoder().encode(request.content).length };
    record.savedContent = request.content;
    record.dirty = record.model.getValue() !== request.content;
    this.changed(); return true;
  }
  async reconcile(manifest: SourceManifest,
    read: (documentId: string) => Promise<DocumentSnapshot>): Promise<void> {
    this.validate(manifest);
    if (this.workspace && manifest.revision < this.workspace.manifest.revision) return;
    const generation = this.generation;
    const refreshGeneration = ++this.refreshGeneration;
    const incoming = new Map(manifest.files.map(file => [file.documentId, file]));
    for (const [id, record] of this.records) {
      const file = incoming.get(id);
      if (!file) {
        if (record.dirty) { record.missing = true; continue; }
        record.listener.dispose(); record.model.dispose(); this.records.delete(id); continue;
      }
      record.missing = false;
      if (file.contentDigest === record.source.contentDigest) {
        record.source = { ...file }; continue;
      }
      if (record.dirty) { record.conflict = true; continue; }
      const editRevision = record.editRevision;
      const snapshot = await read(id);
      if (generation !== this.generation || refreshGeneration !== this.refreshGeneration) return;
      if (record.editRevision !== editRevision || record.dirty) { record.conflict = true; continue; }
      if (snapshot.workspaceId !== this.workspaceId || snapshot.document.documentId !== id) {
        throw new AdapterError('wrong_owner', 'Refreshed source belongs to another workspace.');
      }
      record.suppressChanges = true;
      try { record.model.setValue(snapshot.content); } finally { record.suppressChanges = false; }
      record.source = { ...snapshot.document }; record.savedContent = snapshot.content;
      record.conflict = false;
    }
    if (generation !== this.generation || refreshGeneration !== this.refreshGeneration) return;
    if (this.workspace) this.workspace.manifest = manifest;
    this.changed();
  }
  dispose(): void {
    this.generation += 1;
    for (const record of this.records.values()) { record.listener.dispose(); record.model.dispose(); }
    this.records.clear(); this.workspace = null;
  }
}
