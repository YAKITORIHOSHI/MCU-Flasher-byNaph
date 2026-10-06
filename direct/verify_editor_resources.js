/* Execute the real editor scheduling code against isolated Monaco/bridge fixtures.
 * No browser, files, preferences, hardware or network are mutated.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {execFileSync} = require('node:child_process');
const root = path.resolve(__dirname, '..');
const baseline = process.argv.includes('--baseline');
const html = baseline
  ? execFileSync('git', ['show', 'HEAD:src/editor/index.html'], {cwd: root, encoding: 'utf8'})
  : fs.readFileSync(path.join(root, 'src/editor/index.html'), 'utf8');
for (const script of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)) {
  new vm.Script(script[1]);
}
const start = html.indexOf('      let lastChecked');
const end = html.indexOf('    })();', start);
assert(start > 0 && end > start);
const source = '(function () {\n' + html.slice(start, end) + '\n})();';

async function fixture({modern = true, constrained = true, initiallyEmpty = false} = {}) {
  let now = 0, nextTimer = 0, version = 1, content = 'int value;\n'.repeat(100000);
  let activePath = '/fixture/sample.ino';
  let contentChanged, modelChanged;
  const timers = new Map(), snapshots = new Map();
  const calls = {updates: 0, legacy: 0, bufferChecks: 0, fullChecks: 0, sourceChars: 0, reads: 0};
  const model = {
    getValue: () => { calls.reads++; return content; },
    getVersionId: () => version, getLanguageId: () => 'cpp', isDisposed: () => false,
  };
  let currentModel = initiallyEmpty ? null : model;
  const api = {
    mark_modified: () => { calls.legacy++; },
    snapshot_buffer: (file, text) => {
      calls.legacy++; calls.sourceChars += text.length; snapshots.set(file, text);
    },
    on_editor_content_change: () => { calls.legacy++; },
    realtime_check_syntax: async (file, text) => {
      calls.fullChecks++; calls.sourceChars += text.length; return 'null';
    },
  };
  if (modern) Object.assign(api, {
    update_editor_buffer: (file, text) => {
      calls.updates++; calls.sourceChars += text.length; snapshots.set(file, text);
    },
    realtime_check_buffer: async file => { calls.bufferChecks++; return snapshots.has(file); },
  });
  const window = {
    __mcuResourceConstrained: constrained, pywebview: {api}, setEditorMarkers: () => {},
    editorInstance: {
      getModel: () => currentModel, getValue: model.getValue,
      onDidChangeModelContent: fn => { contentChanged = fn; },
      onDidChangeModel: fn => { modelChanged = fn; },
    },
  };
  const context = {
    window, console, monaco: {editor: {setModelLanguage() {}}},
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
  if (initiallyEmpty) {
    assert.equal(calls.fullChecks, 0);
    assert.equal(timers.size, 0, 'An empty project must not keep polling for its first model');
    currentModel = model;
    modelChanged();
    await advance(650);
  }
  assert.equal(calls.fullChecks, 1, 'Clean files must be checked without a recovery snapshot');
  const initialChars = calls.sourceChars;
  for (let i = 0; i < 10; i++) {
    content += '\n// edit ' + i;
    version++;
    contentChanged();
    assert.equal(snapshots.get(activePath), content, 'Every edit must retain a current recovery snapshot');
    await advance(400);
  }
  await advance(650);
  const report = {...calls, editSourceChars: calls.sourceChars - initialChars, idleTimers: timers.size};
  if (!baseline) {
    if (modern) {
      assert.equal(calls.updates, 10);
      assert.equal(calls.legacy, 0);
      assert.equal(calls.fullChecks, 1, 'Dirty buffers must not be resent for syntax checking');
      assert.equal(calls.bufferChecks, constrained ? 2 : 11);
    } else {
      assert.equal(calls.legacy, 30, 'Legacy bridge remains functional');
      assert.equal(calls.fullChecks, constrained ? 2 : 11);
    }
    assert.equal(timers.size, 0, 'Editor initialization must stop polling once listeners are installed');
    const reads = calls.reads;
    contentChanged(); // same model version: no duplicate syntax request
    await advance(650);
    assert.equal(calls.reads, reads + 1, 'Only the recovery snapshot needs the full source');
    activePath = '/fixture/other.hpp';
    modelChanged();
    await advance(650);
    assert.equal(calls.fullChecks, (modern ? 1 : constrained ? 2 : 11) + 1,
                 'New tabs and header files need an initial source check');
  }
  return report;
}

(async () => {
  const report = await fixture();
  if (!baseline) {
    await fixture({modern: false});
    await fixture({constrained: false});
    await fixture({initiallyEmpty: true});
  }
  console.log(JSON.stringify({baseline, ...report}));
})().catch(error => { console.error(error); process.exitCode = 1; });
