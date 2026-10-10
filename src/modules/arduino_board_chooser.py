"""Owned, bounded chooser for explicit Arduino CLI board permissions."""
from __future__ import annotations

import os
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont, ttk

_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]{0,159})\.name\s*=\s*(.+)$")
_MAX_BOARDS = 4096
_MAX_ENTRIES = 32768
_MAX_DIRECTORIES = 2048
_MAX_BYTES = 16 * 1024 * 1024


def read_board_choices(folder, cancel=None):
    """Read names/IDs only, off Tk, without following package links."""
    root = Path(folder)
    if not root.is_dir():
        raise ValueError('Download and extract this version before choosing Arduino CLI boards.')
    if root.is_symlink() or (hasattr(root, 'is_junction') and root.is_junction()):
        raise ValueError('Choose an extracted board package without folder links.')
    pending, boards = [root], {}
    directories = entries = total = files = 0
    while pending:
        if cancel is not None and cancel.is_set():
            return []
        directory = pending.pop()
        directories += 1
        if directories > _MAX_DIRECTORIES:
            raise ValueError('This board package has too many folders to list safely.')
        with os.scandir(directory) as contents:
            for entry in contents:
                entries += 1
                if entries > _MAX_ENTRIES:
                    raise ValueError('This board package has too many files to list safely.')
                if cancel is not None and cancel.is_set():
                    return []
                if entry.is_symlink() or (hasattr(os.path, 'isjunction') and os.path.isjunction(entry.path)):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.name == 'boards.txt' and entry.is_file(follow_symlinks=False):
                    files += 1
                    if files > 128:
                        raise ValueError('This board package has too many board declarations.')
                    with open(entry.path, 'rb') as stream:
                        raw = stream.read(_MAX_BYTES - total + 1)
                    total += len(raw)
                    if total > _MAX_BYTES:
                        raise ValueError('The board declarations are too large to list safely.')
                    for line in raw.decode('utf-8-sig', errors='replace').splitlines():
                        match = _NAME.fullmatch(line.strip())
                        if match:
                            identifier, name = match.groups()
                            boards[identifier] = name[:512]
                            if len(boards) > _MAX_BOARDS:
                                raise ValueError('This board package has too many boards to list safely.')
    if not boards:
        raise ValueError('No exact board IDs were found. Download and extract this version first.')
    return sorted(boards.items(), key=lambda pair: (pair[1].casefold(), pair[0]))


def open_board_chooser(app, tab, metadata, folder, *, theme, button, load_settings, save_settings):
    """Create one explicit dialog; every disk read/write stays in a worker."""
    from src.modules.arduino_board_selection import FIELD, association_key, invalidate_preferences, selected_boards
    from src.modules.tk_glass import DialogFit, GlassCard, ui_scale

    existing = getattr(app, '_arduino_board_dialog', None)
    if existing is not None and existing.winfo_exists():
        existing.lift()
        existing.focus_set()
        return existing
    key = association_key(metadata)
    if not key:
        app._set_status('This package has no valid vendor and architecture identity. Choose another package.')
        return None
    version = str(metadata.get('version') or '')
    cancelled = threading.Event()
    dialog = tk.Toplevel(app.root)
    app._arduino_board_dialog = dialog
    dialog.title('Choose Arduino CLI boards')
    dialog.transient(app.root)
    dialog.configure(bg=theme.BG_DARKEST)
    scale = ui_scale(dialog)
    pad, gap = max(6, round(10 * scale)), max(4, round(7 * scale))

    def close():
        cancelled.set()
        dialog.destroy()

    dialog.protocol('WM_DELETE_WINDOW', close)
    dialog.bind('<Escape>', lambda _event: close())
    header = GlassCard(dialog, theme, padding=10)
    header.pack(fill='x', padx=pad, pady=(pad, gap))
    title = tk.Label(header.body, text=str(metadata.get('name') or 'Board package'),
                     font=('Montserrat', 11, 'bold'), fg=theme.TEXT_BRIGHT, bg=theme.BG_MID,
                     justify='left', anchor='w')
    title.pack(fill='x')
    hint = tk.Label(header.body,
                    text='PlatformIO first. Only checked boards may use Arduino CLI when an exact PlatformIO target is unavailable.',
                    font=('Montserrat', 9), fg=theme.TEXT, bg=theme.BG_MID, justify='left', anchor='w')
    hint.pack(fill='x', pady=(gap, 0))
    header.body.bind('<Configure>', lambda event: [widget.configure(wraplength=max(1, event.width))
                                                 for widget in (title, hint)], add='+')

    footer = GlassCard(dialog, theme, padding=10)
    footer.pack(side='bottom', fill='x', padx=pad, pady=(gap, pad))
    status = tk.Label(footer.body, text='Reading downloaded boards…', font=('Montserrat', 8),
                      fg=theme.TEXT_DIM, bg=theme.BG_MID, anchor='w', justify='left')
    status.pack(fill='x')
    actions = tk.Frame(footer.body, bg=theme.BG_MID)
    actions.pack(fill='x', pady=(gap, 0))
    footer.body.bind('<Configure>', lambda event: status.configure(wraplength=max(1, event.width)), add='+')
    card = GlassCard(dialog, theme, padding=10, expand=True)
    card.pack(fill='both', expand=True, padx=pad)
    search = tk.StringVar(dialog)
    search_row = tk.Frame(card.body, bg=theme.BG_MID)
    search_row.pack(fill='x', pady=(0, gap))
    tk.Label(search_row, text='Search', font=('Montserrat', 9), fg=theme.TEXT,
             bg=theme.BG_MID).pack(side='left', padx=(0, gap))
    search_entry = tk.Entry(search_row, textvariable=search, font=('Montserrat', 10),
                            bg=theme.BG_DARKEST, fg=theme.TEXT, insertbackground=theme.CYAN,
                            relief='flat', highlightthickness=1, highlightbackground=theme.BORDER,
                            highlightcolor=theme.CYAN)
    search_entry.pack(side='left', fill='x', expand=True, ipady=max(2, round(3 * scale)))
    listing = tk.Frame(card.body, bg=theme.BG_MID)
    listing.pack(fill='both', expand=True)
    listing.columnconfigure(0, weight=1)
    listing.rowconfigure(0, weight=1)
    style = ttk.Style(dialog)
    row_height = max(round(25 * scale), tkfont.Font(dialog, font=('Montserrat', 10)).metrics('linespace') + 6)
    style.configure('ArduinoChoices.Treeview', background=theme.BG_DARKEST, fieldbackground=theme.BG_DARKEST,
                     foreground=theme.TEXT, rowheight=row_height, font=('Montserrat', 10),
                     bordercolor=theme.BORDER, relief='flat')
    style.map('ArduinoChoices.Treeview', background=[('selected', theme.CYAN_DIM)],
               foreground=[('selected', theme.TEXT_BRIGHT)])
    style.configure('ArduinoChoices.Treeview.Heading', background=theme.BG_MID, foreground=theme.TEXT_BRIGHT,
                     font=('Montserrat', 9, 'bold'), relief='flat')
    style.map('ArduinoChoices.Treeview.Heading', background=[('active', theme.BG_HOVER)])
    style.configure('ArduinoChoices.Horizontal.TScrollbar', background=theme.BG_MID,
                     troughcolor=theme.BG_DARKEST, bordercolor=theme.BG_DARKEST, arrowcolor=theme.TEXT_DIM,
                     lightcolor=theme.BG_MID, darkcolor=theme.BG_MID, arrowsize=max(12, round(12 * scale)))
    style.map('ArduinoChoices.Horizontal.TScrollbar', background=[('active', theme.BG_HOVER)])
    tree = ttk.Treeview(listing, columns=('allowed', 'board'), show='headings', selectmode='browse',
                        style='ArduinoChoices.Treeview', height=7)
    tree.heading('allowed', text='CLI')
    tree.heading('board', text='Board name / exact ID')
    tree.column('allowed', width=max(44, round(44 * scale)), minwidth=40, stretch=False, anchor='center')
    tree.column('board', width=400, minwidth=80)
    tree.grid(row=0, column=0, sticky='nsew')
    vertical = ttk.Scrollbar(listing, orient='vertical', command=tree.yview)
    vertical.grid(row=0, column=1, sticky='ns')
    horizontal = ttk.Scrollbar(listing, orient='horizontal', command=tree.xview,
                                style='ArduinoChoices.Horizontal.TScrollbar')
    horizontal.grid(row=1, column=0, sticky='ew')
    tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
    chosen, rows, visible = set(), [], {}
    loaded = saving = False

    def render(*_args):
        if not dialog.winfo_exists():
            return
        query = search.get().strip().casefold()
        tree.delete(*tree.get_children())
        visible.clear()
        matches = [(identifier, name) for identifier, name in rows
                   if not query or query in identifier.casefold() or query in name.casefold()]
        for number, (identifier, name) in enumerate(matches[:250]):
            row = str(number)
            visible[row] = identifier
            tree.insert('', 'end', iid=row, values=('✓' if identifier in chosen else '', f'{name}  [{identifier}]'))
        if loaded and not saving:
            extra = ' Refine the search to see more.' if len(matches) > 250 else ''
            status.configure(text=f'{len(chosen)} allowed; {min(250, len(matches))} of {len(matches)} shown.{extra}')

    def toggle(row):
        if saving or not loaded or row not in visible:
            return
        identifier = visible[row]
        if identifier in chosen:
            chosen.remove(identifier)
        else:
            chosen.add(identifier)
        values = list(tree.item(row, 'values'))
        values[0] = '✓' if identifier in chosen else ''
        tree.item(row, values=values)
        status.configure(text=f'{len(chosen)} board(s) allowed. Click a board or press Space to change it.')

    def click(event):
        if tree.identify_region(event.x, event.y) == 'cell':
            toggle(tree.identify_row(event.y))

    tree.bind('<ButtonRelease-1>', click)
    tree.bind('<space>', lambda _event: (toggle(tree.focus()), 'break')[1])
    search.trace_add('write', render)

    def clear():
        if loaded and not saving:
            chosen.clear()
            render()

    def current_selection():
        selection = tab.listbox.curselection()
        if not selection or selection[0] >= len(tab.filtered_names):
            return False
        item = tab.all_items.get(tab.filtered_names[selection[0]], {})
        return association_key(item) == key and tab.version_var.get() == version

    def save():
        nonlocal saving
        if not loaded or saving:
            return
        if app._busy or not current_selection():
            status.configure(text='Wait for the current operation, then reopen this chooser for the selected package version.')
            return
        saving = True
        save_button.configure(state='disabled')
        clear_button.configure(state='disabled')
        status.configure(text='Saving Arduino CLI choices…')
        values = sorted(chosen)

        def finish(stored, success):
            nonlocal saving
            saving = False
            if success:
                invalidate_preferences()
                app._set_status('Arduino CLI choices saved. Prepare board support to verify chosen boards. PlatformIO remains first.')
                if dialog.winfo_exists():
                    close()
            elif dialog.winfo_exists():
                save_button.configure(state='normal')
                clear_button.configure(state='normal')
                status.configure(text='Settings could not be saved. Check that settings storage is writable.')

        def persist():
            settings = load_settings()
            existing = settings.get(FIELD)
            stored = dict(existing) if isinstance(existing, dict) else {}
            if values:
                stored[key] = values
            else:
                stored.pop(key, None)
            settings[FIELD] = stored
            success = save_settings(settings)
            app._post_ui(finish, stored, success)

        def failed(error):
            finish({}, False)
            if dialog.winfo_exists():
                status.configure(text=f'Settings could not be saved: {error}')

        if app._tasks.start(persist, failed=failed) is False:
            finish({}, False)

    button(actions, 'Cancel', close, theme.BTN_MONITOR, theme.BTN_MONITOR_H).pack(side='right')
    save_button = button(actions, 'Save', save, theme.BTN_COMPILE, theme.BTN_COMPILE_H)
    save_button.pack(side='right', padx=(0, gap))
    clear_button = button(actions, 'Clear choices', clear, theme.BTN_MONITOR, theme.BTN_MONITOR_H)
    clear_button.pack(side='left')
    save_button.configure(state='disabled')
    clear_button.configure(state='disabled')

    def present(choices, selected, error):
        nonlocal loaded
        if cancelled.is_set() or not dialog.winfo_exists():
            return
        chosen.update(selected)
        rows.extend(choices)
        present_ids = {identifier for identifier, _name in choices}
        rows.extend((identifier, 'Saved board (absent from this version)') for identifier in sorted(selected - present_ids))
        loaded = True
        render()
        save_button.configure(state='normal')
        clear_button.configure(state='normal')
        if error:
            status.configure(text=error + ' Existing choices can still be cleared.')

    def scan():
        from src.modules.arduino_board_selection import load_preferences
        selected = selected_boards(metadata, load_preferences(force_read=True))
        try:
            choices, error = read_board_choices(folder, cancelled), ''
        except (OSError, ValueError) as exc:
            choices, error = [], str(exc)
        app._post_ui(present, choices, selected, error)

    def scan_failed(error):
        if dialog.winfo_exists():
            status.configure(text=f'Board choices could not be read: {error}')

    dialog._cli_tree = tree
    dialog._cli_search = search_entry
    dialog._cli_status = status
    dialog._cli_save = save_button
    dialog._cli_clear = clear_button
    dialog._cli_toggle = toggle
    dialog._cli_chosen = chosen
    compact = [False]
    def reflow(event):
        if event.widget is not dialog:
            return
        short = event.height < round(360 * scale)
        if short == compact[0]:
            return
        compact[0] = short
        if short:
            # Keep the searchable board rows and actions usable on short screens.
            header.pack_forget()
        else:
            header.pack(fill='x', padx=pad, pady=(pad, gap), before=card)
    dialog.bind('<Configure>', reflow, add='+')
    dialog._dialog_fit = DialogFit(dialog, app.root, preferred=(640, 470), minimum=(320, 260))
    dialog.grab_set()
    search_entry.focus_set()
    if app._tasks.start(scan, failed=scan_failed) is False:
        scan_failed('The package browser is closing.')
    return dialog
