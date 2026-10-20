/* Only the embedded editor island is JavaScript. Native controls and service ownership are Rust. */
(() => {
  'use strict';
  const send = report => window.ipc.postMessage(JSON.stringify(report));
  const fail = error => send({ kind: 'error', message: String(error?.message ?? error).slice(0, 1024) });
  window.addEventListener('error', event => fail(event.error ?? event.message));
  window.addEventListener('unhandledrejection', event => fail(event.reason));
  let workerMessages = 0;
  const workers = new Map();
  let workerSource;
  window.MonacoEnvironment = { globalAPI: true };
  const getWorker = async (_module, label) => {
    if (workers.size >= 4) throw new Error('Editor fixture worker budget exceeded.');
    workerSource ??= fetch('editor.worker.js').then(async response => {
      if (!response.ok) throw new Error('Offline editor worker is unavailable.');
      return response.text();
    });
    const source = await workerSource;
    if (workers.size >= 4) throw new Error('Editor fixture worker budget exceeded.');
    const url = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }));
    const worker = new Worker(url, { name: label });
    workers.set(worker, url);
    worker.addEventListener('message', () => { workerMessages += 1; });
    worker.addEventListener('error', fail);
    const terminate = worker.terminate.bind(worker);
    worker.terminate = () => { URL.revokeObjectURL(url); workers.delete(worker); terminate(); };
    return worker;
  };
  window.addEventListener('pagehide', () => [...workers.keys()].forEach(worker => worker.terminate()), { once: true });
  const wait = async (stage, predicate) => {
    for (let count = 0; count < 250; count++) {
      if (predicate()) return;
      await new Promise(resolve => setTimeout(resolve, 20));
    }
    throw new Error(`Offline Monaco readiness deadline expired at ${stage}.`);
  };
  window.addEventListener('DOMContentLoaded', () => { void (async () => {
    await wait('global editor', () => window.monaco && window.editorInstance);
    // The retained webpack bundle replaces MonacoEnvironment during loading.
    // Install the custom-scheme-safe worker host after that initialization.
    window.MonacoEnvironment.getWorker = getWorker;
    const monaco = window.monaco;
    const editor = window.editorInstance;
    const fixture = window.__nativeFixture;
    if (!fixture.smoke) {
      const documents = new Map();
      let current;
      let currentTheme = 'default';
      const select = id => {
        const document = documents.get(id);
        if (!document) return false;
        if (current) current.view = editor.saveViewState();
        current = document;
        editor.setModel(document.model);
        if (document.view) editor.restoreViewState(document.view);
        editor.layout();
        return true;
      };
      window.__mcuOpenDocuments = entries => {
        if (!Array.isArray(entries) || entries.length > 256) throw new Error('Source tab limit exceeded.');
        const ids = new Set();
        let total = 0;
        for (const entry of entries) {
          if (!entry || typeof entry.id !== 'string' || !entry.id || entry.id.length > 256 || ids.has(entry.id)
              || typeof entry.name !== 'string' || typeof entry.content !== 'string'
              || entry.content.length > 8 * 1024 * 1024) throw new Error('Invalid native source snapshot.');
          ids.add(entry.id);
          total += entry.content.length;
        }
        if (total > 32 * 1024 * 1024) throw new Error('Source workspace exceeds its byte budget.');
        // A native Project action opens another workspace. Never discard an
        // existing dirty document in response to an older source-load result.
        if ([...documents.values()].some(item => item.model.getValue(undefined, true) !== item.baseline)) {
          fail('The project load was cancelled. Your unsaved edits are still open.');
          return false;
        }
        editor.setModel(null);
        current = undefined;
        for (const item of documents.values()) { item.listener.dispose(); item.model.dispose(); }
        documents.clear();
        for (const entry of entries) {
          const model = monaco.editor.createModel(entry.content, 'cpp', monaco.Uri.parse(`inmemory://native-source/${encodeURIComponent(entry.id)}`));
          const item = { model, original: entry.content, baseline: model.getValue(undefined, true), view: undefined, revision: 0 };
          item.listener = model.onDidChangeContent(() => {
            item.revision += 1;
            send({ kind: 'document_changed', document_id: entry.id,
              edit_revision: item.revision, dirty: model.getValue(undefined, true) !== item.baseline });
          });
          documents.set(entry.id, item);
        }
        if (entries.length) select(entries[0].id);
        send({ kind: 'documents_opened', documents: entries.map(entry => entry.id) });
        return true;
      };
      window.__mcuSelectDocument = select;
      window.__mcuApplyTheme = (mode, palette) => {
        currentTheme = mode;
        monaco.editor.defineTheme('mcu-native-workspace', {
          base: mode === 'light' ? 'vs' : 'vs-dark', inherit: true, rules: [],
          colors: { 'editor.background': palette.background, 'editor.foreground': palette.foreground,
            'editorLineNumber.foreground': mode === 'light' ? '#57606a' : '#adc0d3',
            'editorCursor.foreground': palette.accent, 'editor.selectionBackground': palette.selection }
        });
        monaco.editor.setTheme('mcu-native-workspace');
        document.body.style.background = palette.background;
      };
      let proof;
      window.__mcuWorkspaceSnapshot = async phase => {
        const entries = [...documents.entries()];
        if (phase === 0) {
          const [id, first] = entries.find(([, item]) => item.original.startsWith('\ufeff')) ?? entries[0];
          select(id);
          editor.pushUndoStop();
          editor.executeEdits('native-workspace-proof', [{ range: first.model.getFullModelRange(), text: first.model.getValue() + '// retained edit café 日本語\n' }]);
          editor.pushUndoStop();
          editor.setSelection(new monaco.Selection(2, 1, 2, 3));
          proof = { ids: entries.map(([key, item]) => [key, item.model.id]), first: id,
            changed: first.model.getValue(), history: false };
        }
        if (entries.length > 1) select(entries.find(([id]) => id !== proof.first)[0]);
        select(proof.first);
        if (phase === 3) {
          await current.model.undo();
          const restored = current.model.getValue(undefined, true) === current.baseline && current.model.canRedo();
          await current.model.redo();
          proof.history = restored && current.model.getValue() === proof.changed;
          editor.setSelection(new monaco.Selection(2, 1, 2, 3));
        }
        const selection = editor.getSelection();
        send({ kind: 'workspace_snapshot', phase, documents: entries.length,
          same_models: proof.ids.every(([id, model]) => documents.get(id)?.model.id === model),
          dirty_retained: current.model.getValue() === proof.changed && current.model.getValue(undefined, true) !== current.baseline,
          history_retained: proof.history,
          selection_retained: selection.startLineNumber === 2 && selection.startColumn === 1 && selection.endColumn === 3,
          theme: currentTheme, background: document.body.style.backgroundColor });
      };
      editor.updateOptions({ automaticLayout: false, minimap: { enabled: false }, fontSize: 14 });
      new ResizeObserver(() => editor.layout()).observe(document.getElementById('monaco-anchor'));
      window.__mcuOpenDocuments([{ id: 'untitled', name: 'Untitled.ino', content: fixture.source }]);
      send({ kind: 'workspace_ready' });
      return;
    }
    const model = monaco.editor.createModel(fixture.source, 'cpp', monaco.Uri.parse('inmemory://native-fixture/Fixture.ino'));
    editor.setModel(model);
    editor.updateOptions({ automaticLayout: false, minimap: { enabled: false }, fontSize: 14 });
    monaco.editor.setTheme('vs-dark');
    const resize = () => editor.layout();
    new ResizeObserver(resize).observe(document.getElementById('monaco-anchor'));
    resize();
    await wait('C++ grammar', () => monaco.editor.tokenize('void setup() { return; }', 'cpp').some(line => line.some(token => token.type.includes('keyword'))));
    const diffHost = document.createElement('div');
    diffHost.style.cssText = 'position:fixed;left:-10000px;top:0;width:400px;height:200px';
    document.body.append(diffHost);
    const original = monaco.editor.createModel('before\n', 'plaintext');
    const modified = monaco.editor.createModel('after\n', 'plaintext');
    const diff = monaco.editor.createDiffEditor(diffHost);
    try {
      diff.setModel({ original, modified });
      await wait('diff worker reply', () => workerMessages > 0 && diff.getLineChanges()?.length);
    } finally { diff.dispose(); original.dispose(); modified.dispose(); diffHost.remove(); }
    editor.pushUndoStop();
    editor.executeEdits('native-fixture', [{ range: model.getFullModelRange(), text: fixture.changed }]);
    editor.pushUndoStop();
    editor.setSelection(new monaco.Selection(2, 1, 2, 5));
    let historyRetained = false;
    window.__nativeSnapshot = async phase => {
      if (phase === 7) {
        await model.undo();
        const originalRestored = model.getValue() === fixture.source && model.canRedo();
        await model.redo();
        historyRetained = originalRestored && model.getValue() === fixture.changed;
        editor.setSelection(new monaco.Selection(2, 1, 2, 5));
      }
      editor.layout();
      const selection = editor.getSelection();
      send({ kind: 'editor_snapshot', phase, model_id: model.id, text: model.getValue(),
        dirty: model.getValue() !== fixture.source, same_model: editor.getModel() === model,
        can_undo: model.canUndo(), history_retained: historyRetained,
        selection: [selection.startLineNumber, selection.startColumn, selection.endLineNumber, selection.endColumn],
        worker_messages: Math.min(workerMessages, 65535), cpp_loaded: true });
    };
    await window.__nativeSnapshot(0);
  })().catch(fail); }, { once: true });
})();
