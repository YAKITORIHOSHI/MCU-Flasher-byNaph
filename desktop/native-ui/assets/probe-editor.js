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
      // Native Save owns request IDs and disk baselines. The page supplies only
      // current known models, with bounded frames and exact UTF-8 byte offsets.
      let pendingSave;
      let saveProof;
      const utf8 = new TextEncoder();
      window.__mcuRequestSave = (requestId, all) => { void (async () => {
        if (pendingSave) throw new Error('A save is already running.');
        const selected = all ? [...documents.entries()].filter(([, item]) => item.model.getValue(undefined, true) !== item.baseline)
          : [...documents.entries()].filter(([, item]) => item === current);
        const snapshots = new Map();
        let total = 0;
        for (const [id, item] of selected) {
          const baseline = item.model.getValue(undefined, true);
          // No-op saves retain original mixed EOL/BOM bytes exactly.
          const content = baseline === item.baseline ? item.original : baseline;
          for (let index = 0; index < content.length; index += 1) {
            const code = content.charCodeAt(index);
            if (code >= 0xd800 && code <= 0xdbff) {
              const next = content.charCodeAt(++index);
              if (!(next >= 0xdc00 && next <= 0xdfff)) throw new Error('Source contains an incomplete Unicode character. The buffer was not saved.');
            } else if (code >= 0xdc00 && code <= 0xdfff) throw new Error('Source contains an incomplete Unicode character. The buffer was not saved.');
          }
          const bytes = utf8.encode(content).length;
          total += bytes;
          if (bytes > 8 * 1024 * 1024 || total > 32 * 1024 * 1024) throw new Error('Save source byte budget exceeded.');
          item.revision += 1;
          snapshots.set(id, { item, content, baseline, bytes, revision: item.revision });
        }
        pendingSave = { id: requestId, snapshots };
        if (saveProof?.late) {
          saveProof.late = false;
          const item = documents.get(saveProof.first);
          select(saveProof.first);
          editor.executeEdits('native-save-late-edit', [{ range: item.model.getFullModelRange(), text: item.model.getValue() + '// later unsaved γ\n' }]);
          editor.setSelection(new monaco.Selection(2, 1, 2, 3));
        }
        // Keep headers below the same 16 KiB IPC limit as source chunks.
        const metadata = [...snapshots].map(([id, snapshot]) =>
          ({ document_id: id, edit_revision: snapshot.revision, bytes: snapshot.bytes }));
        for (let index = 0; index < Math.max(1, metadata.length); index += 32) {
          const header = { kind: 'save_started', request_id: requestId,
            documents: metadata.slice(index, index + 32), complete: index + 32 >= metadata.length };
          if (utf8.encode(JSON.stringify(header)).length > 16 * 1024) throw new Error('Save selection header exceeds its frame budget.');
          send(header);
        }
        let frames = 0;
        for (const [id, snapshot] of snapshots) {
          let offset = 0;
          for (let index = 0; index < snapshot.content.length;) {
            if (pendingSave?.id !== requestId) return;
            let end = Math.min(snapshot.content.length, index + 2048);
            const last = snapshot.content.charCodeAt(end - 1);
            if (end < snapshot.content.length && last >= 0xd800 && last <= 0xdbff) end -= 1;
            const content = snapshot.content.slice(index, end);
            send({ kind: 'save_chunk', request_id: requestId, document_id: id, offset, content });
            offset += utf8.encode(content).length;
            index = end;
            if (++frames % 8 === 0) await new Promise(resolve => setTimeout(resolve, 0));
          }
        }
        if (pendingSave?.id === requestId) send({ kind: 'save_finished', request_id: requestId });
      })().catch(error => { pendingSave = undefined;
        send({ kind: 'save_failed', request_id: requestId, message: String(error?.message ?? error).slice(0, 256) }); }); };
      window.__mcuFinishSave = result => {
        if (!pendingSave || result.requestId !== pendingSave.id) return false;
        for (const saved of result.saved ?? []) {
          const snapshot = pendingSave.snapshots.get(saved.id);
          if (!snapshot || snapshot.revision !== saved.editRevision || documents.get(saved.id) !== snapshot.item) continue;
          const item = snapshot.item;
          item.original = snapshot.content;
          item.baseline = snapshot.baseline;
          // Do not replace the model, reset undo, selection, view or a newer edit.
          item.revision += 1;
          send({ kind: 'document_changed', document_id: saved.id, edit_revision: item.revision,
            dirty: item.model.getValue(undefined, true) !== item.baseline });
        }
        pendingSave = undefined;
        return true;
      };
      // Called only by the native owned-fixture Save controller.
      window.__mcuSaveFixtureStep = async phase => {
        const entries = [...documents.entries()];
        if (entries.length !== 2) throw new Error('Save fixture requires its two owned source models.');
        if (phase === 0) {
          const [id] = entries.find(([, item]) => item.original.startsWith('\ufeff')) ?? entries[0];
          saveProof = { first: id, second: entries.find(([key]) => key !== id)[0],
            models: entries.map(([key, item]) => [key, item.model.id]), history: false, late: false };
          select(id);
          editor.pushUndoStop();
          editor.executeEdits('native-save-proof', [{ range: current.model.getFullModelRange(), text: current.model.getValue() + '// native saved café 日本語\n' }]);
          editor.pushUndoStop();
        }
        if (phase === 1) {
          const changed = current.model.getValue();
          await current.model.undo();
          const restored = current.model.canRedo() && current.model.getValue() !== changed;
          await current.model.redo();
          saveProof.history = restored && current.model.getValue() === changed;
        }
        if (phase === 2) {
          for (const id of [saveProof.first, saveProof.second]) {
            select(id);
            editor.pushUndoStop();
            editor.executeEdits('native-save-all-proof', [{ range: current.model.getFullModelRange(), text: current.model.getValue() + '// saved all αβ\n' }]);
            editor.pushUndoStop();
          }
          saveProof.late = true;
        }
        select(saveProof.first);
        editor.setSelection(new monaco.Selection(2, 1, 2, 3));
        const selection = editor.getSelection();
        const dirty = id => { const item = documents.get(id); return item.model.getValue(undefined, true) !== item.baseline; };
        send({ kind: 'save_snapshot', phase,
          same_models: saveProof.models.every(([id, model]) => documents.get(id)?.model.id === model),
          history_retained: saveProof.history,
          selection_retained: selection.startLineNumber === 2 && selection.startColumn === 1 && selection.endColumn === 3,
          first_dirty: dirty(saveProof.first), second_dirty: dirty(saveProof.second) });
      };
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
