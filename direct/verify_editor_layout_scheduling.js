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
const editorContainer = {clientWidth: 960, clientHeight: 540};
const actionBar = {classList: {toggle() {}}};
let layouts = 0;

class FixtureResizeObserver {
  constructor(callback) { this.callback = callback; this.targets = []; observers.push(this); }
  observe(target) { this.targets.push(target); }
}

const window = {
  ResizeObserver: FixtureResizeObserver,
  requestAnimationFrame(callback) { frames.push(callback); return frames.length; },
  addEventListener(name, callback) { windowListeners.set(name, callback); },
  editorInstance: {layout() { layouts += 1; }},
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

console.log('Monaco layout scheduling coalesces resize bursts and ignores hidden zero-size frames.');
