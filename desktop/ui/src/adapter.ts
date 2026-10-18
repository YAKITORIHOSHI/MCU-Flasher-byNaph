import { Channel, invoke, isTauri } from '@tauri-apps/api/core';
import { AdapterError, PROTOCOL_VERSION, serviceError } from './contracts.ts';
import { createFixtureStreamConsumer } from './fixture-stream.ts';
import type { DesktopAdapter, DocumentRequest, DocumentSnapshot, PrototypeInfo,
  SaveAcknowledgement, SaveDocumentRequest, ScaffoldOptions, SourceFile,
  SourceManifest, StreamEvent, WorkspaceRegistration } from './contracts.ts';

async function native<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  try { return await invoke<T>(command, args); } catch (error) { throw serviceError(error); }
}
export class NativeAdapter implements DesktopAdapter {
  readonly mode = 'native-fixture' as const;
  info() { return native<PrototypeInfo>('prototype_info'); }
  openProject(path: string) { return native<WorkspaceRegistration>('open_project', { path }); }
  manifest(workspaceId: string) { return native<SourceManifest>('project_manifest', { workspaceId }); }
  readDocument(request: DocumentRequest) { return native<DocumentSnapshot>('read_document', { request }); }
  saveDocument(request: SaveDocumentRequest) { return native<SaveAcknowledgement>('save_document', { request }); }
  createProject(parentPath: string, options: ScaffoldOptions) {
    return native<WorkspaceRegistration>('create_project', { parentPath, options });
  }
  async streamFixture(onEvent: (event: StreamEvent) => Promise<void>): Promise<void> {
    const onOutput = new Channel<StreamEvent>();
    const consumer = createFixtureStreamConsumer(onEvent, acknowledgement =>
      native<void>('acknowledge_fixture', { ...acknowledgement }));
    onOutput.onmessage = event => { consumer.receive(event); };
    try { await native<void>('stream_fixture', { onEvent: onOutput }); }
    catch (error) { consumer.abort(error); }
    await consumer.finished();
  }
}
const encoder = new TextEncoder();
async function digest(content: string): Promise<string> {
  const hash = await crypto.subtle.digest('SHA-256', encoder.encode(content));
  return [...new Uint8Array(hash)].map(byte => byte.toString(16).padStart(2, '0')).join('');
}

/** Deliberately memory-only. This adapter cannot read/save native project files. */
export class BrowserFixtureAdapter implements DesktopAdapter {
  readonly mode = 'browser-fixture' as const;
  private workspace: WorkspaceRegistration | null = null;
  private contents = new Map<string, string>();
  private generation = 0;
  async info(): Promise<PrototypeInfo> {
    return { protocolVersion: PROTOCOL_VERSION, mode: 'browser_fixture',
      fixtureParent: 'memory://fixtures', fixtureProject: 'memory://fixtures/Welcome',
      host: { os: 'browser', architecture: 'unverified' },
      gates: ['Browser fixture uses memory only. Native disk, Tauri, USB and tool execution are unverified.'] };
  }
  async openProject(path: string): Promise<WorkspaceRegistration> {
    if (path !== 'memory://fixtures/Welcome') {
      throw new AdapterError('forbidden_root', 'Browser preview can open only memory://fixtures/Welcome.');
    }
    if (this.workspace?.name === 'Welcome') return structuredClone(this.workspace);
    return this.seed('Welcome', {
      'Welcome.ino': '#include "Welcome.h"\n\nvoid setup() {\n  Serial.begin(115200);\n}\n\nvoid loop() {\n  // Edit this memory-only fixture to exercise the editor.\n}\n',
      'Welcome.h': '#pragma once\n\n// Shared project declarations.\n',
      'Welcome.cpp': '#include "Welcome.h"\n\n// Additional implementation.\n',
    });
  }
  private async seed(name: string, sources: Record<string, string>): Promise<WorkspaceRegistration> {
    const workspaceId = `browser-fixture-${++this.generation}`;
    this.contents = new Map(Object.entries(sources));
    const files: SourceFile[] = [];
    for (const [filename, content] of this.contents) {
      files.push({ documentId: filename, name: filename, extension: filename.slice(filename.lastIndexOf('.')),
        isMain: filename.endsWith('.ino'), sizeBytes: encoder.encode(content).length,
        revision: 1, contentDigest: await digest(content) });
    }
    this.workspace = { protocolVersion: PROTOCOL_VERSION, workspaceId,
      root: `memory://fixtures/${name}`, name,
      manifest: { protocolVersion: PROTOCOL_VERSION, workspaceId, revision: 1, files } };
    return structuredClone(this.workspace);
  }
  private own(workspaceId: string): WorkspaceRegistration {
    if (this.workspace?.workspaceId !== workspaceId) {
      throw new AdapterError('workspace_not_found', 'This memory workspace is no longer active.');
    }
    return this.workspace;
  }
  async manifest(workspaceId: string): Promise<SourceManifest> {
    return structuredClone(this.own(workspaceId).manifest);
  }
  async readDocument(request: DocumentRequest): Promise<DocumentSnapshot> {
    const workspace = this.own(request.workspaceId);
    const document = workspace.manifest.files.find(file => file.documentId === request.documentId);
    const content = this.contents.get(request.documentId);
    if (!document || content === undefined) throw new AdapterError('not_found', 'Fixture source does not exist.');
    return { workspaceId: workspace.workspaceId, document: structuredClone(document), content };
  }
  async saveDocument(request: SaveDocumentRequest): Promise<SaveAcknowledgement> {
    const workspace = this.own(request.workspaceId);
    const file = workspace.manifest.files.find(candidate => candidate.documentId === request.documentId);
    if (!file) throw new AdapterError('not_found', 'Fixture source does not exist.');
    if (file.revision !== request.expectedRevision || file.contentDigest !== request.expectedDigest) {
      throw new AdapterError('stale_revision', 'Fixture source changed after this buffer was read.');
    }
    const contentDigest = await digest(request.content);
    // A second ownership check prevents an async digest from writing a successor workspace.
    this.own(request.workspaceId);
    const unchanged = contentDigest === file.contentDigest;
    if (!unchanged) {
      file.revision += 1; workspace.manifest.revision += 1;
      file.contentDigest = contentDigest; file.sizeBytes = encoder.encode(request.content).length;
      this.contents.set(request.documentId, request.content);
    }
    return { workspaceId: workspace.workspaceId, documentId: file.documentId,
      revision: file.revision, contentDigest, editRevision: request.editRevision,
      bytesWritten: unchanged ? 0 : file.sizeBytes, unchanged };
  }
  async createProject(parentPath: string, options: ScaffoldOptions): Promise<WorkspaceRegistration> {
    if (parentPath !== 'memory://fixtures' || !/^[A-Za-z_][A-Za-z0-9_-]{0,63}$/.test(options.name)) {
      throw new AdapterError('invalid_path', 'Use the memory fixture parent and a simple project name.');
    }
    const header = options.includeH ? `#include "${options.name}.h"\n` : '';
    // Match the Rust scaffold bytes, themselves retained from the legacy templates.
    const templates = {
      standard: 'void setup() {\n\n}\n\nvoid loop() {\n\n}\n',
      bare: 'void setup() {\n  \n}\n\nvoid loop() {\n  \n}\n',
      blink: 'void setup() {\n  pinMode(LED_BUILTIN, OUTPUT);\n}\n\nvoid loop() {\n  digitalWrite(LED_BUILTIN, HIGH);\n  delay(1000);\n  digitalWrite(LED_BUILTIN, LOW);\n  delay(1000);\n}\n',
    };
    if (!Object.hasOwn(templates, options.template)) throw new AdapterError('invalid_path', 'Unknown fixture project template.');
    const sources: Record<string, string> = {
      [`${options.name}.ino`]: (header ? `${header}\n` : '') + templates[options.template],
    };
    if (options.includeH) sources[`${options.name}.h`] = '#pragma once\n';
    if (options.includeCpp) sources[`${options.name}.cpp`] = header;
    return this.seed(options.name, sources);
  }
  async streamFixture(onEvent: (event: StreamEvent) => Promise<void>): Promise<void> {
    for (const [sequence, data] of [
      'Browser fixture: no process is running.\r\n',
      'Unicode: café · 日本語 · λ\r\n',
      '\u001b[36mANSI rendering uses the existing xterm renderer.\u001b[0m\r\n',
    ].entries()) await onEvent({ sequence, type: 'output', data });
    await onEvent({ sequence: 3, type: 'finished', data: 'Memory fixture output complete.\r\n', exitCode: 0 });
  }
}
export function createAdapter(): DesktopAdapter {
  return isTauri() ? new NativeAdapter() : new BrowserFixtureAdapter();
}
