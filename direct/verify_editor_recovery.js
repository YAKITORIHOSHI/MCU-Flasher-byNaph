/* Run the real editor startup/recovery code with controllable native replies. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {execFileSync} = require('node:child_process');
const root = path.resolve(__dirname, '..');
const baseline = process.argv.includes('--baseline');
const read = file => baseline
  ? execFileSync('git', ['show', 'HEAD:' + file], {cwd: root, encoding: 'utf8'})
  : fs.readFileSync(path.join(root, file), 'utf8');
const html = read('src/editor/index.html');
const host = read('main/qt/editor_panel.py');
const loaderStart = html.indexOf('      let projectLoadInFlight = null;');
const loaderEnd = html.indexOf('      // The account dialog confirms', loaderStart);
assert(loaderStart > 0 && loaderEnd > loaderStart);
const loader = html.slice(loaderStart, loaderEnd);
const bootstrapStart = html.indexOf('      function initQtWebChannel()');
const bootstrapEnd = html.indexOf('      // Let the bundle create', bootstrapStart);
assert(bootstrapStart > 0 && bootstrapEnd > bootstrapStart);
const bootstrap = html.slice(bootstrapStart, bootstrapEnd) + '\nwindow.__initQtWebChannel = initQtWebChannel;';
const hostInit = host.match(/_WEBCHANNEL_INIT_JS = """([\s\S]*?)"""/)[1];

function bridge() {
  return {
    token: 'current', buffers: {}, recovering: false, acknowledgements: 0,
    updates: [],
    mark_renderer_modified(token, file, dirty, callback) {
      if (token === this.token) this.updates.push({file, dirty});
      callback();
    },
    snapshot_renderer_buffer(token, file, text, callback) {
      if (token === this.token) this.updates.push({file, text});
      callback();
    },
    update_renderer_buffer(token, file, text, callback) {
      if (token === this.token) this.updates.push({file, text});
      callback();
    },
    get_recovery_buffers(callback) { callback(this.buffers); },
    get_renderer_recovery(token, callback) {
      callback({accepted: token === this.token, buffers: token === this.token ? {...this.buffers} : {}});
    },
    complete_renderer_recovery(token, restored, callback) {
      const valid = token === this.token && Object.entries(this.buffers)
        .every(([file, text]) => restored[file] === text);
      if (valid) {
        this.acknowledgements++;
        this.buffers = {};
        this.recovering = false;
      }
      callback(valid);
    },
  };
}

function page(native, token, {delay, mapDelay, missing = false, mismatched = false,
    initialReadOnly = false, restoreThrows = false} = {}) {
  const models = new Map();
  const listeners = new Map();
  let active = null;
  let loads = 0;
  let pathsMapped = !mapDelay;
  let readOnly = initialReadOnly;
  const api = new Proxy(native, {get(target, name) {
    if (typeof target[name] !== 'function') return undefined;
    return (...args) => new Promise(resolve => target[name](...args, resolve));
  }});
  const window = {
    location: {search: '?renderer=' + token},
    projectPathKey: file => String(file || '').replaceAll('\\', '/'),
    editorInstance: {getModel: () => models.get(active?._filePath || active?.file),
      getRawOptions: () => ({readOnly}), updateOptions: options => {readOnly = options.readOnly;}},
    addEventListener(name, callback) { listeners.set(name, callback); },
    removeEventListener(name) { listeners.delete(name); },
    dispatchEvent(event) { listeners.get(event.type)?.(); },
    loadProject: async () => {
      loads++;
      if (delay) await delay;
      models.clear();
      for (const file of ['/fixture/sketch.ino', '/fixture/notes.h']) {
        let content = 'saved disk source';
        models.set(file, {getValue: () => content, isDisposed: () => false,
          setValue: value => {content = mismatched ? 'wrong value' : value;}});
      }
      active = {file: '/fixture/sketch.ino', _filePath: pathsMapped ? '/fixture/sketch.ino' : undefined};
    },
    async syncTabPaths() {
      if (mapDelay) await mapDelay;
      pathsMapped = true;
      if (active) active._filePath = active.file;
    },
    activateProjectFile(file) {
      if (missing || !pathsMapped || !models.has(file)) return false;
      if (restoreThrows && file === '/fixture/sketch.ino' && active.file !== file) {
        throw new Error('Original active model was disposed');
      }
      active = {file, _filePath: file};
      return true;
    },
  };
  const context = vm.createContext({window, URLSearchParams, console,
    document: {querySelector: () => active},
    requestAnimationFrame: callback => queueMicrotask(callback),
    Event: class {constructor(type) {this.type = type;}},
    qt: {webChannelTransport: {}},
    QWebChannel: function (transport, callback) {callback({objects: {editorBridge: native}});},
  });
  vm.runInContext(loader, context);
  return {window, context, api, models, loads: () => loads};
}

async function main() {
  const native = bridge();
  const initial = page(native, 'current');
  vm.runInContext(bootstrap, initial.context);
  initial.window.__initQtWebChannel();
  await (initial.window.__mcuInitialProjectLoad || initial.window.safeLoadProject());
  assert.equal(initial.loads(), 1);
  initial.models.get('/fixture/sketch.ino').setValue('unsaved current page');
  vm.runInContext(hostInit, initial.context);
  await Promise.resolve();
  assert.equal(initial.loads(), 1, 'Late Qt loadFinished must not reload the completed startup');
  assert.equal(initial.models.get('/fixture/sketch.ino').getValue(), 'unsaved current page');
  await initial.window.pywebview.api.update_editor_buffer('/fixture/sketch.ino', 'current edit');
  assert.deepEqual(native.updates, [{file: '/fixture/sketch.ino', text: 'current edit'}]);
  native.token = 'another page';
  await initial.window.pywebview.api.mark_modified('/fixture/sketch.ino', false);
  await initial.window.pywebview.api.snapshot_buffer('/fixture/sketch.ino', 'outgoing edit');
  assert.equal(native.updates.length, 1, 'Outgoing clean/snapshot callbacks must not mutate current buffers');
  native.token = 'current';

  const preChannel = page(native, 'current');
  assert.equal(await preChannel.window.safeLoadProject(), false);
  assert.equal(preChannel.loads(), 0, 'A pre-channel request must not create a no-op in-flight load');

  let release;
  const delayed = new Promise(resolve => {release = resolve;});
  native.token = 'outgoing';
  const outgoing = page(native, 'outgoing', {delay: delayed});
  outgoing.window.pywebview = {api: outgoing.api};
  const oldLoad = outgoing.window.safeLoadProject();
  native.token = 'replacement';
  outgoing.window.location.search = '?renderer=replacement';
  native.buffers = {'/fixture/sketch.ino': 'unsaved Unicode 😀', '/fixture/notes.h': ''};
  native.recovering = true;
  const before = native.acknowledgements;
  release();
  assert.equal(await oldLoad, false, 'An outgoing page must not consume replacement recovery');
  assert.equal(native.acknowledgements, before);
  assert.equal(native.recovering, true);

  const replacement = page(native, 'replacement');
  vm.runInContext(bootstrap, replacement.context);
  replacement.window.__initQtWebChannel();
  const recovering = replacement.window.__mcuInitialProjectLoad;
  assert.equal(replacement.window.safeLoadProject(), recovering, 'Overlapping project loads must coalesce');
  assert.equal(await recovering, true);
  assert.equal(replacement.loads(), 1);
  assert.equal(replacement.models.get('/fixture/sketch.ino').getValue(), 'unsaved Unicode 😀');
  assert.equal(replacement.models.get('/fixture/notes.h').getValue(), '');
  assert.equal(native.recovering, false);
  assert.equal(replacement.window.editorInstance.getRawOptions().readOnly, false);
  vm.runInContext(hostInit, replacement.context);
  await Promise.resolve();
  assert.equal(replacement.loads(), 1, 'Late host initialization must not reload an acknowledged recovery');
  assert.equal(replacement.models.get('/fixture/sketch.ino').getValue(), 'unsaved Unicode 😀');

  let releaseMapping;
  const mapDelay = new Promise(resolve => {releaseMapping = resolve;});
  native.buffers = {'/fixture/notes.h': 'last-created dirty header'};
  native.recovering = true;
  const unmapped = page(native, 'replacement', {mapDelay});
  unmapped.window.pywebview = {api: unmapped.api};
  const pendingMapping = unmapped.window.safeLoadProject();
  await Promise.resolve();
  assert.equal(native.recovering, true);
  releaseMapping();
  assert.equal(await pendingMapping, true, 'Recovery must await asynchronous tab path mapping');
  assert.equal(unmapped.models.get('/fixture/notes.h').getValue(), 'last-created dirty header');

  for (const options of [{initialReadOnly: true}, {restoreThrows: true}]) {
    native.buffers = {'/fixture/notes.h': 'preserved header'};
    native.recovering = true;
    const locked = page(native, 'replacement', options);
    locked.window.pywebview = {api: locked.api};
    if (options.restoreThrows) {
      await assert.rejects(locked.window.safeLoadProject(), /Original active model was disposed/);
    } else {
      assert.equal(await locked.window.safeLoadProject(), true);
    }
    assert.equal(locked.window.editorInstance.getRawOptions().readOnly, !!options.initialReadOnly,
      'Recovery must preserve readOnly even if restoring the active tab throws');
  }

  const legacyOnly = {...bridge(), get_renderer_recovery: undefined,
    complete_renderer_recovery: undefined,
    recovery_complete(callback) {throw new Error('Qt must never use an unversioned acknowledgement');}};
  const incompleteQt = page(legacyOnly, 'current');
  vm.runInContext(bootstrap, incompleteQt.context);
  incompleteQt.window.__initQtWebChannel();
  assert.equal(await incompleteQt.window.__mcuInitialProjectLoad, false);

  for (const failure of [{missing: true}, {mismatched: true}]) {
    native.buffers = {'/fixture/sketch.ino': 'keep recoverable'};
    native.recovering = true;
    const failed = page(native, 'replacement', failure);
    failed.window.pywebview = {api: failed.api};
    assert.equal(await failed.window.safeLoadProject(), false);
    assert.equal(native.recovering, true, 'Missing or mismatched models must retain recovery');
    assert.equal(native.buffers['/fixture/sketch.ino'], 'keep recoverable');
    assert.equal(failed.window.editorInstance.getRawOptions().readOnly, false);
  }
  console.log('Editor startup loads once; stale pages and incomplete restoration cannot acknowledge recovery.');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
