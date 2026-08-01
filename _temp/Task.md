# Task Checklist: QScintilla Integration & Freeze-proofing

- [ ] Create and optimize `src/qscintilla_editor.py`
  - [ ] Implement standalone vs embedded mode (`--embedded` and `--dir` CLI arguments)
  - [ ] Apply theme matching the main GUI's dark colors
  - [ ] Implement tab moved event to save tab order to `.mcu_flash_tab_order.json`
  - [ ] Implement Win32 window message handlers for save all (`MCU_Flash_Save_All`) and reload (`MCU_Flash_Reload_All`)
  - [ ] Keep imports minimal and optimize startup for low-end devices (e.g., optimize autocompletion prep)

- [ ] Integrate QScintilla option into `mcu_flash_gui.py`
  - [ ] Update `_open_settings()` to list QScintilla in the Combobox and save `"qscintilla"` editor mode
  - [ ] Add `_build_editor_qscintilla(self, parent_frame)` to launch and embed the QScintilla process
  - [ ] Implement `_try_embed_qsci_window(self)` to Reparent and style the window natively
  - [ ] Add `_qsci_proc` cleanup to `_on_main_window_close()`

- [ ] Implement Editor Optimization & Freeze-proofing
  - [ ] Replace synchronous `MoveWindow` calls in `_resize_embedded_editor` with async `SetWindowPos` using `SWP_ASYNCWINDOWPOS` for both Monaco and QScintilla
  - [ ] Add `<FocusIn>` event binding on root to auto-update Skip-Compile state whenever focus is returned to the app
  - [ ] Ensure `_save_all_editor_files()` is automatically invoked before compile/upload starts

- [ ] Verification and Polish
  - [ ] Verify standalone mode of QScintilla works
  - [ ] Verify embedded mode works
  - [ ] Verify resize behavior is smooth and freeze-free
  - [ ] Verify process exits cleanly on app close
