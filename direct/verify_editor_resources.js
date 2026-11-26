/* Execute the real editor listener code against isolated Monaco/bridge fixtures.
 * No browser, files, preferences, hardware or network are mutated.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'src/editor/index.html'), 'utf8');
for (const script of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)) {
  new vm.Script(script[1]);
}
// Compiling is the only code check: the page asks the app for none while typing.
assert(!/realtime_check|setEditorMarkers|refreshProjectSyntax|SyntaxCheck/.test(html));
const start = html.indexOf('      function syncModelLanguageForActiveTab() {');
const end = html.indexOf('    })();', start);
assert(start > 0 && end > start);
const source = '(function () {\n' + html.slice(start, end) + '\n})();';

async function fixture({modern = true, initiallyEmpty = false} = {}) {
  let now = 0, nextTimer = 0, content = 'int value;\n'.repeat(100000);
  let activePath = '/fixture/sample.ino', language = 'cpp';
  let contentChanged, modelChanged;
  const timers = new Map(), snapshots = new Map();
  const calls = {updates: 0, legacy: 0, reads: 0, languages: []};
  const model = {getValue: () => { calls.reads++; return content; }, getLanguageId: () => language};
  let currentModel = initiallyEmpty ? null : model;
  const api = {
    mark_modified: () => { calls.legacy++; },
    snapshot_buffer: (file, text) => { calls.legacy++; snapshots.set(file, text); },
    on_editor_content_change: () => { calls.legacy++; },
  };
  if (modern) api.update_editor_buffer = (file, text) => { calls.updates++; snapshots.set(file, text); };
  const window = {
    pywebview: {api},
    editorInstance: {
      getModel: () => currentModel, getValue: model.getValue,
      onDidChangeModelContent: fn => { contentChanged = fn; },
      onDidChangeModel: fn => { modelChanged = fn; },
    },
  };
  const context = {
    window, console,
    monaco: {editor: {setModelLanguage: (_model, id) => { language = id; calls.languages.push(id); }}},
    document: {
      querySelector: () => ({_filePath: activePath}),
      getElementById: () => ({addEventListener() {}}),
    },
    setTimeout: (callback, delay) => {
      const id = ++nextTimer; timers.set(id, {callback, at: now + delay}); return id;
    },
    clearTimeout: id => timers.delete(id),
  };
  async function advance(ms) {
    const target = now + ms;
    while (true) {
      const next = [...timers].filter(([, t]) => t.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      now = next[1].at;
      timers.delete(next[0]);
      next[1].callback();
      await Promise.resolve(); await Promise.resolve();
    }
    now = target;
  }
  vm.runInNewContext(source, context);
  await advance(1700);
  assert.equal(timers.size, 0, 'Editor initialization must stop polling once listeners are installed');
  if (initiallyEmpty) {
    currentModel = model;
    modelChanged();
  }
  for (let i = 0; i < 10; i++) {
    content += '\n// edit ' + i;
    contentChanged();
    assert.equal(snapshots.get(activePath), content, 'Every edit must retain a current recovery snapshot');
    await advance(400);
  }
  await advance(650);
  if (modern) {
    assert.equal(calls.updates, 10, 'One bridge message per edit');
    assert.equal(calls.legacy, 0);
  } else {
    assert.equal(calls.legacy, 30, 'Legacy bridge remains functional');
  }
  assert.equal(calls.reads, 10, 'Only the recovery snapshot reads the full source');
  assert.equal(timers.size, 0, 'Typing leaves no timer running');
  activePath = '/fixture/driver.c';
  modelChanged();
  assert.equal(language, 'c', 'A .c tab uses the C language');
  activePath = '/fixture/notes.txt';
  modelChanged();
  assert.equal(language, 'plaintext');
  return {...calls, languages: calls.languages.join(',')};
}

(async () => {
  const report = await fixture();
  await fixture({modern: false});
  await fixture({initiallyEmpty: true});
  console.log(JSON.stringify(report));
})().catch(error => { console.error(error); process.exitCode = 1; });
