"""Single-attempt Arduino CLI builds/uploads for certified exact source boards."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

from src.modules.arduino_cli_support import runtime_command, sha256_file
from main.core import arduino_inputs


def _log(api, text, tag="info"):
    api.emit("console:log", {"text": text, "tag": tag, "newline": True})


def _stream(api, command, environment, cwd, *, upload=False, upload_log=None):
    from main import web_bridge
    from main.core.connection_loss import SerialConnectionGuard
    api._active_process = subprocess.Popen(
        command, cwd=str(cwd), env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
        **web_bridge._HOST_RUNTIME.process_options(priority=True, session=True))
    guard = SerialConnectionGuard(api, api._active_process,
                                  getattr(api, "_active_port_label", api.current_port),
                                  dict(getattr(api, "_active_board_info", {}) or {})) if upload else None
    try:
        for raw in web_bridge._iter_process_output(
                api._active_process, lambda: bool(api._stop_requested and not upload),
                api._kill_active_process_tree,
                lambda seconds: _log(api, f"Arduino CLI is still {'uploading' if upload else 'compiling'} ({seconds}s without new output)…", "dim"),
                on_poll=guard.poll if guard is not None else None):
            line = web_bridge._strip_terminal_escapes(raw).rstrip()
            if not line:
                continue
            if upload_log is not None and upload_log.consume(line):
                continue
            tag = "error" if re.search(r"\b(error|failed|failure|exception)\b", line, re.I) else "warning" if "warning" in line.lower() else "dim"
            _log(api, line, tag)
        code = guard.wait() if guard is not None else api._active_process.wait()
        if api._stop_requested and not upload:
            raise InterruptedError("Compilation cancelled")
        if code:
            raise RuntimeError(f"Arduino CLI {'upload' if upload else 'compile'} exited with code {code}. Review the console output")
    finally:
        process = api._active_process
        if process is not None:
            if not upload and process.poll() is None:
                api._kill_active_process_tree()
            if process.poll() is None:
                # Never unlock a window while an upload child may still write.
                guard.finish() if guard is not None else process.wait()
            if process.stdout is not None:
                process.stdout.close()
        if guard is not None:
            guard.close()
        api._active_process = None


def _stage_sources(api, staging):
    from main.core.file_utils import get_project_root_source_files
    sources = sorted(get_project_root_source_files(api.sketch_dir_path, (".ino", ".cpp", ".c", ".h", ".hpp")), key=lambda p: p.name)
    if not sources:
        raise RuntimeError("The sketch has no root source files")
    inos = [path for path in sources if path.suffix.lower() == ".ino"]
    primary = next((path for path in inos if path.stem.casefold() == api.sketch_dir_path.name.casefold()), None)
    if primary is None and inos:
        primary = next((path for path in inos if re.search(r"\bvoid\s+(?:setup|loop)\s*\(", path.read_text(encoding="utf-8", errors="replace"))), inos[0])
    staging.mkdir(parents=True, exist_ok=True)
    expected = set()
    staged_bytes = {}
    staged_sources = {}
    for path in sources:
        destination = staging / (f"{staging.name}.ino" if path == primary else path.name)
        if destination.name in expected:
            raise RuntimeError("The sketch source names collide when preparing Arduino CLI")
        expected.add(destination.name)
        api._stage_root_source_file(path, destination)
        digest = sha256_file(destination)
        if sha256_file(path) != digest:
            raise RuntimeError("Sketch source bytes changed while preparing Arduino CLI. Compile again")
        staged_bytes[destination.name] = digest
        staged_sources[path.name] = destination
    if primary is None:
        name = staging.name + ".ino"
        expected.add(name)
        (staging / name).write_text("// Root C/C++ source files provide the sketch entry points.\n", encoding="utf-8")
        staged_bytes[name] = sha256_file(staging / name)
    # The folder contains only our previously staged root sources.
    for path in staging.iterdir():
        if path.is_file() and path.suffix.lower() in (".ino", ".cpp", ".c", ".h", ".hpp") and path.name not in expected:
            path.unlink()
    return staged_sources, staged_bytes


def _receipt_matches(path, source_hash, fqbn, build, inputs, *, inputs_only=False):
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            return False
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict):
            return False
        files = receipt.get("files")
        if receipt.get("fqbn") != fqbn:
            return False
        if not arduino_inputs.matches(receipt.get("inputs"), inputs):
            return False
        if inputs_only:
            return True
        if receipt.get("source_hash") != source_hash or not isinstance(files, dict) or not files or len(files) > 128:
            return False
        for name, digest in files.items():
            if Path(name).name != name or sha256_file(build / name) != digest:
                return False
        return True
    except (OSError, ValueError, TypeError):
        return False


def _workspace(api, board, info, fqbn):
    """Select this exact board's native-host workspace without touching files."""
    key = hashlib.sha256(json.dumps({"fqbn": fqbn, "core": info.get("arduino_cli")}, sort_keys=True).encode()).hexdigest()[:16]
    return api._board_workspace_dir(board) / "arduino-cli" / key


def cached_build_status(api, board=None):
    """Read-only worker check of the selected board's certified CLI build."""
    target = board or api.current_board
    if not target:
        return True, "no board selected"
    if not api.sketch_dir_path:
        return True, "no sketch folder loaded"
    if any(getattr(api, "modified_files", {}).values()):
        return True, "unsaved modifications in editor"
    try:
        info = api._resolve_board_info(target)
        command, _environment, fqbn = runtime_command(info)
        source_hash = api._hash_sources(target)
        workspace = _workspace(api, target, info, fqbn)
        collections, _flags = arduino_inputs.library_collections(command, info)
        inputs = arduino_inputs.snapshot(collections, byte_hashes=False)
        if not _receipt_matches(workspace / "build-receipt.json", source_hash, fqbn,
                                workspace / "build", inputs):
            return True, "no unchanged, verified Arduino CLI build for this board"
        return False, "unchanged sketch, Arduino library and firmware bytes"
    except Exception as exc:
        return True, f"Arduino CLI build verification unavailable: {exc}"


def run_arduino_operation(api, *, upload=False):
    """Resolve current certificates, stage root sources and execute once."""
    from main import web_bridge
    board = str(api.current_board or "")
    port = str(getattr(api, "_active_port_label", api.current_port) or "") if upload else ""
    monitor_paused = False
    upload_started = False
    upload_log = None
    upload_time = None
    try:
        info = api._resolve_board_info(board)
        command, environment, fqbn = runtime_command(info)
        if info.get("arduino_backend_role") == "primary":
            _log(api, f"Using the prepared original Arduino target {fqbn} with its declared defaults.", "info")
            api.emit("notification", {"title": "Arduino source target", "message": f"Using the prepared original Arduino definition for {board}.", "type": "info"})
        else:
            _log(api, "PlatformIO does not support this exact board yet. Using the verified Arduino CLI fallback.", "warning")
            api.emit("notification", {"title": "Arduino CLI fallback", "message": f"PlatformIO does not support {board} yet. Using Arduino CLI.", "type": "warning"})
        source_hash = api._hash_sources(board)
        workspace = _workspace(api, board, info, fqbn)
        staging, build = workspace / "sketch/FallbackSketch", workspace / "build"
        receipt = workspace / "build-receipt.json"
        collections, library_flags = arduino_inputs.library_collections(command, info)
        inputs = arduino_inputs.snapshot(collections, byte_hashes=False)
        previous_inputs_valid = _receipt_matches(receipt, source_hash, fqbn, build, inputs, inputs_only=True)
        previous_valid = _receipt_matches(receipt, source_hash, fqbn, build, inputs)
        can_skip = bool(upload and getattr(api, "_active_skip_compile", False) and previous_valid)
        if not can_skip:
            external_inputs = arduino_inputs.previous_dependency_bytes(receipt, collections)
            receipt.unlink(missing_ok=True)
            api.is_busy = True
            api.active_operation = "upload" if upload else "compile"
            api._current_op_phase = "compiling"
            api.emit("operation:phase", {"phase": "compile", "is_busy": True, "can_stop": True, "op": api.active_operation})
            api.emit("window:closable", {"closable": True})
            staged_sources, staged_bytes = _stage_sources(api, staging)
            if api._hash_sources(board) != source_hash or api._hash_sources(board, source_files=staged_sources) != source_hash:
                raise RuntimeError("Sketch source bytes changed while preparing Arduino CLI. Compile again")
            build.mkdir(parents=True, exist_ok=True)
            # Rebuild final images from preserved objects; stale outputs cannot
            # turn an empty/incorrect CLI success into a reusable certificate.
            for path in build.iterdir():
                if path.is_file() and path.suffix.lower() in (".hex", ".bin", ".uf2", ".elf"):
                    path.unlink()
            compile_command = command + ["compile", "--fqbn", fqbn, "--build-path", str(build), "--jobs", str(api._get_jobs()), str(staging)]
            # Arduino supports repeated collection flags. Both stores were
            # prepared by Bootstrap/downloader; this worker never installs.
            compile_command += library_flags
            if not previous_inputs_valid:
                # Coarse-timestamp library edits must not reuse stale objects.
                compile_command += ["--clean"]
            before_inputs = arduino_inputs.snapshot(collections)
            before_inputs["files"].update(external_inputs)
            api.emit("console:progress", {"action": "Compiling"})
            _stream(api, compile_command, environment, workspace)
            if api._hash_sources(board) != source_hash or api.current_board != board:
                raise RuntimeError("The sketch or board changed during compilation. Compile again before uploading")
            if any(sha256_file(staging / name) != digest for name, digest in staged_bytes.items()):
                raise RuntimeError("Staged sketch bytes changed during compilation. Compile again before uploading")
            selected_inputs = arduino_inputs.selected_inputs(build, workspace, before_inputs)
            inputs = arduino_inputs.snapshot(collections, byte_hashes=False)
            if not arduino_inputs.matches(selected_inputs, inputs):
                raise RuntimeError("Arduino library inputs changed during compilation. Compile again before uploading")
            binaries = {path.name: sha256_file(path) for path in build.iterdir() if path.is_file() and path.suffix.lower() in (".hex", ".bin", ".uf2", ".elf")}
            if not binaries or len(binaries) > 128:
                raise RuntimeError("Arduino CLI completed without a firmware output")
            from main.core.file_utils import write_generated_text
            write_generated_text(receipt, json.dumps({"source_hash": source_hash, "fqbn": fqbn, "files": binaries,
                                                       "inputs": selected_inputs}, sort_keys=True))
            api._last_source_hash, api._last_compiled_board = source_hash, board
            _log(api, "Arduino CLI compilation completed.", "success")
            api.update_skip_compile_availability()
        else:
            _log(api, "Verified unchanged sketch, library and firmware bytes; reusing the Arduino CLI build.", "info")
        if not upload:
            api.emit("notification", {"title": "Build completed", "message": "Arduino CLI compiled the exact selected board.", "type": "success"})
            api.emit("console:progress", {"action": "Completed", "percent": 100})
            return True
        if api._stop_requested:
            raise InterruptedError("Upload cancelled before writing")
        if not port or api.current_port != port or api.current_board != board:
            raise RuntimeError("The selected upload port or board changed during compilation")
        owner = web_bridge.port_occupied_owner(port)
        if owner:
            raise RuntimeError(f"Port {port} is in use by another window (PID {owner})")
        # Re-check preparation and source/firmware bytes at the write boundary.
        runtime_command(info)
        current_collections, _ = arduino_inputs.library_collections(command, info)
        inputs = arduino_inputs.snapshot(current_collections, byte_hashes=False)
        if api._hash_sources(board) != source_hash or not _receipt_matches(receipt, source_hash, fqbn, build, inputs):
            raise RuntimeError("Sketch, library or firmware bytes changed before upload. Compile again")
        if api._stop_requested:
            raise InterruptedError("Upload cancelled before writing")
        if api.current_port != port or api.current_board != board:
            raise RuntimeError("The selected upload port or board changed during upload preparation")
        api._check_write_connection(port, info)
        api._stop_serial_monitor()
        monitor_paused = True
        api.is_busy = True
        api.active_operation = "flash"
        api._current_op_phase = "flashing"
        api.emit("operation:phase", {"phase": "flash", "is_busy": True, "can_stop": False, "op": "upload"})
        api.emit("window:closable", {"closable": False})
        api.emit("console:progress", {"action": "Uploading"})
        upload_started = True
        from main.core.upload_log import UploadLog
        upload_log = UploadLog(api, info, board, port, "", backend="Arduino CLI")
        upload_log.start()
        upload_time = time.monotonic()
        _stream(api, command + ["upload", "--fqbn", fqbn, "--port", port, "--input-dir", str(build), str(staging)], environment, workspace, upload=True, upload_log=upload_log)
        upload_log.finish(True, time.monotonic() - upload_time)
        api.emit("notification", {"title": "Upload completed", "message": "Arduino CLI reported a successful upload.", "type": "success"})
        api.emit("console:progress", {"action": "Completed", "percent": 100})
        return True
    except InterruptedError as exc:
        if upload_log is not None:
            upload_log.finish(False, time.monotonic() - upload_time if upload_time is not None else 0, stopped=True)
        _log(api, str(exc), "warning")
        api.emit("console:progress", {"action": "Cancelled"})
        return False
    except Exception as exc:
        if upload_log is not None:
            upload_log.finish(False, time.monotonic() - upload_time if upload_time is not None else 0)
        _log(api, f"Arduino CLI {'upload' if upload else 'build'} failed: {exc}", "error")
        if upload_started:
            _log(api, "The firmware may be incomplete. The upload was attempted once; stabilize the connection before retrying.", "warning")
        if not getattr(api, "_operation_connection_loss", ""):
            api.emit("notification", {"title": "Upload failed" if upload else "Build failed", "message": str(exc), "type": "error"})
        return False
    finally:
        api._active_process = None
        api._release_requested_operation()
        if monitor_paused and api.current_port == port and not getattr(api, "_operation_connection_loss", ""):
            api._start_serial_monitor()
