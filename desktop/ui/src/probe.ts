import { PROTOCOL_VERSION, serviceError } from './contracts.ts';
import { invoke } from '@tauri-apps/api/core';
import { workerEvidence } from './monaco-host.ts';
import type { DesktopAdapter, PrototypeInfo, WorkspaceRegistration } from './contracts.ts';
import type { DocumentStore, TextModel } from './document-store.ts';
import type { Editor, Monaco } from './vendor-types.ts';

interface ProbeModel extends TextModel {
  getFullModelRange(): { startLineNumber: number; startColumn: number; endLineNumber: number; endColumn: number };
  undo(): Promise<void> | void;
  redo(): Promise<void> | void;
  canUndo(): boolean;
  canRedo(): boolean;
}
interface ProbeEditor extends Editor {
  pushUndoStop(): boolean;
  executeEdits(source: string, edits: { range: ReturnType<ProbeModel['getFullModelRange']>; text: string }[]): boolean;
  trigger(source: string, handlerId: string, payload: unknown): void;
}
export interface ProbeResult {
  schemaVersion: 1; nativeAdapter: boolean; protocolVersion: number;
  checks: Record<string, boolean>; errors: { code: string; message: string }[];
}
export interface ProbeContext {
  adapter: DesktopAdapter; info: PrototypeInfo; store: DocumentStore; editor: Editor; monaco: Monaco;
  adopt(registration: WorkspaceRegistration): Promise<void>;
  openDocument(documentId: string): Promise<void>;
  activate(documentId: string): void;
  save(): Promise<void>;
  applyTheme(theme: 'default' | 'light' | 'solarized'): void;
  streamEvent(event: import('./contracts.ts').StreamEvent): Promise<void>;
}
declare global { interface Window { __MCU_PROTOTYPE_PROBE__?: () => Promise<ProbeResult> } }

/** Explicit verification entry point; native fixture writes remain backend-scoped. */
export function installProbe(context: ProbeContext): void {
  if (new URLSearchParams(location.search).get('verify') !== '1') return;
  let running: Promise<ProbeResult> | null = null;
  window.__MCU_PROTOTYPE_PROBE__ = () => {
    if (!running) running = run(context);
    return running;
  };
}
async function run(context: ProbeContext): Promise<ProbeResult> {
  const { adapter, info, store, monaco } = context;
  const editor = context.editor as ProbeEditor;
  const result: ProbeResult = { schemaVersion: 1, nativeAdapter: adapter.mode === 'native-fixture',
    protocolVersion: info.protocolVersion, checks: {}, errors: [] };
  const check = (name: string, value: boolean) => {
    result.checks[name] = value;
    if (!value) throw new Error(`Verification failed: ${name}`);
  };
  try {
    check('native_fixture_protocol', result.nativeAdapter && info.protocolVersion === PROTOCOL_VERSION && info.mode === 'fixture_only');
    check('local_monaco_and_xterm', Boolean(monaco && window.Terminal && window.FitAddon && editor));
    const project = await adapter.openProject(info.fixtureProject);
    await context.adopt(project);
    const source = project.manifest.files.find(file => file.isMain) ?? project.manifest.files[0];
    check('root_source_manifest', Boolean(source));
    const snapshot = await adapter.readDocument({ workspaceId: project.workspaceId, documentId: source.documentId });
    const record = store.records.get(source.documentId)!;
    const model = record.model as ProbeModel;
    check('native_read_matches_editor', model.getValue() === snapshot.content);
    let languageLoaded = false;
    for (let attempt = 0; attempt < 100; attempt++) {
      const tokens = monaco.editor.tokenize('void setup() { return; }', 'cpp');
      languageLoaded = tokens.some(line => line.some(token => token.type.includes('keyword')));
      if (languageLoaded) break;
      await new Promise(resolve => setTimeout(resolve, 20));
    }
    check('reused_cpp_language_chunk_loaded', languageLoaded);
    const workerResponse = await fetch('/editor.worker.js');
    check('local_worker_asset_available', workerResponse.ok && (await workerResponse.text()).length > 1000);
    const diffHost = document.createElement('div');
    diffHost.style.cssText = 'position:fixed;left:-10000px;top:0;width:400px;height:200px';
    document.body.append(diffHost);
    const original = monaco.editor.createModel('original line\n', 'plaintext');
    const modified = monaco.editor.createModel('modified line\n', 'plaintext');
    const diff = monaco.editor.createDiffEditor(diffHost, { automaticLayout: false });
    try {
      diff.setModel({ original, modified });
      for (let attempt = 0; attempt < 200 && (!diff.getLineChanges() || workerEvidence.messages === 0); attempt++) {
        await new Promise(resolve => setTimeout(resolve, 20));
      }
      check('native_editor_worker_executed_and_replied', workerEvidence.created > 0 && workerEvidence.messages > 0 && workerEvidence.errors === 0);
      check('reused_diff_worker_computes_changes', Boolean(diff.getLineChanges()?.length));
    } finally { diff.dispose(); original.dispose(); modified.dispose(); diffHost.remove(); }
    const changed = snapshot.content + '\n// Migration probe: café 日本語 λ\n';
    editor.pushUndoStop();
    editor.executeEdits('migration-probe', [{ range: model.getFullModelRange(), text: changed }]);
    editor.pushUndoStop();
    check('model_edits_are_dirty', record.dirty && model.getValue() === changed);
    for (const theme of ['light', 'solarized', 'default'] as const) { context.applyTheme(theme); editor.layout(); }
    const other = project.manifest.files.find(file => file.documentId !== source.documentId);
    if (other) { await context.openDocument(other.documentId); context.activate(source.documentId); }
    check('model_identity_survives_theme_layout_tabs', editor.getModel() === model && store.records.get(source.documentId)?.model === model);
    const viewState = editor.saveViewState();
    const transfer = await invoke<{ cycles: number; sameWebview: boolean }>('verify_webview_transfer');
    check('native_webview_transfer_three_cycles', transfer.cycles === 3 && transfer.sameWebview);
    check('dirty_model_identity_survives_native_transfer', editor.getModel() === model && record.dirty && model.getValue() === changed);
    check('view_state_survives_native_transfer', JSON.stringify(editor.saveViewState()) === JSON.stringify(viewState));
    // The model's public history API proves retained undo independently of OS
    // window activation. Keyboard/IME/focus routing is a separate native gate.
    check('undo_history_available_after_native_transfer', model.canUndo());
    await model.undo();
    check('undo_survives_theme_layout_tabs', model.getValue() === snapshot.content);
    check('redo_history_available_after_native_transfer', model.canRedo());
    await model.redo();
    check('redo_survives_theme_layout_tabs', model.getValue() === changed);
    await context.save();
    const saved = await adapter.readDocument({ workspaceId: project.workspaceId, documentId: source.documentId });
    check('native_save_ack_and_unicode_readback', saved.content === changed && !record.dirty && saved.document.contentDigest === record.source.contentDigest);
    let staleRejected = false;
    try {
      await adapter.saveDocument({ workspaceId: project.workspaceId, documentId: source.documentId,
        expectedRevision: snapshot.document.revision, expectedDigest: snapshot.document.contentDigest,
        editRevision: 900, content: '// Must never replace a newer acknowledged save' });
    } catch (error) { staleRejected = ['stale_revision', 'external_change'].includes(serviceError(error).code); }
    check('native_ipc_rejects_stale_save', staleRejected);
    let escapedRootRejected = false;
    try { await adapter.openProject(info.fixtureParent + '/../../../../'); }
    catch (error) { escapedRootRejected = ['invalid_path', 'forbidden_root'].includes(serviceError(error).code); }
    check('native_ipc_rejects_escaping_root', escapedRootRejected);
    const manifest = await adapter.manifest(project.workspaceId);
    const identity = record.model;
    await store.reconcile(manifest, documentId => adapter.readDocument({ workspaceId: project.workspaceId, documentId }));
    check('manifest_refresh_retains_model', store.records.get(source.documentId)?.model === identity);
    const scaffold = await adapter.createProject(info.fixtureParent, { name: `Probe_${Date.now()}`,
      includeH: true, includeCpp: true, template: 'standard' });
    const header = scaffold.manifest.files.find(file => file.extension === '.h');
    check('native_scaffold_header', Boolean(header));
    const implementation = scaffold.manifest.files.filter(file => ['.ino', '.cpp'].includes(file.extension));
    let includes = implementation.length === 2;
    for (const file of implementation) {
      const generated = await adapter.readDocument({ workspaceId: scaffold.workspaceId, documentId: file.documentId });
      includes = includes && generated.content.includes(`#include "${header!.name}"`);
    }
    check('native_scaffold_includes_header_in_sketch_and_cpp', includes);
    let previous = -1; let output = ''; let finished = false; let exitCode: number | undefined;
    await adapter.streamFixture(async event => {
      check('ordered_channel', event.sequence > previous); previous = event.sequence;
      if (event.data) output += event.data;
      check('bounded_fixture_stream', output.length <= 65536);
      if (event.type === 'finished') { finished = true; exitCode = event.exitCode; }
      await context.streamEvent(event);
    });
    check('native_stream_finished', finished && exitCode === 0);
    check('native_stream_unicode', /café|日本語|λ/.test(output));
    check('native_stream_ansi', output.includes('\u001b['));
  } catch (error) {
    const failure = serviceError(error);
    result.errors.push({ code: failure.code, message: failure.message });
  }
  return result;
}
