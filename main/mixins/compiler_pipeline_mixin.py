#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import sys
import os
import time
import re
import subprocess
from typing import TYPE_CHECKING
from pathlib import Path
import tkinter as tk
from tkinter import messagebox


from main.core.constants import *
from main.core.theme import *
from main.core.config import *
from main.core.file_utils import *
from main.core.toolchain import *
from main.core.board_catalog import *
from main.core.board_compat import *
from main.widgets import *
from main.dialogs import *
from main.editor_api import *

if TYPE_CHECKING:
    from main.mcu_flash_gui import MCUUploadGUI
    _Base = MCUUploadGUI
else:
    _Base = object

class CompilerPipelineMixin(_Base):
    """Mixin providing CompilerPipelineMixin capabilities for MCUUploadGUI."""
    def _freeze_build_sources_at_boundary(self, action_name: str) -> bool:
        """Stage immutable build inputs between two synchronous AI scans.

        The first scan catches writes that arrived while the worker was doing
        setup.  The second catches a write racing the copy itself.  A write after
        the second scan is harmless to this operation because ``src/`` no longer
        shares storage with the editable project files.
        """
        if self._block_action_for_pending_ai_review(action_name):
            return False
        # Remote projects are staged into the local board workspace.  This
        # keeps the final source snapshot small and deterministic while
        # preventing PlatformIO from reading/writing the network share.
        self._sync_src_dir(self._platformio_project_dir())
        if self._block_action_for_pending_ai_review(action_name):
            return False
        return True

    def _run_compile(self, is_upload: bool = False) -> bool:
        self._stop_requested = False
        self._op_session_id = getattr(self, "_op_session_id", 0) + 1
        is_clean_retry = getattr(self, "_clean_retry_in_progress", False)
        self._clean_retry_in_progress = False

        # Stage remote sources through a short-lived drive-letter mapping. The
        # standalone Compile wrapper removes it only after success; Upload keeps
        # it mounted until its final flash result is known.
        if is_unc_or_network_path(self.sketch_dir_path):
            self._map_unc_for_build(self.sketch_dir_path)

        # Syntax checking is scheduled by the Tk-owned background syntax
        # service. Do not run the legacy UI-bound checker from this worker:
        # it reads Tk widgets and calls root.update(), which can deadlock the
        # main window when a WebView2 terminal is still attaching.
        ensure_platformio_penv_with_hook()

        self.is_busy = True
        self._framework_download_active = False
        self._set_buttons_state(True, operation="compile")
        self._set_status("Compiling...", Theme.YELLOW)

        self._append("")
        self._append("=" * 50, "header")
        self._append("  ⚙  COMPILING (PlatformIO)", "header")
        self._append("=" * 50, "header")
        self._append(f"  Sketch : {self.sketch_dir_path}", "dim")
        self._append("  Tool   : PlatformIO Core", "dim")
        core_dir, core_was_refreshed = _refresh_platformio_core_environment(SCRIPT_DIR)
        self._append(f"  Store  : {core_dir}", "dim")
        if core_was_refreshed:
            self._append(
                "  ✔ PlatformIO core/junction path verified for this build.",
                "info",
            )
        if is_unc_or_network_path(self.sketch_dir_path):
            _share = _unc_share_root(self.sketch_dir_path) or str(self.sketch_dir_path)
            self._append(f"  🌐 Source  : Network share ({_share})", "info")
        self._append("")

        if is_upload and not self.skip_compile_var.get():
            self._append(
                "  🔄 Skip Compile is unchecked — compiling firmware before upload.",
                "info",
            )

        # Never equate "no final firmware yet" with "no reusable cache".
        # A normal compiler error can leave many valid framework/object files;
        # deleting those on the retry forced a complete rebuild after every
        # source fix.  The exact board workspace is either new/empty or safe to
        # retain, and PlatformIO incrementally reconciles changed inputs.
        current_board = self.board_var.get()
        self._migrate_legacy_board_cache()
        if self._board_workspace().exists():
            if self._last_compiled_board != current_board:
                self._append("  ♻ Board switch — restored this board's isolated cache.", "info")
            self._append("  ℹ Incremental build enabled; successful objects are preserved.", "info")
        else:
            self._append("  🔧 First build for this board — creating its isolated workspace.", "info")

        if not self._ensure_platformio_ini():
            self.is_busy = False
            self._set_buttons_state(False)
            self._set_status("Error: platformio.ini missing", Theme.RED)
            return False

        # Check if all required libraries are installed on the computer
        if not self._check_libraries_installed():
            self.is_busy = False
            self._set_buttons_state(False)
            self._set_status("Error: Missing libraries", Theme.RED)
            return False

        # Validate that setup() and loop() are defined (catches blank/incomplete files)
        if not self._validate_entry_points():
            self.is_busy = False
            self._set_buttons_state(False)
            self._set_status("Error: Missing setup() / loop()", Theme.RED)
            return False

        # Emit compatibility notice inside the COMPILING section
        pending_compat = getattr(self, "_pending_compat_reasons", [])
        if pending_compat:
            self._append("  ℹ Compatibility Notice — board/sketch details detected", "dim")
            for r in pending_compat:
                self._append(f"    ℹ {r}", "dim")
            self._append("")
            self._pending_compat_reasons = []  # consumed


        pio_path = find_pio_executable()
        if not pio_path:
            self._append("  ⚠ PlatformIO not found — installing automatically...", "warning")
            self._set_status("Installing PlatformIO...", Theme.YELLOW)
            pio_path = ensure_platformio()
            if not pio_path:
                self._append("  ✖ Failed to install PlatformIO!", "error")
                self._append("  Try manually: pip install platformio", "info")
                self.is_busy = False
                self._set_buttons_busy(False)
                self._set_status("Error: PlatformIO not found", Theme.RED)
                return False
            self._append(f"  ✔ PlatformIO installed: {' '.join(pio_path)}", "success")

        # ── Self-heal: verify the framework package actually has this
        # board's pin-mapping file before we spend time compiling only to
        # hit "pins_arduino.h: No such file" deep into the build. Fully
        # dynamic — reads the board's own manifest, no board/platform
        # names hardcoded here.
        board_info = self._resolve_board_info()
        variant_ok, missing_variant = self._verify_board_variant_exists(
            board_info["platform"], board_info["board"]
        )
        if not variant_ok:
            self._append(
                f"  ⚠ Framework package looks incomplete — missing pin-mapping for variant '{missing_variant}'.",
                "warning"
            )
            self._append("  This usually means a previous framework download was interrupted or corrupted.", "dim")
            if self._attempt_framework_repair(pio_path, board_info["platform"]):
                variant_ok, _ = self._verify_board_variant_exists(
                    board_info["platform"], board_info["board"]
                )
                if variant_ok:
                    self._append("  ✔ Framework repaired successfully — continuing compile.", "success")
                else:
                    self._append(
                        "  ✖ Repair did not resolve the issue — the installed platform may not "
                        "support this board/variant.", "error"
                    )
                    self.is_busy = False
                    self._set_buttons_state(False)
                    self._set_status("Error: framework repair failed", Theme.RED)
                    return False
            else:
                self._append("  ✖ Automatic repair failed. Try deleting the platform's package folder manually and recompiling.", "error")
                self.is_busy = False
                self._set_buttons_state(False)
                self._set_status("Error: framework repair failed", Theme.RED)
                return False

        jobs = self._get_cpu_cores_jobs()
        logical_processors = max(1, os.cpu_count() or jobs)
        reserved_processors = _system_reserved_cpu_count(logical_processors)
        reserved_word = "Processor" if reserved_processors == 1 else "Processors"
        text_part = f"⚡ Running Parallel Compilation on {jobs} Logical Processors"
        inner_w = max(73, len(text_part) + 4)

        top = " ╔" + "═" * inner_w + "╗"
        mid = "   " + text_part.center(inner_w)
        bot = " ╚" + "═" * inner_w + "╝"

        self._append("")
        self._append(top, "header")
        self._append(mid, "header")
        self._append(bot, "header")
        self._append(
            f"   >>> System Reserved — {reserved_processors} Logical {reserved_word} <<<",
            "dim",
        )
        self._append("")

        self._append("  ℹ Selected-board workspace is isolated from every other board.", "info")
        self._append("    PlatformIO will compile only missing or changed units.", "dim")
        self._append("")

        # Freeze sketch files into src/ so PlatformIO uses its default src_dir.
        # Without this, src_dir=. causes InoToCPPConverter to write .ino.cpp
        # into the sketch root while SCons expects it under .pio/build/<env>/src/,
        # resulting in g++ receiving no input file.  The boundary helper also
        # closes the AI-review race immediately before PlatformIO is launched.
        boundary_action = "Upload" if is_upload else "Compile"
        if not self._freeze_build_sources_at_boundary(boundary_action):
            self.is_busy = False
            self._set_buttons_state(False)
            self._set_status(f"{boundary_action} paused for AI review", Theme.YELLOW)
            return False

        env_name = self._pio_env_name()
        cmd = pio_path + [
            "run",
            "-e", env_name,
            "-j", str(jobs)
        ]

        self._append("  ⚙ Initializing PlatformIO build engine & dependency tree...", "purple_header")
        self._append("    SCons is resolving header dependencies in memory (takes 15–30s on fresh build)...", "purple_dim")
        # ── Remote project support ────────────────────────────────────────
        # Remote sketches are staged into a local board-isolated project by
        # _freeze_build_sources_at_boundary().  PlatformIO therefore never
        # uses the UNC/share directory as its cwd or repeatedly scans it.
        effective_cwd = self._platformio_project_dir(self.sketch_dir_path)
        env = self._platformio_subprocess_env(
            project_dir=self.sketch_dir_path,
            board_name=current_board,
            jobs=jobs,
        )

        # Normal process priority keeps Tk, WebView2, USB handling, and the
        # serial reader responsive while compiler workers are busy.
        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

        hide_generated_directory(effective_cwd)

        try:
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8", errors="replace",
                creationflags=creation_flags,
                cwd=str(effective_cwd),
                env=env,
            )
        except FileNotFoundError:
            self._append("  ✖ PlatformIO executable not found at: " + ' '.join(pio_path), "error")
            self._append("  Try manually: pip install platformio", "info")
            self.is_busy = False
            self._set_buttons_busy(False)
            self._set_status("Error: PlatformIO not found", Theme.RED)
            return False

        output_lines = []
        line_count = 0
        compile_start = time.time()
        _build_start   = [None]   # set to time.time() the instant framework download finishes
        spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        was_killed = False

        # Read the merged stdout+stderr stream via a queue so the main
        # loop can process lines with a timeout (for the spinner).
        # stderr=subprocess.STDOUT ensures GCC diagnostics are never lost.
        import queue as _queue
        import threading as _threading

        _line_queue: _queue.Queue = _queue.Queue()

        def _reader(stream, q):
            try:
                for line in iter(stream.readline, ''):
                    if line:
                        q.put(line)
            except Exception:
                pass
            finally:
                q.put(None)  # sentinel

        _t_out = _threading.Thread(target=_reader, args=(self.process.stdout, _line_queue), daemon=True)
        _t_out.start()

        # ── Spinner thread ────────────────────────────────────────────────────
        # Drive the spinner from a dedicated timer thread so it keeps ticking
        # even when GCC produces no output for several seconds (e.g. during
        # LTO, framework archive linking, or slow first-time builds).
        _spinner_active = [True]
        _spin_frame     = [0]
        _package_check_active = [False]
        _package_check_start = [None]
        _package_check_item = [""]

        def _spin_loop():
            while _spinner_active[0] and not getattr(self, "_stop_requested", False):
                if getattr(self, "_framework_download_active", False):
                    elapsed = int(time.time() - compile_start)
                    frame   = spinner[_spin_frame[0] % len(spinner)]
                    _spin_frame[0] += 1
                    self._set_status(
                        f"{frame} Downloading/Unpacking PlatformIO package... ({elapsed}s elapsed)",
                        Theme.YELLOW,
                    )
                elif _package_check_active[0]:
                    started = _package_check_start[0] or compile_start
                    elapsed = int(time.time() - started)
                    item = _package_check_item[0] or "required package"
                    if len(item) > 40:
                        item = item[:37] + "..."
                    frame = spinner[_spin_frame[0] % len(spinner)]
                    _spin_frame[0] += 1
                    self._set_status(
                        f"{frame} Checking PlatformIO package {item}... ({elapsed}s)",
                        Theme.YELLOW,
                    )
                else:
                    # Only show the pure build timer — package preparation is done.
                    build_elapsed = int(time.time() - (_build_start[0] or compile_start))
                    frame   = spinner[_spin_frame[0] % len(spinner)]
                    _spin_frame[0] += 1
                    self._set_status(
                        f"{frame} Compiling... ({build_elapsed}s)",
                        Theme.YELLOW,
                    )
                time.sleep(0.2)

        _spin_thread = _threading.Thread(target=_spin_loop, daemon=True)
        _spin_thread.start()

        _sentinels_remaining = 1           # only one reader thread (merged stdout+stderr)
        _process_exited_at = None          # timestamp when we first see poll()!=None

        _in_error_block = [False]
        _error_block_type = ["error"]

        _tool_dl_active = [False]
        _tool_dl_start = [None]
        _tool_dl_total = [0.0]

        # One framework/toolchain setup session can contain many PlatformIO
        # packages (platform, framework, compiler, uploader, etc.).  Keep the
        # safety banner session-scoped so it is printed once, then label every
        # package phase with the actual package currently being processed.
        _framework_banner_shown = [False]
        _current_framework_item = [""]
        _current_framework_kind = ["Package"]
        _framework_logged_installs: set[str] = set()
        _framework_logged_done: set[str] = set()

        _first_divider_printed = [False]
        _has_intermediate_content = [False]
        _init_divider_closed = [False]

        # Tracks the last displayed SCons progress text so consecutive identical
        # lines (PlatformIO emits one "Archiving <lib>.a" per library, and both
        # "Checking size" + "Retrieving maximum program size") don't show up as
        # repeated redundant rows in the console.
        _last_progress_text = [None]

        def _diagnostic_location(raw_path: str) -> str:
            """Return a compact, useful source path for GCC diagnostics."""
            value = str(raw_path).strip().strip('"').replace("\\", "/")
            lowered = value.lower()
            for marker in ("/src/", "/lib/", "/include/"):
                marker_pos = lowered.rfind(marker)
                if marker_pos >= 0:
                    return value[marker_pos + 1:]
            return value.rsplit("/", 1)[-1] or value

        def _format_gcc_diagnostic(raw_line: str):
            """Render GCC diagnostics without noisy absolute staging paths."""
            diagnostic = re.match(
                r"^(?P<file>.+?):(?P<line>\d+):(?P<column>\d+):\s*"
                r"(?P<kind>fatal error|error|warning|note)\s*:\s*(?P<message>.*)$",
                raw_line,
                re.IGNORECASE,
            )
            if diagnostic:
                kind = diagnostic.group("kind").lower()
                label = {
                    "fatal error": "Fatal error",
                    "error": "Error",
                    "warning": "Warning",
                    "note": "Note",
                }.get(kind, kind.title())
                tag = "warning" if kind == "warning" else "info" if kind == "note" else "error"
                icon = "⚠" if kind == "warning" else "ℹ" if kind == "note" else "✖"
                location = _diagnostic_location(diagnostic.group("file"))
                self._append(
                    f"  {icon} {label} at {location}:"
                    f"{diagnostic.group('line')}:{diagnostic.group('column')}",
                    tag,
                )
                message = diagnostic.group("message").strip()
                if message:
                    self._append(f"      {message}", tag)
                return kind, False

            context = re.match(
                r"^(?P<file>.+?):\s+(?P<context>"
                r"(?:In function|In member function|In constructor|In destructor|At global scope|"
                r"In file included from).*)$",
                raw_line,
                re.IGNORECASE,
            )
            if context:
                self._append(
                    f"    {_diagnostic_location(context.group('file'))}: "
                    f"{context.group('context').strip()}",
                    "dim",
                )
                return "info", True
            return None

        # One-time hint flag for PlatformIO's non-fatal build-cache clean
        # failures (WinError 145 etc.) — see is_nonfatal_pio_clean_report.
        _clean_warn_shown = [False]

        # Paths PlatformIO failed to delete ("Please manually remove the file
        # `...`"). The GUI retries the removal itself right after the build
        # process exits — no manual user intervention needed.
        _stale_clean_paths: list[str] = []

        def _maybe_close_init_divider():
            if _first_divider_printed[0] and _has_intermediate_content[0] and not _init_divider_closed[0]:
                _init_divider_closed[0] = True
                self._append("  ──────────────────────────────────────────────────", "purple_dim")

        def _ensure_framework_download_banner():
            """Show the critical package warning only after real download/unpack starts.

            PlatformIO often emits ``Tool Manager: Installing ...`` even while it
            is only checking an already-installed package.  Treating that line as
            proof of a download caused false first-time warnings on every clean.
            """
            if _framework_banner_shown[0]:
                return
            _framework_banner_shown[0] = True
            self._append("", "")
            self._append("  ────────────────────────────────────────────────────────────────────────────", "warning")
            self._append("  ⚠ Preparing required core framework & toolchain packages...", "warning")
            self._append("    A required shared PlatformIO package is actually being downloaded/unpacked.", "info")
            self._append("    Keep the application open until package preparation finishes.", "info")
            self._append("  ────────────────────────────────────────────────────────────────────────────", "warning")
            self._append("", "")

        def _classify_and_display(stripped):
            """Classify a single PlatformIO / GCC output line and display it using a stateful parser."""
            low = stripped.lower()

            # When a board mismatch was detected pre-compile, suppress all
            # error / warning / context noise — only let progress lines through.
            if getattr(self, '_board_mismatch_detected', False):
                # Still show SCons progress (Compiling, Building, Linking, result)
                if any(kw in low for kw in ('compiling', 'building', 'linking',
                                             'archiving', 'took')):
                    pass  # fall through to normal progress handling below
                else:
                    return  # suppress everything else

            # Patterns that indicate a real linker/compiler error even without
            # the word "error" in the line (e.g. ld's "undefined reference to")
            LINKER_ERROR_HINTS = (
                "undefined reference to",
                "multiple definition of",
                "cannot find -l",
                "undefined symbol",
                "duplicate symbol",
                "ld returned",
                "collect2",
                "overflowed by",
                "will not fit in region",
                "relocation truncated",
                "ld.exe:",
                "ld:",
                "section `",
                "region `",
            )
            is_linker_error = any(hint in low for hint in LINKER_ERROR_HINTS)

            # GCC/Clang diagnostic format: <file>:<line>:<col>: error: <msg>
            is_gcc_diagnostic = bool(re.search(r':\d+:\d+:\s+(error|warning|note|fatal error)\s*:', low))

            # ── SCons make-error wrapper: "*** [...] Error N" ─────────────────
            is_scons_wrapper = bool(re.search(r'^\*\*\*\s+\[', stripped))

            # SCons or PlatformIO administrative/progress markers that should terminate
            # a compiler error tracking block.
            is_scons_progress = (
                "compiling" in low or
                "archiving" in low or
                "linking" in low or
                "building" in low or
                "checking size" in low or
                "retrieving maximum" in low or
                "took" in low or
                low.startswith("platform:") or
                low.startswith("hardware:") or
                low.startswith("package") or
                low.startswith("embedded") or
                low.startswith("configuration") or
                low.startswith("sdk") or
                "ram:" in low or
                "flash:" in low or
                stripped.startswith("===") or
                stripped.startswith("---") or
                is_scons_wrapper
            )

            # Package-manager completion lines ("has been installed") are NOT
            # the end of framework setup: PlatformIO commonly installs several
            # packages back-to-back.  Only genuine SCons build work ends the
            # framework/toolchain phase.
            is_real_build_progress = any(kw in low for kw in (
                "compiling", "archiving", "linking", "building",
                "checking size", "retrieving maximum", "took",
            ))

            if is_scons_progress or "has been installed" in low:
                _in_error_block[0] = False

            if is_real_build_progress:
                _package_check_active[0] = False
                _package_check_start[0] = None
                _package_check_item[0] = ""
                if _tool_dl_active[0]:
                    if _tool_dl_start[0] is not None:
                        _tool_dl_total[0] += (time.time() - _tool_dl_start[0])
                    _tool_dl_active[0] = False
                    _tool_dl_start[0] = None
                # Real build progress means all package preparation has finished.
                if getattr(self, "_framework_download_active", False):
                    self._framework_download_active = False
                    if _build_start[0] is None:
                        _build_start[0] = time.time()
                    try:
                        self.btn_stop.configure(state=tk.NORMAL)
                    except Exception:
                        pass

            # Context headers indicate where the error occurred (e.g., function, file stack)
            is_context_header = (
                "in function" in low or
                "in member function" in low or
                "in constructor" in low or
                "in destructor" in low or
                "at global scope" in low or
                "in file included from" in low or
                low.startswith("from ") or
                low.startswith("in file included")
            )

            if is_gcc_diagnostic or is_context_header:
                _in_error_block[0] = True
                if "warning" in low and "error" not in low:
                    _error_block_type[0] = "warning"
                elif "note" in low:
                    _error_block_type[0] = "info"
                elif is_context_header and not is_gcc_diagnostic:
                    # A function/include context line is not itself an error;
                    # the following diagnostic determines the severity.
                    _error_block_type[0] = "info"
                else:
                    _error_block_type[0] = "error"

            # Show meaningful lines to user
            if is_nonfatal_pio_clean_report(stripped):
                # PlatformIO's rmtree retry report (e.g. "[WinError 145] The
                # directory is not empty") — non-fatal, build continues.
                _in_error_block[0] = False
                self._append(f"  ⚠ {stripped}", "warning")
                if not _clean_warn_shown[0]:
                    _clean_warn_shown[0] = True
                    self._append(
                        "  ℹ Non-fatal: a stale build cache could not be fully cleaned "
                        "(file in use or antivirus scan). PlatformIO will continue and "
                        "rebuild the affected parts automatically.",
                        "info",
                    )
                # Auto-heal: remember the locked path so we can retry deleting
                # it ourselves once the build process has fully exited (the
                # lock usually clears right after the toolchain closes its
                # handles). Fully automated — the user no longer needs to
                # manually remove anything.
                _m = re.search(r"`([^`]+)`", stripped)
                if _m and _m.group(1).strip() not in _stale_clean_paths:
                    _stale_clean_paths.append(_m.group(1).strip())
            elif is_linker_error:
                self._append(f"  ✖ {stripped}", "error")
                _in_error_block[0] = False
            elif is_gcc_diagnostic or is_context_header:
                # Keep warnings, errors, notes, and context lines, but render
                # them as a compact diagnostic block instead of repeating the
                # full isolated-workspace path on every line.
                if _format_gcc_diagnostic(stripped) is None:
                    prefix = "    " if is_context_header else "      "
                    self._append(f"{prefix}{stripped}", _error_block_type[0])
            elif _in_error_block[0]:
                # Indent non-header context/caret/code lines for cleaner hierarchy
                prefix = "    " if is_context_header else "      "
                self._append(f"{prefix}{stripped}", _error_block_type[0])
            elif is_scons_wrapper:
                pass  # swallow — SCons wrapper contains no diagnostic detail
            elif "error" in low and "werror" not in low:
                self._append(f"  ✖ {stripped}", "error")
            elif "warning" in low:
                self._append(f"  ⚠ {stripped}", "warning")
            elif any(kw in low for kw in ("tool manager:", "platform manager:", "library manager:", "downloading", "unpacking", "installing", "installed", "removing", "processing")):
                # Library Manager activity is project-local dependency linking.
                # Tool/Platform Manager activity is the one-time core framework
                # setup.  Keep those two categories visually distinct.
                is_library_manager_line = "library manager:" in low
                is_tool_manager_line = "tool manager:" in low
                is_platform_manager_line = "platform manager:" in low
                is_core_framework_event = is_tool_manager_line or is_platform_manager_line

                def _pio_item_from_manager_line(raw: str) -> str:
                    value = re.sub(
                        r"^(?:Tool|Platform) Manager:\s*", "", raw,
                        flags=re.IGNORECASE,
                    ).strip()
                    value = re.sub(
                        r"^(?:Installing|Downloading|Unpacking|Removing)\s+", "",
                        value, flags=re.IGNORECASE,
                    ).strip()
                    value = re.split(
                        r"\s+has been installed!?$", value,
                        flags=re.IGNORECASE,
                    )[0].strip()
                    return value or "PlatformIO package"

                if is_library_manager_line and ("installing" in low or "linking" in low):
                    installed_item = re.split(r"installing|linking", stripped, flags=re.IGNORECASE)[-1].strip()
                    verb = "Linked" if ("symlink" in low or "linking" in low or "installing" in low) else "Installed"
                    self._append_notif(
                        f"  📚 PlatformIO Auto-{verb} Library: {installed_item}",
                        tag="success", category="library_install", title=f"Library {verb}"
                    )
                elif is_core_framework_event:
                    manager_kind = "Toolchain/Tool" if is_tool_manager_line else "Platform/Framework"
                    item = _pio_item_from_manager_line(stripped)

                    # ``... Manager: Installing`` is not proof that bytes are
                    # being installed; PlatformIO also emits it while checking an
                    # already-present package.  Record the candidate item now, but
                    # only enter framework-download mode when a real Downloading or
                    # Unpacking progress line arrives.
                    if "installing" in low:
                        _current_framework_item[0] = item
                        _current_framework_kind[0] = manager_kind
                        _package_check_active[0] = True
                        _package_check_start[0] = time.time()
                        _package_check_item[0] = item
                        key = f"{manager_kind}:{item}"
                        if key not in _framework_logged_installs:
                            _framework_logged_installs.add(key)
                            self._append(f"  🔎 Checking {manager_kind}: {item}", "info")
                    elif "has been installed" in low or ("installed" in low and "installing" not in low):
                        _package_check_active[0] = False
                        _package_check_start[0] = None
                        _package_check_item[0] = ""
                        # Completion belongs to the item named on this manager
                        # line; use it rather than whichever previous package was
                        # active when the last progress percentage arrived.
                        _current_framework_item[0] = item
                        _current_framework_kind[0] = manager_kind
                        key = f"{manager_kind}:{item}"
                        if key not in _framework_logged_done:
                            _framework_logged_done.add(key)
                            self._append(f"  ✔ Installed {manager_kind}: {item}", "success")
                    return

                if is_library_manager_line:
                    _has_intermediate_content[0] = True
                    if not _first_divider_printed[0]:
                        _first_divider_printed[0] = True
                        self._append("  ──────────────────────────────────────────────────", "purple_dim")
                    formatted_lib_line = stripped
                    formatted_lib_line = re.sub(r'\bInstalling\b', 'Linking', formatted_lib_line)
                    formatted_lib_line = re.sub(r'\binstalling\b', 'linking', formatted_lib_line)
                    formatted_lib_line = re.sub(r'\bhas been installed\b', 'has been linked', formatted_lib_line, flags=re.IGNORECASE)
                    formatted_lib_line = re.sub(r'\bInstalled\b', 'Linked', formatted_lib_line)
                    self._append(f"    {formatted_lib_line}", "info")
                    return

                # Bare PlatformIO progress rows normally follow the preceding
                # Tool/Platform Manager line, so annotate them with that package.
                if "downloading" in low or "unpacking" in low:
                    _package_check_active[0] = False
                    _package_check_start[0] = None
                    _package_check_item[0] = ""
                    if not _tool_dl_active[0]:
                        _tool_dl_active[0] = True
                        _tool_dl_start[0] = time.time()
                    self._framework_download_active = True
                    try:
                        self.btn_stop.configure(state=tk.DISABLED)
                    except Exception:
                        pass
                    _ensure_framework_download_banner()
                    pcts = re.findall(r'(\d+)%', stripped)
                    if pcts:
                        pct = int(pcts[-1])
                        filled = int(pct / 100 * 30)
                        bar = "\u25b0" * filled + "\u25b1" * (30 - filled)
                        act_name = "Downloading" if "downloading" in low else "Unpacking"
                        item = _current_framework_item[0] or "PlatformIO package"
                        # Keep the package label readable without allowing an
                        # unusually long registry string to consume the console.
                        item_label = item if len(item) <= 44 else item[:41] + "..."
                        icon = "✔ " if pct >= 100 else "  "
                        progress_text = f"  {icon}{act_name:<11} [{item_label}]  {bar}  {pct:3d}%"
                        self._append_progress(
                            progress_text,
                            "success" if pct >= 100 else "info",
                            action_type=f"{act_name}:{item_label}",
                        )
                        return

                if stripped.startswith("Processing") or ("processing " in low and "(" in low):
                    self._append(f"    {stripped}", "purple")
                    if not _first_divider_printed[0]:
                        _first_divider_printed[0] = True
                        self._append("  ──────────────────────────────────────────────────", "purple_dim")
            elif is_scons_progress:
                if any(kw in low for kw in ("compiling", "building", "linking", "archiving", "checking size", "took")):
                    _maybe_close_init_divider()
                _prog_text = None
                _prog_tag = "dim"
                if "linking" in low:
                    _prog_text, _prog_tag = "  🔗 Linking...", "dim"
                elif "checking size" in low or "retrieving maximum" in low:
                    _prog_text, _prog_tag = "  📏 Checking firmware size...", "dim"
                elif "compiling" in low:
                    match = re.search(r'compiling\s+(.+)$', low)
                    if match:
                        filename = Path(match.group(1)).name
                        _prog_text = f"  ⚙ Compiling {filename}..."
                        _prog_tag = "info"
                    else:
                        _prog_text = f"  ⚙ {stripped}"
                        _prog_tag = "info"
                elif "archiving" in low:
                    _prog_text, _prog_tag = "  📦 Archiving...", "dim"
                elif "building" in low:
                    _prog_text, _prog_tag = "  ⚙ Building...", "info"
                elif "took" in low and ("success" in low or "failed" in low):
                    _prog_text = f"  {stripped}"
                    _prog_tag = "success" if "success" in low else "error"

                if _prog_text is None:
                    return
                # Suppress consecutive duplicate progress rows
                if _prog_text == _last_progress_text[0]:
                    return
                _last_progress_text[0] = _prog_text
                self._append(_prog_text, _prog_tag)

        while _sentinels_remaining > 0:
            try:
                line = _line_queue.get(timeout=0.1)
            except _queue.Empty:
                # After the process exits, give the reader threads a generous
                # window to flush any remaining data (especially stderr which
                # may contain the actual GCC error messages).  Only bail out
                # after 5 s of no new data post-exit.
                if self.process.poll() is not None:
                    if _process_exited_at is None:
                        _process_exited_at = time.time()
                    elif time.time() - _process_exited_at > 5:
                        break
                continue

            if line is None:
                _sentinels_remaining -= 1
                continue

            # Reset the post-exit timer whenever we receive real data,
            # so we wait a full 5 s after the *last* line, not after exit.
            _process_exited_at = None

            stripped = line.rstrip()
            if not stripped:
                continue
            output_lines.append(stripped)
            line_count += 1
            _classify_and_display(stripped)

        # Wait for reader thread to finish flushing any remaining data.
        _t_out.join(timeout=5)

        # ── Drain any lines that arrived after the main loop exited ──────────
        # This catches late-arriving stderr content (GCC error messages) that
        # the reader threads enqueued after we broke out of the loop above.
        while not _line_queue.empty():
            try:
                line = _line_queue.get_nowait()
            except _queue.Empty:
                break
            if line is None:
                continue
            stripped = line.rstrip()
            if not stripped:
                continue
            output_lines.append(stripped)
            _classify_and_display(stripped)

        _maybe_close_init_divider()

        # Stop the spinner thread before reading the final return code
        _spinner_active[0] = False
        _spin_thread.join(timeout=1)

        self.process.wait()
        if _tool_dl_active[0] and _tool_dl_start[0] is not None:
            _tool_dl_total[0] += (time.time() - _tool_dl_start[0])
            _tool_dl_active[0] = False

        rc = self.process.returncode

        # ── Auto-repair stale paths explicitly reported by PlatformIO ─────
        # The build process has exited, so handles it could not release while
        # running are gone now. Retry the removal with robust_rmtree (which
        # also falls back to a hidden rename) so the next run starts clean
        # without any manual user intervention.
        if _stale_clean_paths:
            self._auto_clean_stale_build_paths(
                _stale_clean_paths,
                env_name,
                build_ok=(rc == 0),
                build_root=self._board_build_root(),
            )

        total_sec    = round(time.time() - compile_start, 1)
        tool_dl_sec  = round(_tool_dl_total[0], 1)
        if _build_start[0] is not None:
            # Precise build time: only counts from when actual code build started
            build_sec = max(0.0, round(time.time() - _build_start[0], 1))
            # Adjust to not exceed total (edge-case guard)
            build_sec = min(build_sec, total_sec)
        else:
            # No framework was downloaded — entire time was code build
            build_sec = max(0.0, round(total_sec - tool_dl_sec, 1))

        # Check if killed by user (returncode is negative on SIGTERM/SIGKILL, or _stop_requested set)
        was_killed = getattr(self, "_stop_requested", False) or (sys.platform != "win32" and rc < 0)


        if rc == 0:
            self._capture_build_metadata(output_lines)
            # Show advanced memory usage summary
            for line in output_lines:
                if "ram:" in line.lower() or "flash:" in line.lower():
                    self._append(f"  {line.strip()}", "success")
            self._append("")

            if tool_dl_sec >= 1.5:
                breakdown_fields = [
                    ("Framework & Tool Download", f"{tool_dl_sec}s"),
                    ("Code Build & Compilation", f"{build_sec}s"),
                    ("Total Elapsed Time", f"{total_sec}s"),
                ]
                self._print_info_box("Compilation Time Breakdown", breakdown_fields)
                self._append(f"  ✔ Compilation successful! (Build: {build_sec}s | Tools Download: {tool_dl_sec}s | Total: {total_sec}s)", "success")
                self._set_status(f"Compile OK (Build: {build_sec}s, Tools: {tool_dl_sec}s)", Theme.GREEN)
            else:
                breakdown_fields = [
                    ("Code Build & Compilation", f"{build_sec}s"),
                    ("Total Elapsed Time", f"{total_sec}s"),
                ]
                self._print_info_box("Compilation Time Breakdown", breakdown_fields)
                self._append(f"  ✔ Compilation successful! ({total_sec}s)", "success")
                self._set_status(f"Compile OK ({total_sec}s)", Theme.GREEN)

            # ── Board compatibility → dedicated 'Compatible Devices' tab ──
            self._refresh_compatible_devices(force=True)
            self._append("  ℹ Compatible devices analysis updated → see the '🔧 Compatible Devices' tab.", "dim")

            # ── Check for Selected Board Hardware Compatibility ─────────────
            selected_board_name = self.board_var.get()
            gpio_analysis = _analyze_gpio_compatibility(self.sketch_dir_path)
            
            if selected_board_name in gpio_analysis.get("excluded", set()):
                bad_hits = gpio_analysis.get("pin_hits", {}).get(selected_board_name, [])
                bad_pins = sorted({pin for pin, _ in bad_hits})
                if bad_pins:
                    pins_formatted = ", ".join(f"GPIO {p}" for p in bad_pins)
                    is_s3_selected = "s3" in selected_board_name.lower()
                    max_gpio = 48 if is_s3_selected else 39
                    
                    self._append("")
                    self._append("  🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨", "severe_alert")
                    self._append("  ════════════════════════════════════════════════════════════════════════════", "severe_alert")
                    self._append("  CRITICAL HARDWARE INCOMPATIBILITY DETECTED ON SELECTED BOARD!", "severe_alert")
                    self._append("  ════════════════════════════════════════════════════════════════════════════", "severe_alert")
                    self._append(f"  SELECTED BOARD : {selected_board_name.upper()}", "severe_alert")
                    self._append(f"  INVALID GPIO(S): {pins_formatted.upper()} IS OUT OF HARDWARE RANGE (MAX VALID: GPIO {max_gpio})!", "severe_alert")
                    self._append("  ", "severe_alert")
                    self._append("  THE COMPILER SUCCEEDED BUT THIS CODE WILL NOT WORK AT RUNTIME ON THIS BOARD!", "severe_alert")
                    self._append(f"  {selected_board_name.upper()} DOES NOT HAVE {pins_formatted.upper()} CONNECTED OR ACCESSIBLE ON THIS CHIP/BOARD VARIANT!", "severe_alert")
                    self._append("  ", "severe_alert")
                    self._append("  BOARD PINOUT VARIANT CONSIDERATIONS (30-PIN / 38-PIN / 44-PIN BOARDS):", "severe_alert")
                    self._append("  - ESP32 DEV MODULE (30-PIN & 38-PIN BOARDS): HARDWARE GPIO RANGE IS 0-39 (MAX GPIO 39).", "severe_alert")
                    self._append("  - ESP32-S3 DEV MODULE (38-PIN & 44-PIN BOARDS): HARDWARE GPIO RANGE IS 0-48 (MAX GPIO 48).", "severe_alert")
                    self._append("    NOTE: ON 38-PIN ESP32-S3 VARIANTS, HIGHER PINS (GPIO 45-48) & PSRAM PINS (GPIO 26-32)", "severe_alert")
                    self._append("    MAY NOT BE BROKEN OUT TO PHYSICAL HEADERS OR MAY BE USED BY ONBOARD NEOPIXEL/FLASH.", "severe_alert")
                    self._append("  ", "severe_alert")
                    self._append("  RECOMMENDED ACTION:", "severe_alert")
                    self._append("  - CHANGE THE PIN IN YOUR CODE TO AN ACCESSIBLE GPIO (E.G., GPIO 2, 4, 16, 17), OR", "severe_alert")
                    self._append("  - SWITCH SELECTED BOARD TO ESP32-S3 DEV MODULE IF YOUR PHYSICAL BOARD USES AN S3 CHIP!", "severe_alert")
                    self._append("  ════════════════════════════════════════════════════════════════════════════", "severe_alert")
                    self._append("  🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨", "severe_alert")
                    self._append("")

                    def _show_severe_gpio_popup(b_name=selected_board_name, p_str=pins_formatted, m_gpio=max_gpio):
                        from tkinter import messagebox
                        msg = (
                            f"CRITICAL HARDWARE INCOMPATIBILITY DETECTED!\n\n"
                            f"Selected Board : {b_name}\n"
                            f"Invalid GPIO(s) : {p_str} (Out of hardware range 0–{m_gpio})\n\n"
                            f"The code compiled successfully, but it will NOT work on your {b_name} board at runtime!\n"
                            f"{b_name} does not have {p_str} connected or accessible on this chip.\n\n"
                            f"RECOMMENDED ACTION:\n"
                            f"• Change the pin number in your code to a valid GPIO (e.g., GPIO 2 or GPIO 4).\n"
                            f"• OR switch the selected board to ESP32-S3 Dev Module if using an ESP32-S3 board."
                        )
                        messagebox.showwarning(
                            "Critical Hardware Incompatibility — MCU Flasher",
                            msg,
                            parent=self.root
                        )
                    self.root.after(100, _show_severe_gpio_popup)

            self._save_compile_cache()  # snapshot so upload can skip recompile

            # Automatically update the Skip Compile checkbox on successful compilation
            self.root.after(0, self._update_skip_compile_state)
        elif was_killed:
            self._append("")
            self._append("  ■ Compilation stopped by user.", "warning")
            self._set_status("Compile stopped", Theme.YELLOW)
            if current_board:
                self._last_compiled_board = current_board
            self.root.after(0, self._update_skip_compile_state)
        else:
            self.root.after(0, self._update_skip_compile_state)
            self._append("")
            self._append(f"  ✖ Compilation FAILED after {total_sec}s:", "error")

            # Parse compiler error lines
            parsed_errors = []
            error_lines = []
            # Regex handles Windows absolute/relative paths and error/warning/note severity levels
            err_pattern = re.compile(
                r'^(?:([a-zA-Z]:[\\/][^:]+)|([^:]+)):(\d+):(\d+):\s+(fatal\s+error|error|warning|note):\s+(.+)$',
                re.IGNORECASE
            )
            for line in output_lines:
                match = err_pattern.match(line.strip())
                if match:
                    file_path = match.group(1) or match.group(2)
                    line_num = match.group(3)
                    col_num = match.group(4)
                    error_type = match.group(5)
                    error_msg = match.group(6)
                    try:
                        file_name = Path(file_path).name
                    except Exception:
                        file_name = file_path
                    parsed_errors.append({
                        "file": file_name,
                        "line": line_num,
                        "col": col_num,
                        "type": error_type,
                        "msg": error_msg
                    })

            if parsed_errors:
                # Group by file name and deduplicate identical compilation issues
                errors_by_file = {}
                seen_issues = set()
                for err in parsed_errors:
                    severity = err["type"].lower().strip()
                    # Deduplicate based on file name, line, column, severity, and message content
                    key = (err["file"], err["line"], err["col"], severity, err["msg"].strip())
                    if key in seen_issues:
                        continue
                    seen_issues.add(key)
                    errors_by_file.setdefault(err["file"], []).append(err)

                # ── Check if this is a board-mismatch rather than a real code bug ──
                try:
                    compat_boards, compat_reasons = detect_board_compatibility(self.sketch_dir_path)
                    selected_board = self.board_var.get()
                    is_board_mismatch = bool(compat_boards) and selected_board not in compat_boards
                except Exception:
                    is_board_mismatch = False
                    compat_boards = set()

                if is_board_mismatch:
                    # ── Board mismatch: show a single, clear message ──
                    # Summarise by platform family instead of listing 200+ board names
                    _platforms = {
                        SUPPORTED_BOARDS.get(b, {}).get("platform", "")
                        for b in compat_boards
                    }
                    _plat_labels = {
                        "espressif32": "ESP32-family",
                        "espressif8266": "ESP8266-family",
                        "atmelavr": "Arduino AVR (Uno, Nano, Mega …)",
                    }
                    family_names = sorted(
                        _plat_labels.get(p, p) for p in _platforms if p
                    )
                    compat_summary = ", ".join(family_names) if family_names else "other boards"
                    self._append("")
                    self._append("  ⚠ This sketch is NOT compatible with the selected board.", "warning")
                    self._append(f"     Selected board : {selected_board}", "warning")
                    self._append(f"     Compatible with: {compat_summary}", "info")
                    self._append("")
                    self._append("  💡 Please select a compatible board and try again.", "info")
                else:
                    # ── Real code issues: show the detailed per-file listing ──
                    # Pick header color: red only if real errors exist, yellow for warnings-only
                    has_real_errors = any(
                        "error" in e["type"].lower() and "warning" not in e["type"].lower()
                        for e in parsed_errors
                    )
                    header_tag = "error" if has_real_errors else "warning"
                    self._append("⚠️  ISSUES LISTED BY FILE:", header_tag)
                    self._append("─" * 50, header_tag)

                    for file_name, errs in errors_by_file.items():
                        self._append(f"  📁 {file_name}", "warning")
                        for err in errs:
                            severity = err["type"].lower().strip()
                            tag = "info" if "note" in severity else ("warning" if "warning" in severity else "error")
                            bullet = "•" if tag == "error" else ("⚠" if tag == "warning" else "ℹ")
                            self._append(f"     {bullet} Line {err['line']} (Col {err['col']}): {severity} — {err['msg']}", tag)
                    self._append("─" * 50, header_tag)
            else:
                self._append("─" * 50, "error")
                # Show actual error lines — GCC diagnostics + linker errors.
                # Explicitly exclude bare SCons make-error wrappers like
                #   "*** [.pio/build/.../foo.o] Error 1"
                # because those contain "error" but are NOT the real compiler
                # message — the actual g++ diagnostic appeared earlier and
                # showing only the wrapper hides the root cause from the user.
                LINKER_HINTS = (
                    "undefined reference to",
                    "multiple definition of",
                    "cannot find -l",
                    "undefined symbol",
                    "duplicate symbol",
                    "ld returned",
                    "collect2",
                    "overflowed by",
                    "will not fit in region",
                    "relocation truncated",
                    "ld.exe:",
                    "ld:",
                    "section `",
                    "region `",
                    "packageexception",
                    "not a directory",
                    "cannot create a symbolic link",
                    "exception:",
                )

                _scons_re = re.compile(r'^\*\*\*\s+\[')
                _gcc_context_re = re.compile(r'^\s*\d*\s*\|(?!--)')
                error_lines = [
                    l for l in output_lines
                    if (
                        # GCC/Clang diagnostic: file:line:col: error/note: msg
                        bool(re.search(r':\d+:\d+:\s+(error|fatal error|note)\s*:', l, re.IGNORECASE))
                        # Linker errors (no file:line:col prefix)
                        or any(hint in l.lower() for hint in LINKER_HINTS)
                        # GCC source-context lines (source, carets, suggestions)
                        or bool(_gcc_context_re.match(l.strip()))
                        # Generic "error" lines, but NOT SCons *** [...] Error N wrappers
                        or ("error" in l.lower() and "werror" not in l.lower()
                            and not _scons_re.match(l.strip()))
                    )
                ]
                if error_lines:
                    for line in error_lines[-20:]:
                        self._append(f"  {line}", "error")
                else:
                    # Nothing matched — dump raw tail so user sees something useful
                    error_lines = [l for l in output_lines[-20:] if l.strip()]
                    for line in error_lines:
                        self._append(f"  {line}", "error")
                self._append("─" * 50, "error")

            self._set_status("Compile FAILED", Theme.RED)
            av_lock_failure = is_transient_platformio_lock_report(output_lines)

            # Windows security/indexing software can briefly lock a freshly
            # generated object, archive, or SCons database after PlatformIO
            # has already exited.  Give that narrow failure one clean retry;
            # the incremental workspace is preserved, so successful objects
            # do not need to be rebuilt.  Never loop indefinitely and never
            # retry a real compiler diagnostic.
            if (
                av_lock_failure
                and not getattr(self, "_av_compile_retry_in_progress", False)
                and not was_killed
            ):
                self._append(
                    "  ⚠ A temporary antivirus/file lock interrupted the build; retrying once…",
                    "warning",
                )
                self._append(
                    "    Successful incremental objects are being preserved.",
                    "dim",
                )
                self._av_compile_retry_in_progress = True
                completed_process = self.process
                try:
                    try:
                        if completed_process and completed_process.stdout:
                            completed_process.stdout.close()
                    except Exception:
                        pass
                    self.process = None
                    self.is_busy = False
                    self._set_buttons_busy(False)
                    time.sleep(0.8)
                    return self._run_compile(is_upload=is_upload)
                finally:
                    self._av_compile_retry_in_progress = False

            failure_kind = classify_platformio_failure(output_lines)
            if failure_kind == "cache" and not is_clean_retry:
                self._append(
                    "  ⚠ PlatformIO reported explicit selected-board cache corruption.",
                    "warning",
                )
                removed, repair_errors = self._perform_clean_current_board()
                if removed and not repair_errors:
                    self._append(
                        "  ♻ Repaired only this board's workspace; every other board cache was preserved.",
                        "info",
                    )
                    self.is_busy = False
                    self._set_buttons_busy(False)
                    self._clean_retry_in_progress = True
                    if is_upload:
                        # _run_upload owns one bounded retry.
                        return None  # type: ignore[return-value]
                    return self._run_compile(is_upload=False)
                for repair_error in repair_errors:
                    self._append(f"  ⚠ Cache repair could not remove {repair_error}", "warning")
            elif failure_kind == "cache":
                self._append(
                    "  ⚠ Selected-board cache repair was already attempted once; preserving diagnostics and stopping.",
                    "warning",
                )
            elif failure_kind == "source":
                self._append(
                    "  ℹ Incremental cache preserved — fix the code and compile again; only changed/failed units rebuild.",
                    "info",
                )
            elif failure_kind == "configuration":
                self._append(
                    "  ℹ Build cache preserved — correct the board/library/configuration issue and retry.",
                    "info",
                )
            else:
                self._append(
                    "  ℹ Build cache preserved. No evidence of cache corruption was found, so Clean is not required.",
                    "info",
                )
            self.is_busy = False
            self._set_buttons_busy(False)
            return rc == 0

        hide_generated_directory(self._platformio_project_dir(self.sketch_dir_path))
        self.process = None
        if not is_upload or rc != 0:
            self.is_busy = False
            self._set_buttons_busy(False)
            self._set_buttons_state(False)
        return rc == 0

