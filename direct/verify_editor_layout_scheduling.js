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
    // The anchor fills the well less a fixed inset, so the fixture's one size stands for both.
    return id === 'editor-container' || id === 'monaco-anchor' ? editorContainer : actionBar;
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

// Monaco's own size is what must match the anchor. Something else (a bare layout()
// while the anchor could not be measured) can leave Monaco at another size; the next
// geometry notification at the unchanged anchor size must still correct it.
let monacoSize = {width: 960, height: 540};
window.editorInstance = {
  layout(size) { layouts += 1; dimensions.push({...size}); monacoSize = {...size}; },
  getLayoutInfo() { return {...monacoSize}; },
};
observers[0].callback();
frames.shift()();
const settled = layouts;
assert.equal(settled, 7, 'An editor already at the anchor size needs no layout');
monacoSize = {width: 0, height: 0};
observers[0].callback();
assert.equal(frames.length, 1);
frames.shift()();
assert.equal(layouts, settled + 1, 'A Monaco size that drifted from its anchor must be corrected');
assert.deepEqual(dimensions.at(-1), {width: 960, height: 540});
observers[0].callback();
frames.shift()();
assert.equal(layouts, settled + 1, 'A fitted editor must not repaint again');
window.editorInstance.getLayoutInfo = () => { throw new Error('disposed'); };
observers[0].callback();
frames.shift()();
assert.equal(layouts, settled + 2, 'An unreadable layout is treated as unfitted, never as fitted');
assert.equal(frames.length, 0);

// The page's sheet takes its host widget's logical size, never the rounded-up viewport.
const backdropScript = html.slice(html.indexOf('      window.setGlassBackdrop = (backdrop) => {'),
                                  html.indexOf('      const backdrop = (parameters.get'));
const properties = new Map();
vm.runInNewContext(backdropScript, {window: {}, document: {documentElement: {style: {
  setProperty(name, value) { properties.set(name, value); }}}}});
assert.match(html, /#app-container \{[^}]*width: min\(100%, var\(--host-width, 100%\)\);[^}]*height: min\(100%, var\(--host-height, 100%\)\);/,
             'The editor sheet must be bounded by its host widget');
const pageWindow = {};
vm.runInNewContext(`${backdropScript}; target.set = window.setGlassBackdrop;`, {
  window: pageWindow, target: pageWindow,
  document: {documentElement: {style: {setProperty(name, value) { properties.set(name, value); }}}}});
pageWindow.set({width: 1600, height: 1000, x: -4, y: -90, radius: 16, hostWidth: 421, hostHeight: 250});
assert.equal(properties.get('--host-width'), '421px');
assert.equal(properties.get('--host-height'), '250px');
assert.equal(properties.get('--scene-radius'), '16px');
pageWindow.set({width: 1600, height: 1000, x: -4, y: -90, radius: 16, hostWidth: 0, hostHeight: NaN});
assert.equal(properties.get('--host-width'), '421px', 'A hidden or unmeasured host keeps the last real size');

console.log('Monaco layout coalesces geometry/DPR changes, keeps CSS dimensions, corrects drifted sizes, ignores hidden frames, and the sheet follows its host.');
