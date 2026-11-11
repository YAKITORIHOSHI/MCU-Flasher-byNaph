/* A save acknowledgement applies only to the exact model revision written. */
(() => {
  let tail = Promise.resolve();
  // One round close mark for every floating editor surface (notices, the declaration
  // list and library previews). Its look lives in index.html as `.mcu-close`.
  window.mcuCloseButton = (label, onClose) => {
    const close = document.createElement('button');
    close.type = 'button'; close.className = 'mcu-close';
    close.setAttribute('aria-label', label); close.title = label;
    close.addEventListener('click', event => { event.stopPropagation(); onClose(event); });
    return close;
  };
  let noticeTimer = 0;
  window.showEditorNotice = message => {
    document.getElementById('editor-notice')?.remove();
    clearTimeout(noticeTimer);
    const notice = document.createElement('div');
    notice.id = 'editor-notice';
    notice.setAttribute('role', 'status');
    notice.setAttribute('aria-live', 'polite');
    const icon = document.createElement('span');
    icon.className = 'editor-notice-icon';
    icon.setAttribute('aria-hidden', 'true');
    const text = document.createElement('span');
    text.className = 'editor-notice-text';
    text.textContent = String(message);
    const dismiss = () => {
      clearTimeout(noticeTimer);
      if (!notice.isConnected) return;
      notice.classList.add('leaving');
      setTimeout(() => notice.remove(), 160);
    };
    notice.append(icon, text, window.mcuCloseButton('Close notification', dismiss));
    // Short notices leave by themselves; reading or hovering one keeps it open.
    const schedule = () => { clearTimeout(noticeTimer); noticeTimer = setTimeout(dismiss, 12000); };
    notice.addEventListener('pointerenter', () => clearTimeout(noticeTimer));
    notice.addEventListener('pointerleave', schedule);
    notice.addEventListener('focusin', () => clearTimeout(noticeTimer));
    (document.getElementById('editor-container') || document.body).append(notice);
    schedule();
  };
  const queued = operation => {
    const next = tail.then(operation);
    tail = next.catch(() => {});
    return next;
  };
  async function save(files) {
    const store = window.__mcuProjectFileStore, api = window.pywebview?.api;
    if (!store || !api) throw new Error('The editor is not ready to save.');
    for (const file of files) {
      if (file.preview || file.readOnly || !file.isDirty) continue;
      if (file.model.isDisposed()) throw new Error('The source changed before saving finished.');
      const content = file.model.getValue(), revision = file.model.getVersionId();
      const result = await api.save_file(file.path, content);
      if (result?.success !== true) throw new Error(result?.error || 'The file could not be saved.');
      if (file.model.isDisposed()) throw new Error('The source changed before saving finished.');
      if (file.model.getVersionId() !== revision) {
        file.isDirty = true;
        store.refresh(file);
        await api.update_editor_buffer?.(file.path, file.model.getValue());
        throw new Error('More edits arrived while saving. Your latest text is still unsaved; save again before compiling or uploading.');
      }
      file.isDirty = false;
      store.refresh(file);
      await api.mark_modified?.(file.path, false);
    }
  }
  const report = async operation => {
    try { return await operation(); }
    catch (error) { window.showEditorNotice(error.message || error); throw error; }
  };
  const install = () => {
    window.saveAllFiles = () => queued(() => report(() => save(window.__mcuProjectFileStore?.list() || [])));
    window.saveActiveFile = () => queued(() => report(() => {
      const active = window.__mcuProjectFileStore?.active();
      return save(active ? [active] : []);
    }));
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install);
  else install();
})();
