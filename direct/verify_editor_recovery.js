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
const cloudReloadEnd = html.indexOf('\n      function dedupeProjectTabs()', loaderEnd);
assert(cloudReloadEnd > loaderEnd);
const cloudReload = html.slice(loaderEnd, cloudReloadEnd);
const mapperStart = html.indexOf('      let fileList = [];');
const mapperEnd = html.indexOf('      let dragSrcEl = null;', mapperStart);
assert(mapperStart > 0 && mapperEnd > mapperStart);
const tabMapper = html.slice(mapperStart, mapperEnd);
const bootstrapStart = html.indexOf('      function initQtWebChannel()');
const bootstrapEnd = html.indexOf('      // Let the bundle create', bootstrapStart);
assert(bootstrapStart > 0 && bootstrapEnd > bootstrapStart);
const bootstrap = html.slice(bootstrapStart, bootstrapEnd) + '\nwindow.__initQtWebChannel = initQtWebChannel;';
const hostInit = host.match(/_WEBCHANNEL_INIT_JS = """([\s\S]*?)"""/)[1];

function bridge() {
  return {
    token: 'current', buffers: {}, recovering: false, acknowledgements: 0,
    ordinaryFileLists: 0,
    files: ['/fixture/sketch.ino', '/fixture/notes.h'].map(file => ({path: file, name: path.basename(file)})),
    updates: [],
    get_project_files(callback) { this.ordinaryFileLists++; callback(this.files); },
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
    initialReadOnly = false, restoreThrows = false, loadedPaths,
    initialReadFailure, unmappedPaths = [], disposedPaths = [],
    reloadReadFailure} = {}) {
  const models = new Map();
  const listeners = new Map();
  let active = null;
  let loads = 0;
  let reloads = 0;
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
      const files = await window.pywebview.api.get_project_files();
      if (delay) await delay;
      models.clear();
      for (const file of loadedPaths || files.map(file => file.path)) {
        // The real bundle catches an initial read rejection and resolves after
        // leaving only the models created before that failed read.
        if (file === initialReadFailure) {
          window._editorReadFailures = (window._editorReadFailures || 0) + 1;
          return;
        }
        let content = 'saved disk source';
        models.set(file, {getValue: () => content, isDisposed: () => disposedPaths.includes(file),
          setValue: value => {content = mismatched ? 'wrong value' : value;}});
      }
      const file = models.keys().next().value;
      active = file ? {file, _filePath: pathsMapped ? file : undefined} : null;
      window.__tabMutation?.();
    },
    async syncTabPaths() {
      await window.pywebview.api.get_project_files();
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
    async reloadActiveFile() {
      reloads++;
      const model = models.get(active?._filePath);
      if (!model || model.isDisposed()) return false;
      if (active._filePath === reloadReadFailure) {
        window._editorReadFailures = (window._editorReadFailures || 0) + 1;
        return false;
      }
      model.setValue('saved disk source');
      return true;
    },
  };
  const context = vm.createContext({window, URLSearchParams, console,
    document: {querySelector: () => active,
      querySelectorAll: () => Array.from(models.keys(), file => ({
        _filePath: pathsMapped && !unmappedPaths.includes(file) ? file : undefined,
        querySelector: () => ({textContent: path.basename(file)}), setAttribute() {}}))},
    requestAnimationFrame: callback => queueMicrotask(callback),
    Event: class {constructor(type) {this.type = type;}},
    qt: {webChannelTransport: {}},
    QWebChannel: function (transport, callback) {callback({objects: {editorBridge: native}});},
  });
  vm.runInContext(loader, context);
  vm.runInContext(cloudReload, context);
  return {window, context, api, models, loads: () => loads, reloads: () => reloads,
    reloadCloud: () => window.reloadCloudSnapshot({success: true, files: native.files})};
}

async function main() {
  for (const manifest of [undefined, {success: false, files: []},
    {success: false, files: [{path: '/fixture/sketch.ino'}]},
    {success: true, files: null}]) {
    const rejected = page(bridge(), 'current');
    rejected.window.pywebview = {api: rejected.api};
    assert.equal(await rejected.window.reloadCloudSnapshot(manifest), false,
      'A missing or failed strict enumeration must not acknowledge a Pull');
    assert.equal(rejected.loads(), 0, 'Do not replace buffers after a rejected source enumeration');
  }
  for (const options of [
    {initialReadFailure: '/fixture/notes.h'},
    {loadedPaths: ['/fixture/sketch.ino']},
    {loadedPaths: []},
    {loadedPaths: ['/fixture/sketch.ino', '/fixture/notes.h', '/fixture/deleted.h']},
    {unmappedPaths: ['/fixture/notes.h']},
    {disposedPaths: ['/fixture/notes.h']},
    {reloadReadFailure: '/fixture/notes.h'},
  ]) {
    const failed = page(bridge(), 'current', options);
    failed.window.pywebview = {api: failed.api};
    assert.equal(await failed.reloadCloud(), false,
      'A failed initial read, incomplete source set or failed live-model reload must not acknowledge a Pull: ' +
      JSON.stringify(options));
    assert.equal(failed.window.pywebview.api, failed.api,
      'A failed Cloud reload must restore the ordinary API');
  }
  const emptyNative = bridge();
  emptyNative.files = [];
  const empty = page(emptyNative, 'current');
  empty.window.pywebview = {api: empty.api};
  assert.equal(await empty.reloadCloud(), true,
    'A genuinely empty source list must complete a confirmed Pull');
  assert.equal(empty.reloads(), 0);
  assert.equal(emptyNative.ordinaryFileLists, 0,
    'An empty Cloud reload must reuse its worker-certified list without scanning on Qt');
  assert.equal(empty.window.pywebview.api, empty.api);

  const mixedNative = bridge();
  mixedNative.files = ['sketch.ino', 'logic.cpp', 'driver.c', 'notes.h', 'Notes.h',
    'config.hpp', 'NOTE.txt'].map(name => ({path: '/fixture/' + name, name}));
  const mixed = page(mixedNative, 'current');
  mixed.window.pywebview = {api: mixed.api};
  assert.equal(await mixed.reloadCloud(), true,
    'Pull coverage must include every editable backend source/note and preserve Linux path case');
  assert.equal(mixed.reloads(), mixedNative.files.length);
  assert.equal(mixedNative.ordinaryFileLists, 0,
    'The bundled loader and tab mapping must both reuse the Cloud manifest');
  assert.equal(mixed.window.pywebview.api, mixed.api);
  await mixed.window.pywebview.api.get_project_files();
  assert.equal(mixedNative.ordinaryFileLists, 1,
    'Ordinary editor listing must resume after the Cloud reload');

  // Execute the actual tab mapper and observer scheduling as well as the
  // loader fixture. A timer left after the scoped API is restored would
  // call the native getter and repeat the directory walk on Qt.
  const mappedNative = bridge();
  mappedNative.get_project_files = () => {throw new Error('Qt source scan must not run during Cloud reload');};
  const mapped = page(mappedNative, 'current');
  mapped.window.pywebview = {api: mapped.api};
  let nextTimer = 0;
  const mappingTimers = new Map();
  mapped.context.setTimeout = callback => {
    const timer = ++nextTimer;
    mappingTimers.set(timer, callback);
    return timer;
  };
  mapped.context.clearTimeout = timer => mappingTimers.delete(timer);
  mapped.context.document.getElementById = () => ({});
  mapped.context.dedupeProjectTabs = () => {};
  mapped.context.MutationObserver = function (callback) {
    mapped.window.__tabMutation = callback;
    this.observe = () => {};
  };
  vm.runInContext(tabMapper, mapped.context);
  // Settle the mapper's startup callback while the fixture API is usable.
  mappedNative.get_project_files = bridge().get_project_files;
  for (const [timer, callback] of [...mappingTimers]) {
    mappingTimers.delete(timer);
    await callback();
  }
  const ordinaryBeforeCloud = mappedNative.ordinaryFileLists;
  mappedNative.get_project_files = () => {throw new Error('Qt source scan must not run during Cloud reload');};
  assert.equal(await mapped.reloadCloud(), true,
    'The actual tab mapper and loader must use the certified Cloud manifest');
  assert.equal(mappedNative.ordinaryFileLists, ordinaryBeforeCloud);
  assert.equal(mappingTimers.size, 0,
    'Cloud tab mapping must cancel its queued observer refresh before restoring the API');
  assert.equal(mapped.window.pywebview.api, mapped.api);

  let releaseCloud;
  const cloudDelay = new Promise(resolve => {releaseCloud = resolve;});
  const slowCloud = page(bridge(), 'current', {delay: cloudDelay});
  slowCloud.window.pywebview = {api: slowCloud.api};
  const firstCloud = slowCloud.reloadCloud();
  await Promise.resolve();
  assert.equal(await slowCloud.reloadCloud(), false,
    'A retry after the native timeout must not overlap a still-running renderer reload');
  assert.equal(slowCloud.loads(), 1);
  releaseCloud();
  assert.equal(await firstCloud, true);
  assert.equal(slowCloud.window.pywebview.api, slowCloud.api,
    'The original API must return after a delayed Cloud reload and rejected overlapping retry');

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
  assert.equal(await preChannel.reloadCloud(), false,
    'An unavailable channel must not acknowledge a Pull with zero source tabs');
  assert.equal(preChannel.reloads(), 0);

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
  assert.equal(await outgoing.reloadCloud(), false,
    'An outgoing renderer must not acknowledge a Pull after its project load was rejected');
  assert.equal(outgoing.reloads(), 0);
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
  assert.equal(await replacement.reloadCloud(), true,
    'A confirmed Pull with a current renderer must reload every source model');
  assert.equal(replacement.reloads(), 2);
  assert.equal(replacement.models.get('/fixture/sketch.ino').getValue(), 'saved disk source');
  assert.equal(replacement.models.get('/fixture/notes.h').getValue(), 'saved disk source');

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
  assert.equal(await incompleteQt.reloadCloud(), false,
    'Incomplete Qt recovery methods must not acknowledge a Pull');
  assert.equal(incompleteQt.reloads(), 0);

  const rejectsCompletion = {...bridge(), complete_renderer_recovery(token, restored, callback) {
    callback(false);
  }};
  const rejectedCompletion = page(rejectsCompletion, 'current');
  rejectedCompletion.window.pywebview = {api: rejectedCompletion.api};
  assert.equal(await rejectedCompletion.reloadCloud(), false,
    'A failed recovery acknowledgement must retain the Pull reload lock');
  assert.equal(rejectedCompletion.reloads(), 0);

  for (const failure of [{missing: true}, {mismatched: true}]) {
    native.buffers = {'/fixture/sketch.ino': 'keep recoverable'};
    native.recovering = true;
    const failed = page(native, 'replacement', failure);
    failed.window.pywebview = {api: failed.api};
    assert.equal(await failed.window.safeLoadProject(), false);
    assert.equal(native.recovering, true, 'Missing or mismatched models must retain recovery');
    assert.equal(native.buffers['/fixture/sketch.ino'], 'keep recoverable');
    assert.equal(failed.window.editorInstance.getRawOptions().readOnly, false);
    assert.equal(await failed.reloadCloud(), false,
      'A missing or mismatched recovered model must not acknowledge a Pull');
    assert.equal(failed.reloads(), 0);
    assert.equal(native.recovering, true);
    assert.equal(native.buffers['/fixture/sketch.ino'], 'keep recoverable');
  }
  console.log('Editor startup loads once; stale pages and incomplete restoration cannot acknowledge recovery.');
  console.log('Cloud Pull requires complete mapped live source models and successful reads; empty projects remain valid.');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
