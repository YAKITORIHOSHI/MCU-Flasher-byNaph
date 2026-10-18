import './styles.css';
import './vendor-types.ts';
import { createAdapter } from './adapter.ts';
import { AdapterError, PROTOCOL_VERSION, serviceError } from './contracts.ts';
import type { PrototypeInfo, ScaffoldOptions, StreamEvent, WorkspaceRegistration } from './contracts.ts';
import { DocumentStore } from './document-store.ts';
import type { DocumentRecord } from './document-store.ts';
import { defineThemes, palettes } from './themes.ts';
import type { ThemeName } from './themes.ts';
import type { Editor, Monaco, Terminal, TerminalFit } from './vendor-types.ts';
import { installProbe } from './probe.ts';
import { installMonacoWorkerHost } from './monaco-host.ts';

function element<T extends HTMLElement>(id: string): T {
  const result = document.getElementById(id);
  if (!result) throw new Error(`Required interface element is missing: ${id}`);
  return result as T;
}
const adapter = createAdapter();
const status = element('status');
const errorBox = element('error');
let editor: Editor;
let monaco: Monaco;
let term: Terminal | null = null;
let fit: TerminalFit | null = null;
let activeId: string | null = null;
let operationGeneration = 0;
let busy = false;
let saving = false;
let streaming = false;
let info: PrototypeInfo;
let theme: ThemeName = 'default';
const store = new DocumentStore(snapshot => {
  const language = snapshot.document.extension === '.c' ? 'c' :
    ['.txt', '.md'].includes(snapshot.document.extension) ? 'plaintext' : 'cpp';
  return monaco.editor.createModel(snapshot.content, language,
    monaco.Uri.parse(`inmemory://${encodeURIComponent(snapshot.workspaceId)}/${encodeURIComponent(snapshot.document.documentId)}`));
}, renderSources);

function showError(error: unknown): void {
  const failure = serviceError(error);
  errorBox.textContent = `${failure.message} (${failure.code})`;
  errorBox.hidden = false;
  status.textContent = 'Action failed. Existing editor buffers are retained.';
}
function clearError(): void { errorBox.hidden = true; errorBox.textContent = ''; }
function updateControls(): void {
  element<HTMLButtonElement>('open-project').disabled = busy || saving;
  element<HTMLButtonElement>('new-project').disabled = busy || saving;
  element<HTMLButtonElement>('refresh').disabled = busy || !store.workspaceId;
  const record = activeId ? store.records.get(activeId) : undefined;
  element<HTMLButtonElement>('save').disabled = busy || saving || !record || !record.dirty || record.conflict || record.missing;
  element<HTMLButtonElement>('run-stream').disabled = streaming;
}
function renderSources(): void {
  if (!editor) return;
  const list = element('file-list');
  const tabs = element('source-tabs');
  // Preserve focused tab by updating the existing button objects rather than rebuilding.
  const existingFiles = new Map([...list.querySelectorAll<HTMLButtonElement>('button')].map(button => [button.dataset.id, button]));
  const files = store.registration?.manifest.files ?? [];
  for (const source of files) {
    let button = existingFiles.get(source.documentId);
    if (!button) {
      button = document.createElement('button'); button.type = 'button'; button.dataset.id = source.documentId;
      button.addEventListener('click', () => { void openDocument(source.documentId).catch(showError); });
      list.append(button);
    }
    button.textContent = source.name; button.title = source.name;
    button.setAttribute('aria-current', String(source.documentId === activeId));
    existingFiles.delete(source.documentId);
  }
  for (const button of existingFiles.values()) button.remove();
  const existingTabs = new Map([...tabs.querySelectorAll<HTMLButtonElement>('button')].map(button => [button.dataset.id, button]));
  for (const [id, record] of store.records) {
    let button = existingTabs.get(id);
    if (!button) {
      button = document.createElement('button'); button.type = 'button'; button.dataset.id = id;
      button.setAttribute('role', 'tab'); button.setAttribute('aria-controls', 'monaco-anchor');
      button.addEventListener('click', () => activate(id));
      button.addEventListener('keydown', event => navigateSourceTabs(event, id));
      tabs.append(button);
    }
    button.replaceChildren(document.createTextNode(record.source.name));
    if (record.dirty) { const dot = document.createElement('span'); dot.className = 'dirty-mark'; dot.setAttribute('aria-hidden', 'true'); button.append(dot); }
    if (record.conflict || record.missing) { const label = document.createElement('span'); label.className = 'conflict-mark'; label.textContent = record.missing ? 'missing' : 'conflict'; button.append(label); }
    button.setAttribute('aria-label', `${record.source.name}${record.dirty ? ', unsaved changes' : ''}${record.conflict ? ', external conflict' : ''}${record.missing ? ', deleted from disk' : ''}`);
    button.setAttribute('aria-selected', String(id === activeId)); button.tabIndex = id === activeId ? 0 : -1;
    existingTabs.delete(id);
  }
  for (const button of existingTabs.values()) button.remove();
  const active = activeId ? store.records.get(activeId) : undefined;
  element('document-status').textContent = active ?
    active.missing ? 'Source removed from disk; unsaved buffer retained' : active.conflict ? 'External change; buffer retained' : active.dirty ? 'Unsaved changes' : 'Saved source' : 'No source open';
  element('editor-empty').hidden = Boolean(active);
  updateControls();
}
function navigateSourceTabs(event: KeyboardEvent, id: string): void {
  const ids = [...store.records.keys()]; const index = ids.indexOf(id);
  let next = index;
  if (event.key === 'ArrowRight') next = (index + 1) % ids.length;
  else if (event.key === 'ArrowLeft') next = (index + ids.length - 1) % ids.length;
  else if (event.key === 'Home') next = 0;
  else if (event.key === 'End') next = ids.length - 1;
  else return;
  event.preventDefault(); activate(ids[next], false);
  [...element('source-tabs').querySelectorAll<HTMLButtonElement>('button')].find(button => button.dataset.id === ids[next])?.focus();
}
function activate(id: string, focus = true): void {
  const record = store.records.get(id);
  if (!record) return;
  const prior = activeId ? store.records.get(activeId) : undefined;
  if (prior) prior.viewState = editor.saveViewState();
  activeId = id; editor.setModel(record.model);
  if (record.viewState) editor.restoreViewState(record.viewState);
  renderSources(); queueLayout(); if (focus) editor.focus();
}
async function openDocument(id: string): Promise<void> {
  if (store.records.has(id)) { activate(id); return; }
  const workspaceId = store.workspaceId; const generation = operationGeneration;
  if (!workspaceId) return;
  const snapshot = await adapter.readDocument({ workspaceId, documentId: id });
  if (generation !== operationGeneration || workspaceId !== store.workspaceId) return;
  store.add(snapshot); activate(id);
}
async function adopt(registration: WorkspaceRegistration): Promise<void> {
  operationGeneration += 1; activeId = null; editor.setModel(null);
  store.reset(registration);
  element('project-name').textContent = registration.name;
  element('project-name').title = registration.root;
  element<HTMLInputElement>('project-path').value = registration.root;
  const first = registration.manifest.files[0];
  if (first) await openDocument(first.documentId);
  status.textContent = `Opened ${registration.name}: ${registration.manifest.files.length} root sources.`;
}
function mayReplace(): boolean {
  return !store.dirty || window.confirm('This preview has unsaved buffers. Discard them and switch fixture project?');
}
async function openProject(path: string): Promise<void> {
  if (busy || !mayReplace()) return;
  clearError(); busy = true; updateControls();
  try { const registration = await adapter.openProject(path); await adopt(registration); }
  finally { busy = false; updateControls(); }
}
async function save(): Promise<void> {
  if (!activeId || saving || busy) return;
  let record: DocumentRecord | undefined = store.records.get(activeId);
  if (!record?.dirty) return;
  clearError(); const request = store.saveRequest(activeId);
  saving = true; updateControls();
  try {
    const acknowledgement = await adapter.saveDocument(request);
    if (!store.acknowledge(request, acknowledgement)) throw new AdapterError('stale_reply', 'Ignored a save reply from an older workspace or edit.');
    record = store.records.get(request.documentId);
    status.textContent = `${adapter.mode === 'browser-fixture' ? 'Memory fixture' : 'Native fixture'} save acknowledged${record?.dirty ? '; newer edits remain unsaved.' : '.'}`;
  } finally { saving = false; updateControls(); }
}
async function refresh(): Promise<void> {
  const workspaceId = store.workspaceId; if (!workspaceId || busy) return;
  clearError(); busy = true; updateControls();
  try {
    const manifest = await adapter.manifest(workspaceId);
    await store.reconcile(manifest, documentId => adapter.readDocument({ workspaceId, documentId }));
    if (activeId && !store.records.has(activeId)) {
      activeId = null; editor.setModel(null);
      const first = store.records.keys().next().value;
      if (first) activate(first); else if (manifest.files[0]) await openDocument(manifest.files[0].documentId);
    }
    renderSources(); status.textContent = `Manifest refreshed: ${manifest.files.length} root sources; existing models retained.`;
  } finally { busy = false; updateControls(); }
}
let layoutPending = 0;
function queueLayout(): void {
  if (layoutPending) return;
  layoutPending = requestAnimationFrame(() => {
    layoutPending = 0; editor?.layout();
    if (!element('terminal-pane').hidden && element('terminal').clientWidth > 50 && element('terminal').clientHeight > 50) fit?.fit();
  });
}
function applyTheme(value: ThemeName): void {
  theme = value; document.body.dataset.theme = value;
  monaco?.editor.setTheme(`mcu-${value}`);
  if (term) term.options.theme = { background: palettes[value].background, foreground: palettes[value].foreground,
    cursor: palettes[value].cursor, selectionBackground: palettes[value].selectionBackground };
  queueLayout();
}
function selectTool(name: 'terminal' | 'gates'): void {
  for (const item of ['terminal', 'gates']) {
    const selected = item === name;
    element(`${item}-pane`).hidden = !selected;
    const tab = element<HTMLButtonElement>(`${item}-tab`);
    tab.setAttribute('aria-selected', String(selected)); tab.tabIndex = selected ? 0 : -1;
  }
  element('run-stream').hidden = name !== 'terminal'; element('clear-terminal').hidden = name !== 'terminal'; queueLayout();
}
async function streamEvent(event: StreamEvent): Promise<void> {
  if (!term) throw new AdapterError('renderer_missing', 'Existing xterm renderer is unavailable.');
  const data = event.data ?? (event.type === 'finished' ? `\r\nFixture ended with exit code ${event.exitCode ?? 'unknown'}.\r\n` : '');
  if (data.length > 65536) throw new AdapterError('limit_exceeded', 'Fixture output exceeded the preview event limit.');
  // Wait for xterm's consumption callback before processing the next queued event.
  await new Promise<void>(resolve => term!.write(data, resolve));
}
async function runStream(): Promise<void> {
  if (streaming) return;
  clearError(); streaming = true; updateControls(); selectTool('terminal');
  try { await adapter.streamFixture(streamEvent); status.textContent = 'Controlled fixture stream completed; no hardware or toolchain was accessed.'; }
  finally { streaming = false; updateControls(); }
}
async function createProject(event: SubmitEvent): Promise<void> {
  event.preventDefault(); if (busy || !mayReplace()) return;
  clearError(); const button = element<HTMLButtonElement>('submit-create'); button.disabled = true; busy = true; updateControls();
  const options: ScaffoldOptions = { name: element<HTMLInputElement>('create-name').value,
    includeH: element<HTMLInputElement>('include-header').checked,
    includeCpp: element<HTMLInputElement>('include-cpp').checked,
    template: element<HTMLSelectElement>('create-template').value as ScaffoldOptions['template'] };
  try {
    const registration = await adapter.createProject(element<HTMLInputElement>('create-parent').value, options);
    element<HTMLDialogElement>('create-dialog').close(); await adopt(registration);
  } finally { button.disabled = false; busy = false; updateControls(); }
}
async function start(): Promise<void> {
  // Module scripts execute while readyState is interactive, before the legacy
  // bundle's DOMContentLoaded initializer. Wait for that actual initialization.
  if (!window.editorInstance && document.readyState !== 'complete') {
    await new Promise<void>(resolve => document.addEventListener('DOMContentLoaded', () => resolve(), { once: true }));
  }
  if (!window.monaco || !window.editorInstance) throw new AdapterError('renderer_missing', 'Existing bundled Monaco editor did not initialize. Rebuild local assets.');
  monaco = window.monaco; editor = window.editorInstance;
  installMonacoWorkerHost();
  editor.updateOptions({ automaticLayout: false, ariaLabel: 'MCU Flasher source editor',
    fontFamily: 'Ubuntu Mono, DejaVu Sans Mono, Consolas, monospace', fontSize: 14 });
  defineThemes(monaco);
  if (window.Terminal && window.FitAddon) {
    term = new window.Terminal({ scrollback: 2000, fontFamily: 'Ubuntu Mono, DejaVu Sans Mono, Consolas, monospace',
      fontSize: 13, minimumContrastRatio: 4.5, cursorBlink: false, disableStdin: true });
    fit = new window.FitAddon.FitAddon(); term.loadAddon(fit); term.open(element('terminal'));
  } else throw new AdapterError('renderer_missing', 'Existing xterm assets did not initialize.');
  applyTheme(theme);
  const observer = new ResizeObserver(queueLayout);
  observer.observe(element('monaco-anchor')); observer.observe(element('terminal'));
  window.addEventListener('beforeunload', event => {
    if (store.dirty) { event.preventDefault(); event.returnValue = ''; }
  });
  window.addEventListener('pagehide', () => { observer.disconnect(); if (layoutPending) cancelAnimationFrame(layoutPending); store.dispose(); term?.dispose(); fit?.dispose(); }, { once: true });
  info = await adapter.info();
  if (info.protocolVersion !== PROTOCOL_VERSION) throw new AdapterError('protocol', 'Native protocol version does not match this interface.');
  element('mode-notice').textContent = adapter.mode === 'native-fixture' ?
    'Native fixture preview — file access is restricted to the migration audit scratch area. The production application remains unchanged.' :
    'Browser fixture preview — edits and saves are memory-only. Native disk access, Tauri and hardware compatibility are unverified.';
  element('host-status').textContent = `${info.host.os} / ${info.host.architecture}`;
  element<HTMLInputElement>('project-path').value = info.fixtureProject;
  element<HTMLInputElement>('create-parent').value = info.fixtureParent;
  for (const gate of info.gates) { const li = document.createElement('li'); li.textContent = gate; element('gate-list').append(li); }
  element<HTMLFormElement>('open-form').addEventListener('submit', event => { event.preventDefault(); void openProject(element<HTMLInputElement>('project-path').value).catch(showError); });
  element('save').addEventListener('click', () => { void save().catch(showError); });
  element('refresh').addEventListener('click', () => { void refresh().catch(showError); });
  element('new-project').addEventListener('click', () => element<HTMLDialogElement>('create-dialog').showModal());
  element('cancel-create').addEventListener('click', () => element<HTMLDialogElement>('create-dialog').close());
  element<HTMLFormElement>('create-form').addEventListener('submit', event => { void createProject(event).catch(showError); });
  element<HTMLSelectElement>('theme').addEventListener('change', event => applyTheme((event.target as HTMLSelectElement).value as ThemeName));
  element('run-stream').addEventListener('click', () => { void runStream().catch(showError); });
  element('clear-terminal').addEventListener('click', () => { term?.clear(); status.textContent = 'Terminal display cleared.'; });
  for (const name of ['terminal', 'gates'] as const) {
    element(`${name}-tab`).addEventListener('click', () => selectTool(name));
    element(`${name}-tab`).addEventListener('keydown', event => {
      if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
        event.preventDefault(); const next = event.key === 'Home' ? 'terminal' : event.key === 'End' ? 'gates' : name === 'terminal' ? 'gates' : 'terminal';
        selectTool(next); element(`${next}-tab`).focus();
      }
    });
  }
  window.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); void save().catch(showError); }
    if (event.key === 'Escape') clearError();
  });
  installProbe({ adapter, info, store, editor, monaco, adopt, openDocument, activate, save, applyTheme, streamEvent });
  updateControls(); status.textContent = 'Ready. Open the supplied fixture folder to begin.';
}
void start().catch(showError);
