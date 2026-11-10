/* A save acknowledgement applies only to the exact model revision written. */
(() => {
  let tail = Promise.resolve();
  window.showEditorNotice = message => {
    document.getElementById('editor-notice')?.remove();
    const notice = document.createElement('div');
    notice.id = 'editor-notice';
    notice.setAttribute('role', 'status');
    notice.setAttribute('aria-live', 'polite');
    Object.assign(notice.style, {position:'absolute', top:'44px', right:'12px', zIndex:'10000',
      maxWidth:'min(520px, calc(100% - 24px))', maxHeight:'50%', overflow:'auto',
      boxSizing:'border-box', padding:'12px', border:'1px solid var(--hover-border)',
      borderRadius:'10px', background:'var(--reading)', backdropFilter:'blur(18px)', color:'var(--text)',
      font:'12px Montserrat, sans-serif', whiteSpace:'pre-wrap', overflowWrap:'anywhere'});
    const close = document.createElement('button');
    close.type = 'button'; close.textContent = 'Close';
    close.setAttribute('aria-label', 'Close notification');
    Object.assign(close.style, {float:'right', margin:'0 0 6px 12px', padding:'4px 8px',
      border:'1px solid var(--hover-border)', borderRadius:'5px', cursor:'pointer',
      background:'var(--chip)', color:'var(--chip-ink)', font:'inherit'});
    close.onclick = () => notice.remove();
    notice.append(close, document.createTextNode(String(message)));
    (document.getElementById('editor-container') || document.body).append(notice);
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
