"""Mutation checks for native-owned recipe observations; no live package access."""
from pathlib import Path
import ast
import ctypes
import hashlib
import json
import os
import platform
import queue
import stat
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[2]
EXE = ROOT / "desktop/target/debug" / (
    "recipe_generation_probe.exe" if sys.platform == "win32" else "recipe_generation_probe")
SCRATCH = ROOT / "audit/migration/scratch/recipe-generation"
FRAME_LIMIT = 8192
WAIT_SECONDS = 8

# Read only literal upstream fixture bytes, without importing application code.
tree = ast.parse((ROOT / "direct/verify_platformio_board_compat.py").read_text(encoding="utf-8"))
literals = {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in {"BOARD_BYTES", "MINIMAIN_BYTES"}}
q_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "UnoQChecks")
q_literals = ast.literal_eval(next(node.value for node in q_class.body
                                 if isinstance(node, ast.Assign)
                                 and isinstance(node.targets[0], ast.Name)
                                 and node.targets[0].id == "METADATA"))
HOOK = (ROOT / "src/modules/arduinoq_link_paths.py").read_bytes()
seed_tree = ast.parse((ROOT / "desktop/scripts/verify-board-compatibility.py").read_text(encoding="utf-8"))
seed_functions = [node for node in seed_tree.body if isinstance(node, ast.FunctionDef)
                  and node.name in {"write_json", "seed"}]
assert len(seed_functions) == 2
namespace = dict(Path=Path, json=json, literals=literals, q_literals=q_literals, HOOK=HOOK)
exec(compile(ast.Module(body=seed_functions, type_ignores=[]), str(seed_tree), "exec"), namespace)
seed = namespace["seed"]
identity_tree = ast.parse((ROOT / "desktop/scripts/verify-file-identity.py").read_text(encoding="utf-8"))
identity_function = next(node for node in identity_tree.body
                         if isinstance(node, ast.FunctionDef) and node.name == "reference_identity")
identity_namespace = dict(sys=sys, ctypes=ctypes)
exec(compile(ast.Module(body=[identity_function], type_ignores=[]), str(identity_tree), "exec"), identity_namespace)
native_identity = identity_namespace["reference_identity"]
checks = []
commands = 0


def fingerprint(root):
    result = {}
    for path in root.rglob("*"):
        metadata = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(metadata.st_mode) else None
        result[str(path.relative_to(root))] = (metadata.st_mode, metadata.st_size,
                                              metadata.st_mtime_ns, digest)
    return result


class Probe:
    def __init__(self, root):
        self.child = subprocess.Popen([str(EXE), "--fixture-root", str(root)],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE)
        self.frames = queue.Queue(maxsize=66)
        self.errors = bytearray()
        self.closed = False

        def read_output():
            while True:
                line = self.child.stdout.readline(FRAME_LIMIT + 1)
                self.frames.put(line)
                if not line or len(line) > FRAME_LIMIT:
                    return

        def read_errors():
            self.errors.extend(self.child.stderr.read(4097))

        self.output_thread = threading.Thread(target=read_output, daemon=True)
        self.error_thread = threading.Thread(target=read_errors, daemon=True)
        self.output_thread.start()
        self.error_thread.start()

    def request(self, value, expected):
        global commands
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        assert len(raw) <= FRAME_LIMIT
        self.child.stdin.write(raw)
        self.child.stdin.flush()
        line = self.frames.get(timeout=WAIT_SECONDS)
        assert line.endswith(b"\n") and len(line) <= FRAME_LIMIT, line[:100]
        frame = json.loads(line)
        assert isinstance(frame, dict) and frame.get("status") == expected, (value, frame)
        commands += 1
        return frame

    def capture(self, target):
        frame = self.request(dict(action="capture", target=target), "captured")
        assert frame.get("rule") and frame.get("options"), frame
        generation = frame.get("generation")
        assert type(generation) is int and 0 < generation <= 64, frame
        return generation

    def revalidate(self, generation, target, expected="matches"):
        return self.request(dict(action="revalidate", generation=generation, target=target), expected)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.request(dict(action="exit"), "bye")
            self.child.stdin.close()
            assert self.child.wait(timeout=WAIT_SECONDS) == 0
            self.output_thread.join(timeout=WAIT_SECONDS)
            self.error_thread.join(timeout=WAIT_SECONDS)
            assert not self.output_thread.is_alive() and not self.error_thread.is_alive()
            assert not self.errors, self.errors[:4096]
            assert self.frames.get(timeout=WAIT_SECONDS) == b""
        finally:
            if self.child.poll() is None:
                self.child.kill()
                self.child.wait(timeout=WAIT_SECONDS)
            for stream in (self.child.stdin, self.child.stdout, self.child.stderr):
                if stream and not stream.closed:
                    stream.close()

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        if kind is None:
            self.close()
        else:
            if self.child.poll() is None:
                self.child.kill()
                self.child.wait(timeout=WAIT_SECONDS)
            for stream in (self.child.stdin, self.child.stdout, self.child.stderr):
                if stream and not stream.closed:
                    stream.close()


def checked_request(probe, root, value, expected):
    before = fingerprint(root)
    result = probe.request(value, expected)
    assert fingerprint(root) == before, "Native observation changed fixture bytes/metadata"
    return result


def mutation_case(parent, target, label, mutate):
    root = parent / "fixture"
    root.mkdir()
    info = seed(root, target)
    with Probe(root) as probe:
        before = fingerprint(root)
        generation = probe.capture(target)
        probe.revalidate(generation, target)
        assert fingerprint(root) == before
        mutate(root, info)
        changed = fingerprint(root)
        probe.revalidate(generation, target, "refused")
        assert fingerprint(root) == changed
    checks.append(label)


SCRATCH.mkdir(parents=True, exist_ok=True)
targets = ["onehorse", "minimain"] + (["q"] if sys.platform == "linux" else [])
with tempfile.TemporaryDirectory(prefix="native café 日本語 ", dir=SCRATCH) as folder:
    outer = Path(folder)
    for target in targets:
        root = outer / (target + "-stable")
        root.mkdir()
        seed(root, target)
        before = fingerprint(root)
        with Probe(root) as probe:
            first = probe.capture(target)
            probe.revalidate(first, target)
            second = probe.capture(target)
            assert second > first
            probe.revalidate(first, target, "refused")
            # A refused old token cannot become a success or mutate sources.
            third = probe.capture(target)
            probe.revalidate(third, "minimain" if target == "onehorse" else "onehorse", "refused")
            fourth = probe.capture(target)
            probe.request(dict(action="clear"), "cleared")
            probe.revalidate(fourth, target, "refused")
            assert fingerprint(root) == before
        checks.append(target + ":stable-monotonic-stale-foreign-target-clear-read-only")

        # Re-read every consulted descriptor, even if size and mtime match.
        files = sorted(path.relative_to(root) for path in (root / "core").rglob("*") if path.is_file())
        if target == "q":
            files.append(Path("application/src/modules/arduinoq_link_paths.py"))
        for index, relative in enumerate(files):
            parent = outer / f"{target}-bytes-{index}"
            parent.mkdir()

            def change_bytes(fixture, info, relative=relative):
                path = fixture / relative
                old = path.read_bytes()
                metadata = path.stat()
                # One byte, same length; restored timestamp excludes mtime shortcuts.
                position = next(i for i, byte in enumerate(old) if 65 <= byte <= 90 or 97 <= byte <= 122)
                raw = old[:position] + bytes([ord("x") if old[position] != ord("x") else ord("y")]) + old[position + 1:]
                path.write_bytes(raw)
                os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
                assert path.stat().st_size == metadata.st_size
                assert path.stat().st_mtime_ns == metadata.st_mtime_ns

            mutation_case(parent, target, target + ":same-size-mtime:" + relative.as_posix(), change_bytes)

        # Identical policy output must still refuse changed raw metadata.
        equivalent = outer / (target + "-same-policy")
        equivalent.mkdir()
        seed(equivalent, target)
        metadata_path = equivalent / (
            "core/packages/framework-arduino-zephyr/package.json" if target == "q"
            else "core/platforms/espressif32/platform.json")
        value = json.loads(metadata_path.read_bytes())
        value["unused_generation_marker"] = "a"
        original = json.dumps(value, separators=(",", ":")).encode()
        metadata_path.write_bytes(original)
        with Probe(equivalent) as probe:
            token = probe.capture(target)
            stamp = metadata_path.stat()
            changed = original.replace(b'"unused_generation_marker":"a"',
                                       b'"unused_generation_marker":"b"')
            assert len(changed) == len(original) and changed != original
            metadata_path.write_bytes(changed)
            os.utime(metadata_path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            probe.revalidate(token, target, "refused")
            # A fresh capture still selects the exact same valid policy.
            probe.capture(target)
        checks.append(target + ":same-policy-same-size-mtime-different-bytes")

        def replace_leaf(fixture, info):
            path = Path(info["pio_manifest"])
            original_identity = native_identity(path)
            original_bytes = path.read_bytes()
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(original_bytes)
            # Windows can refuse overwriting a retained open file. Move the
            # original object aside, then put the distinct same-byte file here.
            path.rename(path.with_suffix(".retained-original"))
            replacement.rename(path)
            assert path.read_bytes() == original_bytes
            assert native_identity(path) != original_identity

        parent = outer / (target + "-leaf")
        parent.mkdir()
        mutation_case(parent, target, target + ":same-bytes-new-leaf", replace_leaf)

        def replace_ancestor(fixture, info):
            boards = Path(info["pio_manifest"]).parent
            retained = boards.with_name("retained-boards")
            source = Path(info["pio_manifest"])
            original_identity = native_identity(source)
            # Windows may refuse a directory rename with an open descendant.
            # Move the same leaf out, replace the empty ancestor, then restore it.
            retained_leaf = boards.parent / "retained-source.json"
            source.rename(retained_leaf)
            boards.rename(retained)
            boards.mkdir()
            retained_leaf.rename(source)
            assert native_identity(source) == original_identity

        parent = outer / (target + "-ancestor")
        parent.mkdir()
        mutation_case(parent, target, target + ":same-leaf-new-ancestor", replace_ancestor)

    # Retain the fixture root object while changing one of its ancestors.
    parent = outer / "parent-generation"
    parent.mkdir()
    root = parent / "fixture"
    root.mkdir()
    seed(root, "onehorse")
    with Probe(root) as probe:
        token = probe.capture("onehorse")
        retained = parent.with_name("retained-parent-generation")
        original_identity = native_identity(root)
        # Evacuate owned descendants without changing their object identities,
        # permitting the same root object to move on Windows too.
        holding = outer / "retained-fixture-objects"
        holding.mkdir()
        held_files = []
        for index, path in enumerate(sorted(p for p in root.rglob("*") if p.is_file())):
            relative = path.relative_to(root)
            destination = holding / ("file-" + str(index))
            path.rename(destination)
            held_files.append((relative, destination))
        held_directories = []
        for index, path in enumerate(sorted((p for p in root.rglob("*") if p.is_dir()),
                                           key=lambda p: len(p.parts), reverse=True)):
            relative = path.relative_to(root)
            destination = holding / ("directory-" + str(index))
            path.rename(destination)
            held_directories.append((relative, destination))
        held_root = holding / "fixture-root"
        root.rename(held_root)
        parent.rename(retained)
        parent.mkdir()
        held_root.rename(root)
        for relative, path in sorted(held_directories, key=lambda row: len(row[0].parts)):
            path.rename(root / relative)
        for relative, path in held_files:
            path.rename(root / relative)
        assert native_identity(root) == original_identity
        checked_request(probe, root, dict(action="revalidate", generation=token, target="onehorse"), "refused")
    checks.append("same-root-new-fixture-ancestor")

    root = outer / "failed-capture"
    root.mkdir()
    info = seed(root, "onehorse")
    with Probe(root) as probe:
        token = probe.capture("onehorse")
        manifest = Path(info["pio_manifest"])
        original = manifest.read_bytes()
        manifest.write_bytes(b"{}")
        checked_request(probe, root, dict(action="capture", target="onehorse"), "refused")
        manifest.write_bytes(original)
        probe.revalidate(token, "onehorse", "refused")
    checks.append("failed-capture-invalidates-older-observation")

    root = outer / "invalid-requests"
    root.mkdir()
    seed(root, "onehorse")
    with Probe(root) as probe:
        for request in [dict(action="unknown"), {},
                        dict(action="capture", target="../../outside"),
                        dict(action="revalidate", generation=-1, target="onehorse"),
                        dict(action="revalidate", generation=True, target="onehorse"),
                        dict(action="revalidate", generation="1", target="onehorse")]:
            token = probe.capture("onehorse")
            checked_request(probe, root, request, "refused")
            probe.revalidate(token, "onehorse", "refused")
    checks.append("invalid-request-cannot-reuse-older-observation")

    if sys.platform == "win32":
        root = outer / "unsupported-q"
        root.mkdir()
        seed(root, "q")
        with Probe(root) as probe:
            checked_request(probe, root, dict(action="capture", target="q"), "refused")
        checks.append("non-linux-q-never-produces-generation")

    root = outer / "protocol"
    root.mkdir()
    seed(root, "onehorse")
    for raw in [b"{\n", b"\xff\n", b"x" * FRAME_LIMIT + b"\n",
                b'{"action":"clear"}\n' * 65]:
        result = subprocess.run([str(EXE), "--fixture-root", str(root)],
                                input=raw, capture_output=True, timeout=15, check=False)
        assert len(result.stdout) <= 66 * FRAME_LIMIT and len(result.stderr) <= 4096
        frames = [json.loads(line) for line in result.stdout.splitlines()]
        assert frames and any(frame.get("status") == "refused" for frame in frames), frames
        assert all(frame.get("status") not in ("captured", "matches") for frame in frames), frames
        assert len(frames) <= 65, len(frames)
    checks.append("invalid-utf8-json-frame-and-command-bounds")

    idle = subprocess.Popen([str(EXE), "--fixture-root", str(root)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    try:
        assert idle.wait(timeout=14) == 0
        output = idle.stdout.read(FRAME_LIMIT + 1)
        errors = idle.stderr.read(4097)
        assert len(output) <= FRAME_LIMIT and not errors
        assert json.loads(output) == dict(status="refused", refusal="Timeout")
    finally:
        if idle.poll() is None:
            idle.kill()
            idle.wait(timeout=WAIT_SECONDS)
        for stream in (idle.stdin, idle.stdout, idle.stderr):
            stream.close()
    checks.append("idle-input-deadline-releases-process")

    for forbidden in [ROOT, ROOT / "audit/migration/scratch"]:
        result = subprocess.run([str(EXE), "--fixture-root", str(forbidden)],
                                input=b'{"action":"capture","target":"onehorse"}\n',
                                capture_output=True, timeout=WAIT_SECONDS, check=False)
        assert len(result.stdout) <= FRAME_LIMIT and len(result.stderr) <= 4096
        assert result.returncode != 0
        assert not any(json.loads(line).get("status") in ("captured", "matches")
                       for line in result.stdout.splitlines())
    checks.append("checkout-and-scratch-parent-cannot-be-adopted")

print(json.dumps(dict(passed=True, checks=checks, checksPassed=len(checks), framedExchanges=commands,
                      platform=sys.platform, architecture=platform.machine(), fixtureOnly=True,
                      exactRawFixtureHashes=True, nativeSourceWrites=False,
                      controllerMutatesOwnedFixtures=True, livePackageAccess=False,
                      hardwareAccess=False, buildAuthority=False, finalUseRaceQualified=False,
                      pythonApplicationServicesRetired=0)))
