/** Mirrors desktop/crates/contracts/src/lib.rs. Revision numbers are JS-safe. */
export const PROTOCOL_VERSION = 1;
export interface SourceFile {
  documentId: string; name: string; extension: string; isMain: boolean;
  sizeBytes: number; revision: number; contentDigest: string;
}
export interface SourceManifest {
  protocolVersion: number; workspaceId: string; revision: number; files: SourceFile[];
}
export interface WorkspaceRegistration {
  protocolVersion: number; workspaceId: string; root: string; name: string;
  manifest: SourceManifest;
}
export interface DocumentSnapshot { workspaceId: string; document: SourceFile; content: string }
export interface DocumentRequest { workspaceId: string; documentId: string }
export interface SaveDocumentRequest extends DocumentRequest {
  expectedRevision: number; expectedDigest: string; editRevision: number; content: string;
}
export interface SaveAcknowledgement extends DocumentRequest {
  revision: number; contentDigest: string; editRevision: number;
  bytesWritten: number; unchanged: boolean;
}
export interface ScaffoldOptions {
  name: string; includeH: boolean; includeCpp: boolean; template: 'standard' | 'bare' | 'blink';
}
export interface PrototypeInfo {
  protocolVersion: number; mode: 'fixture_only' | 'browser_fixture';
  fixtureParent: string; fixtureProject: string;
  host: { os: string; architecture: string }; gates: string[];
}
export interface StreamEvent {
  type: 'started' | 'output' | 'finished'; sequence: number;
  streamId?: string;
  data?: string; bytes?: number; exitCode?: number;
}
export interface DesktopAdapter {
  readonly mode: 'native-fixture' | 'browser-fixture';
  info(): Promise<PrototypeInfo>;
  openProject(path: string): Promise<WorkspaceRegistration>;
  manifest(workspaceId: string): Promise<SourceManifest>;
  readDocument(request: DocumentRequest): Promise<DocumentSnapshot>;
  saveDocument(request: SaveDocumentRequest): Promise<SaveAcknowledgement>;
  createProject(parentPath: string, options: ScaffoldOptions): Promise<WorkspaceRegistration>;
  streamFixture(onEvent: (event: StreamEvent) => Promise<void>): Promise<void>;
}
export class AdapterError extends Error {
  readonly code: string; readonly retryable: boolean;
  constructor(code: string, message: string, retryable = false) {
    super(message); this.name = 'AdapterError'; this.code = code; this.retryable = retryable;
  }
}
export function serviceError(value: unknown): AdapterError {
  if (value instanceof AdapterError) return value;
  if (typeof value === 'object' && value !== null && 'message' in value) {
    const error = value as { code?: unknown; message: unknown; retryable?: unknown };
    return new AdapterError(typeof error.code === 'string' ? error.code : 'transport',
      String(error.message), error.retryable === true);
  }
  return new AdapterError('transport', String(value));
}
