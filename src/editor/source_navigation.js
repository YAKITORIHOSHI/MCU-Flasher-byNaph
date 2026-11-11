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
  const baseName = path => String(path || '').replaceAll('\\', '/').split('/').pop();
  const folderOf = path => {
    const parts = String(path || '').replaceAll('\\', '/').split('/');
    parts.pop();
    return parts.slice(-3).join('/');
  };

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
  function closeChooser(focusEditor = true) {
    if (!choices) return;
    choices.remove(); choices = null;
    document.removeEventListener('pointerdown', outsideChooser, true);
    if (focusEditor) editor()?.focus();
  }
  function outsideChooser(event) {
    if (choices && !choices.contains(event.target)) closeChooser(false);
  }
  window.closeAllSourcePreviews = () => {
    ++latestRequest;
    closePreview();
    history = []; historyIndex = -1;
    closeChooser(false);
  };
  function materialize(destination) {
    const existing = record(destination.path);
    if (existing) return existing;
    closePreview();
    const name = baseName(destination.path);
    const file = {name, path:destination.path, preview:true, readOnly:true, isDirty:false,
      model:monaco.editor.createModel(destination.content, name.endsWith('.c') ? 'c' : 'cpp')};
    store().add(file);
    const tab = document.getElementById(store().tabId(file.path));
    tab._filePath = file.path;
    tab.dataset.filePath = file.path;
    tab.dataset.preview = 'true';
    tab.draggable = false;
    tab.title = `${destination.path}\nRead-only library preview`;
    tab.addEventListener('contextmenu', event => { event.preventDefault(); event.stopImmediatePropagation(); }, true);
    tab.setAttribute('aria-label', name + ', read-only library preview');
    const lock = document.createElement('span');
    lock.className = 'mcu-preview-lock';
    lock.setAttribute('aria-hidden', 'true');
    tab.append(lock, window.mcuCloseButton('Close ' + name + ' preview', () => closePreview(file)));
    preview = file;
    return file;
  }
  function range(destination, model) {
    const column = (line, index) => Array.from(model.getLineContent(line)).slice(0, index).join('').length + 1;
    const line = Math.min(model.getLineCount(), destination.line + 1);
    const endLine = Math.min(model.getLineCount(), (destination.end_line >= 0 ? destination.end_line : destination.line) + 1);
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
  function choose(destinations, position, word) {
    const model = editor().getModel(), revision = model.getVersionId();
    const generation = window.__mcuProjectFilesGeneration || 0;
    closeChooser(false);
    const container = document.getElementById('editor-container') || document.body;
    const menu = document.createElement('div'); choices = menu;
    menu.id = 'mcu-declaration-chooser';
    menu.setAttribute('role', 'dialog'); menu.setAttribute('aria-label', 'Choose a declaration');
    const head = document.createElement('div'); head.className = 'mcu-chooser-head';
    const icon = document.createElement('span'); icon.className = 'mcu-chooser-icon'; icon.setAttribute('aria-hidden', 'true');
    const title = document.createElement('span'); title.className = 'mcu-chooser-title';
    title.textContent = word ? `Go to ${word}` : 'Choose a declaration';
    const count = document.createElement('span'); count.className = 'mcu-chooser-count';
    count.textContent = `${destinations.length} matches`;
    head.append(icon, title, count, window.mcuCloseButton('Close declaration list', () => closeChooser()));
    const list = document.createElement('div'); list.className = 'mcu-chooser-list'; list.setAttribute('role', 'listbox');
    for (const destination of destinations) {
      const option = document.createElement('button'); option.type = 'button';
      option.className = 'mcu-chooser-row'; option.setAttribute('role', 'option');
      const signature = document.createElement('span'); signature.className = 'mcu-chooser-signature';
      signature.textContent = destination.signature || destination.name;
      const location = document.createElement('span'); location.className = 'mcu-chooser-location';
      const fileName = document.createElement('b'); fileName.textContent = `${baseName(destination.path)}:${destination.line + 1}`;
      location.append(fileName, document.createTextNode(`  ${folderOf(destination.path)}`));
      option.title = `${destination.signature || destination.name}\n${destination.path}:${destination.line + 1}`;
      option.append(signature, location);
      option.onclick = () => {
        closeChooser(false);
        if (model.isDisposed() || editor().getModel() !== model || model.getVersionId() !== revision ||
            generation !== (window.__mcuProjectFilesGeneration || 0)) return;
        jump(destination);
      };
      list.append(option);
    }
    menu.append(head, list);
    menu.onkeydown = event => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeChooser(); }
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const rows = Array.from(list.querySelectorAll('button'));
        const index = rows.indexOf(document.activeElement);
        rows[(index + (event.key === 'ArrowDown' ? 1 : rows.length - 1) + rows.length) % rows.length]?.focus();
      }
    };
    container.append(menu);
    // Open beside the word that asked, like an editor's own definition list.
    const bounds = container.getBoundingClientRect();
    const width = Math.max(240, Math.min(560, bounds.width - 24));
    menu.style.width = width + 'px';
    const anchor = position && editor().getScrolledVisiblePosition?.(position);
    const editorTop = (document.getElementById('monaco-anchor')?.getBoundingClientRect().top || bounds.top) - bounds.top;
    let left = 12, top = 12;
    if (anchor) {
      left = Math.max(8, Math.min(bounds.width - width - 8, anchor.left + 24));
      top = editorTop + anchor.top + anchor.height + 6;
      if (top + menu.offsetHeight > bounds.height - 8) top = Math.max(8, editorTop + anchor.top - menu.offsetHeight - 6);
    }
    menu.style.left = left + 'px'; menu.style.top = top + 'px';
    document.addEventListener('pointerdown', outsideChooser, true);
    list.querySelector('button')?.focus();
  }
  window.navigateSourceAtPosition = async (position, explicitWord) => {
    const model = editor()?.getModel(), pos = position || editor()?.getPosition();
    if (explicitWord && model?.getWordAtPosition(pos)?.word !== explicitWord) return false;
    const result = await window.requestSourceDefinition(model, pos);
    if (!result || result.cancelled) return false;
    if (!result.success || !result.destinations?.length) {
      if (result.word || result.error) {
        window.showEditorNotice?.(result.error || `No declaration of ${result.word} was found. Compile once to record the selected target’s include paths and defines, then try again.`);
      }
      return false;
    }
    if (editor().getModel() !== model) return false;
    // Conditional compilation can hide a declaration, but one candidate is still
    // the only answer there is: a list appears only when there is a real choice.
    if (result.destinations.length === 1) return jump(result.destinations[0]);
    choose(result.destinations, pos, result.word);
    return true;
  };
  // Monaco asks its definition provider while Ctrl is held over a word, before any
  // click. Answering with tabs or lists there opened previews and an empty chooser
  // on hover, and its request replaced the click's own. Ctrl+Click, F12 and the
  // context menu navigate through navigateSourceAtPosition instead.
  window.sourceDefinitionLocations = async () => null;
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
      closeChooser(false);
    });
    ed.onDidScrollChange?.(() => closeChooser(false));
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
    // The context menu's definition entries run these commands; route them through the
    // same navigation as Ctrl+Click so they never report "No definition found".
    const navigateHere = () => {
      const language = ed.getModel()?.getLanguageId();
      if (['cpp', 'c', 'ino', 'h'].includes(language)) window.navigateSourceAtPosition(ed.getPosition());
    };
    for (const id of ['editor.action.revealDefinition', 'editor.action.goToDeclaration',
                      'editor.action.peekDefinition', 'editor.action.revealDefinitionAside']) {
      try { monaco.editor.registerCommand(id, navigateHere); } catch (error) { }
    }
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
