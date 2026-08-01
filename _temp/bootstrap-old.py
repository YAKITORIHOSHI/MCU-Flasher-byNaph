#!/usr/bin/env python3
"""
bootstrap.py — MCU Upload GUI Dependency Bootstrap
==================================================
Ensures pip, pyserial, psutil, pywin32 (Windows), and PlatformIO Core are
installed before launching the main GUI, then checks all utilities for
updates.

Called by MCU-Flash-GUI.vbs. On a fresh system this runs
once with a visible console window showing progress.
Subsequent launches skip straight through in <1 second.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import importlib.util
from pathlib import Path
from typing import Optional

# Configure stdout/stderr to use UTF-8 encoding on Windows to prevent UnicodeEncodeError
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            getattr(sys.stdout, "reconfigure")(encoding="utf-8")
        if hasattr(sys.stderr, "reconfigure"):
            getattr(sys.stderr, "reconfigure")(encoding="utf-8")
    except Exception:
        pass

# Fast-path env var: set MCU_FLASH_GUI_SKIP_TOOLCHAINS=1 to skip board toolchain pre-install
# (useful for dev/testing or if you only use ESP32 and already have it installed)
SKIP_TOOLCHAINS = os.environ.get("MCU_FLASH_GUI_SKIP_TOOLCHAINS", "").lower() in ("1", "true", "yes")

if getattr(sys, 'frozen', False):
    SCRIPT_DIR = Path(sys.executable).resolve().parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent

BOOTSTRAP_CONFIG_FILE = SCRIPT_DIR / "bootstrap_config.json"

def load_bootstrap_config() -> dict:
    if BOOTSTRAP_CONFIG_FILE.is_file():
        try:
            return json.loads(BOOTSTRAP_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_bootstrap_config(cfg: dict):
    try:
        BOOTSTRAP_CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass

def ensure_platformio_penv_with_hook(script_dir: Path = None) -> bool:
    """
    Install the subprocess-hide hook into PlatformIO's private venv (penv)
    so that compiler subprocesses spawned by SCons don't flash console windows.
    Returns True if the hook was installed, False if penv doesn't exist yet.
    """
    if sys.platform != "win32":
        return False

    root = script_dir or SCRIPT_DIR
    pio_core_dir = os.environ.get("PLATFORMIO_CORE_DIR", "")
    if not pio_core_dir:
        return False

    penv_site = Path(pio_core_dir) / "penv" / "Lib" / "site-packages"
    if not penv_site.is_dir():
        return False  # penv not created yet — called again after first compile

    try:
        # pyrefly: ignore [missing-import]
        from win_subprocess_hide import install_venv_site_hook
        # Re-use the existing hook installer but targeting the penv site-packages
        hook_py  = penv_site / "mcu_flash_gui_subprocess_hook.py"
        hook_pth = penv_site / "mcu_flash_gui_subprocess_hook.pth"
        hook_py.write_text(
            f'"""Auto-installed hook: hide subprocess console windows on Windows."""\n'
            f'import sys\nfrom pathlib import Path\n\n'
            f'_root = Path({str(root)!r})\n'
            f'if str(_root) not in sys.path:\n    sys.path.insert(0, str(_root))\n\n'
            f'if sys.platform == "win32":\n'
            f'    try:\n'
            f'        from win_subprocess_hide import install\n'
            f'        install()\n'
            f'    except Exception:\n        pass\n',
            encoding="utf-8",
        )
        hook_pth.write_text("import mcu_flash_gui_subprocess_hook\n", encoding="utf-8")
        return True
    except Exception:
        return False

def _get_safe_platformio_core_dir(script_dir: Path) -> str:
    local_path = script_dir / "env" / ".platformio"
    local_path_str = str(local_path)
    if sys.platform == "win32" and (" " in local_path_str or "(" in local_path_str or ")" in local_path_str):
        junction_path = Path("C:\\") / ".platformio-mcu-gui"
        try:
            local_path.mkdir(parents=True, exist_ok=True)
            # Remove stale junction first (rmdir only removes the junction, not the target)
            if junction_path.exists() or junction_path.is_symlink():
                subprocess.run(
                    ["cmd", "/c", "rmdir", str(junction_path)],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            res = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction_path), local_path_str],
                capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            # Verify the junction is actually traversable (not just present)
            if junction_path.exists() and (res.returncode == 0 or junction_path.is_dir()):
                # Quick sanity-check: can we list the junction target?
                try:
                    list(junction_path.iterdir())
                except Exception:
                    pass  # Target empty is fine; PermissionError would be a problem
                return str(junction_path)
            # Junction creation failed — fall through to long path
        except Exception:
            pass
        # Warn: path has spaces/parens but junction couldn't be created.
        # PlatformIO tools called via subprocess may fail with CreateProcess
        # if the path contains spaces. Log here; user will see it in bootstrap.
        print(
            f"\n  \033[93m⚠\033[0m  WARNING: Could not create C:\\.platformio-mcu-gui junction.\n"
            f"       PlatformIO will use the long path:\n"
            f"       {local_path_str}\n"
            f"       If compilation fails with 'CreateProcess: No such file',\n"
            f"       move this project to a path without spaces or parentheses.\n",
            file=sys.stderr,
        )
    return local_path_str

os.environ["PLATFORMIO_CORE_DIR"] = _get_safe_platformio_core_dir(SCRIPT_DIR)
os.environ["PYTHONUNBUFFERED"] = "1"

if sys.platform == "win32":
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        # pyrefly: ignore [missing-import]
        from win_subprocess_hide import install as _install_subprocess_hide
        # pyrefly: ignore [missing-import]
        from win_subprocess_hide import install_venv_site_hook as _install_venv_site_hook

        _install_subprocess_hide()
        _install_venv_site_hook(SCRIPT_DIR)
    except Exception:
        pass

GUI_SCRIPT = SCRIPT_DIR / "mcu_flash_gui.py"

# ── ANSI codes kept for any direct print() fallbacks ────────
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
DIM = "\033[90m"
BOLD = "\033[1m"
RESET = "\033[0m"

# ── Theme colours matching MCU Flash GUI exactly ─────────────
T_BG_DARKEST  = "#0a0e14"
T_BG_DARK     = "#10151c"
T_BG_MID      = "#161d27"
T_BG_LIGHT    = "#1c2532"
T_BG_HOVER    = "#243040"
T_BORDER      = "#2a3545"
T_TEXT        = "#c8d2dc"
T_TEXT_DIM    = "#6b7d94"
T_TEXT_BRIGHT = "#e8edf3"
T_CYAN        = "#39c5bb"
T_GREEN       = "#5ccc6e"
T_YELLOW      = "#e8b83a"
T_RED         = "#f05050"
T_MAGENTA     = "#c678dd"

# ── Shared GUI state (set up by BootstrapGUI) ────────────────
_gui: "BootstrapGUI | None" = None   # set when the window is live

# ─────────────────────────────────────────────────────────────
# BootstrapGUI — dark Tkinter window with scrollable log,
# animated spinner, and a status bar; matches MCU Flash GUI.
# ─────────────────────────────────────────────────────────────
class BootstrapGUI:
    """
    Displays bootstrap progress in a styled GUI window.
    All methods are safe to call from any thread; they use
    root.after() to marshal updates to the Tk main thread.
    """
    SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self):
        import sys
        if sys.platform == "win32":
            try:
                import ctypes
                gdi32 = ctypes.windll.gdi32
                FR_PRIVATE = 0x10
                fonts_dir = SCRIPT_DIR / "src" / "fonts" / "Montserrat" / "static"
                if not fonts_dir.exists():
                    fonts_dir = SCRIPT_DIR / "src" / "fonts" / "Montserrat"
                if fonts_dir.exists():
                    for ttf_file in fonts_dir.glob("*.ttf"):
                        path_buf = ctypes.create_unicode_buffer(str(ttf_file))
                        gdi32.AddFontResourceExW(path_buf, FR_PRIVATE, 0)
            except Exception:
                pass
        import tkinter as tk
        from tkinter import scrolledtext, font as tkfont, ttk

        self.root = tk.Tk()
        self.root.title("MCU Uploader IDE by Naph — Setup")
        
        # Set window icon if available
        try:
            icon_path = SCRIPT_DIR / "src" / "mcu_icon.ico"
            if icon_path.exists():
                self.root.iconbitmap(default=str(icon_path))
                self.root.iconbitmap(str(icon_path))
            else:
                log_dir = SCRIPT_DIR / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                (log_dir / "bootstrap_icon.log").write_text(f"Icon file does not exist at: {icon_path}\n", encoding="utf-8")
        except Exception as e:
            import traceback
            log_dir = SCRIPT_DIR / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "bootstrap_icon.log").write_text(f"Error setting icon: {e}\n{traceback.format_exc()}\n", encoding="utf-8")
        self.root.geometry("680x500")
        self.root.minsize(560, 380)
        self.root.configure(bg=T_BG_DARKEST)
        self.root.resizable(False, False)

        # Centre on screen
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - 680) // 2
        y = (sh - 500) // 2
        self.root.geometry(f"680x500+{x}+{y}")

        # Force window always on top for 1 second (1000ms) upon launch
        try:
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(1000, self._unset_topmost)
        except Exception:
            pass

        # ── Header bar ──────────────────────────────────────
        hdr = tk.Frame(self.root, bg=T_BG_DARK, pady=10, padx=16)
        hdr.pack(fill=tk.X)

        fnt_title = tkfont.Font(family="Montserrat", size=13, weight="bold")
        fnt_sub   = tkfont.Font(family="Montserrat", size=9)

        tk.Label(hdr, text="⚡  MCU Uploader IDE by Naph", font=fnt_title,
                 fg=T_CYAN, bg=T_BG_DARK).pack(side=tk.LEFT)
        tk.Label(hdr, text="Setting up dependencies…", font=fnt_sub,
                 fg=T_TEXT_DIM, bg=T_BG_DARK).pack(side=tk.LEFT, padx=(12, 0), pady=(3, 0))

        # ── Divider ─────────────────────────────────────────
        tk.Frame(self.root, bg=T_BORDER, height=1).pack(fill=tk.X)

        # ── Log area ────────────────────────────────────────
        log_frame = tk.Frame(self.root, bg=T_BG_DARKEST)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        fnt_log = tkfont.Font(family="Consolas", size=9)

        self.log = tk.Text(
            log_frame,
            font=fnt_log,
            bg=T_BG_DARKEST,
            fg=T_TEXT,
            insertbackground=T_CYAN,
            selectbackground=T_BG_HOVER,
            selectforeground=T_TEXT_BRIGHT,
            relief=tk.FLAT,
            bd=0,
            wrap=tk.WORD,
            state=tk.DISABLED,
            padx=14,
            pady=10,
        )
        self.scrollbar = ttk.Scrollbar(
            log_frame,
            orient=tk.VERTICAL,
            style="Vertical.TScrollbar",
            command=self.log.yview,
        )
        self.log.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Tag colour palette
        self.log.tag_configure("section",  foreground=T_CYAN,    font=tkfont.Font(family="Consolas", size=9, weight="bold"))
        self.log.tag_configure("ok",       foreground=T_GREEN)
        self.log.tag_configure("warn",     foreground=T_YELLOW)
        self.log.tag_configure("fail",     foreground=T_RED)
        self.log.tag_configure("dim",      foreground=T_TEXT_DIM)
        self.log.tag_configure("normal",   foreground=T_TEXT)
        self.log.tag_configure("update",   foreground=T_MAGENTA)
        self.log.tag_configure("pip_row",  foreground=T_CYAN,    font=tkfont.Font(family="Consolas", size=10))

        # ── Divider ─────────────────────────────────────────
        tk.Frame(self.root, bg=T_BORDER, height=1).pack(fill=tk.X)

        # ── Status bar (spinner + text + auto-scroll toggle) ────────────────
        sb = tk.Frame(self.root, bg=T_BG_DARK, pady=5, padx=14)
        sb.pack(fill=tk.X)

        fnt_status = tkfont.Font(family="Montserrat", size=9)

        self._spin_var = tk.StringVar(value="⠋")
        tk.Label(sb, textvariable=self._spin_var, font=fnt_status,
                 fg=T_CYAN, bg=T_BG_DARK).pack(side=tk.LEFT)

        self._status_var = tk.StringVar(value="Initialising…")
        tk.Label(sb, textvariable=self._status_var, font=fnt_status,
                 fg=T_TEXT_DIM, bg=T_BG_DARK).pack(side=tk.LEFT, padx=(6, 0))

        # Auto-scroll checkbox — right-aligned in the status bar
        self._auto_scroll_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            sb,
            text="Auto-Scroll",
            variable=self._auto_scroll_var,
            font=fnt_status,
            bg=T_BG_DARK,
            fg=T_TEXT_DIM,
            activebackground=T_BG_DARK,
            activeforeground=T_TEXT,
            selectcolor=T_BG_DARKEST,
            relief=tk.FLAT,
            bd=0,
            cursor="hand2",
        ).pack(side=tk.RIGHT)

        # Skip updates checkbox (for offline mode / fast startup)
        cfg_init = load_bootstrap_config()
        self._skip_updates_var = tk.BooleanVar(value=cfg_init.get("skip_updates", False))

        def _on_toggle_skip_updates():
            c = load_bootstrap_config()
            c["skip_updates"] = self._skip_updates_var.get()
            save_bootstrap_config(c)

        tk.Checkbutton(
            sb,
            text="Skip Updates",
            variable=self._skip_updates_var,
            command=_on_toggle_skip_updates,
            font=fnt_status,
            bg=T_BG_DARK,
            fg=T_TEXT_DIM,
            activebackground=T_BG_DARK,
            activeforeground=T_TEXT,
            selectcolor=T_BG_DARKEST,
            relief=tk.FLAT,
            bd=0,
            cursor="hand2",
        ).pack(side=tk.RIGHT, padx=(0, 12))

        # ── Progress bar (step progress + busy/marquee for downloads) ─
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Bootstrap.Horizontal.TProgressbar",
            troughcolor=T_BG_LIGHT,
            background=T_CYAN,
            bordercolor=T_BG_DARK,
            lightcolor=T_CYAN,
            darkcolor=T_CYAN,
            thickness=5,
        )
        style.configure(
            "Vertical.TScrollbar",
            background=T_BG_MID,
            troughcolor=T_BG_DARKEST,
            bordercolor=T_BG_DARKEST,
            arrowcolor=T_TEXT_DIM,
            lightcolor=T_BG_MID,
            darkcolor=T_BG_MID,
        )
        style.map(
            "Vertical.TScrollbar",
            background=[("active", T_BG_HOVER)]
        )
        self._progress = ttk.Progressbar(
            self.root,
            orient="horizontal",
            mode="determinate",
            maximum=100,
            style="Bootstrap.Horizontal.TProgressbar",
        )
        self._progress.pack(fill=tk.X, side=tk.BOTTOM)

        # Total number of top-level "Checking X" steps in the setup flow.
        self.TOTAL_STEPS = 9
        self._step_index = 0

        self._spin_idx = 0
        self._spinning = True
        self._closed = False          # must be set before _tick_spinner reads it
        self._tick_spinner()

        # Allow closing without killing the main process immediately
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _unset_topmost(self):
        try:
            if hasattr(self, "root") and self.root:
                self.root.attributes("-topmost", False)
        except Exception:
            pass

    # ── Thread-safe live PIP Table Block update ──────────────
    def update_pip_table_block(self, table_text: str):
        """Thread-safe live update of the multithreaded pip progress table block."""
        def _do():
            if self._closed:
                return
            import tkinter as tk
            self.log.configure(state="normal")
            if "pip_table_start" in self.log.mark_names():
                self.log.delete("pip_table_start", "pip_table_end")
            else:
                self.log.insert("end", "\n")
                self.log.mark_set("pip_table_start", "end-1c")
                self.log.mark_gravity("pip_table_start", tk.LEFT)
                self.log.insert("end", "\n")
                self.log.mark_set("pip_table_end", "end-1c")
                self.log.mark_gravity("pip_table_end", tk.RIGHT)

            self.log.insert("pip_table_start", table_text + "\n", "pip_row")
            self.log.configure(state="disabled")
            if getattr(self, "_auto_scroll_var", None) and self._auto_scroll_var.get():
                self.log.see("pip_table_end")
        self.root.after(0, _do)

    # ── Spinner ───────────────────────────────────────────────
    def _tick_spinner(self):
        if self._closed:
            return
        if self._spinning:
            self._spin_var.set(self.SPINNER[self._spin_idx % len(self.SPINNER)])
            self._spin_idx += 1
        self.root.after(90, self._tick_spinner)

    def stop_spinner(self, done_text: str = "Done", ok: bool = True):
        self._spinning = False
        self._spin_var.set("✔" if ok else "✖")
        self._status_var.set(done_text)
        if ok:
            self.set_step_progress(self.TOTAL_STEPS, self.TOTAL_STEPS)

    # ── Progress bar ───────────────────────────────────────────
    def set_step_progress(self, current: int, total: int):
        """Determinate progress across the overall setup steps."""
        def _do():
            if self._closed:
                return
            self._progress.stop()
            total = max(1, self._total_steps_safe())
            self._progress.configure(mode="determinate", maximum=total)
            self._progress["value"] = min(current, total)
        self.root.after(0, _do)

    def _total_steps_safe(self) -> int:
        return getattr(self, "TOTAL_STEPS", 14)

    def set_progress_percent(self, pct: int):
        """Determinate 0-100% progress, used for downloads with a known size."""
        def _do():
            if self._closed:
                return
            self._progress.configure(mode="determinate", maximum=100)
            self._progress["value"] = max(0, min(100, pct))
        self.root.after(0, _do)

    def start_busy(self):
        """Switch the progress bar into an indeterminate 'marquee' state
        while a subprocess (pip download/install, an installer, etc.) is
        doing real work but we can't get byte-level percentages out of it —
        this at least makes clear the app is alive and not frozen."""
        def _do():
            if self._closed:
                return
            # pip and Windows installers do not provide reliable byte-level
            # progress. Keep the real completed-setup-step bar visible
            # instead of showing a misleading bouncing animation.
            total = max(1, self._total_steps_safe())
            self._progress.stop()
            self._progress.configure(mode="determinate", maximum=total)
            self._progress["value"] = min(self._step_index, total)
        self.root.after(0, _do)

    def stop_busy(self, restore_step: bool = True):
        """Stop the marquee and go back to showing overall step progress."""
        def _do():
            if self._closed:
                return
            self._progress.stop()
            if restore_step:
                total = max(1, self._total_steps_safe())
                self._progress.configure(mode="determinate", maximum=total)
                self._progress["value"] = min(self._step_index, total)
        self.root.after(0, _do)

    # ── Thread-safe log append ────────────────────────────────
    def _append(self, text: str, tag: str = "normal"):
        def _do():
            if self._closed:
                return
            self.log.configure(state="normal")
            self.log.insert("end", text + "\n", tag)
            self.log.configure(state="disabled")
            if getattr(self, "_auto_scroll_var", None) and self._auto_scroll_var.get():
                self.log.see("end")
        self.root.after(0, _do)

    def set_status(self, text: str):
        def _do():
            if not self._closed:
                self._status_var.set(text)
        self.root.after(0, _do)

    # ── Public logging API ────────────────────────────────────
    def log_banner(self):
        self._append("=" * 56, "section")
        self._append("  ⚡  MCU Uploader IDE by Naph — Bootstrap", "section")
        self._append("=" * 56, "section")
        self._append("")

    def log_section(self, title: str):
        """A top-level step (e.g. 'Checking pyserial'). Advances the
        overall step progress bar."""
        self._step_index += 1
        self._append(f"\n── {title} ──", "section")
        self.set_status(title)
        self.set_step_progress(self._step_index, self.TOTAL_STEPS)

    def log_subsection(self, title: str):
        """A nested sub-step (e.g. 'Installing pyserial') — same styling
        as log_section but doesn't advance the step counter, since several
        of these can happen inside a single top-level step."""
        self._append(f"\n── {title} ──", "section")
        self.set_status(title)

    def log_pip_line(self, line: str):
        """One line of raw pip output, shown dim so it doesn't compete
        visually with our own ok/warn/fail lines."""
        self._append(f"    {line}", "dim")

    def log_status(self, msg: str):
        self._append(f"  ▸ {msg}", "normal")

    def log_ok(self, msg: str):
        self._append(f"  ✔ {msg}", "ok")

    def log_warn(self, msg: str):
        self._append(f"  ⚠ {msg}", "warn")

    def log_fail(self, msg: str):
        self._append(f"  ✖ {msg}", "fail")

    def log_update_notice(self, pkg: str, current: str, latest: str):
        self._append(f"  ↑  {pkg}: {current} → {latest}", "update")

    def log_up_to_date(self, pkg: str, version: str):
        self._append(f"  ✔ {pkg} {version} is up to date", "dim")

    def log_dim(self, msg: str):
        self._append(f"  – {msg}", "dim")

    def ask_update(self, count: int) -> bool:
        """
        Show a modal Yes/No dialog asking whether to install updates.
        Returns True if the user clicks Yes.
        Must be called from the Tk main thread (or via after()).
        """
        import tkinter.messagebox as mb
        return mb.askyesno(
            "Updates Available",
            f"{count} update(s) are available.\n\nInstall them now?",
            parent=self.root,
        )

    def show_error(self, title: str, msg: str):
        import tkinter.messagebox as mb
        mb.showerror(title, msg, parent=self.root)

    def _on_close(self):
        # Prevent closing during setup — user must wait for completion
        # or explicitly cancel via the Cancel button if provided
        import tkinter.messagebox as mb
        mb.showwarning(
            "Setup in Progress",
            "The setup process is running and cannot be closed.\n\n"
            "Please wait for it to complete, or use the Cancel button if available.",
            parent=self.root,
        )

    def pump(self):
        """Process pending Tk events without blocking."""
        try:
            self.root.update()
        except Exception:
            pass

    def mainloop_until_done(self):
        """Run Tk event loop until destroy() is called."""
        try:
            self.root.mainloop()
        except Exception:
            pass

    def close(self):
        if not self._closed:
            self._closed = True
            self._spinning = False
            # Disable window protocol to prevent callback loops during destroy
            try:
                self.root.protocol("WM_DELETE_WINDOW", lambda: None)
            except Exception:
                pass
            try:
                self.root.quit()
            except Exception:
                pass
            try:
                self.root.destroy()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────
# Console helpers — proxy to GUI when live, else plain print
# ─────────────────────────────────────────────────────────────
def banner():
    if _gui:
        _gui.log_banner()
    else:
        os.system("")
        print(f"\n{CYAN}{BOLD}{'=' * 56}")
        print(f"  ⚡  MCU Uploader IDE by Naph — Bootstrap")
        print(f"{'=' * 56}{RESET}\n")

def status(msg: str, color: str = CYAN):
    if _gui:
        _gui.log_status(msg)
    else:
        print(f"  {color}▸{RESET} {msg}")

def ok(msg: str):
    if _gui:
        _gui.log_ok(msg)
    else:
        print(f"  {GREEN}✔{RESET} {msg}")

def warn(msg: str):
    if _gui:
        _gui.log_warn(msg)
    else:
        print(f"  {YELLOW}⚠{RESET} {msg}")

def fail(msg: str):
    if _gui:
        _gui.log_fail(msg)
    else:
        print(f"  {RED}✖{RESET} {msg}")

def section(title: str):
    if _gui:
        _gui.log_subsection(title)
    else:
        print(f"\n  {CYAN}{BOLD}── {title} ──{RESET}")

def update_notice(pkg: str, current: str, latest: str):
    if _gui:
        _gui.log_update_notice(pkg, current, latest)
    else:
        print(f"  {YELLOW}↑{RESET}  {BOLD}{pkg}{RESET}: {DIM}{current}{RESET} → {GREEN}{latest}{RESET}")

def up_to_date(pkg: str, version: str):
    if _gui:
        _gui.log_up_to_date(pkg, version)
    else:
        print(f"  {GREEN}✔{RESET} {pkg} {DIM}{version}{RESET} is up to date")


# ═══════════════════════════════════════════════════════════════
# UPDATE CHECKER
# ═══════════════════════════════════════════════════════════════

def _fetch_url(url: str, timeout: int = 8) -> str | None:
    """Fetch a URL, return body text or None on any error."""
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "mcu-flash-gui-bootstrap/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _pip_installed_version(pkg_import: str, pkg_name: str) -> str | None:
    """Return the installed version string for a pip package, or None."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", pkg_name],
            capture_output=True, text=True, timeout=8,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        for line in result.stdout.splitlines():
            if line.lower().startswith("version:"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return None


def _pip_latest_version(pkg_name: str) -> str | None:
    """Query PyPI JSON API for the latest stable release of a package."""
    body = _fetch_url(f"https://pypi.org/pypi/{pkg_name}/json")
    if not body:
        return None
    try:
        data = json.loads(body)
        return data["info"]["version"]
    except Exception:
        return None


def _pip_upgrade(pkg_name: str) -> bool:
    """Upgrade a pip package, return True on success."""
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--upgrade", pkg_name, "--no-cache-dir", "--quiet"],
            stdout=sys.stdout, stderr=sys.stderr,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return True
    except Exception:
        return False


def _version_tuple(v: str) -> tuple:
    """Convert '1.2.3' to (1, 2, 3) for comparison, ignoring non-numeric parts."""
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def check_pip_package_update(pkg_name: str, pkg_import: str | None = None) -> dict:
    """
    Check one pip package for updates.
    Returns: {"name": str, "installed": str|None, "latest": str|None,
               "update_available": bool, "error": str|None}
    """
    installed = _pip_installed_version(pkg_import or pkg_name, pkg_name)
    if installed is None:
        return {"name": pkg_name, "installed": None, "latest": None,
                "update_available": False, "error": "not installed"}

    latest = _pip_latest_version(pkg_name)
    if latest is None:
        return {"name": pkg_name, "installed": installed, "latest": None,
                "update_available": False, "error": "could not reach PyPI"}

    update_available = _version_tuple(latest) > _version_tuple(installed)
    return {"name": pkg_name, "installed": installed, "latest": latest,
            "update_available": update_available, "error": None}


def _arduino_cli_installed_version() -> str | None:
    """Return the installed arduino-cli version string, e.g. '1.1.1'."""
    cli = find_arduino_cli()
    if not cli:
        return None
    try:
        result = subprocess.run(
            [cli, "version"],
            capture_output=True, text=True, timeout=8,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        # Output looks like: "arduino-cli  Version: 1.1.1 Commit: ..."
        import re
        m = re.search(r"Version:\s*([\d.]+)", result.stdout)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _arduino_cli_latest_version() -> str | None:
    """
    Query the latest version of arduino-cli.
    """
    # ── Strategy 1: GitHub releases redirect URL (extremely reliable & non-rate-limited) ──
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://github.com/arduino/arduino-cli/releases/latest",
            headers={"User-Agent": "mcu-flash-gui-bootstrap/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            final_url = r.geturl()
            # final_url looks like: https://github.com/arduino/arduino-cli/releases/tag/v1.5.1
            tag = final_url.split("/")[-1]
            if tag and tag.startswith("v"):
                return tag.lstrip("v")
    except Exception:
        pass

    # ── Strategy 2: arduino-cli upgrade (fallback) ──
    cli = find_arduino_cli()
    if cli:
        try:
            result = subprocess.run(
                [cli, "upgrade"],
                capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            # "arduino-cli X.Y.Z -> A.B.C" or "already up to date"
            import re
            m = re.search(r"(\d+\.\d+[\.\d]*)\s*$", result.stdout)
            if m:
                return m.group(1).strip()
        except Exception:
            pass

    # ── Strategy 3: GitHub releases API (fallback) ──
    body = _fetch_url("https://api.github.com/repos/arduino/arduino-cli/releases/latest")
    if body:
        try:
            data = json.loads(body)
            tag = data.get("tag_name", "")          # e.g. "v1.1.1"
            if tag:
                return tag.lstrip("v")
        except Exception:
            pass

    return None


def check_arduino_cli_update() -> dict:
    """Check arduino-cli for updates, tolerating unreachable GitHub."""
    installed = _arduino_cli_installed_version()
    if installed is None:
        return {"name": "arduino-cli", "installed": None, "latest": None,
                "update_available": False, "error": "not installed"}

    latest = _arduino_cli_latest_version()
    if latest is None:
        # Network is simply unreachable — not an error worth printing,
        # just skip silently so the bootstrap doesn't scare users.
        return {"name": "arduino-cli", "installed": installed, "latest": None,
                "update_available": False, "error": "network unavailable — skipping"}

    update_available = _version_tuple(latest) > _version_tuple(installed)
    return {"name": "arduino-cli", "installed": installed, "latest": latest,
            "update_available": update_available, "error": None}


def _pio_installed_version() -> str | None:
    """Return the installed platformio version string, e.g. '6.1.16'."""
    pio = find_pio()
    if not pio:
        return None
    try:
        cmd = list(pio)
        cmd.append("--version")
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=8,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        import re
        m = re.search(r"version\s*([\d.]+)", result.stdout, re.IGNORECASE)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _pio_upgrade() -> bool:
    """Upgrade PlatformIO Core using its built-in upgrade command."""
    pio = find_pio()
    if not pio:
        return False
    try:
        cmd = list(pio)
        cmd.append("upgrade")
        subprocess.check_call(
            cmd,
            stdout=sys.stdout, stderr=sys.stderr,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return True
    except Exception:
        return False


def check_pio_update() -> dict:
    """Check PlatformIO for updates."""
    installed = _pio_installed_version()
    if installed is None:
        return {"name": "platformio", "installed": None, "latest": None,
                "update_available": False, "error": "not installed"}

    latest = _pip_latest_version("platformio")
    if latest is None:
        return {"name": "platformio", "installed": installed, "latest": None,
                "update_available": False, "error": "could not reach PyPI"}

    update_available = _version_tuple(latest) > _version_tuple(installed)
    return {"name": "platformio", "installed": installed, "latest": latest,
            "update_available": update_available, "error": None}


def check_python_update() -> dict:
    """Check if a newer version of Python is available on winget."""
    import sys
    current_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    
    if sys.platform != "win32" or not shutil.which("winget"):
        return {"name": "python", "installed": current_ver, "latest": current_ver,
                "update_available": False, "error": None}
                
    try:
        # Run winget search silently
        import subprocess as sp
        res = sp.run(
            ["winget", "search", "Python.Python"],
            capture_output=True, text=True, timeout=20, shell=False,
            creationflags=sp.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        if res.returncode == 0:
            highest_minor = sys.version_info.minor
            highest_id = None
            for line in res.stdout.splitlines():
                if "Python.Python.3." in line:
                    parts = line.split()
                    for part in parts:
                        if part.startswith("Python.Python.3."):
                            try:
                                minor_ver = int(part.split(".")[-1])
                                if minor_ver > highest_minor:
                                    highest_minor = minor_ver
                                    highest_id = part
                            except Exception:
                                pass
            if highest_id:
                latest_ver_str = f"3.{highest_minor}"
                return {
                    "name": "python",
                    "installed": current_ver,
                    "latest": latest_ver_str,
                    "update_available": True,
                    "error": None,
                    "package_id": highest_id,
                }
    except FileNotFoundError:
        return {"name": "python", "installed": current_ver, "latest": current_ver,
                "update_available": False, "error": None}
    except Exception as e:
        return {"name": "python", "installed": current_ver, "latest": None,
                "update_available": False, "error": f"check failed: {e}"}
                
    return {"name": "python", "installed": current_ver, "latest": current_ver,
            "update_available": False, "error": None}


def _install_python_update(package_id: str) -> bool:
    """Install the newer Python package discovered through winget."""
    if sys.platform != "win32" or not package_id:
        return False
    status(f"Installing {package_id} with Windows Package Manager...")
    try:
        result = subprocess.run(
            [
                "winget", "install", "--id", package_id, "--exact", "--scope", "user",
                "--override", "/passive Include_tcltk=1 PrependPath=1 Include_test=0",
                "--accept-package-agreements", "--accept-source-agreements",
                "--disable-interactivity",
            ],
            capture_output=True, text=True, timeout=900,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if result.returncode in (0, 2316632107, -1978335205):
            ok(f"{package_id} is installed or already up to date.")
            return True
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        warn(f"winget could not install {package_id} (exit {result.returncode}).")
        if detail:
            warn(detail[-1])
    except FileNotFoundError:
        warn("winget is not available on this Windows installation.")
    except subprocess.TimeoutExpired:
        warn("Python installation timed out; it may still be running in the background.")
    except Exception as exc:
        warn(f"Could not install Python update: {exc}")
    return False


def run_update_checks(auto_update: bool = False):
    """
    Check all managed utilities for updates, running checks in parallel.
    If auto_update=True, upgrade pip packages automatically (arduino-cli
    requires a manual MSI re-run, so we only notify for that one).
    """
    cfg_skip = load_bootstrap_config().get("skip_updates", False)
    env_skip = os.environ.get("MCU_FLASH_GUI_SKIP_UPDATES", "").lower() in ("1", "true", "yes")

    if (_gui and getattr(_gui, "_skip_updates_var", None) and _gui._skip_updates_var.get()) or cfg_skip or env_skip:
        if _gui:
            _gui.log_dim("  ⏭  Skipping online update checks (Skip Updates / Offline mode enabled).")
        else:
            print("  ⏭  Skipping online update checks (Skip Updates / Offline mode enabled).")
        return

    section("Checking for updates")
    status("Querying PyPI, GitHub, and winget...", DIM)

    import threading

    results: dict[str, dict] = {}
    lock = threading.Lock()

    checks = [
        ("python",     lambda: check_python_update()),
        ("pip",        lambda: check_pip_package_update("pip")),
        ("pyserial",   lambda: check_pip_package_update("pyserial")),
        ("psutil",     lambda: check_pip_package_update("psutil")),
        ("pywebview",  lambda: check_pip_package_update("pywebview", "webview")),
        ("pywinpty",   lambda: check_pip_package_update("pywinpty", "winpty")),
        ("websockets", lambda: check_pip_package_update("websockets")),
        ("esptool",    lambda: check_pip_package_update("esptool")),
        ("platformio", lambda: check_pio_update()),
        ("arduino-cli",lambda: check_arduino_cli_update()),
    ]
    if sys.platform == "win32":
        checks.append(("pywin32", lambda: check_pip_package_update("pywin32")))

    def _run(key, fn):
        r = fn()
        with lock:
            results[key] = r

    threads = [threading.Thread(target=_run, args=(k, fn), daemon=True) for k, fn in checks]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)   # max 15 s total (network-bound)

    # ── Display results ──────────────────────────────────────
    updates_found: list[dict] = []
    skipped: list[str] = []

    # Print in a consistent order
    order = ["python", "pip", "pyserial", "psutil", "pywebview", "pywinpty", "websockets", "esptool", "platformio", "arduino-cli"]
    if sys.platform == "win32":
        order.append("pywin32")
    for key in order:
        r = results.get(key)
        if r is None:
            skipped.append(key)
            continue
        if r["error"] == "not installed":
            print(f"  {DIM}–  {key} not installed — skipping{RESET}")
            continue
        if r["error"]:
            # Distinguish "can't reach network" (dim, non-scary) from real errors
            if "network unavailable" in (r["error"] or "").lower() or \
               "skipping" in (r["error"] or "").lower():
                print(f"  {DIM}–  {key}: {r['error']}{RESET}")
            else:
                warn(f"{key}: {r['error']}")
            continue
        if r["update_available"]:
            update_notice(key, r["installed"], r["latest"])
            updates_found.append(r)
        else:
            up_to_date(key, r["installed"])

    if not updates_found:
        print(f"\n  {GREEN}All utilities are up to date.{RESET}")
        return

    # ── Prompt / auto-update ─────────────────────────────────
    pip_updates  = [r for r in updates_found if r["name"] not in ("arduino-cli", "python")]
    cli_updates  = [r for r in updates_found if r["name"] == "arduino-cli"]
    python_updates = [r for r in updates_found if r["name"] == "python"]

    print()
    if auto_update:
        answer = "y"
    elif _gui:
        # GUI path: ask via modal dialog on the main thread
        answer = "y" if _gui.ask_update(len(updates_found)) else "n"
    else:
        import threading
        if threading.current_thread().name == "fast-path-update-check":
            try:
                import tkinter as tk
                import tkinter.messagebox as mb
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                ans = mb.askyesno(
                    "Updates Available",
                    f"{len(updates_found)} update(s) are available for MCU Flash GUI components.\n\n"
                    "Would you like to install them now?",
                    parent=root
                )
                answer = "y" if ans else "n"
                root.destroy()
            except Exception:
                answer = "n"
        else:
            try:
                answer = input(
                    f"  {YELLOW}▸{RESET} {len(updates_found)} update(s) available. "
                    f"Install now? [{GREEN}y{RESET}/{RED}n{RESET}]: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "n"
                print()

    if answer == "y":
        for r in pip_updates:
            if r["name"] == "platformio":
                status(f"Upgrading platformio {r['installed']} → {r['latest']}...")
                if _pio_upgrade():
                    ok(f"platformio upgraded to {r['latest']}")
                else:
                    warn("Failed to upgrade platformio — continuing")
            else:
                status(f"Upgrading {r['name']} {r['installed']} → {r['latest']}...")
                if _pip_upgrade(r["name"]):
                    ok(f"{r['name']} upgraded to {r['latest']}")
                else:
                    warn(f"Failed to upgrade {r['name']} — continuing")

        if python_updates:
            r = python_updates[0]
            if _install_python_update(r.get("package_id", "")):
                # Purge old env virtual environment folder so the new Python version builds a fresh env
                (SCRIPT_DIR / ".force_rebuild").touch()
                try:
                    shutil.rmtree(SCRIPT_DIR / "env", ignore_errors=True)
                except Exception:
                    pass

                ok(f"Python updated ({r['installed']} → {r['latest']}). Purged old environment.")
                
                # Release single-instance lock slot before restarting
                _release_bootstrap_slot()
                
                try:
                    import tkinter as tk
                    import tkinter.messagebox as mb
                    root = tk.Tk()
                    root.withdraw()
                    root.attributes("-topmost", True)
                    mb.showinfo(
                        "Python Updated — Restarting",
                        f"Python has been updated from {r['installed']} to {r['latest']}.\n\n"
                        "The old environment was removed and the application will now restart automatically with the new Python version.",
                        parent=root
                    )
                    root.destroy()
                except Exception:
                    pass
                
                # Relaunch runThisOnWindows.vbs and exit current process
                vbs_launcher = SCRIPT_DIR / "runThisOnWindows.vbs"
                if vbs_launcher.exists():
                    try:
                        subprocess.Popen(["wscript.exe", str(vbs_launcher)], cwd=str(SCRIPT_DIR))
                    except Exception:
                        pass
                os._exit(0)
            else:
                warn("Python was not updated; the existing environment will be kept.")

        if cli_updates:
            r = cli_updates[0]
            cli_path = find_arduino_cli()
            upgraded = False

            # ── Strategy A: arduino-cli upgrade (built-in self-updater) ──
            if cli_path:
                status(f"Running arduino-cli upgrade {r['installed']} -> {r['latest']}...")
                try:
                    subprocess.check_call(
                        [cli_path, "upgrade"],
                        stdout=sys.stdout, stderr=sys.stderr,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                    )
                    ok(f"arduino-cli upgraded to {r['latest']}")
                    upgraded = True
                except Exception:
                    warn("arduino-cli upgrade command failed — falling back to MSI re-install...")

            # ── Strategy B: download fresh MSI then msiexec ──────────────
            if not upgraded:
                msi_path = SCRIPT_DIR / "installers" / "arduino-cli.msi"
                if _refresh_bundled_msi(r["latest"]):
                    if _run_arduino_cli_msi(msi_path):
                        ok(f"arduino-cli upgraded to {r['latest']} via MSI")
                        upgraded = True
                    else:
                        warn(
                            f"MSI install failed. Download manually:\n"
                            f"    {CYAN}https://arduino.github.io/arduino-cli/latest/installation/{RESET}"
                        )
                else:
                    warn(
                        f"Could not download MSI. Download manually:\n"
                        f"    {CYAN}https://arduino.github.io/arduino-cli/latest/installation/{RESET}"
                    )

            # ── Always refresh the bundled MSI copy ───────────────────────
            # Even when the self-updater succeeded, keep installers/arduino-cli.msi
            # current so the next fresh-machine install gets the new version.
            if upgraded:
                _refresh_bundled_msi(r["latest"])
                new_cli = find_arduino_cli()
                if new_cli:
                    _cache_arduino_cli_path(new_cli)
        
        import threading
        if threading.current_thread().name == "fast-path-update-check":
            try:
                import tkinter as tk
                import tkinter.messagebox as mb
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                mb.showinfo(
                    "Updates Installed",
                    "Updates for MCU Flash GUI components have been installed successfully.\n\n"
                    "Please restart the application to apply the updates.",
                    parent=root
                )
                root.destroy()
            except Exception:
                pass
    else:
        status("Skipping updates.", DIM)


# ═══════════════════════════════════════════════════════════════
# ENSURE FUNCTIONS (install if missing)
# ═══════════════════════════════════════════════════════════════

# ── 1. Ensure pip ────────────────────────────────────────────
def ensure_pip() -> bool:
    """Make sure pip is available in the current Python."""
    try:
        # pyrefly: ignore [missing-import]
        import pip
        ok("pip already installed")
        return True
    except ImportError:
        pass

    section("Installing pip")
    status("pip not found, bootstrapping...")

    # Try ensurepip first (works on most full installs)
    if _gui:
        _gui.start_busy()
    try:
        subprocess.check_call(
            [sys.executable, "-m", "ensurepip", "--upgrade"],
            stdout=sys.stdout, stderr=sys.stderr,
        )
        ok("pip installed via ensurepip")
        return True
    except Exception:
        pass
    finally:
        if _gui:
            _gui.stop_busy(restore_step=True)

    # For embeddable Python: need to enable pip by editing pth file
    # and downloading get-pip.py
    python_dir = Path(sys.executable).parent
    pth_files = list(python_dir.glob("python*._pth"))
    for pth in pth_files:
        content = pth.read_text(encoding="utf-8")
        if "#import site" in content:
            status("Enabling site-packages in embeddable Python...")
            content = content.replace("#import site", "import site")
            pth.write_text(content, encoding="utf-8")
            ok("Enabled import site in " + pth.name)

    # Prepare get-pip.py
    get_pip = python_dir / "get-pip.py"
    if not get_pip.exists():
        status("Preparing get-pip.py...")
        url = "https://bootstrap.pypa.io/get-pip.py"
        try:
            _download_file(url, get_pip)
        except Exception as e:
            fail(f"Failed to prepare get-pip.py: {e}")
            return False

    if _gui:
        _gui.start_busy()
    try:
        subprocess.check_call(
            [sys.executable, str(get_pip)],
            stdout=sys.stdout, stderr=sys.stderr,
        )
        ok("pip installed via get-pip.py")
        return True
    except Exception as e:
        fail(f"get-pip.py failed: {e}")
        return False
    finally:
        if _gui:
            _gui.stop_busy(restore_step=True)


# ── 2. Ensure pyserial ───────────────────────────────────────
def upgrade_pip() -> bool:
    """Update pip after it has been bootstrapped successfully."""
    section("Updating pip")
    status("Installing the latest compatible pip...")
    if _run_pip_install(["--upgrade", "pip"]):
        ok("pip is up to date")
        return True
    warn("Could not update pip; continuing with the installed version.")
    return False


def ensure_pyserial() -> bool:
    try:
        import serial  # noqa: F401
        import serial.tools.list_ports  # noqa: F401
        ok("pyserial already installed")
        return True
    except ImportError:
        pass

    status("pyserial not found, installing via pip...")

    section("Installing pyserial")
    try:
        if not _run_pip_install(["pyserial"]):
            raise RuntimeError("pip exited with a non-zero status")
        ok("pyserial installed successfully")
        return True
    except Exception as e:
        fail(f"Failed to install pyserial: {e}")
        return False


# ── 2b. Ensure psutil ────────────────────────────────────────
def ensure_psutil() -> bool:
    try:
        import psutil  # noqa: F401
        ok("psutil already installed")
        return True
    except ImportError:
        pass

    status("psutil not found, installing via pip...")

    section("Installing psutil")
    try:
        if not _run_pip_install(["psutil"]):
            raise RuntimeError("pip exited with a non-zero status")
        ok("psutil installed successfully")
        return True
    except Exception as e:
        fail(f"Failed to install psutil: {e}")
        return False


# ── 2c. Ensure pywin32 (Windows only) ─────────────────────────
def ensure_pywin32() -> bool:
    """pywin32 (win32gui/win32con) is used on Windows to embed the code
    editor's native window directly inside the main GUI window instead of
    it opening as a separate floating window. It's optional — if it's
    missing or fails to install, the editor just falls back to opening in
    its own window, so this is never treated as fatal."""
    if sys.platform != "win32":
        return True

    try:
        import win32gui  # noqa: F401
        import win32con  # noqa: F401
        ok("pywin32 already installed")
        return True
    except ImportError:
        pass

    status("pywin32 not found, installing via pip...")

    section("Installing pywin32")
    try:
        if not _run_pip_install(["pywin32"]):
            raise RuntimeError("pip exited with a non-zero status")
        ok("pywin32 installed successfully")
        return True
    except Exception as e:
        fail(f"Failed to install pywin32: {e}")
        return False


# ── 2c. Ensure esptool ───────────────────────────────────────
def ensure_esptool() -> bool:
    try:
        # pyrefly: ignore [missing-import]
        import esptool
        ok("esptool already installed")
        return True
    except ImportError:
        pass

    status("esptool not found, installing via pip...")

    section("Installing esptool")
    try:
        if not _run_pip_install(["esptool"]):
            raise RuntimeError("pip exited with a non-zero status")
        ok("esptool installed successfully")
        return True
    except Exception as e:
        fail(f"Failed to install esptool: {e}")
        return False


# ── 2d. Ensure pywebview ───────────────────────────────────────
def ensure_pywebview() -> bool:
    try:
        # pyrefly: ignore [missing-import]
        import webview  # noqa: F401
        ok("pywebview already installed")
        return True
    except ImportError:
        pass

    status("pywebview not found, installing via pip...")

    section("Installing pywebview")
    try:
        if not _run_pip_install(["pywebview"]):
            raise RuntimeError("pip exited with a non-zero status")
        ok("pywebview installed successfully")
        return True
    except Exception as e:
        fail(f"Failed to install pywebview: {e}")
        return False


# ── 2e. Ensure PyQt5 + QScintilla ─────────────────────────────
# ── Multithreaded Parallel Pip Package Manager ────────────────
# Fast import check using importlib.util.find_spec (no actual import overhead)
def _check_spec(import_name: str) -> bool:
    return importlib.util.find_spec(import_name) is not None

def _check_import_pyserial() -> bool:
    return _check_spec("serial") and _check_spec("serial.tools.list_ports")

def _check_import_psutil() -> bool:
    return _check_spec("psutil")

def _check_import_pywin32() -> bool:
    if sys.platform != "win32":
        return True
    return _check_spec("win32gui") and _check_spec("win32con")

def _check_import_esptool() -> bool:
    return _check_spec("esptool")

def _check_import_pywebview() -> bool:
    return _check_spec("webview")

def _check_import_pywinpty() -> bool:
    if sys.platform != "win32":
        return True
    return _check_spec("winpty")

def _check_import_websockets() -> bool:
    return _check_spec("websockets")

def _check_import_pyqt5_qscintilla() -> bool:
    return _check_spec("PyQt5.QtWidgets") and _check_spec("PyQt5.Qsci")

def _check_import_certifi() -> bool:
    return _check_spec("certifi")

PIP_PACKAGES_SPEC = [
    {
        "id": "pyserial",
        "name": "pyserial",
        "check": _check_import_pyserial,
        "pip_args": ["pyserial"],
        "critical": True,
    },
    {
        "id": "psutil",
        "name": "psutil",
        "check": _check_import_psutil,
        "pip_args": ["psutil"],
        "critical": True,
    },
    {
        "id": "pywin32",
        "name": "pywin32",
        "check": _check_import_pywin32,
        "pip_args": ["pywin32"],
        "critical": False,
    },
    {
        "id": "esptool",
        "name": "esptool",
        "check": _check_import_esptool,
        "pip_args": ["esptool"],
        "critical": True,
    },
    {
        "id": "pywebview",
        "name": "pywebview",
        "check": _check_import_pywebview,
        "pip_args": ["pywebview"],
        "critical": True,
    },
    {
        "id": "pywinpty",
        "name": "pywinpty",
        "check": _check_import_pywinpty,
        "pip_args": ["pywinpty"],
        "critical": False,
    },
    {
        "id": "websockets",
        "name": "websockets",
        "check": _check_import_websockets,
        "pip_args": ["websockets"],
        "critical": False,
    },
    {
        "id": "certifi",
        "name": "certifi",
        "check": _check_import_certifi,
        "pip_args": ["certifi"],
        "critical": True,
    },
    {
        "id": "pyqt5_qscintilla",
        "name": "PyQt5 / QScintilla",
        "check": _check_import_pyqt5_qscintilla,
        "pip_args": ["PyQt5", "QScintilla"],
        "critical": True,
    },
]


def ensure_pip_packages_parallel(gui: Optional[BootstrapGUI] = None) -> bool:
    """
    Multi-threaded parallel checking and installation of pip dependencies.
    Renders a live ASCII progress table with progress bars in the setup console log.
    """
    import concurrent.futures
    import threading
    import time
    import re

    # Filter applicable packages (e.g. skip pywin32 on non-Windows)
    active_specs = [
        spec for spec in PIP_PACKAGES_SPEC
        if not (spec["id"] == "pywin32" and sys.platform != "win32")
    ]

    pkg_states = {}
    for spec in active_specs:
        pkg_states[spec["id"]] = {
            "name": spec["name"],
            "status": "⏳ Checking...",
            "pct": 0,
            "done": False,
            "ok": False,
        }

    table_lock = threading.Lock()

    def _render_table():
        if not gui or getattr(gui, "_closed", False):
            return

        divider = "  " + "\u2500" * 52  # ─────────────────────────
        rows = [divider]
        for spec in active_specs:
            st = pkg_states[spec["id"]]
            pct = max(0, min(100, st["pct"]))
            filled = int(pct / 100 * 30)
            bar    = "\u25b0" * filled + "\u25b1" * (30 - filled)

            name_col   = f"  {st['name']:<22}"
            status_col = f"  {st['status']}"
            rows.append(name_col + status_col)
            rows.append(f"  {bar}  {pct:3d}%")
            rows.append(divider)

        gui.update_pip_table_block("\n".join(rows))

    # 1. Parallel Pre-Check
    def _do_precheck(spec):
        is_installed = spec["check"]()
        with table_lock:
            if is_installed:
                pkg_states[spec["id"]]["status"] = "✔ Installed"
                pkg_states[spec["id"]]["pct"] = 100
                pkg_states[spec["id"]]["done"] = True
                pkg_states[spec["id"]]["ok"] = True
            else:
                pkg_states[spec["id"]]["status"] = "⏳ Pending..."
                pkg_states[spec["id"]]["pct"] = 0
            _render_table()
        return is_installed

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(active_specs)) as executor:
        futures = [executor.submit(_do_precheck, spec) for spec in active_specs]
        concurrent.futures.wait(futures)

    missing_specs = [spec for spec in active_specs if not pkg_states[spec["id"]]["done"]]
    if not missing_specs:
        if gui:
            gui.log_ok("All pip package dependencies are verified!")
        return True

    # 2. Sequential Install missing packages (Single worker thread + Timeout + Retry protection to prevent Windows file-lock collisions)
    def _do_install(spec):
        sid = spec["id"]
        t0 = time.time()
        max_attempts = 3

        for attempt in range(1, max_attempts + 1):
            with table_lock:
                status_lbl = "📥 Downloading..." if attempt == 1 else f"📥 Retrying ({attempt}/{max_attempts})..."
                pkg_states[sid]["status"] = status_lbl
                pkg_states[sid]["pct"] = 20
                _render_table()

            cmd = [
                sys.executable, "-m", "pip", "install", *spec["pip_args"],
                "--disable-pip-version-check", "--prefer-binary",
                "--no-warn-script-location",
                "--retries", "3", "--timeout", "30",
            ]
            kwargs = dict(
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )

            try:
                proc = subprocess.Popen(cmd, **kwargs)
                if proc.stdout is not None:
                    for line in proc.stdout:
                        line = line.strip()
                        if not line:
                            continue
                        low = line.lower()
                        with table_lock:
                            if "downloading" in low:
                                m_sz = re.search(r'\(([\d\.]+\s*[kmg]?b)\)', line, re.IGNORECASE)
                                sz_str = f" ({m_sz.group(1)})" if m_sz else ""
                                pkg_states[sid]["status"] = f"📥 Downloading{sz_str}..."
                                m_pct = re.search(r'(\d+)\s*%', line)
                                if m_pct:
                                    pkg_states[sid]["pct"] = max(pkg_states[sid]["pct"], int(m_pct.group(1)))
                                else:
                                    pkg_states[sid]["pct"] = max(pkg_states[sid]["pct"], 40)
                            elif "installing" in low or "building wheel" in low or "preparing metadata" in low:
                                pkg_states[sid]["status"] = "⚙ Installing..."
                                pkg_states[sid]["pct"] = max(pkg_states[sid]["pct"], 80)
                            elif "successfully installed" in low:
                                pkg_states[sid]["status"] = "✔ Installed"
                                pkg_states[sid]["pct"] = 100
                            _render_table()

                proc.wait(timeout=120)
                dt = round(time.time() - t0, 1)

                if proc.returncode == 0 or spec["check"]():
                    with table_lock:
                        pkg_states[sid]["status"] = f"✔ Installed ({dt}s)"
                        pkg_states[sid]["pct"] = 100
                        pkg_states[sid]["done"] = True
                        pkg_states[sid]["ok"] = True
                        _render_table()
                    return True
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
            except Exception:
                pass

            if attempt < max_attempts:
                time.sleep(1)

        dt = round(time.time() - t0, 1)
        with table_lock:
            pkg_states[sid]["status"] = "✖ Install Failed"
            pkg_states[sid]["pct"] = 0
            pkg_states[sid]["done"] = True
            pkg_states[sid]["ok"] = False
            _render_table()
        return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        install_futures = [executor.submit(_do_install, spec) for spec in missing_specs]
        concurrent.futures.wait(install_futures)

    all_ok = True
    for spec in active_specs:
        sid = spec["id"]
        st = pkg_states[sid]
        if not st["ok"] and spec["critical"]:
            all_ok = False
            if gui:
                gui.log_fail(f"Critical pip package '{spec['name']}' failed to install.")

    if all_ok and gui:
        gui.log_ok("All pip package dependencies successfully installed & verified!")

    return all_ok



# ── 2f. Ensure Microsoft Edge WebView2 Runtime (Windows only) ─
# pywebview's default Windows backend (edgechromium) needs the WebView2
# Runtime installed system-wide — that's what actually renders the Monaco
# editor. It usually already ships with Windows 10/11 + Edge, but on some
# systems (older builds, stripped-down/enterprise images, or ones where
# Edge itself was removed) it can be missing, and pywebview's window then
# never finishes initializing.
_WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def check_webview2_runtime() -> bool:
    """Detect the Evergreen WebView2 Runtime using Microsoft's documented
    registry check: look up the 'pv' (product version) value under the
    WebView2 Runtime's EdgeUpdate client registration. Checked in the
    per-machine (WOW6432Node, for 64-bit Windows), per-machine (32-bit
    Windows), and per-user locations, since the runtime can be registered
    in any of the three depending on how it was installed."""
    if sys.platform != "win32":
        return True

    import winreg
    candidates = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\%s" % _WEBVIEW2_CLIENT_GUID),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\%s" % _WEBVIEW2_CLIENT_GUID),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\EdgeUpdate\Clients\%s" % _WEBVIEW2_CLIENT_GUID),
    ]
    for hive, subkey in candidates:
        try:
            key = winreg.OpenKey(hive, subkey)
            try:
                pv, _ = winreg.QueryValueEx(key, "pv")
                if pv and pv != "0.0.0.0":
                    return True
            finally:
                winreg.CloseKey(key)
        except (FileNotFoundError, OSError):
            continue
    return False


def ensure_webview2_runtime() -> bool:
    """Make sure the Microsoft Edge WebView2 Runtime is installed. Runs
    the bundled installers/MicrosoftEdgeWebview2Setup.exe silently if the
    runtime isn't detected. Never treated as fatal — if it's missing, the
    Monaco editor just won't start, everything else (compile/upload/serial)
    still works."""
    if sys.platform != "win32":
        return True

    if check_webview2_runtime():
        ok("Microsoft Edge WebView2 Runtime is already installed")
        return True

    section("Installing Microsoft Edge WebView2 Runtime")
    installer = SCRIPT_DIR / "installers" / "MicrosoftEdgeWebview2Setup.exe"

    if not _is_valid_exe(installer):
        warn(f"WebView2 Runtime installer is missing or invalid: {installer}")
        warn("Download it from https://developer.microsoft.com/microsoft-edge/webview2/ if the editor fails to start.")
        return False

    status("Launching WebView2 Runtime installer (silent)...")
    try:
        # /silent suppresses all UI; /install performs the actual install
        # (as opposed to the bootstrapper's default update-check behavior).
        result = subprocess.run(
            [str(installer), "/silent", "/install"],
            capture_output=True, text=True, timeout=180,
        )
        # The bootstrapper returns 0 on success. If the runtime was
        # actually already present (e.g. our registry check missed a
        # non-standard install location), it exits non-zero with an
        # "already installed" message — that's fine, not a real failure.
        already_installed = "already installed" in (result.stdout + result.stderr).lower()
        if result.returncode == 0 or already_installed or check_webview2_runtime():
            ok("Microsoft Edge WebView2 Runtime installed successfully")
            return True
        else:
            warn(f"WebView2 Runtime installer exited with code {result.returncode} "
                 f"— editor may not start.")
            return False
    except Exception as e:
        warn(f"Failed to run WebView2 Runtime installer: {e}")
        return False


# ── 3. Ensure PlatformIO ─────────────────────────────────────
def find_pio() -> list[str] | None:
    """
    Check if PlatformIO is available. Always uses  python -m platformio
    rather than the pio.exe / platformio.exe wrapper scripts — those are
    MSVC-compiled launchers that throw 0xc0000142 when the C++ runtime
    they were built against is missing on the target machine.
    """
    _cf = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    _pio_launcher = SCRIPT_DIR / "_pio_launcher.py"

    def _pio_cmd_for(py: Path) -> list[str]:
        if sys.platform == "win32" and _pio_launcher.exists():
            return [str(py), str(_pio_launcher)]
        return [str(py), "-m", "platformio"]

    def _py_has_platformio(py: Path) -> bool:
        try:
            if py.resolve() == Path(sys.executable).resolve():
                import importlib.util
                if importlib.util.find_spec("platformio") is not None:
                    return True
        except Exception:
            pass
        try:
            res = subprocess.run(
                [str(py), "-m", "platformio", "--version"],
                capture_output=True, timeout=15, creationflags=_cf,
            )
            return res.returncode == 0
        except Exception:
            return False

    # ── 1. Current interpreter (fastest) ──────────────────────────────────
    if _py_has_platformio(Path(sys.executable)):
        return _pio_cmd_for(Path(sys.executable))

    # ── 2. python.exe siblings in the venv Scripts / bin dir ──────────────
    python_dir = Path(sys.executable).parent
    for name in ["python.exe", "python3.exe", "python", "python3"]:
        for d in [python_dir, python_dir.parent / "Scripts", python_dir.parent / "bin"]:
            py_cand = d / name
            if py_cand.exists() and py_cand.resolve() != Path(sys.executable).resolve():
                if _py_has_platformio(py_cand):
                    return _pio_cmd_for(py_cand)

    # ── 3. PlatformIO's own embedded venv (PLATFORMIO_CORE_DIR/penv) ──────
    pio_core_dir = os.environ.get("PLATFORMIO_CORE_DIR")
    if pio_core_dir:
        pio_core_path = Path(pio_core_dir)
        for scripts_dir in [pio_core_path / "penv" / "Scripts", pio_core_path / "penv" / "bin"]:
            for name in ["python.exe", "python3.exe", "python", "python3"]:
                py_cand = scripts_dir / name
                if py_cand.exists():
                    if _py_has_platformio(py_cand):
                        return _pio_cmd_for(py_cand)

    return None


def ensure_platformio() -> bool:
    pio = find_pio()
    if pio:
        ok("PlatformIO already installed")
        return True

    status("PlatformIO not found, installing via pip...")
    status("This may take a few minutes on first run...")

    section("Installing PlatformIO Core")
    print()

    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "platformio"],
            stdout=sys.stdout, stderr=sys.stderr,
        )
        print()
        ok("PlatformIO Core installed successfully")
        return True
    except Exception as e:
        fail(f"Failed to install PlatformIO: {e}")
        return False


# ── 3b. Pre-install board toolchains ─────────────────────────
def _get_board_download_dir() -> Path:
    """Read the same download directory arduino_lib_req.py / mcu_flash_gui.py
    use, so bootstrap can see which board cores the user has downloaded via
    the Board Downloader. Mirrors mcu_flash_gui.py's _get_download_dir()."""
    settings_file = SCRIPT_DIR / "arduino_browser_settings.json"
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text(encoding="utf-8"))
            download_dir = settings.get("download_dir", "")
            if download_dir and os.path.isdir(download_dir):
                return Path(download_dir)
        except Exception:
            pass
    return Path(os.path.expanduser("~")) / "Documents" / "_MCUFlasherByNaph_src"


def _scan_downloaded_platforms() -> set[str]:
    """Scan <download_dir>/Boards for boards.txt files and map each
    downloaded board-core folder to its PlatformIO platform id. This is the
    same folder-name heuristic mcu_flash_gui.py's load_dynamic_boards() uses
    to populate SUPPORTED_BOARDS, kept in sync here so bootstrap knows which
    frameworks/toolchains it should pre-install."""
    platforms: set[str] = set()
    boards_path = _get_board_download_dir() / "Boards"
    if not boards_path.is_dir():
        return platforms
    try:
        for p in boards_path.glob("**/boards.txt"):
            parent_name = p.parent.name.lower()
            if "esp32" in parent_name:
                platforms.add("espressif32")
            elif "esp8266" in parent_name:
                platforms.add("espressif8266")
            elif "avr" in parent_name or "uno" in parent_name:
                platforms.add("atmelavr")
    except Exception:
        pass
    return platforms


# Friendly label + rough one-time download size shown while installing.
_PLATFORM_INFO = {
    "espressif32":   ("ESP32 / ESP32-S3", "~180 MB"),
    "espressif8266": ("ESP8266",          "~60 MB"),
    "atmelavr":      ("Arduino UNO / AVR", "~30 MB"),
}

# Toolchain package-folder substrings used to detect an existing install.
_PLATFORM_TOOLCHAIN_SUBSTRINGS = {
    "espressif32":   ("framework-arduinoespressif32", "toolchain-xtensa"),
    "espressif8266": ("framework-arduinoespressif8266", "toolchain-xtensa"),
    "atmelavr":      ("framework-arduino-avr", "toolchain-atmelavr"),
}


def _platform_already_installed(pio_core_dir: str, platform: str) -> bool:
    """Check if a PlatformIO platform AND all required toolchain/framework packages are installed.
    
    Uses multiple detection strategies for reliability:
    1. Check platform manifest exists (fast filesystem check)
    2. Check for all required toolchain and framework packages in PLATFORMIO_CORE_DIR or user profile
    3. Fallback: query PlatformIO directly via `pio platform list` (cached)
    """
    if not pio_core_dir:
        pio_core_dir = str(Path.home() / ".platformio")
    
    # Strategy 0: Check standalone downloaded AVR boards core (for atmelavr)
    if platform == "atmelavr":
        try:
            dest_dir = _get_board_download_dir() / "Boards"
            if dest_dir.is_dir():
                for p in dest_dir.glob("**/boards.txt"):
                    parent_name = p.parent.name.lower()
                    if "avr" in parent_name or "uno" in parent_name:
                        return True
        except Exception:
            pass

    # Strategy 1: Fast filesystem check - platform manifest should exist (handles versioned folders like espressif32@5.3.0)
    candidate_platform_dirs = [
        Path(pio_core_dir) / "platforms",
        Path(os.path.expanduser("~")) / ".platformio" / "platforms",
    ]
    for p_root in candidate_platform_dirs:
        if p_root.is_dir():
            try:
                for p_dir in p_root.iterdir():
                    if p_dir.is_dir() and (p_dir.name.lower() == platform.lower() or p_dir.name.lower().startswith(f"{platform.lower()}@")):
                        if (p_dir / "platform.json").exists():
                            return True
            except Exception:
                pass

    # Strategy 2: Check for toolchain packages in pio_core_dir or user home
    candidates_packages = [
        Path(pio_core_dir) / "packages",
        Path(os.path.expanduser("~")) / ".platformio" / "packages",
    ]
    required_substrings = _PLATFORM_TOOLCHAIN_SUBSTRINGS.get(platform, ())
    if required_substrings:
        all_packages: list[str] = []
        for packages_dir in candidates_packages:
            if packages_dir.exists():
                try:
                    all_packages.extend([p.name.lower() for p in packages_dir.iterdir() if p.is_dir()])
                except Exception:
                    pass

        if not all_packages:
            return False

        for sub in required_substrings:
            if not any(sub.lower() in pkg for pkg in all_packages):
                return False

        return True

    # Strategy 3: Fallback - query PlatformIO directly (cached)
    return _platform_installed_via_pio_cli(pio_core_dir, platform)


# Cache for PlatformIO CLI query results (avoid repeated subprocess calls)
_PIO_PLATFORM_LIST_CACHE: dict[str, bool] = {}


def _platform_installed_via_pio_cli(pio_core_dir: str, platform: str) -> bool:
    """Fallback: ask PlatformIO directly if platform is installed.
    Results are cached to avoid repeated subprocess calls."""
    cache_key = f"{pio_core_dir}:{platform}"
    if cache_key in _PIO_PLATFORM_LIST_CACHE:
        return _PIO_PLATFORM_LIST_CACHE[cache_key]
    
    pio = find_pio()
    if not pio:
        _PIO_PLATFORM_LIST_CACHE[cache_key] = False
        return False
    
    try:
        # Set the core dir so pio looks in the right place
        env = os.environ.copy()
        env["PLATFORMIO_CORE_DIR"] = pio_core_dir
        cmd = list(pio) + ["platform", "list", "--json-output"]
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=15,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if result.returncode == 0:
            import json
            data = json.loads(result.stdout)
            # data is a list of platform dicts with 'name' field
            installed = any(p.get("name") == platform for p in data if isinstance(p, dict))
            _PIO_PLATFORM_LIST_CACHE[cache_key] = installed
            return installed
    except Exception:
        pass
    
    _PIO_PLATFORM_LIST_CACHE[cache_key] = False
    return False


def ensure_board_toolchains() -> bool:
    """
    Pre-install the PlatformIO platform package (framework + toolchain) for
    every board platform the user actually has available -- ESP32 & ESP32-S3 always
    (this app's default targets), Arduino UNO (AVR) always, plus ESP8266/etc. for any board
    cores downloaded via the Board Downloader.

    Without this, PlatformIO fetches a newly-seen platform's framework and
    toolchain on-demand during the *first* compile for that board -- the
    multi-minute "Downloading/Installing core framework or tools..." wall
    of text that otherwise shows up mid-build. Doing it here means the GUI
    never has to sit through that; every later compile for that board is a
    pure incremental build.

    Idempotent per platform: anything already installed is skipped instantly.
    
    Set MCU_FLASH_GUI_SKIP_TOOLCHAINS=1 to skip entirely (dev/testing).
    """
    # Fast path: skip toolchain pre-install if requested via env var
    if SKIP_TOOLCHAINS:
        status("Skipping board toolchain pre-install (MCU_FLASH_GUI_SKIP_TOOLCHAINS=1)")
        return True
        
    pio = find_pio()
    if not pio:
        warn("PlatformIO not found — skipping board toolchain pre-install.")
        return False

    platforms = {"espressif32", "atmelavr"} | _scan_downloaded_platforms()
    pio_core_dir = os.environ.get("PLATFORMIO_CORE_DIR") or str(Path.home() / ".platformio")

    # First, filter to only platforms that aren't already installed.
    # Already-installed platforms are skipped silently so the user doesn't
    # see a flurry of "already installed" messages every single launch.
    platforms_to_install = []
    already_installed_labels: list[str] = []
    for platform in sorted(platforms):
        label, size_hint = _PLATFORM_INFO.get(platform, (platform, "a one-time"))

        if _platform_already_installed(pio_core_dir, platform):
            already_installed_labels.append(label)
        else:
            platforms_to_install.append((platform, label, size_hint))

    # Report already-installed platforms as a single consolidated line so
    # repeated launches stay clean and quiet.
    if already_installed_labels:
        joined = ", ".join(already_installed_labels)
        ok(f"Board toolchains already installed ({joined})")

    if not platforms_to_install:
        return True

    # Install missing platforms concurrently using ThreadPoolExecutor
    # to maximize download and installation speed across multiple board cores.
    all_ok = True
    if platforms_to_install:
        import concurrent.futures

        def _do_platform_install(item):
            platform, label, size_hint = item
            try:
                section(f"Preparing {label}")
                status(f"Preparing {platform} platform & core framework packages via PlatformIO...")
                status(f"This is a {size_hint} one-time setup. Subsequent launches skip this step.")

                cmd = list(pio) + ["platform", "install", platform]
                env = os.environ.copy()
                env["PLATFORMIO_NO_TELEMETRY"] = "1"
                env["PLATFORMIO_DISABLE_TELEMETRY"] = "1"
                res = subprocess.run(
                    cmd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True, text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )

                if res.returncode == 0 or _platform_already_installed(pio_core_dir, platform):
                    ok(f"{label} framework & toolchain installed successfully.")
                    return True
                else:
                    err_msg = (res.stderr or res.stdout or "Unknown error").strip()
                    warn(f"{platform} toolchain pre-install notice: {err_msg[:250]}")
                    warn("PlatformIO will auto-install core packages during build if required.")
                    return True  # Non-blocking: allow IDE setup to complete smoothly
            except Exception as e:
                warn(f"{platform} toolchain pre-install notice: {e}")
                return True

        if len(platforms_to_install) == 1:
            all_ok = _do_platform_install(platforms_to_install[0])
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(platforms_to_install))) as executor:
                futures = [executor.submit(_do_platform_install, item) for item in platforms_to_install]
                results = [f.result() for f in concurrent.futures.as_completed(futures)]
                all_ok = all(results)

    return all_ok


def ensure_arduino_avr_board() -> bool:
    """Pre-download and extract Arduino AVR Boards framework if not present."""
    dest_dir = _get_board_download_dir() / "Boards"
    if dest_dir.is_dir():
        for p in dest_dir.glob("**/boards.txt"):
            parent_name = p.parent.name.lower()
            if "avr" in parent_name or "uno" in parent_name:
                ok("Arduino AVR boards framework is already downloaded.")
                return True

    section("Preparing Arduino AVR Boards")
    status("Preparing Arduino AVR Boards core (v1.8.6) to enable AVR compilation...")

    candidate_urls = [
        "https://downloads.arduino.cc/cores/staging/avr-1.8.6.tar.bz2",
        "https://downloads.arduino.cc/cores/avr-1.8.6.tar.bz2",
        "https://downloads.arduino.cc/cores/avr-1.8.5.tar.bz2",
    ]

    dest_dir.mkdir(parents=True, exist_ok=True)
    download_success = False
    filepath = None
    last_err = None

    for url in candidate_urls:
        archive_name = url.rsplit("/", 1)[-1]
        candidate_path = dest_dir / archive_name
        try:
            _download_file(url, candidate_path)
            download_success = True
            filepath = candidate_path
            break
        except Exception as e:
            last_err = e

    if not download_success or not filepath:
        warn(f"Failed to setup/extract Arduino AVR Boards: {last_err}")
        return False

    try:
        status("Extracting Arduino AVR Boards...")
        folder_path = dest_dir / "avr-1.8.6"
        folder_path.mkdir(parents=True, exist_ok=True)

        import tarfile
        with tarfile.open(str(filepath), 'r:bz2') as tar_ref:
            tar_ref.extractall(str(folder_path))

        # Self-heal / flatten double nesting if present
        try:
            subdirs = [p for p in folder_path.iterdir() if p.is_dir()]
            files = [p for p in folder_path.iterdir() if p.is_file()]
            if len(subdirs) == 1 and len(files) == 0:
                nested = subdirs[0]
                for item in nested.iterdir():
                    shutil.move(str(item), str(folder_path))
                nested.rmdir()
        except Exception:
            pass

        try:
            filepath.unlink()
        except OSError:
            pass

        ok("Arduino AVR Boards framework configured and extracted successfully.")
        return True
    except Exception as e:
        warn(f"Failed to setup/extract Arduino AVR Boards: {e}")
        return False


# ── Download helper ───────────────────────────────────────────
def _download_file(url: str, dest: Path, timeout: int = 45, attempts: int = 3):
    """Download atomically with retries so failed downloads never poison an installer."""
    import urllib.request

    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    last_error = None
    if _gui:
        _gui.set_progress_percent(0)

    try:
        for attempt in range(1, attempts + 1):
            try:
                status(f"Downloading {dest.name} ({attempt}/{attempts})...")
                request = urllib.request.Request(
                    url, headers={"User-Agent": "mcu-flash-gui-bootstrap/1.0"}
                )
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    total = int(response.headers.get("Content-Length", "0") or 0)
                    received = 0
                    with open(partial, "wb") as output:
                        while True:
                            block = response.read(1024 * 256)
                            if not block:
                                break
                            output.write(block)
                            received += len(block)
                            if _gui:
                                if total:
                                    _gui.set_progress_percent(int(received * 100 / total))
                                else:
                                    _gui.start_busy()
                if received <= 0 or (total and received != total):
                    raise OSError(f"incomplete download ({received} of {total or 'unknown'} bytes)")
                os.replace(partial, dest)
                ok(f"Saved to {dest.name}")
                return
            except Exception as exc:
                last_error = exc
                try:
                    partial.unlink(missing_ok=True)
                except OSError:
                    pass
                if attempt < attempts:
                    warn(f"Download attempt {attempt} failed; retrying...")
                    time.sleep(attempt)
    finally:
        if _gui:
            _gui.stop_busy(restore_step=True)

    raise RuntimeError(f"download failed after {attempts} attempts: {last_error}")


# ── pip install helper (streams output + drives the progress bar) ─────
def _run_pip_install(args: list, quiet_ok_msg: str | None = None) -> bool:
    """
    Run `python -m pip install <args>`, hidden (no console flash), streaming
    pip's own output into the bootstrap log and switching the progress bar
    to a busy/marquee state for the duration.

    pip stops reporting a byte-level percentage as soon as it detects its
    output isn't a real terminal (which is always true here, since we pipe
    it), so a determinate bar isn't reliable for installs — the marquee at
    least shows the app is actively working instead of looking frozen,
    while the streamed lines give real visibility into what's happening.
    """
    if _gui:
        _gui.start_busy()

    try:
        for attempt in range(1, 3):
            cmd = [
                sys.executable, "-m", "pip", "install", *args,
                "--disable-pip-version-check", "--no-cache-dir", "--prefer-binary",
                "--retries", "3", "--timeout", "30",
            ]
            kwargs = dict(
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            try:
                proc = subprocess.Popen(cmd, **kwargs)
                if proc.stdout is not None:
                    for line in proc.stdout:
                        line = line.rstrip()
                        # pip's "Requirement already satisfied ... in
                        # C:\\Users\\..." line is not useful setup progress
                        # and exposes a long machine-specific directory in
                        # the bootstrap UI. The concise success line emitted
                        # by the caller already communicates the result.
                        if re.match(r"^Requirement already satisfied:", line, re.IGNORECASE):
                            continue
                        if line and _gui:
                            _gui.log_pip_line(line)
                if proc.wait() == 0:
                    return True
                if attempt == 1:
                    warn("pip install failed; retrying once...")
                    time.sleep(1)
            except Exception as exc:
                if attempt == 2:
                    fail(f"pip failed to launch: {exc}")
                else:
                    warn(f"pip launch failed; retrying once: {exc}")
                    time.sleep(1)
        return False
    finally:
        if _gui:
            _gui.stop_busy(restore_step=True)


# ── 4. Arduino-CLI ───────────────────────────────────────────

# Shared with mcu_flash_gui.py — whichever side finds arduino-cli first
# writes its path here so the other never has to search (or prompt) again.
ARDUINO_CLI_CACHE_FILE = SCRIPT_DIR / "arduino_cli_path.txt"


def _cache_arduino_cli_path(path: str) -> None:
    """Persist a known-good arduino-cli path so mcu_flash_gui.py finds it instantly."""
    try:
        ARDUINO_CLI_CACHE_FILE.write_text(path, encoding="utf-8")
    except Exception:
        pass


def _search_arduino_cli_install_dirs() -> str | None:
    """
    Look in every directory arduino-cli's Windows MSI is known to install
    into. The MSI's default target has varied across releases (plain
    Program Files vs. a per-user LOCALAPPDATA\\Programs folder), so a
    single hardcoded path isn't reliable — check them all.
    """
    if sys.platform != "win32":
        return None

    candidates = [
        r"C:\Program Files\Arduino CLI\arduino-cli.exe",
        r"C:\Program Files (x86)\Arduino CLI\arduino-cli.exe",
    ]

    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        candidates += [
            str(Path(local_app) / "Programs" / "Arduino CLI" / "arduino-cli.exe"),
            str(Path(local_app) / "Arduino CLI" / "arduino-cli.exe"),
            str(Path(local_app) / "Programs" / "arduino-cli" / "arduino-cli.exe"),
        ]

    for p in candidates:
        if Path(p).exists():
            return p

    # Last resort: ask Windows Installer directly where it put the product.
    # The MSI registers an install location under the uninstall registry
    # key even when the app isn't on PATH or in a "standard" folder.
    try:
        import winreg
        uninstall_roots = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for hive, subkey in uninstall_roots:
            try:
                key = winreg.OpenKey(hive, subkey)
            except OSError:
                continue
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    sub_name = winreg.EnumKey(key, i)
                    sub = winreg.OpenKey(key, sub_name)
                    name = winreg.QueryValueEx(sub, "DisplayName")[0]
                    if "arduino cli" not in name.lower():
                        continue
                    install_loc = winreg.QueryValueEx(sub, "InstallLocation")[0]
                    exe = Path(install_loc) / "arduino-cli.exe"
                    if exe.exists():
                        return str(exe)
                except OSError:
                    continue
    except Exception:
        pass

    return None


def find_arduino_cli() -> str | None:
    """Check if Arduino-CLI is already available."""
    # Fastest path: a location either bootstrap or the GUI already confirmed.
    if ARDUINO_CLI_CACHE_FILE.exists():
        try:
            cached = ARDUINO_CLI_CACHE_FILE.read_text(encoding="utf-8").strip()
            if cached and Path(cached).exists():
                return cached
        except Exception:
            pass

    cli = shutil.which("arduino-cli")
    if cli:
        return cli

    cli = _search_arduino_cli_install_dirs()
    if cli:
        return cli
    return None


def _arduino_cli_msi_url(version: str | None = None) -> str:
    """
    Return the direct GitHub download URL for the Windows arduino-cli MSI.

    If *version* is None the /latest/ redirect is used, which always
    resolves to the newest release without needing to know the tag up-front.
    Architecture is detected at runtime: arm64 gets the ARM64 build,
    everything else gets the 64-bit x86 build (the vast majority of PCs).
    """
    import platform
    machine = platform.machine().lower()
    if machine == "arm64":
        arch_label = "Windows_ARM64"
    else:
        arch_label = "Windows_64bit"

    if version:
        # Pinned URL: github.com/.../releases/download/vX.Y.Z/arduino-cli_vX.Y.Z_Windows_64bit.msi
        # Arduino uses both "v"-prefixed tags and bare version strings in the asset name.
        v = version.lstrip("v")
        return (
            f"https://github.com/arduino/arduino-cli/releases/download/"
            f"v{v}/arduino-cli_{arch_label}.msi"
        )
    else:
        # /latest/download/ resolves via a redirect — no tag needed.
        return (
            f"https://github.com/arduino/arduino-cli/releases/latest/download/"
            f"arduino-cli_{arch_label}.msi"
        )


def _refresh_bundled_msi(version: str | None = None) -> bool:
    """
    Download the latest (or a specific) arduino-cli MSI into
    SCRIPT_DIR/installers/arduino-cli.msi, replacing whatever is there.

    Returns True on success, False on any network/IO error.
    This keeps the bundled installer in sync so the next fresh-machine
    install gets the current version without a separate manual download.
    """
    msi_dir  = SCRIPT_DIR / "installers"
    msi_path = msi_dir / "arduino-cli.msi"
    url      = _arduino_cli_msi_url(version)

    try:
        msi_dir.mkdir(parents=True, exist_ok=True)
        status(f"Refreshing bundled MSI from GitHub{' v' + version if version else ' (latest)'}...")
        status(f"  {DIM}{url}{RESET}")
        _download_file(url, msi_path)
        if not _is_valid_msi(msi_path):
            raise RuntimeError("downloaded file is not a valid MSI package")
        ok(f"Bundled MSI updated → {msi_path.name}")
        return True
    except Exception as e:
        warn(f"Could not refresh bundled MSI: {e}")
        return False


def _is_valid_msi(path: Path) -> bool:
    """Reject empty, partial, or non-MSI downloads before invoking msiexec."""
    try:
        # MSI files are OLE compound documents and begin with this signature.
        if not path.is_file() or path.stat().st_size <= 1024 * 1024:
            return False
        with path.open("rb") as installer_file:
            return installer_file.read(8) == bytes.fromhex("D0CF11E0A1B11AE1")
    except OSError:
        return False


def _is_valid_exe(path: Path) -> bool:
    """Reject a truncated or substituted Windows executable before running it."""
    try:
        if not path.is_file() or path.stat().st_size < 64 * 1024:
            return False
        with path.open("rb") as installer_file:
            return installer_file.read(2) == b"MZ"
    except OSError:
        return False


_MSIEXEC_ERROR_CODES = {
    -2: "The Administrator permission (UAC) prompt was declined.",
    -1: "Could not launch or elevate msiexec.",
    5: "Access denied — the install needs to run elevated (as Administrator).",
    1601: "Windows Installer service could not be accessed.",
    1602: "User cancelled the installation.",
    1603: "Fatal error during installation (often: already installed, or a locked file).",
    1618: "Another installation is already in progress. Wait for it to finish and retry.",
    1619: "The installation package could not be opened — the .msi file may be missing or corrupt.",
    1620: "The installation package could not be opened — invalid or damaged .msi.",
    1633: "This installation package is not supported on this platform (check 32-bit vs 64-bit / ARM64).",
    3010: "Install succeeded but a reboot is required to finish.",
}


_LAST_ARDUINO_CLI_ERROR = ""


def get_last_arduino_cli_error() -> str:
    """Return the reason the most recent ensure_arduino_cli() call failed, if any."""
    return _LAST_ARDUINO_CLI_ERROR


def _run_msiexec(args: list[str]) -> tuple[int, str]:
    """
    Run msiexec with a verbose log file and return (exit_code, log_tail).

    On Windows this launches msiexec via ShellExecuteEx with the "runas"
    verb, which triggers the real UAC consent prompt. This matters because
    bootstrap/the GUI run hidden via pythonw.exe — a plain subprocess.run()
    call has no interactive desktop for Windows to silently elevate against,
    so an install that needs admin rights fails with Access Denied (return 5)
    and a rollback instead of ever prompting the user.
    """
    import tempfile
    log_path = Path(tempfile.gettempdir()) / "arduino_cli_msi_install.log"
    full_args = [*args, "/L*V", str(log_path)]

    if sys.platform == "win32":
        try:
            code = _shell_execute_elevated_wait("msiexec", full_args)
        except PermissionError as e:
            if str(e).startswith("UAC_DECLINED"):
                return -2, "The Administrator permission prompt (UAC) was declined."
            return -1, str(e)
        except Exception as e:
            return -1, f"Could not request elevation: {e}"
    else:
        try:
            result = subprocess.run(
                ["msiexec", *full_args],
                capture_output=True,
                text=True,
            )
            code = result.returncode
        except Exception as e:
            return -1, f"Could not launch msiexec: {e}"

    log_tail = ""
    try:
        if log_path.exists():
            text = log_path.read_text(encoding="utf-16", errors="replace")
            # Pull out the lines that actually explain the failure rather than
            # dumping the whole (often huge) verbose log.
            interesting = [
                ln.strip() for ln in text.splitlines()
                if ("error" in ln.lower() or "return value 3" in ln.lower())
                and ln.strip()
            ]
            log_tail = "\n".join(interesting[-8:])
    except Exception:
        pass

    return code, log_tail


def _shell_execute_elevated_wait(exe: str, args: list[str], timeout: float = 300.0) -> int:
    """
    Launch exe with args via ShellExecuteEx(verb='runas') so Windows shows
    the UAC consent prompt, then block until it exits and return its exit
    code.

    Raises PermissionError (message prefixed "UAC_DECLINED:") if the user
    clicked "No" on the elevation prompt, or OSError for any other failure
    to even launch the elevated process.
    """
    import ctypes
    from ctypes import wintypes

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_HIDE = 0

    class SHELLEXECUTEINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hKeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    params = subprocess.list2cmdline(args)
    sei = SHELLEXECUTEINFO()
    sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFO)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.hwnd = None
    sei.lpVerb = "runas"
    sei.lpFile = exe
    sei.lpParameters = params
    sei.lpDirectory = None
    sei.nShow = SW_HIDE
    sei.hInstApp = None

    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        err = ctypes.windll.kernel32.GetLastError()
        ERROR_CANCELLED = 1223
        if err == ERROR_CANCELLED:
            raise PermissionError(
                "UAC_DECLINED: the elevation (Administrator) prompt was declined."
            )
        raise OSError(f"ShellExecuteEx failed to launch {exe} (Win32 error {err}).")

    WAIT_TIMEOUT = 0x00000102
    result = ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, int(timeout * 1000))
    if result == WAIT_TIMEOUT:
        ctypes.windll.kernel32.TerminateProcess(sei.hProcess, 1)
        ctypes.windll.kernel32.CloseHandle(sei.hProcess)
        return 1

    exit_code = wintypes.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(exit_code))
    ctypes.windll.kernel32.CloseHandle(sei.hProcess)
    return exit_code.value


def _run_arduino_cli_msi(msi_path: Path) -> bool:
    """
    Run msiexec silently on the given MSI. Returns True on success.

    If the install fails with 1603 (the generic "fatal error" code — most
    often meaning Windows Installer already has this product registered
    even though the actual files are missing or damaged), automatically
    retry as a repair install before giving up. That single retry resolves
    the large majority of real-world 1603s without any user action.
    """
    global _LAST_ARDUINO_CLI_ERROR
    status("Running MSI installer (this may request elevation)...")

    code, detail = _run_msiexec(["/i", str(msi_path), "/quiet", "/norestart"])

    if code == 0:
        return True
    if code == 3010:
        warn("arduino-cli installed, but Windows wants a reboot to finish cleanly.")
        return True
    if code == -2:
        _LAST_ARDUINO_CLI_ERROR = (
            "The install needs Administrator permission. A Windows prompt should have "
            "appeared asking to allow this — please click 'Yes' when it shows up, then "
            "try again."
        )
        fail(_LAST_ARDUINO_CLI_ERROR)
        return False

    if code == 1603:
        warn("Install failed (1603) — product may already be registered with missing "
             "files. Retrying as a repair install...")
        # /fa = reinstall all files regardless of checksum/version, fixing the
        # "registered but files gone" case without needing a manual uninstall first.
        repair_code, repair_detail = _run_msiexec(["/fa", str(msi_path), "/quiet", "/norestart"])
        if repair_code in (0, 3010):
            ok("Repair install succeeded.")
            return True
        detail = repair_detail or detail
        code = repair_code if repair_code not in (0,) else code

    reason = _MSIEXEC_ERROR_CODES.get(code, "Unknown msiexec error.")
    _LAST_ARDUINO_CLI_ERROR = f"msiexec exit code {code}: {reason}"
    if detail:
        _LAST_ARDUINO_CLI_ERROR += f"\n{detail[:800]}"
    if code == 1603:
        _LAST_ARDUINO_CLI_ERROR += (
            "\n\nA repair install was attempted and also failed. Windows Installer "
            "likely still has an old arduino-cli registration pointing at files that "
            "no longer exist. Fix: open 'Add or Remove Programs', search for "
            "'Arduino CLI', remove it if listed, then retry. If it's not listed there, "
            "check Task Manager / antivirus isn't holding arduino-cli.exe locked, "
            "close it, and retry."
        )
    fail(f"msiexec failed (exit code {code}): {reason}")
    if detail:
        fail(f"  msiexec log: {detail[:800]}")
    return False


def ensure_arduino_cli() -> bool:
    """
    Make sure Arduino-CLI is installed.

    Priority:
      1. Already on PATH / known install dirs → nothing to do.
      2. Bundled MSI in installers/arduino-cli.msi → run it.
      3. No bundled MSI → download latest from GitHub, run it, then
         keep the downloaded copy as the new bundled MSI.
    """
    global _LAST_ARDUINO_CLI_ERROR
    _LAST_ARDUINO_CLI_ERROR = ""

    cli = find_arduino_cli()
    if cli:
        ok("Arduino-CLI is already installed")
        _cache_arduino_cli_path(cli)
        return True

    section("Installing Arduino-CLI")
    msi_path = SCRIPT_DIR / "installers" / "arduino-cli.msi"

    if not _is_valid_msi(msi_path):
        warn("Bundled MSI is missing or invalid — preparing latest from GitHub...")
        if not _refresh_bundled_msi():          # download into installers/
            _LAST_ARDUINO_CLI_ERROR = (
                "Could not prepare arduino-cli installer. "
                "Check your internet connection or place arduino-cli.msi "
                f"in {SCRIPT_DIR / 'installers'}."
            )
            fail(_LAST_ARDUINO_CLI_ERROR)
            return False

    if not _run_arduino_cli_msi(msi_path):
        # _LAST_ARDUINO_CLI_ERROR already set by _run_arduino_cli_msi with the real reason
        return False

    cli = find_arduino_cli()
    if cli:
        ok(f"Arduino-CLI installed successfully: {cli}")
        _cache_arduino_cli_path(cli)
        return True
    else:
        _LAST_ARDUINO_CLI_ERROR = (
            "msiexec reported success, but arduino-cli.exe still couldn't be located "
            "afterward. It may have installed to a non-standard folder — try 'Manually "
            "locate' and browse to it, or check %LOCALAPPDATA%\\Programs and "
            "C:\\Program Files for an 'Arduino CLI' folder."
        )
        fail("Arduino-CLI installation finished but executable could not be found.")
        return False


# ── 5. CP210x Driver ─────────────────────────────────────────
_CP210X_SENTINEL = SCRIPT_DIR / "logs" / ".cp210x_installed"


def _get_machine_id() -> str:
    """Return a stable identifier for the current machine so sentinel files
    written on one device are not mistakenly accepted on another when the
    project folder is copied/deployed across machines."""
    import platform
    return platform.node()


def _cp210x_sentinel_valid() -> bool:
    """Return True only if the sentinel file exists AND was written on this
    same machine (checked via hostname stored inside the file)."""
    try:
        if not _CP210X_SENTINEL.exists():
            return False
        content = _CP210X_SENTINEL.read_text(encoding="utf-8", errors="replace")
        # The sentinel's first line is "machine:<hostname>"
        for line in content.splitlines():
            if line.startswith("machine:"):
                return line.split(":", 1)[1].strip() == _get_machine_id()
        # Legacy sentinel without a machine line — treat as invalid so the
        # check runs properly on this device.
        return False
    except Exception:
        return False


def _cp210x_driver_in_store() -> bool:
    """Check if a CP210x driver package is staged in the Windows Driver Store.
    DPInst-based installers stage the .inf into the store; the actual .sys
    is only extracted into System32\\drivers when a matching device is
    plugged in and the OS loads the driver. So this check covers the
    'installed but no device connected yet' case."""
    try:
        import winreg
        # Walk HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Setup\PnpLockdownFiles
        # and the OEM*.inf files in %SystemRoot%\INF for 'silabser'
        windir = os.environ.get("SystemRoot", "C:\\Windows")
        inf_dir = Path(windir) / "INF"
        if inf_dir.is_dir():
            for inf in inf_dir.glob("oem*.inf"):
                try:
                    text = inf.read_text(encoding="utf-8", errors="replace")
                    if "silabser" in text.lower():
                        return True
                except Exception:
                    continue
    except Exception:
        pass
    return False


def check_cp210x_driver() -> bool:
    """Check if the Silicon Labs CP210x VCP driver is installed, staged, or
    was previously installed successfully by this bootstrap."""
    if sys.platform != "win32":
        return True

    # Fast path: a previous bootstrap run on *this machine* confirmed the install
    if _cp210x_sentinel_valid():
        return True

    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\silabser")

        # Check if the service is marked for deletion
        try:
            delete_flag, _ = winreg.QueryValueEx(key, "DeleteFlag")
            if delete_flag == 1:
                winreg.CloseKey(key)
                return False
        except FileNotFoundError:
            pass

        try:
            driver_delete, _ = winreg.QueryValueEx(key, "DriverDelete")
            if driver_delete == 1:
                winreg.CloseKey(key)
                return False
        except FileNotFoundError:
            pass

        # Check if the driver binary file actually exists on disk
        sys_file_exists = False
        try:
            image_path, _ = winreg.QueryValueEx(key, "ImagePath")
            winreg.CloseKey(key)

            if image_path:
                resolved_path = image_path
                if resolved_path.lower().startswith(r"\systemroot"):
                    windir = os.environ.get("SystemRoot", "C:\\Windows")
                    resolved_path = resolved_path.replace(r"\SystemRoot", windir).replace(r"\systemroot", windir)
                elif resolved_path.lower().startswith("system32"):
                    windir = os.environ.get("SystemRoot", "C:\\Windows")
                    resolved_path = os.path.join(windir, resolved_path)

                resolved_path = os.path.expandvars(resolved_path)
                sys_file_exists = os.path.exists(resolved_path)
        except Exception:
            windir = os.environ.get("SystemRoot", "C:\\Windows")
            default_sys_file = os.path.join(windir, "System32", "drivers", "silabser.sys")
            sys_file_exists = os.path.exists(default_sys_file)

        if sys_file_exists:
            return True

        # Registry key exists but .sys is missing — this is normal when the
        # driver package was staged by DPInst but no CP210x device has been
        # connected yet (Windows only copies the .sys on first device plug).
        # Check the Driver Store for a staged .inf package.
        if _cp210x_driver_in_store():
            return True

        return False
    except FileNotFoundError:
        return False


def ensure_cp210x() -> bool:
    """Make sure Silicon Labs CP210x VCP driver is installed."""
    if check_cp210x_driver():
        ok("CP210x driver is already installed")
        return True

    section("Installing CP210x Driver")
    driver_dir = SCRIPT_DIR / "installers" / "CP210x"

    import platform
    is_64bit = platform.machine().endswith("64") or sys.maxsize > 2**32
    installer = driver_dir / ("CP210xVCPInstaller_x64.exe" if is_64bit else "CP210xVCPInstaller_x86.exe")

    if not _is_valid_exe(installer):
        fail(f"CP210x driver installer is missing or invalid: {installer}")
        return False

    status("Launching CP210x driver installer...")
    try:
        exit_code = None
        if sys.platform == "win32":
            # Run DPInst elevated and silently. Suppress dialogs using /q /se
            exit_code = _shell_execute_elevated_wait(str(installer), ["/q", "/se"])
            # DPInst uses a bitfield return code:
            # - Bit 31 (0x80000000) is set on failure
            # - Other bits indicate successfully installed packages (e.g. exit code 1 or 2),
            #   copied packages (e.g. exit code 256), or reboot required (3010).
            is_success = False
            if exit_code in (0, 3010):
                is_success = True
            elif exit_code is not None and (exit_code & 0x80000000) == 0:
                is_success = True

            if not is_success:
                fail(f"CP210x installer exited with code {exit_code}.")
                return False
        else:
            proc = subprocess.run([str(installer)], check=True)
            exit_code = proc.returncode

        # Write sentinel so future bootstrap runs skip the installer.
        # DPInst stages the driver package into the Driver Store; the actual
        # .sys is only copied into System32\drivers when a CP210x device is
        # first plugged in, so check_cp210x_driver() may still return False
        # even after a fully successful install.
        try:
            _CP210X_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
            _CP210X_SENTINEL.write_text(
                f"machine:{_get_machine_id()}\n"
                f"CP210x driver installer completed successfully (exit code {exit_code}).\n",
                encoding="utf-8",
            )
        except Exception:
            pass

        if check_cp210x_driver():
            ok("CP210x driver installed successfully")
            return True
        else:
            # Sentinel was written — next run will skip. Inform the user.
            ok("CP210x driver package staged successfully")
            status("The driver will activate automatically when a CP210x device is connected.")
            return True
    except Exception as e:
        fail(f"Failed to run CP210x driver installer: {e}")
        return False


def ensure_python_system_environment() -> bool:
    """
    Permanently register Python and its Scripts directory in the Windows System Environment Variable
    (HKEY_LOCAL_MACHINE\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment\\Path) for
    global use by ALL USERS on the machine. Fall back to HKCU if elevation is declined.
    """
    current_python = Path(sys.executable).resolve()
    py_dir = current_python.parent
    scripts_dir = py_dir / "Scripts"

    venv_dir = SCRIPT_DIR / "env"
    venv_scripts = venv_dir / "Scripts" if sys.platform == "win32" else venv_dir / "bin"

    dirs_to_add = [str(py_dir)]
    if scripts_dir.is_dir():
        dirs_to_add.append(str(scripts_dir))
    if venv_scripts.is_dir():
        dirs_to_add.append(str(venv_scripts.resolve()))

    if sys.platform != "win32":
        ok("Python environment configuration checked.")
        return True

    status("Configuring Python System Environment Variables (Global / All Users)...")

    hklm_key_path = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
    hklm_updated = False

    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, hklm_key_path, 0, winreg.KEY_READ | winreg.KEY_WRITE)
        try:
            current_path, path_type = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current_path, path_type = "", winreg.REG_EXPAND_SZ

        existing_parts = [p.strip() for p in current_path.split(";") if p.strip()]
        existing_parts_lower = [p.lower() for p in existing_parts]

        added_any = False
        new_parts = list(existing_parts)

        for d in dirs_to_add:
            if d.lower() not in existing_parts_lower:
                new_parts.append(d)
                existing_parts_lower.append(d.lower())
                added_any = True

        if added_any:
            new_path = ";".join(new_parts)
            winreg.SetValueEx(key, "Path", 0, path_type, new_path)
            ok("Python directory permanently added to System Path (HKLM - Global / All Users)")
        else:
            ok("Python directory is already present in System Path (HKLM - Global / All Users)")

        # Set PYTHON_HOME system variable in HKLM for global tools
        try:
            winreg.SetValueEx(key, "PYTHON_HOME", 0, winreg.REG_SZ, str(py_dir))
        except Exception:
            pass

        winreg.CloseKey(key)
        hklm_updated = True
    except PermissionError:
        warn("Direct HKLM registry access requires Administrator privileges; updating User environment (HKCU).")
    except Exception as reg_err:
        warn(f"Error checking HKLM registry: {reg_err}")

    # Fallback to HKCU if HKLM modification could not be performed
    if not hklm_updated:
        try:
            import winreg
            hkcu_key_path = r"Environment"
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, hkcu_key_path, 0, winreg.KEY_READ | winreg.KEY_WRITE)
            try:
                current_path, path_type = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                current_path, path_type = "", winreg.REG_EXPAND_SZ

            existing_parts = [p.strip() for p in current_path.split(";") if p.strip()]
            existing_parts_lower = [p.lower() for p in existing_parts]

            added_any = False
            new_parts = list(existing_parts)

            for d in dirs_to_add:
                if d.lower() not in existing_parts_lower:
                    new_parts.append(d)
                    existing_parts_lower.append(d.lower())
                    added_any = True

            if added_any:
                new_path = ";".join(new_parts)
                winreg.SetValueEx(key, "Path", 0, path_type, new_path)
                ok("Python directory added to User Path (HKCU - Current User Fallback)")
            else:
                ok("Python directory is already present in User Path (HKCU)")

            winreg.CloseKey(key)
        except Exception as hkcu_err:
            warn(f"Could not update User environment variable: {hkcu_err}")

    # Broadcast WM_SETTINGCHANGE so system and active windows pick up environment updates immediately
    try:
        import ctypes
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 5000, ctypes.byref(result)
        )
    except Exception:
        pass

    return True





# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════
def _relaunch_visible_if_hidden():
    """
    No-op: the VBS now launches bootstrap via pythonw.exe and the GUI
    provides its own visible window — no console re-launch is needed.
    Kept for compatibility in case it is called elsewhere.
    """
    pass


def _is_env_healthy() -> bool:
    """
    Fast-path check: returns True only when the venv exists, the current
    interpreter IS the venv interpreter, and all required packages import
    cleanly.  If True, bootstrap can skip all setup work and launch the GUI
    immediately without showing the bootstrap window at all.
    """
    venv_dir = SCRIPT_DIR / "env"
    try:
        current_exe = Path(sys.executable).resolve()
        if venv_dir.resolve() not in current_exe.parents:
            return False
    except Exception:
        return False

    try:
        import serial  # noqa: F401
        import serial.tools.list_ports  # noqa: F401
        # pyrefly: ignore [missing-import]
        import platformio  # noqa: F401
        # pyrefly: ignore [missing-import]
        import esptool  # noqa: F401
        # pyrefly: ignore [missing-import]
        import webview  # noqa: F401
        # pyrefly: ignore [missing-import]
        import psutil  # noqa: F401
        # pyrefly: ignore [missing-import]
        import certifi  # noqa: F401
        # pyrefly: ignore [missing-import]
        import websockets  # noqa: F401
        # pyrefly: ignore [missing-import]
        from PyQt5 import QtWidgets, Qsci  # noqa: F401
        if sys.platform == "win32":
            import win32gui  # noqa: F401
            import win32con  # noqa: F401
            # pyrefly: ignore [missing-import]
            import winpty  # noqa: F401
    except ImportError:
        return False

    return True


def _spawn_main_gui() -> "tuple[subprocess.Popen | None, Path | None]":
    """
    Launch MCU Flasher.exe (if exists) or mcu_flash_gui.py as a detached process.
    Returns a tuple: (Popen object or None, Log Path or None)
    """
    import subprocess as sp

    # Skip running the wrapper EXE to prevent relaunch loop; always run Python GUI script


    if not GUI_SCRIPT.exists():
        return None, None

    logs_dir = SCRIPT_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    gui_log = logs_dir / "gui_crash.log"
    log_fh = None

    if sys.platform == "win32":
        # Prefer venv python (env/Scripts/pythonw.exe or python.exe) where all pip dependencies live
        venv_dir = SCRIPT_DIR / "env"
        venv_pythonw = venv_dir / "Scripts" / "pythonw.exe"
        venv_python  = venv_dir / "Scripts" / "python.exe"

        if venv_pythonw.exists():
            python_exe = venv_pythonw
        elif venv_python.exists():
            python_exe = venv_python
        else:
            python_exe = Path(sys.executable).parent / "pythonw.exe"
            if not python_exe.exists():
                python_exe = Path(sys.executable).parent / "python.exe"
            if not python_exe.exists():
                python_exe = Path(sys.executable)

        # Try to find a non-locked log file name to support multiple concurrent windows
        for i in range(10):
            suffix = "" if i == 0 else f"_{i}"
            candidate_log = logs_dir / f"gui_crash{suffix}.log"
            try:
                candidate_log.write_text("", encoding="utf-8")
                log_fh = open(candidate_log, "w", encoding="utf-8")
                gui_log = candidate_log
                break
            except Exception:
                continue

        if log_fh is None:
            try:
                log_fh = open(os.devnull, "w", encoding="utf-8")
            except Exception:
                log_fh = open(os.devnull, "w")
        
        # Override inherited hidden window state to ensure the spawned GUI displays normally
        startupinfo = sp.STARTUPINFO()
        startupinfo.dwFlags |= sp.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 1  # SW_SHOWNORMAL
        
        try:
            proc = sp.Popen(
                [str(python_exe), str(GUI_SCRIPT), "--from-bootstrap"],
                cwd=str(SCRIPT_DIR),
                stdin=sp.DEVNULL,   # never inherit a dead/closed handle from pythonw
                stderr=log_fh,
                stdout=log_fh,
                startupinfo=startupinfo,
                # DETACHED_PROCESS breaks the parent-console inheritance chain
                # so the main GUI is a fully independent top-level process.
                creationflags=sp.DETACHED_PROCESS,
            )
        finally:
            log_fh.close()
    else:
        proc = sp.Popen(
            [sys.executable, str(GUI_SCRIPT), "--from-bootstrap"],
            cwd=str(SCRIPT_DIR),
        )

    return proc, gui_log


def _activate_bootstrap_venv(venv_dir: Path, venv_python: Path) -> bool:
    """Point this bootstrap process at an existing venv without reopening its UI."""
    if not venv_python.exists():
        return False
    # Verify that pip inside the virtual environment is intact and functional
    try:
        res = subprocess.run(
            [str(venv_python), "-c", "import pip; from pip._internal.cli.main import main"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if res.returncode != 0:
            return False
    except Exception:
        return False
    try:
        if sys.platform == "win32":
            site_packages = venv_dir / "Lib" / "site-packages"
            scripts_dir = venv_dir / "Scripts"
        else:
            site_packages = next((venv_dir / "lib").glob("python*/site-packages"))
            scripts_dir = venv_dir / "bin"
        if site_packages.is_dir():
            site_path = str(site_packages)
            if site_path in sys.path:
                sys.path.remove(site_path)
            sys.path.insert(0, site_path)
        os.environ["VIRTUAL_ENV"] = str(venv_dir)
        os.environ["PATH"] = str(scripts_dir) + os.pathsep + os.environ.get("PATH", "")
        # All bootstrap subprocesses use this value, so pip and the final
        # GUI launch now target env without starting a second bootstrap GUI.
        sys.executable = str(venv_python)
        return True
    except Exception:
        return False


def _run_setup_in_thread(gui: BootstrapGUI):
    """
    Runs all dependency checks on a background thread so the Tk event loop
    stays responsive (spinner keeps animating, window stays draggable).

    When complete, posts a callback to the main thread to close the
    bootstrap window and launch the main GUI.
    """
    global _gui

    try:
        if sys.platform == "win32":
            try:
                # pyrefly: ignore [missing-import]
                from win_subprocess_hide import install_venv_site_hook
                install_venv_site_hook(SCRIPT_DIR)
            except Exception:
                pass

        # ── Python System Environment Variable (Global Base Python) ────
        gui.root.after(0, lambda: gui.log_section("Checking Python System Environment"))
        if sys.platform == "win32" and not ensure_python_system_environment():
            gui.root.after(0, lambda: gui.log_warn("Could not permanently update System environment variables."))

        # ── Venv setup ────────────────────────────────────────────────
        venv_dir = SCRIPT_DIR / "env"
        if sys.platform == "win32":
            venv_python = venv_dir / "Scripts" / "python.exe"
        else:
            venv_python = venv_dir / "bin" / "python"

        current_python = Path(sys.executable).resolve()
        target_python = venv_python.resolve() if venv_python.exists() else None

        is_in_venv = False
        try:
            is_in_venv = venv_dir.resolve() in current_python.parents
        except Exception:
            pass

        # When bootstrap was started by a system Python but env already
        # exists, keep this window and switch its child commands to env.
        if not is_in_venv and venv_python.exists():
            gui.root.after(0, lambda: gui.log_section("Python Environment"))
            if _activate_bootstrap_venv(venv_dir, venv_python):
                gui.root.after(0, lambda: gui.log_ok("Existing env folder found; using it."))
                is_in_venv = True
            else:
                gui.root.after(0, lambda: gui.log_warn(
                    "Existing env could not be activated; it will be recreated."
                ))

        if not is_in_venv:
            is_portable = (SCRIPT_DIR / "_python").resolve() in current_python.parents

            gui.root.after(0, lambda: gui.log_section("Setting up Python Environment"))

            # Remove any leftover corrupted env folder so venv.create()
            # doesn't hang on a half-baked directory (most common cause of
            # "stuck at Creating virtual environment" reports).
            if venv_dir.exists():
                gui.root.after(0, lambda: gui.log_status("Removing stale env folder…"))
                try:
                    shutil.rmtree(venv_dir, ignore_errors=True)
                except Exception:
                    pass

            venv_created = False
            try:
                import venv as _venv
                gui.root.after(0, lambda: gui.log_status("Creating virtual environment in 'env'…"))

                # Direct single-pass creation with_pip=True (fastest on Windows & standard Python)
                try:
                    _venv.create(str(venv_dir), with_pip=True, clear=True,
                                 symlinks=sys.platform != "win32")
                    gui.root.after(0, lambda: gui.log_ok("Virtual environment created."))
                    venv_created = True
                except Exception:
                    # Fallback: create skeleton with with_pip=False, then run ensurepip
                    gui.root.after(0, lambda: gui.log_status("Creating virtual environment skeleton…"))
                    _venv.create(str(venv_dir), with_pip=False, clear=True,
                                 symlinks=sys.platform != "win32")
                    gui.root.after(0, lambda: gui.log_ok("Virtual environment skeleton created."))
                    gui.root.after(0, lambda: gui.log_status("Bootstrapping pip inside venv…"))
                    try:
                        subprocess.run(
                            [str(venv_python), "-m", "ensurepip", "--default-pip"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                        )
                    except Exception:
                        pass
                    gui.root.after(0, lambda: gui.log_ok("Virtual environment created."))
                    venv_created = True
            except Exception as venv_error:
                gui.root.after(0, lambda venv_error=venv_error: gui.log_warn(
                    f"Built-in venv setup failed ({venv_error}); trying the Python command fallback."
                ))
                # Clean up partial/broken env before the subprocess attempt
                try:
                    shutil.rmtree(venv_dir, ignore_errors=True)
                except Exception:
                    pass
                if is_portable:
                    gui.root.after(0, lambda: gui.log_status("Embeddable Python — installing directly…"))
                else:
                    try:
                        gui.root.after(0, lambda: gui.log_status("Creating venv via subprocess…"))
                        subprocess.check_call(
                            [sys.executable, "-m", "venv", "--without-pip", str(venv_dir)],
                            timeout=60,
                            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                        )
                        gui.root.after(0, lambda: gui.log_ok("Virtual environment created (subprocess fallback)."))
                        venv_created = True
                    except Exception:
                        gui.root.after(0, lambda: gui.log_fail("Could not create virtual environment."))
                        if not is_portable:
                            gui.root.after(0, lambda: gui.log_warn(
                                "Installing into global Python (recommend Python with venv support)."))
                        else:
                            gui.root.after(0, lambda: gui.log_ok("Proceeding with portable Python."))

            if venv_created:
                try:
                    reset_script = SCRIPT_DIR / "src" / "libs" / "reset_editor.py"
                    if reset_script.exists():
                        subprocess.run(
                            [sys.executable, str(reset_script)],
                            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                        )
                except Exception:
                    pass

                if sys.platform == "win32":
                    try:
                        # pyrefly: ignore [missing-import]
                        from win_subprocess_hide import install_venv_site_hook
                        install_venv_site_hook(SCRIPT_DIR)
                    except Exception:
                        pass

                if not venv_python.exists():
                    def _no_venv():
                        gui.log_fail(f"venv Python not found at {venv_python}")
                        gui.stop_spinner("Setup failed", ok=False)
                        gui.show_error("MCU Upload GUI — Error",
                                       f"Virtual environment Python not found:\n{venv_python}")
                        gui.close()
                    gui.root.after(0, _no_venv)
                    return

                if _activate_bootstrap_venv(venv_dir, venv_python):
                    gui.root.after(0, lambda: gui.log_ok(
                        "env folder created; continuing setup in this window."
                    ))
                else:
                    def _venv_activate_fail():
                        gui.log_fail(f"Could not activate env at {venv_python}")
                        gui.stop_spinner("Setup failed", ok=False)
                        gui.show_error("MCU Upload GUI — Error",
                                       f"Virtual environment could not be activated:\n{venv_python}")
                        gui.close()
                    gui.root.after(0, _venv_activate_fail)
                    return

        def _fail_and_exit(component: str, detail: str = ""):
            def _on_gui():
                gui.log_fail(f"Installation/Setup failed for {component}.")
                if detail:
                    gui.log_fail(f"  {detail}")
                gui.log_fail("Setup aborted. Application cannot proceed.")
                gui.stop_spinner("Setup failed", ok=False)
                err_msg = f"Failed to download/install {component}.\n\n"
                if detail:
                    err_msg += f"Details: {detail}\n\n"
                err_msg += "Setup cannot proceed. Please resolve the error and try again."
                gui.show_error("MCU Uploader IDE by Naph — Setup Error", err_msg)
                gui.close()
            gui.root.after(0, _on_gui)

        # ── pip & Python Dependencies (Multithreaded Parallel Install) ─────
        gui.root.after(0, lambda: gui.log_section("Checking pip & Python Dependencies"))
        if not ensure_pip():
            _fail_and_exit("pip", "pip could not be installed.")
            return

        if not ensure_pip_packages_parallel(gui):
            _fail_and_exit("Python Dependencies", "One or more required pip packages failed to install.")
            return

        # ── WebView2 Runtime (Windows only, needed by pywebview to render
        #    the Monaco editor) ────────────────────────────────────────
        gui.root.after(0, lambda: gui.log_section("Checking Microsoft Edge WebView2 Runtime"))
        if sys.platform == "win32" and not ensure_webview2_runtime():
            _fail_and_exit("Microsoft Edge WebView2 Runtime", "Microsoft Edge WebView2 Runtime is missing or could not be verified.")
            return

        # ── PlatformIO + Board Toolchains (combined step) ───────────────
        gui.root.after(0, lambda: gui.log_section("Checking PlatformIO & Board Toolchains"))
        
        # Check PlatformIO
        if not ensure_platformio():
            _fail_and_exit("PlatformIO Core", "Failed to install PlatformIO Core.")
            return

        # Check Arduino AVR Boards (for AVR platform)
        if not ensure_arduino_avr_board():
            _fail_and_exit("Arduino AVR Boards", "Failed to download or extract Arduino AVR Boards core.")
            return

        # Pre-install board toolchains (ESP32 + any downloaded boards)
        # Uses parallel installation for multiple platforms
        if not ensure_board_toolchains():
            _fail_and_exit("Board Toolchains", "Failed to pre-install required board toolchain packages.")
            return

        # ── Arduino-CLI ──────────────────────────────────────────────
        gui.root.after(0, lambda: gui.log_section("Checking Arduino-CLI"))
        if not ensure_arduino_cli():
            _fail_and_exit("Arduino-CLI", "Failed to install Arduino-CLI.")
            return

        # ── CP210x Driver ─────────────────────────────────────────────
        gui.root.after(0, lambda: gui.log_section("Checking CP210x Driver"))
        if sys.platform == "win32" and not ensure_cp210x():
            _fail_and_exit("CP210x Driver", "Failed to run CP210x driver installer.")
            return

        # ── Refresh Python System Environment Variable (including env) ─
        gui.root.after(0, lambda: gui.log_section("Updating Python System Environment"))
        if sys.platform == "win32" and not ensure_python_system_environment():
            gui.root.after(0, lambda: gui.log_warn("Could not permanently update System environment variables."))

        # ── Update checks ─────────────────────────────────────────────
        run_update_checks(auto_update=False)

        # ── Summary & launch ─────────────────────────────────────────
        def _finish():
            gui.log_ok("All dependencies ready!")

            exe_path = SCRIPT_DIR / "MCU Flasher.exe"
            if not exe_path.exists() and not GUI_SCRIPT.exists():
                gui.log_fail(f"Application target not found in {SCRIPT_DIR}")
                gui.stop_spinner("GUI target missing", ok=False)
                gui.show_error("MCU Uploader IDE by Naph — Error",
                               f"Target application not found in:\n{SCRIPT_DIR}")
                gui.close()
                return

            gui.log_status("Launching MCU Uploader IDE by Naph…")
            gui.stop_spinner("Launching…", ok=True)

        gui.root.after(0, _finish)

        # Small delay so the user can read "Launching…" before the window closes
        time.sleep(1.2)

        # Hide Bootstrap before the main process is spawned. The old flow
        # waited for the main GUI's crash check before closing this window,
        # which let the Project Selector appear while Bootstrap was still
        # visibly disposing. Keep the crash check, but remove that overlap.
        import threading
        bootstrap_hidden = threading.Event()

        def _hide_bootstrap_before_launch():
            try:
                gui.root.withdraw()
            finally:
                bootstrap_hidden.set()

        gui.root.after(0, _hide_bootstrap_before_launch)
        bootstrap_hidden.wait(timeout=2.0)

        proc, gui_log = _spawn_main_gui()

        def _launch_done(proc=proc, gui_log=gui_log):
            if proc is None:
                gui.stop_spinner("GUI target missing", ok=False)
                gui.show_error("MCU Uploader IDE by Naph — Error",
                               f"Target application not found in:\n{SCRIPT_DIR}")
                gui.close()
                return

            # Brief wait — check if it exited right away
            for _ in range(4):
                time.sleep(0.5)
                if proc.poll() is not None:
                    break

            exit_code = proc.poll()
            if exit_code is not None:
                # Exit code 0 means clean exit (e.g. user cancelled project selector).
                # Non-zero exit means an actual crash.
                if exit_code == 0:
                    # Clean exit — just close the bootstrap window quietly
                    try:
                        if gui_log and gui_log.exists() and gui_log.stat().st_size == 0:
                            gui_log.unlink()
                    except Exception:
                        pass
                    gui.root.after(0, gui.close)
                    return

                # Read crash log
                try:
                    crash_text = gui_log.read_text(encoding="utf-8", errors="replace").strip() if gui_log else ""
                except Exception:
                    crash_text = ""

                def _show_crash(crash_text=crash_text, code=exit_code, gui_log=gui_log):
                    try:
                        gui.root.deiconify()
                        gui.root.lift()
                    except Exception:
                        pass
                    gui.log_fail(f"MCU GUI crashed immediately (exit code {code}).")
                    if crash_text:
                        gui.log_section("Crash output")
                        for ln in crash_text.splitlines()[:30]:
                            gui.log_fail(f"  {ln}")
                    gui.stop_spinner("GUI crashed", ok=False)
                    gui.show_error(
                        "MCU Uploader IDE by Naph — Crash",
                        f"The GUI crashed immediately (code {code}).\n\n"
                        + (crash_text[:600] if crash_text else "(no output captured)")
                        + f"\n\nLog: {gui_log}",
                    )
                    gui.close()

                gui.root.after(0, _show_crash)
                return

            # GUI alive — clean up empty log and close bootstrap window
            try:
                if gui_log and gui_log.exists() and gui_log.stat().st_size == 0:
                    gui_log.unlink()
            except Exception:
                pass

            gui.root.after(0, gui.close)

        _launch_done()

    except Exception as exc:
        def _err(exc=exc):
            gui.log_fail(f"Unexpected error: {exc}")
            gui.stop_spinner("Error", ok=False)
            gui.show_error("MCU Uploader IDE by Naph — Error", f"Unexpected error:\n{exc}")
            gui.close()
        gui.root.after(0, _err)


# ─────────────────────────────────────────────────────────────
# SELF-GUARD: same launcher/bootstrap-phase lock launcher.py uses
# ─────────────────────────────────────────────────────────────
# launcher.py already guards the normal .vbs -> launcher.py -> bootstrap.py
# flow with a PID lock file. These mirror that exact mechanism (same lock
# file, same stale-owner reclaim logic) so bootstrap.py is *also*
# self-guarding when it's launched some other way (double-clicked directly,
# run from a dev shortcut, etc.) instead of only ever being safe when
# invoked through launcher.py. Sharing the one lock file means either entry
# point blocks the other.
_BOOTSTRAP_LOCK_FILE = SCRIPT_DIR / "logs" / "launcher.lock"


def _bootstrap_process_is_alive(pid: int) -> bool:
    """True if a process with this PID currently exists, hasn't exited, AND is a Python/launcher process."""
    if sys.platform != "win32":
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    try:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            if exit_code.value != STILL_ACTIVE:
                return False

            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                exe_name = Path(buf.value).name.lower()
                return any(k in exe_name for k in ("python", "mcu", "flasher", "launcher"))
            return True
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return False


def _bootstrap_try_create_lock_exclusive() -> bool:
    """Atomically create the lock file only if it doesn't already exist."""
    try:
        fd = os.open(str(_BOOTSTRAP_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(os.getpid()).encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:
        # Don't block a launch merely because the lock file couldn't be
        # created (e.g. read-only filesystem, permissions oddity).
        return True


def _claim_bootstrap_slot() -> bool:
    """Claim the launcher/bootstrap-phase lock. Returns False only when
    another launcher/bootstrap process is genuinely still alive and
    holding it."""
    try:
        _BOOTSTRAP_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return True

    if _bootstrap_try_create_lock_exclusive():
        return True

    # Lock file already exists — find out whether its owner is actually
    # still running, or whether this is a stale leftover from a crash/kill.
    try:
        existing_pid = int(_BOOTSTRAP_LOCK_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        existing_pid = None

    if existing_pid and existing_pid != os.getpid() and _bootstrap_process_is_alive(existing_pid):
        return False  # a real launcher/bootstrap is genuinely in progress

    # Stale lock — reclaim it.
    try:
        _BOOTSTRAP_LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    return _bootstrap_try_create_lock_exclusive()


def _release_bootstrap_slot():
    """Best-effort cleanup so the lock file doesn't linger after a clean
    exit. Safe to skip on crash — the next launch's liveness check
    reclaims it automatically."""
    try:
        if _BOOTSTRAP_LOCK_FILE.exists():
            existing_pid = int(_BOOTSTRAP_LOCK_FILE.read_text(encoding="utf-8").strip())
            if existing_pid == os.getpid():
                _BOOTSTRAP_LOCK_FILE.unlink()
    except Exception:
        pass


def _notify_bootstrap_already_running():
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0,
            "MCU Uploader IDE by Naph is already starting up in another window.\n\n"
            "Please wait for it to finish loading before launching it again.",
            "MCU Uploader IDE by Naph",
            0x40,  # MB_ICONINFORMATION
        )
    except Exception:
        pass


def _is_main_gui_running() -> bool:
    """Check (without claiming it) whether the Main GUI's single-instance
    mutex is currently held by a live process. mcu_flash_gui.py claims
    "Local\\MCUFlasherByNaph.MainGUI" via CreateMutexW as soon as it starts;
    opening (rather than creating) that same name here lets bootstrap tell
    the Main GUI is already up without racing it for ownership."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenMutexW(SYNCHRONIZE, False, "Local\\MCUFlasherByNaph.MainGUI")
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    except Exception:
        return False


def main():
    global _gui

    # If the Main GUI already holds its single-instance mutex, it's already
    # up and running. There is nothing bootstrap needs to prepare that the
    # user is waiting on, and popping open another BootstrapGUI window (and
    # possibly spawning a second Main GUI) would just be redundant — skip
    # setup entirely in that case.
    if _is_main_gui_running():
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                "MCU Uploader IDE by Naph is already running.\n\n"
                "Switch to the existing window instead of starting a new one.",
                "MCU Uploader IDE by Naph",
                0x40,  # MB_ICONINFORMATION
            )
        except Exception:
            pass
        return

    # ── Launch Bootstrap GUI Window ─────────────────────────────────
    # Bootstrap setup is mandatory and runs its verification checks before launching the main GUI.
    import threading

    gui = BootstrapGUI()
    _gui = gui

    # Run setup on a background thread; Tk mainloop stays on main thread
    t = threading.Thread(target=_run_setup_in_thread, args=(gui,), daemon=True)
    t.start()

    gui.mainloop_until_done()
    sys.exit(0)


if __name__ == "__main__":
    # Self-guard against a second concurrent bootstrap when this script is
    # invoked directly rather than through launcher.py (e.g. a dev shortcut,
    # or manual testing). Shares launcher.py's own lock file, so whichever
    # of the two entry points got there first blocks the other one.
    if "--new-window" not in sys.argv:
        if not _claim_bootstrap_slot():
            _notify_bootstrap_already_running()
            sys.exit(0)
        import atexit
        atexit.register(_release_bootstrap_slot)

    # The VBS launcher deliberately hides its console.  Keep a startup
    # failure visible and diagnosable instead of letting pythonw.exe exit
    # silently before the BootstrapGUI can be created.
    import traceback

    crash_log = SCRIPT_DIR / "logs" / "bootstrap_crash.log"
    try:
        main()
    except Exception:
        error_text = traceback.format_exc()
        try:
            crash_log.parent.mkdir(parents=True, exist_ok=True)
            crash_log.write_text(error_text, encoding="utf-8")
        except Exception:
            pass

        message = (
            "MCU Uploader IDE setup could not start.\n\n"
            f"{error_text[:1200]}\n\n"
            f"Full log: {crash_log}"
        )
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("MCU Uploader IDE by Naph - Setup Error", message, parent=root)
            root.destroy()
        except Exception:
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    0, message, "MCU Uploader IDE by Naph - Setup Error", 0x10
                )
            except Exception:
                pass
        raise SystemExit(1)
