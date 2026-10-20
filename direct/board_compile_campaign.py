#!/usr/bin/env python3
"""Compile explicit PlatformIO targets in a bounded Linux scratch package store.

This is an opt-in online audit runner, never an application readiness generator.
Input is {"schema": 1, "targets": [{"platform": "platformio/atmelavr@5.1.0",
"board": "uno", "framework": "arduino", ...provenance...}]}. A bare target list
and ``platform_spec`` in place of ``platform`` are also accepted. Every original
target field is retained in the append-only results. No upload target is invoked.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
from contextlib import contextmanager
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main.core.platformio_board_compat import board_compatibility_options, compatibility_rule
GIB = 1024 ** 3
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,159}$")
FRAMEWORK_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$")
ARDUINO_SAMPLE = "#include <Arduino.h>\nvoid setup() {}\nvoid loop() {}\n"
MARKER = ".board-compile-campaign.json"
RESULT_SCHEMA = 1


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def digest_file(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def timestamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def host_identity():
    release = Path("/etc/os-release")
    return {"system": platform.system(), "machine": platform.machine(),
            "python": platform.python_version(), "python_executable": sys.executable,
            "os_release_sha256": digest_file(release) if release.is_file() else None}


def load_manifest(path):
    data = Path(path).read_bytes()
    if len(data) > 16 * 1024 * 1024:
        raise ValueError("Target manifest exceeds 16 MiB")
    document = json.loads(data)
    targets = document.get("targets") if isinstance(document, dict) else document
    if isinstance(document, dict) and document.get("schema", 1) != 1:
        raise ValueError("Unsupported campaign manifest schema")
    if not isinstance(targets, list) or not targets or len(targets) > 100000:
        raise ValueError("Manifest requires a nonempty targets list (maximum 100000)")
    normalized = []
    seen = set()
    for row in targets:
        if not isinstance(row, dict):
            raise ValueError("Every target must be an object")
        spec = row.get("platform_spec") or row.get("platform")
        if (not isinstance(spec, str) or not spec.strip() or len(spec) > 4096
                or any(ord(char) < 32 for char in spec) or ";" in spec):
            raise ValueError("Each target requires one literal PlatformIO platform specification")
        board, framework = row.get("board"), row.get("framework")
        if not isinstance(board, str) or not IDENTIFIER.fullmatch(board):
            raise ValueError("Invalid exact PlatformIO board identifier")
        if not isinstance(framework, str) or not FRAMEWORK_IDENTIFIER.fullmatch(framework):
            raise ValueError("Invalid exact framework identifier")
        target = {"platform_spec": spec.strip(), "board": board, "framework": framework,
                  "provenance": row}
        # Different source declarations remain separate receipts even if they
        # resolve to the same target. Identical duplicates have no extra meaning.
        key = digest_bytes(canonical(target))
        if key not in seen:
            seen.add(key)
            normalized.append(target)
    return normalized, digest_bytes(data), document


def owned_root(path, resume):
    from direct.board_compile_indexes import scratch_path
    resolved = scratch_path(path).resolve()
    allowed = [ROOT / "temp", ROOT / "audit/migration/scratch"]
    if not any(resolved != base and resolved.is_relative_to(base) for base in allowed):
        raise ValueError("Scratch must be a descendant of checkout temp/ or audit/migration/scratch/")
    # Existing nonempty directories require our marker; never claim someone
    # else's fixture or package store merely because it lives under temp/.
    marker = resolved / MARKER
    if resolved.exists() and any(resolved.iterdir()):
        if not resume or not marker.is_file() or marker.is_symlink():
            raise ValueError("Nonempty scratch requires --resume and an existing campaign ownership marker")
        ownership = json.loads(marker.read_text(encoding="utf-8"))
        if ownership.get("root") != str(resolved) or ownership.get("kind") != "mcu-board-compile-campaign":
            raise ValueError("Campaign ownership marker does not match scratch")
    else:
        resolved.mkdir(parents=True, exist_ok=True)
        ownership = {"kind": "mcu-board-compile-campaign", "root": str(resolved),
                     "id": str(uuid.uuid4()), "created_at": timestamp()}
        with marker.open("x", encoding="utf-8") as output:
            json.dump(ownership, output, sort_keys=True)
            output.write("\n")
    for name in ("logs", "work"):
        child = resolved / name
        if child.is_symlink():
            raise ValueError(f"Owned campaign child cannot be a symlink: {name}")
        child.mkdir(exist_ok=True)
    return resolved


def remove_owned(child, scratch):
    child = Path(child)
    if child.is_symlink() or child.parent.resolve() != scratch.resolve() or child.name != "work":
        raise ValueError("Cleanup is restricted to this campaign's direct work/ directory")
    if child.exists():
        shutil.rmtree(child)


@contextmanager
def campaign_lock(scratch):
    """One runner owns scratch at a time; never delete a peer's work tree."""
    path = scratch / ".campaign.lock"
    if path.is_symlink():
        raise ValueError("Campaign lock must not be a symlink")
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Another campaign runner owns this scratch directory") from exc
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another campaign runner owns this scratch directory") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def allocated_bytes(path):
    total = 0
    for directory, folders, files in os.walk(path, followlinks=False):
        for name in folders + files:
            try:
                info = os.lstat(Path(directory) / name)
            except FileNotFoundError:
                continue
            total += getattr(info, "st_blocks", 0) * 512 if hasattr(info, "st_blocks") else info.st_size
    return total


def storage_state(scratch, reserve, cap):
    free = shutil.disk_usage(scratch).free
    owned = allocated_bytes(scratch)
    reason = ("free_space_reserve" if free < reserve else "owned_storage_cap" if owned > cap else None)
    return {"free_bytes": free, "owned_bytes": owned, "reason": reason}


def child_environment(work, jobs):
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("PLATFORMIO_") and key not in {
                       "PYTHONHOME", "PYTHONPATH", "MCU_FLASHER_OFFLINE_RUNTIME",
                       "MCU_FLASHER_WORKSPACE_RUNTIME", "MCU_FLASHER_APP_ROOT", "PIP_NO_INDEX",
                       "SCONSFLAGS", "PIP_TARGET", "PIP_PREFIX", "PIP_USER"}}
    core = work / "core"
    folders = {"CORE": core, "PACKAGES": core / "packages", "PLATFORMS": core / "platforms",
               "GLOBALLIB": core / "lib", "CACHE": core / ".cache", "BUILD_CACHE": work / "build-cache"}
    for key, folder in folders.items():
        folder.mkdir(parents=True, exist_ok=True)
        environment[f"PLATFORMIO_{key}_DIR"] = str(folder)
    private_home, temporary = work / "home", work / "tmp"
    private_home.mkdir(exist_ok=True)
    temporary.mkdir(exist_ok=True)
    environment.update({"HOME": str(private_home), "USERPROFILE": str(private_home),
                        "APPDATA": str(private_home / "AppData/Roaming"),
                        "LOCALAPPDATA": str(private_home / "AppData/Local"),
                        "XDG_CONFIG_HOME": str(private_home / ".config"),
                        "XDG_CACHE_HOME": str(private_home / ".cache"),
                        "XDG_DATA_HOME": str(private_home / ".local/share"),
                        "TMP": str(temporary), "TEMP": str(temporary), "TMPDIR": str(temporary),
                        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                        "PLATFORMIO_NO_TELEMETRY": "1", "PLATFORMIO_SETTING_ENABLE_TELEMETRY": "false",
                        "PLATFORMIO_DISABLE_UPGRADE_CHECK": "true", "PLATFORMIO_BUILD_JOBS": str(jobs),
                        "PLATFORMIO_RUN_JOBS": str(jobs), "SCONSFLAGS": f"-j{jobs}"})
    return environment


class OwnedLinuxChild:
    """Retain the direct child until its captured session/group is cleaned.

    WNOWAIT prevents the leader PID/group number being recycled between signal
    phases. Only this object may reap that child; losing it refuses signaling.
    The output-reader thread never observes or reaps process state.
    """

    def __init__(self, command, **kwargs):
        if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
            raise RuntimeError("Owned campaign child requires default SIGCHLD disposition")
        self.process = subprocess.Popen(command, start_new_session=True, **kwargs)
        self.pid = self.process.pid
        self.stdout = self.process.stdout
        self.closed = False

    def _owned(self):
        try:
            event = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError as exc:
            raise RuntimeError("Campaign child was reaped elsewhere; refusing group signals") from exc
        if os.getpgid(self.pid) != self.pid or os.getsid(self.pid) != self.pid:
            raise RuntimeError("Campaign child no longer owns its captured session/group")
        return event

    def poll(self):
        if self.closed:
            return self.process.returncode
        event = self._owned()
        return None if event is None else event.si_status if event.si_code == os.CLD_EXITED else -event.si_status

    @property
    def returncode(self):
        return self.poll()

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        while (result := self.poll()) is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.process.args, timeout)
            time.sleep(.02)
        return result

    def live_members(self):
        self._owned()
        group = str(self.pid).encode()
        members = []
        with os.scandir("/proc") as entries:
            for count, entry in enumerate(entries):
                if count > 65536:
                    raise RuntimeError("Campaign process inventory exceeds cleanup bound")
                if not entry.name.isdecimal():
                    continue
                try:
                    with open(Path(entry.path) / "stat", "rb") as source:
                        raw = source.read(8193)
                except (FileNotFoundError, ProcessLookupError, PermissionError):
                    continue
                if len(raw) > 8192:
                    raise RuntimeError("Campaign process metadata exceeds cleanup bound")
                fields = raw[raw.rfind(b")") + 2:].split()
                if len(fields) >= 4 and fields[2:4] == [group, group] and fields[0] not in (b"Z", b"X"):
                    members.append(int(entry.name))
        return members

    def terminate_group(self):
        if self.closed:
            return
        self._owned()
        for sig in (signal.SIGTERM, signal.SIGKILL):
            os.killpg(self.pid, sig)
            deadline = time.monotonic() + 2
            while self.live_members():
                if time.monotonic() >= deadline:
                    break
                time.sleep(.02)
            else:
                self.process.wait(timeout=5)
                self.closed = True
                return
        raise RuntimeError("Owned campaign group remains alive after SIGKILL")


def terminate(process):
    if not isinstance(process, OwnedLinuxChild):
        raise TypeError("Cleanup requires the captured campaign child capability")
    process.terminate_group()


def run_child(command, *, environment, cwd, log_path, scratch, timeout, reserve, cap, log_limit):
    if sys.platform != "linux":
        raise RuntimeError("This audit campaign's process lifecycle is qualified only on Linux")
    before = storage_state(scratch, reserve, cap)
    if before["reason"]:
        return {"status": "storage_deferred", "storage": before, "exit_code": None,
                "elapsed_seconds": 0, "command": command, "log": None}
    started = time.monotonic()
    kwargs = {"env": environment, "cwd": cwd, "stdin": subprocess.DEVNULL,
              "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT}
    process = OwnedLinuxChild(command, **kwargs)
    reader_error, tail, written = [], bytearray(), [0]
    done = threading.Event()

    def consume():
        try:
            with gzip.open(log_path, "wb") as output:
                while chunk := process.stdout.read(16384):
                    tail.extend(chunk)
                    if len(tail) > 8192:
                        del tail[:-8192]
                    remaining = max(0, log_limit - written[0])
                    if remaining:
                        output.write(chunk[:remaining])
                        written[0] += min(remaining, len(chunk))
                    if len(chunk) > remaining and written[0] <= log_limit:
                        output.write(b"\n[Campaign log limit reached; remaining output drained.]\n")
                        written[0] = log_limit + 1
        except Exception as exc:
            reader_error.append(str(exc))
        finally:
            done.set()

    reader = threading.Thread(target=consume, name="board-campaign-output", daemon=True)
    reader.start()
    status, state = None, before
    next_storage, next_heartbeat = 0.0, started + 20
    try:
        while process.poll() is None or not done.is_set():
            now = time.monotonic()
            if reader_error:
                status = "output_failed"
                break
            if now - started >= timeout:
                status = "timed_out"
                break
            if now >= next_storage:
                state = storage_state(scratch, reserve, cap)
                next_storage = now + 3
                if state["reason"]:
                    status = "storage_deferred"
                    break
            if now >= next_heartbeat:
                print(f"  running {now - started:.0f}s; {state['free_bytes'] / GIB:.1f} GiB free", flush=True)
                next_heartbeat = now + 20
            time.sleep(.15)
        if status:
            terminate(process)
        else:
            status = "succeeded" if process.wait(timeout=5) == 0 else "failed"
    finally:
        terminate(process)
        reader.join(timeout=5)
        if reader.is_alive():
            raise RuntimeError("Owned child output reader failed to stop")
        process.stdout.close()
    return {"status": status, "exit_code": process.returncode,
            "elapsed_seconds": round(time.monotonic() - started, 3), "command": command,
            "log": str(log_path.relative_to(scratch)), "log_limit_bytes": log_limit,
            "log_truncated": written[0] > log_limit, "tail": bytes(tail).decode("utf-8", "replace"),
            "reader_errors": reader_error, "storage": state}


def installed_identity(core, board):
    candidates = []
    for directory in sorted((core / "platforms").iterdir()):
        manifest, definition = directory / "platform.json", directory / "boards" / f"{board}.json"
        if manifest.is_file() and definition.is_file():
            if directory.is_symlink() or not definition.resolve().is_relative_to(core.resolve()):
                raise ValueError("Installed platform or board manifest escaped the owned core")
            metadata = json.loads(manifest.read_text(encoding="utf-8-sig"))
            candidates.append({"name": metadata.get("name"), "version": metadata.get("version"),
                               "manifest_path": str(definition),
                               "platform_manifest_sha256": digest_file(manifest),
                               "platform_receipt_sha256": digest_file(directory / ".piopm") if (directory / ".piopm").is_file() else None,
                               "board_manifest_sha256": digest_file(definition),
                               "definition": json.loads(definition.read_text(encoding="utf-8-sig"))})
    if len(candidates) != 1:
        raise ValueError(f"Expected one installed exact definition for {board}, found {len(candidates)}")
    return candidates[0]


def package_identity(core):
    rows = []
    for directory in sorted((core / "packages").iterdir()):
        manifest, receipt = directory / "package.json", directory / ".piopm"
        if manifest.is_file():
            if directory.is_symlink() or not manifest.resolve().is_relative_to(core.resolve()):
                raise ValueError("Installed package escaped the owned core")
            metadata = json.loads(manifest.read_text(encoding="utf-8-sig"))
            rows.append({"directory": directory.name, "name": metadata.get("name"),
                         "version": metadata.get("version"), "manifest_sha256": digest_file(manifest),
                         "receipt_sha256": digest_file(receipt) if receipt.is_file() else None})
    return rows


def verify_expected_identity(target, identity):
    """Bind enriched index matching to the installed bytes before compilation."""
    for field in ("board_manifest_sha256", "platform_manifest_sha256"):
        expected = target.get("provenance", {}).get("expected_" + field)
        if expected is not None and expected != identity.get(field):
            raise ValueError(f"Installed {field} differs from the matched catalog")


def firmware_receipts(project):
    build = project / ".pio/build/campaign"
    artifacts = []
    if build.is_dir():
        for path in sorted(build.iterdir()):
            if path.suffix.lower() in {".elf", ".bin", ".hex", ".uf2", ".srec"} and path.is_file():
                if path.is_symlink():
                    raise ValueError("Firmware artifact must not be a symlink")
                artifacts.append({"name": path.name, "bytes": path.stat().st_size, "sha256": digest_file(path)})
    return artifacts


def append_result(path, receipt):
    if path.is_file() and path.stat().st_size:
        with path.open("rb") as existing:
            existing.seek(-1, os.SEEK_END)
            incomplete = existing.read(1) != b"\n"
        if incomplete:
            with path.open("ab") as existing:
                existing.write(b"\n")
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())


def previous_results(path):
    results = {}
    if path.is_file():
        with path.open(encoding="utf-8") as source:
            for line in source:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # A killed append never grants a resume success.
                if row.get("status") in {"passed", "resumed_pass"}:
                    results[row.get("binding")] = row
    return results


def project_configuration(target, identity, core):
    """Use the application's reviewed recipe while preserving the exact target."""
    info = {"platform": identity["name"], "board": target["board"], "framework": target["framework"],
            "pio_manifest": identity.get("manifest_path", "")}
    options = board_compatibility_options(info, core)
    text = (f"[env:campaign]\nplatform = {target['platform_spec']}\nboard = {target['board']}\n"
            f"framework = {target['framework']}\n")
    return text + "".join(f"{key} = {value}\n" for key, value in options.items()), options


def campaign(arguments):
    targets, manifest_hash, document = load_manifest(arguments.manifest)
    scratch = owned_root(arguments.scratch, arguments.resume)
    with campaign_lock(scratch):
        return _campaign(arguments, scratch, targets, manifest_hash, document)


def _campaign(arguments, scratch, targets, manifest_hash, document):
    results_path = scratch / "results.jsonl"
    if results_path.is_symlink():
        raise ValueError("Result journal must not be a symlink")
    prior = previous_results(results_path) if arguments.resume else {}
    context = {"schema": RESULT_SCHEMA, "manifest_sha256": manifest_hash,
               "harness_sha256": digest_file(__file__), "host": host_identity(),
               "board_compatibility_sha256": digest_file(ROOT / "main/core/platformio_board_compat.py"),
               "firmware_tool_hook_sha256": digest_file(ROOT / "src/modules/arduinoq_link_paths.py"),
               "sample_sha256": digest_bytes(ARDUINO_SAMPLE.encode())}
    batches = OrderedDict()
    for target in targets:
        batches.setdefault(target["platform_spec"], []).append(target)
    reserve, cap = int(arguments.reserve_gib * GIB), int(arguments.max_owned_gib * GIB)
    sequence, failed, stopped = 0, False, False
    completed_counts = {}
    common = {"scratch": scratch, "reserve": reserve, "cap": cap,
              "log_limit": arguments.log_limit_kib * 1024}
    try:
        for spec, rows in batches.items():
            remove_owned(scratch / "work", scratch)
            work = scratch / "work"
            work.mkdir()
            environment = child_environment(work, arguments.jobs)
            core = work / "core"
            batch_id = digest_bytes(spec.encode())[:16]
            print(f"Preparing {spec}: {len(rows)} explicit target(s)", flush=True)
            command = [sys.executable, "-B", "-m", "platformio", "pkg", "install", "--global",
                       "--platform", spec, "--skip-dependencies"]
            provision = run_child(command, environment=environment, cwd=work,
                                  log_path=scratch / "logs" / f"platform-{batch_id}-{time.time_ns()}.log.gz",
                                  timeout=arguments.provision_timeout, **common)
            if provision["status"] != "succeeded":
                for target in rows:
                    receipt = {"schema": RESULT_SCHEMA, "at": timestamp(), "target": target,
                               "context": context, "status": "storage_deferred" if provision["status"] == "storage_deferred" else "provisioning_failed",
                               "provision": provision, "upload": False, "live_persistence": False}
                    append_result(results_path, receipt)
                failed = True
                status = "storage_deferred" if provision["status"] == "storage_deferred" else "provisioning_failed"
                completed_counts[status] = completed_counts.get(status, 0) + len(rows)
                if provision["status"] == "storage_deferred":
                    stopped = True
                    break
                continue
            for target in rows:
                sequence += 1
                receipt = {"schema": RESULT_SCHEMA, "at": timestamp(), "target": target, "context": context,
                           "upload": False, "live_persistence": False}
                project = work / "project"
                if project.exists():
                    shutil.rmtree(project)
                project.mkdir()
                try:
                    identity = installed_identity(core, target["board"])
                    verify_expected_identity(target, identity)
                    receipt["installed_platform"] = {key: value for key, value in identity.items() if key != "definition"}
                    declared = identity["definition"].get("frameworks", [])
                    if target["framework"] not in declared:
                        receipt.update(status="framework_not_declared", reason="Exact installed board does not declare the requested framework")
                    elif target["framework"] != "arduino":
                        receipt.update(status="unsupported_sample_framework", reason="No reviewed minimal sample is supplied for this exact non-Arduino framework")
                    else:
                        (project / "src").mkdir()
                        (project / "src/main.cpp").write_text(ARDUINO_SAMPLE, encoding="utf-8")
                        configuration, compatibility = project_configuration(target, identity, core)
                        (project / "platformio.ini").write_text(configuration, encoding="utf-8")
                        if compatibility:
                            receipt["board_compatibility"] = {"rule": compatibility_rule(compatibility), "options": compatibility,
                                                              "helper_sha256": context["board_compatibility_sha256"]}
                        packages_before = package_identity(core)
                        binding = digest_bytes(canonical({"context": context, "target": target,
                                                          "platform": receipt["installed_platform"], "packages": packages_before}))
                        previous = prior.get(binding)
                        if previous and packages_before:
                            receipt.update(status="resumed_pass", binding=binding, packages=packages_before,
                                           artifacts=previous["artifacts"], prior_completed_at=previous["at"],
                                           reason="Exact manifest, host, harness, sample, platform/board and installed package receipts match")
                        else:
                            print(f"[{sequence}/{len(targets)}] {spec}:{target['board']} ({target['framework']})", flush=True)
                            command = [sys.executable, "-B", "-m", "platformio", "run", "-d", str(project),
                                       "-e", "campaign", "--jobs", str(arguments.jobs)]
                            compiled = run_child(command, environment=environment, cwd=work,
                                                 log_path=scratch / "logs" / f"{sequence:06d}-{target['board']}-{time.time_ns()}.log.gz",
                                                 timeout=arguments.timeout, **common)
                            # A cold first build may install the exact packages
                            # needed to prove a reviewed compatibility recipe.
                            # Permit one build-only retry only when that proof
                            # appears; preserve the original failure receipt.
                            if not compatibility and compiled["status"] == "failed":
                                repaired_configuration, repaired = project_configuration(target, identity, core)
                                if repaired:
                                    receipt["compatibility_preparation_compile"] = compiled
                                    compatibility = repaired
                                    receipt["board_compatibility"] = {"rule": compatibility_rule(repaired), "options": repaired,
                                                                      "helper_sha256": context["board_compatibility_sha256"]}
                                    (project / "platformio.ini").write_text(repaired_configuration, encoding="utf-8")
                                    compiled = run_child(command, environment=environment, cwd=work,
                                                         log_path=scratch / "logs" / f"{sequence:06d}-{target['board']}-compat-{time.time_ns()}.log.gz",
                                                         timeout=arguments.timeout, **common)
                            receipt["compile"] = compiled
                            receipt["packages"] = package_identity(core)
                            receipt["binding"] = digest_bytes(canonical({"context": context, "target": target,
                                                                        "platform": receipt["installed_platform"], "packages": receipt["packages"]}))
                            receipt["artifacts"] = firmware_receipts(project)
                            if compiled["status"] == "succeeded":
                                receipt["status"] = "passed" if receipt["artifacts"] else "missing_artifact"
                            else:
                                receipt["status"] = "compile_failed" if compiled["status"] == "failed" else compiled["status"]
                    if receipt["status"] not in {"passed", "resumed_pass"}:
                        failed = True
                    append_result(results_path, receipt)
                    completed_counts[receipt["status"]] = completed_counts.get(receipt["status"], 0) + 1
                    print(f"  {receipt['status']}: {target['board']}", flush=True)
                    if receipt["status"] == "storage_deferred":
                        stopped = True
                        break
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    receipt.update(status="runner_failed", reason=str(exc))
                    append_result(results_path, receipt)
                    failed = True
                    completed_counts["runner_failed"] = completed_counts.get("runner_failed", 0) + 1
                    print(f"  runner_failed: {target['board']}: {exc}", flush=True)
                finally:
                    if project.exists():
                        shutil.rmtree(project)
                    for child in (work / "build-cache", work / "tmp"):
                        if child.exists():
                            shutil.rmtree(child)
                        child.mkdir()
            if stopped:
                break
    finally:
        remove_owned(scratch / "work", scratch)
    summary = {"schema": RESULT_SCHEMA, "at": timestamp(), "requested_targets": len(targets),
               "stopped_for_storage": stopped, "all_targets_passed": not failed,
               "this_run_counts": completed_counts,
               "compiled_this_run": completed_counts.get("passed", 0),
               "resumed_previous_passes": completed_counts.get("resumed_pass", 0),
               "unattempted_targets": len(targets) - sum(completed_counts.values()),
               "results": str(results_path), "context": context,
               "manifest_provenance": {key: value for key, value in document.items() if key != "targets"} if isinstance(document, dict) else {}}
    summary_path = scratch / "summary.json"
    if summary_path.is_symlink():
        raise ValueError("Summary must not be a symlink")
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 2 if stopped else 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--provision-timeout", type=float, default=900)
    parser.add_argument("--reserve-gib", type=float, default=5)
    parser.add_argument("--max-owned-gib", type=float, default=6)
    parser.add_argument("--log-limit-kib", type=int, default=1024)
    parser.add_argument("--resume", action="store_true")
    arguments = parser.parse_args(argv)
    if (arguments.jobs < 1 or arguments.jobs > 256 or arguments.timeout <= 0
            or arguments.provision_timeout <= 0 or arguments.reserve_gib < 5
            or arguments.max_owned_gib <= 0 or arguments.max_owned_gib > 8
            or arguments.log_limit_kib < 1 or arguments.log_limit_kib > 8192):
        parser.error("Require 1..256 jobs, positive timeouts, reserve >=5 GiB, cap <=8 GiB and 1..8192 KiB logs")
    try:
        return campaign(arguments)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Campaign refused/failed: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
