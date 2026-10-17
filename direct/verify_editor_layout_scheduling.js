/* Verify the real Monaco resize scheduler without loading WebEngine. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'src/editor/index.html'), 'utf8');
const start = html.indexOf("      const editorContainerElem = document.getElementById('editor-container');");
const end = html.indexOf('      window.setDetachedButtonStates', start);
assert(start >= 0 && end > start, 'Could not find the Monaco layout scheduler');
const source = html.slice(start, end);

const frames = [];
const observers = [];
const windowListeners = new Map();
const documentListeners = new Map();
const resolutionQueries = [];
const editorContainer = {clientWidth: 960, clientHeight: 540};
const actionBar = {classList: {toggle() {}}};
let layouts = 0;
const dimensions = [];

class FixtureResizeObserver {
  constructor(callback) { this.callback = callback; this.targets = []; observers.push(this); }
  observe(target) { this.targets.push(target); }
}

const window = {
  devicePixelRatio: 1,
  matchMedia(query) {
    const result = {query, listener: null,
      addEventListener(_, callback) { this.listener = callback; },
      removeEventListener(_, callback) { if (this.listener === callback) this.listener = null; }};
    resolutionQueries.push(result);
    return result;
  },
  ResizeObserver: FixtureResizeObserver,
  requestAnimationFrame(callback) { frames.push(callback); return frames.length; },
  addEventListener(name, callback) { windowListeners.set(name, callback); },
  editorInstance: {layout(size) { layouts += 1; dimensions.push({...size}); }},
};
const document = {
  hidden: false,
  getElementById(id) {
    return id === 'editor-container' ? editorContainer : actionBar;
  },
  addEventListener(name, callback) { documentListeners.set(name, callback); },
};

vm.runInNewContext(source, {window, document, ResizeObserver: FixtureResizeObserver});
assert.equal(observers.length, 1);
assert.deepEqual(observers[0].targets, [editorContainer], 'Only the outer container may trigger layout');

// A Qt resize, a ResizeObserver callback and an explicit layout request can
// arrive together. They must cost one Monaco canvas layout for that frame.
windowListeners.get('resize')();
observers[0].callback();
window.forceEditorLayout();
assert.equal(frames.length, 1, 'Layout requests were not coalesced');
frames.shift()();
assert.equal(layouts, 1);
assert.deepEqual(dimensions.at(-1), {width: 960, height: 540}, 'Layout must receive current CSS dimensions');

// Unchanged geometry from the observer does not repaint the canvas again.
observers[0].callback();
assert.equal(frames.length, 1);
frames.shift()();
assert.equal(layouts, 1);

// A deliberate reveal/detach action still forces exactly one repaint.
window.setDetachedActionBar(true);
assert.equal(frames.length, 1);
frames.shift()();
assert.equal(layouts, 2);

editorContainer.clientWidth = 0;
window.forceEditorLayout();
frames.shift()();
assert.equal(layouts, 2, 'Hidden editor must not lay out at zero size');
editorContainer.clientWidth = 960;
documentListeners.get('visibilitychange')();
frames.shift()();
assert.equal(layouts, 3);

// Retained CSS size does not imply retained physical canvas resolution. Cover
// lower-than-default and fractional display scales without converting twice.
for (const ratio of [0.75, 1.25, 1.75, 1]) {
  const previousQuery = resolutionQueries.at(-1);
  window.devicePixelRatio = ratio;
  previousQuery.listener();
  windowListeners.get('resize')();
  assert.equal(previousQuery.listener, null, 'Previous monitor listener must be released');
  assert.equal(resolutionQueries.at(-1).query, `(resolution: ${ratio}dppx)`);
  assert.equal(frames.length, 1);
  frames.shift()();
  assert.deepEqual(dimensions.at(-1), {width: 960, height: 540});
}
assert.equal(layouts, 7, 'Each scale transition must relayout once even at unchanged CSS size');
assert.equal(frames.length, 0, 'Scale handling must have no idle repaint loop');

console.log('Monaco layout coalesces geometry/DPR changes, keeps CSS dimensions and ignores hidden frames.');
