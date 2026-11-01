/* Exercise real cold-save gating without a renderer, filesystem writes or RPC. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../src/editor/index.html'), 'utf8');
const start = html.indexOf('      window.saveAllFilesSafe = async function () {');
const end = html.indexOf('      window.normalizeEditorLocation', start);
assert(start > 0 && end > start);

async function main() {
  let saved = 0, active = 0, release;
  const window = {editorInstance: {}, pywebview:{api:{}},
    saveAllFiles: async () => {saved++;}, saveActiveFile: async () => {active++;}};
  vm.runInNewContext(html.slice(start, end), {window, console});
  const initial = new Promise(resolve => {release = resolve;});
  window.__mcuInitialProjectLoad = initial;
  window.waitForProjectLoad = () => initial;
  const pending = window.saveAllFilesSafe();
  await Promise.resolve();
  assert.equal(saved, 0, 'A newly constructed editor is not a complete sketch');
  release(true);
  assert.equal(await pending, true);
  assert.equal(saved, 1);
  assert.equal(active, 1);

  window.waitForProjectLoad = () => Promise.resolve(false);
  await assert.rejects(window.saveAllFilesSafe(), /sketch is still unavailable/);
  assert.equal(saved, 1, 'A failed source/recovery load must never acknowledge Save');
  // A retry's current success must supersede the original failure, so a cold
  // renderer cannot leave Compile permanently disabled after recovering.
  window.__mcuInitialProjectLoad = Promise.resolve(false);
  window.waitForProjectLoad = () => Promise.resolve(true);
  assert.equal(await window.saveAllFilesSafe(), true);
  assert.equal(saved, 2);

  window.saveAllFiles = async () => {window._editorSaveFailures = 1;};
  await assert.rejects(window.saveAllFilesSafe(), /could not be saved/);
  window.saveAllFiles = async () => {saved++;};
  assert.equal(await window.saveAllFilesSafe(), true, 'Save failures must permit a later retry');
  console.log('Cold Save waits for complete models/recovery, refuses failed loads and permits retries.');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
