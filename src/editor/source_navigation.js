/* One definition protocol and locked preview tabs, independent of source saves. */
(() => {
  const renderer = new URLSearchParams(location.search).get('renderer') || '';
  const replies = new Map();
  let bridge = null, sequence = 0, latestRequest = 0, preview = null, choices = null;
  let history = [], historyIndex = -1, replaying = false;
  const key = path => window.projectPathKey?.(path) || path;
  const store = () => window.__mcuProjectFileStore;
  const editor = () => window.editorInstance;
  const record = path => store()?.list().find(file => key(file.path) === key(path));
  function connect() {
    const current = window.editorBridge;
    if (!current?.request_definition || !current.navigation_ready?.connect) return false;
    if (bridge !== current) {
      bridge = current;
      bridge.navigation_ready.connect((token, request, result) => {
        if (token === renderer) replies.get(request)?.(result);
      });
    }
    return true;
  }
  window.requestSourceDefinition = async (model, position) => {
    if (!model || !position || !connect()) return null;
    const current = store()?.list().find(file => file.model === model);
    if (!current || model.getValueLength() > 4 * 1024 * 1024) return null;
    const serial = ++latestRequest;
    const revision = model.getVersionId(), generation = window.__mcuProjectFilesGeneration || 0;
    const snapshots = store().list().filter(file => !file.model.isDisposed() && file.model.getValueLength() <= 4 * 1024 * 1024)
      .map(file => [file.path, file.model.getValue()]);
    const result = await new Promise(resolve => {
      const request = renderer + ':nav:' + (++sequence);
      const finish = value => { if (!replies.delete(request)) return; clearTimeout(timer); resolve(value); };
      const timer = setTimeout(() => finish({success:false, error:'Source navigation timed out.'}), 12000);
      replies.set(request, finish);
      bridge.request_definition(renderer, request,
        JSON.stringify({path:current.path, line:position.lineNumber, column:position.column, snapshots}),
        accepted => { if (!accepted) finish({success:false, cancelled:true}); });
    });
    if (serial !== latestRequest || model.isDisposed() || revision !== model.getVersionId() || generation !== (window.__mcuProjectFilesGeneration || 0)) {
      return {success:false, cancelled:true};
    }
    return result;
  };
  function remember() {
    if (replaying) return;
    const active = store()?.active();
    if (!active || editor().getModel() !== active.model) return;
    const entry = {path:active.path, state:editor().saveViewState()};
    if (historyIndex >= 0 && key(history[historyIndex].path) === key(entry.path)) history[historyIndex] = entry;
    else { history = history.slice(0, historyIndex + 1); history.push(entry); }
    if (history.length > 64) history.shift();
    historyIndex = history.length - 1;
  }
  function closePreview(file = preview) {
    if (!file || !store()) return;
    const wasActive = store().active() === file;
    document.getElementById(store().tabId(file.path))?.remove();
    store().remove(file);
    history = history.filter(entry => key(entry.path) !== key(file.path));
    historyIndex = Math.min(historyIndex, history.length - 1);
    if (wasActive) {
      const replacement = record(history[historyIndex]?.path) || store().list().find(item => !item.preview);
      if (replacement) {
        store().activate(replacement);
        const previous = history[historyIndex];
        if (previous && key(previous.path) === key(replacement.path)) editor().restoreViewState(previous.state);
      } else editor().setModel(null);
    }
    if (!file.model.isDisposed()) file.model.dispose();
    if (preview === file) preview = null;
  }
  window.closeAllSourcePreviews = () => {
    ++latestRequest;
    closePreview();
    history = []; historyIndex = -1;
    choices?.remove(); choices = null;
  };
  function materialize(destination) {
    const existing = record(destination.path);
    if (existing) return existing;
    closePreview();
    const name = destination.path.replaceAll('\\', '/').split('/').pop();
    const file = {name, path:destination.path, preview:true, readOnly:true, isDirty:false,
      model:monaco.editor.createModel(destination.content, name.endsWith('.c') ? 'c' : 'cpp')};
    store().add(file);
    const tab = document.getElementById(store().tabId(file.path));
    tab._filePath = file.path;
    tab.dataset.filePath = file.path;
    tab.dataset.preview = 'true';
    tab.draggable = false;
    tab.addEventListener('contextmenu', event => { event.preventDefault(); event.stopImmediatePropagation(); }, true);
    tab.setAttribute('aria-label', name + ', read-only library preview');
    const lock = document.createElement('span');
    lock.textContent = 'Read only'; lock.style.fontSize = '10px';
    lock.style.marginLeft = '6px';
    const close = document.createElement('button');
    close.type = 'button'; close.textContent = 'Close';
    close.setAttribute('aria-label', 'Close ' + name + ' preview');
    Object.assign(close.style, {marginLeft:'6px', padding:'2px 5px', cursor:'pointer',
      background:'var(--chip)', color:'var(--chip-ink)', border:'1px solid var(--tab-rim)', borderRadius:'4px', font:'inherit'});
    close.onclick = event => { event.stopPropagation(); closePreview(file); };
    tab.append(lock, close);
    preview = file;
    return file;
  }
  function range(destination, model) {
    const column = (line, index) => Array.from(model.getLineContent(line)).slice(0, index).join('').length + 1;
    const line = destination.line + 1, endLine = (destination.end_line >= 0 ? destination.end_line : destination.line) + 1;
    return new monaco.Range(line, column(line, destination.column), endLine,
      column(endLine, destination.end_column >= 0 ? destination.end_column : destination.column + destination.name.length));
  }
  function jump(destination) {
    remember();
    const file = materialize(destination);
    store().activate(file);
    editor().setSelection(range(destination, file.model));
    editor().revealRangeInCenter(range(destination, file.model));
    editor().focus();
    // Retain an explicit second position even when origin and target share a
    // file. Back then restores the call site's selection and scroll position.
    history = history.slice(0, historyIndex + 1);
    history.push({path:file.path, state:editor().saveViewState()});
    if (history.length > 64) history.shift();
    historyIndex = history.length - 1;
    return true;
  }
  function choose(destinations) {
    const model = editor().getModel(), revision = model.getVersionId();
    const generation = window.__mcuProjectFilesGeneration || 0;
    choices?.remove();
    const menu = document.createElement('div'); choices = menu;
    menu.setAttribute('role', 'dialog'); menu.setAttribute('aria-label', 'Choose a declaration');
    Object.assign(menu.style, {position:'absolute', left:'12px', right:'12px', top:'48px',
      zIndex:'10001', maxHeight:'60%', overflow:'auto', padding:'10px', borderRadius:'10px',
      border:'1px solid var(--hover-border)', background:'var(--reading)', backdropFilter:'blur(18px)', color:'var(--text)'});
    const heading = document.createElement('div'); heading.textContent = 'Choose a declaration';
    const close = document.createElement('button'); close.type='button'; close.textContent='Close';
    close.onclick = () => { menu.remove(); editor().focus(); };
    heading.append(close); menu.append(heading);
    for (const destination of destinations) {
      const option = document.createElement('button'); option.type='button';
      option.textContent = `${destination.signature || destination.name} — ${destination.path}:${destination.line + 1}`;
      Object.assign(option.style, {display:'block', width:'100%', textAlign:'left', padding:'8px',
        marginTop:'4px', whiteSpace:'normal', overflowWrap:'anywhere', cursor:'pointer',
        color:'var(--text)', background:'var(--tab-fill)', border:'1px solid var(--tab-rim)', borderRadius:'6px'});
      option.onclick = () => {
        menu.remove();
        if (model.isDisposed() || editor().getModel() !== model || model.getVersionId() !== revision ||
            generation !== (window.__mcuProjectFilesGeneration || 0)) return;
        jump(destination);
      };
      menu.append(option);
    }
    menu.onkeydown = event => {
      if (event.key === 'Escape') { event.stopPropagation(); menu.remove(); editor().focus(); }
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const buttons = Array.from(menu.querySelectorAll('button'));
        const index = buttons.indexOf(document.activeElement);
        buttons[(index + (event.key === 'ArrowDown' ? 1 : buttons.length - 1)) % buttons.length].focus();
      }
    };
    (document.getElementById('editor-container') || document.body).append(menu);
    menu.querySelectorAll('button')[1]?.focus();
  }
  window.navigateSourceAtPosition = async (position, explicitWord) => {
    const model = editor()?.getModel(), pos = position || editor()?.getPosition();
    if (explicitWord && model?.getWordAtPosition(pos)?.word !== explicitWord) return false;
    const result = await window.requestSourceDefinition(model, pos);
    if (!result || result.cancelled) return false;
    if (!result.success || !result.destinations?.length) {
      window.showEditorNotice?.(result.error || 'No declaration could be resolved. Compile once to record the selected target’s include paths and defines, then try again.');
      return false;
    }
    if (editor().getModel() !== model) return false;
    if (result.destinations.length === 1 && !result.requires_choice) return jump(result.destinations[0]);
    choose(result.destinations);
    return true;
  };
  window.sourceDefinitionLocations = async (model, position, token) => {
    const result = await window.requestSourceDefinition(model, position);
    if (!result?.success || result.cancelled || token?.isCancellationRequested || editor().getModel() !== model) return null;
    if (result.destinations.length > 1 || result.requires_choice) { choose(result.destinations); return []; }
    return result.destinations.map(destination => {
      const file = materialize(destination);
      return {uri:file.model.uri, range:range(destination, file.model)};
    });
  };
  function install() {
    const ed = editor();
    if (!ed) return;
    let requestedReadOnly = !!ed.getRawOptions().readOnly;
    const updateOptions = ed.updateOptions.bind(ed);
    const previewActive = () => !!store()?.list().find(item => item.model === ed.getModel())?.readOnly;
    window.getRequestedEditorReadOnly = () => requestedReadOnly;
    ed.updateOptions = options => {
      if (options && Object.hasOwn(options, 'readOnly')) requestedReadOnly = !!options.readOnly;
      return updateOptions({...options, readOnly:requestedReadOnly || previewActive()});
    };
    ed.onDidChangeModel(() => {
      const file = store()?.list().find(item => item.model === ed.getModel());
      if (file && store().active() !== file) store().activate(file);
      updateOptions({readOnly:requestedReadOnly || !!file?.readOnly});
    });
    monaco.editor.registerEditorOpener({openCodeEditor: (_editor, uri, selection) => {
      const file = store()?.list().find(item => item.model.uri.toString() === uri.toString());
      if (!file) return false;
      remember(); store().activate(file);
      if (selection) {
        const selected = selection.startLineNumber ? selection :
          new monaco.Range(selection.lineNumber, selection.column, selection.lineNumber, selection.column);
        ed.setSelection(selected); ed.revealRangeInCenter(selected);
      }
      history = history.slice(0, historyIndex + 1);
      history.push({path:file.path, state:ed.saveViewState()});
      if (history.length > 64) history.shift();
      historyIndex = history.length - 1;
      return true;
    }});
    for (const [code, direction] of [[monaco.KeyCode.LeftArrow, -1], [monaco.KeyCode.RightArrow, 1]]) {
      ed.addAction({id:direction < 0 ? 'mcu.navigation.back' : 'mcu.navigation.forward',
        label:direction < 0 ? 'Go Back' : 'Go Forward', keybindings:[monaco.KeyMod.Alt | code], run:() => {
        const next = historyIndex + direction;
        if (next < 0 || next >= history.length) return;
        const entry = history[next], file = record(entry.path);
        if (!file) return;
        replaying = true;
        try { store().activate(file); ed.restoreViewState(entry.state); historyIndex=next; ed.focus(); }
        finally { replaying=false; }
      }});
    }
    const load = window.safeLoadProject;
    if (load) window.safeLoadProject = function (...args) { window.closeAllSourcePreviews(); return load.apply(this, args); };
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install);
  else install();
})();
