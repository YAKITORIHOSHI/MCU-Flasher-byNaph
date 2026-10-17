/* Incremental root-source changes. The bundle supplies only its file-store hook;
 * unchanged records, Monaco models, undo stacks and DOM tabs remain owned there.
 */
(() => {
  'use strict';
  let pending = Promise.resolve();
  let project = '';
  let revision = 0;
  const renderer = new URLSearchParams(window.location.search).get('renderer') || '';

  window.invalidateProjectFileOperations = (generation, root, expectedRenderer = renderer) => {
    if (expectedRenderer !== renderer) return false;
    window.__mcuProjectFilesGeneration = generation;
    project = root || '';
    revision = 0;
    return true;
  };

  function tabFor(store, file) {
    return document.getElementById(store.tabId(file.path));
  }

  function mapTab(store, file) {
    const tab = tabFor(store, file);
    if (!tab) throw new Error('The changed file has no source tab.');
    tab._filePath = file.path;
    tab.setAttribute('data-file-path', file.path);
    tab.setAttribute('draggable', 'true');
    return tab;
  }

  async function apply(change) {
    const store = window.__mcuProjectFileStore;
    const api = window.pywebview?.api;
    const editor = window.editorInstance;
    if (!store || !api || !editor || !change?.file) return false;
    const key = window.projectPathKey;
    const current = () => change.renderer === renderer &&
      change.generation === (window.__mcuProjectFilesGeneration || 0) &&
      (!project || key(project) === key(change.path));
    // Native replies and queued events may finish after a same-page project
    // switch. A discarded event succeeds silently without touching its successor.
    if (!current() || change.revision <= revision) return true;
    await window.waitForProjectLoad?.();
    if (!current()) return true;
    const root = await api.get_project_dir();
    await window.waitForProjectLoad?.();
    if (!current() || key(root) !== key(change.path)) return true;
    project = change.path;
    const files = store.list();
    const target = files.find(file => key(file.path) === key(change.file));
    const original = files.find(file => key(file.path) === key(change.old_file));

    if (change.operation === 'add') {
      // A renderer may have completed its initial load after the disk change.
      if (!target) {
        const reply = typeof change.content === 'string'
          ? {success:true, content:change.content} : await api.read_file(change.file);
        await window.waitForProjectLoad?.();
        if (!current()) return true;
        if (typeof change.content !== 'string') {
          const rootAfterRead = await api.get_project_dir();
          if (!current() || key(rootAfterRead) !== key(change.path)) return true;
        }
        if (reply?.success !== true || typeof reply.content !== 'string') return false;
        const name = change.file.replaceAll('\\', '/').split('/').pop();
        const extension = name.split('.').pop().toLowerCase();
        const language = extension === 'c' ? 'c' : extension === 'txt' ? 'plaintext' : 'cpp';
        const file = {name, path:change.file,
          model:monaco.editor.createModel(reply.content, language), isDirty:false};
        file.model.onDidChangeContent(() => {
          if (!file.isDirty) {
            file.isDirty = true;
            store.refresh(file);
            window.pywebview?.api?.mark_modified(file.path, true);
          }
        });
        store.add(file);
        mapTab(store, file);
        if (!store.active()) store.activate(file);
      }
    } else if (change.operation === 'rename') {
      if (original && original !== target) {
        if (original.isDirty) return false;
        const tab = tabFor(store, original);
        if (!tab) return false;
        if (target) {
          // Initial project loading can already contain the final name while
          // the queued Add still refers to the earlier name. Keep that loaded
          // model instead of materializing a second tab for the same path.
          const active = store.active() === original;
          tab.remove();
          store.remove(original);
          if (active) store.activate(target);
          original.model.dispose();
        } else {
          original.path = change.file;
          original.name = change.file.replaceAll('\\', '/').split('/').pop();
          tab.id = store.tabId(original.path);
          tab.querySelector('span:first-child').textContent = original.name;
          mapTab(store, original);
        }
      } else if (!target) {
        return false;
      }
    } else if (change.operation === 'delete') {
      if (target) {
        if (target.isDirty) return false;
        const tab = tabFor(store, target);
        const tabs = Array.from(document.querySelectorAll('#tab-bar .tab'));
        const index = tabs.indexOf(tab);
        const nextTab = tabs[index + 1] || tabs[index - 1];
        const next = files.find(file => tabFor(store, file) === nextTab);
        const active = store.active() === target;
        tab?.remove();
        store.remove(target);
        if (active) {
          if (next) store.activate(next);
          else editor.setModel(null);
        }
        target.model.dispose();
      }
    } else {
      return false;
    }

    // Consumers such as cross-file symbols see the new root immediately; the
    // existing tab mapper also refreshes its authoritative metadata snapshot.
    // Invalidate earlier listings before publishing or awaiting persistence:
    // their replies may still arrive within this same project generation.
    window.__mcuProjectFileListRevision = (window.__mcuProjectFileListRevision || 0) + 1;
    window.projectFileList = store.list().map(file => ({name:file.name, path:file.path}));
    const paths = Array.from(document.querySelectorAll('#tab-bar .tab'), tab => tab._filePath)
      .filter(Boolean);
    if (api.save_project_tab_order) {
      await api.save_project_tab_order(renderer, change.path, paths);
    }
    if (!current()) return true;
    if (window.syncTabPaths) await window.syncTabPaths();
    if (!current()) return true;
    revision = change.revision;
    window.syncModelLanguageForActiveTab?.();
    window.refreshProjectSyntax?.();
    return true;
  }

  window.reconcileProjectFiles = change => {
    const operation = pending.then(() => apply(change));
    pending = operation.catch(() => false);
    return operation;
  };
})();
