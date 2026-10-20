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
