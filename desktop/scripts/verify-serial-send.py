"""Compare bounded pure Send traces with the original queue, without drivers.

The original class is compiled from its AST without importing the application.
Its worker's single outer iteration becomes a generator at the driver-write
call, allowing deterministic before/after-write state changes. All admission,
matching, discard, failure, stop and finally bodies remain the original Python.
No real thread, serial device, settings, project or cache is opened.
"""
from collections import deque
from pathlib import Path
import ast
import copy
import hashlib
import itertools
import json
import random
import subprocess
import sys
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
REFERENCE_PATH = ROOT / "main/core/serial_send.py"
EXE = ROOT / "desktop/target/debug" / (
    "serial_send_probe.exe" if sys.platform == "win32" else "serial_send_probe")
MAX_BATCH_BYTES = 1_000_000
MAX_BATCH_TRACES = 128
MAX_BATCH_ACTIONS = 4096
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
STATE_KEYS = {"generation", "connection", "port", "baud", "isOpen", "busy",
              "operation", "lockAvailable"}


class ImmediateCondition:
    """Single-controller condition; never waits or starts a worker."""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def wait_for(self, predicate):
        assert predicate(), "The controller must not step an idle worker"
        return True

    def notify(self):
        pass

    def notify_all(self):
        pass


class NativeStateLock:
    def __init__(self):
        self.available = True

    def acquire(self, *, blocking):
        assert blocking is False
        return self.available

    def release(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class InertThread:
    def __init__(self, *, target, name, daemon, fail):
        assert callable(target) and name == "MCU_SerialSend" and daemon is True
        self.fail = fail

    def start(self):
        if self.fail:
            raise RuntimeError("fixture worker start refused")


class WorkerStep(ast.NodeTransformer):
    def __init__(self):
        self.write_calls = 0
        self.continues = 0

    def visit_Continue(self, node):
        self.continues += 1
        return ast.copy_location(ast.Return(value=None), node)

    def visit_Call(self, node):
        if (isinstance(node.func, ast.Attribute) and node.func.attr == "write"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "conn"):
            assert len(node.args) == 1 and isinstance(node.args[0], ast.Name)
            assert node.args[0].id == "data" and not node.keywords
            self.write_calls += 1
            return ast.copy_location(ast.Yield(value=ast.Tuple(
                elts=[ast.Name(id="conn", ctx=ast.Load()),
                      ast.Name(id="data", ctx=ast.Load())], ctx=ast.Load())), node)
        return self.generic_visit(node)


def isolated_reference(threading_shim):
    source = REFERENCE_PATH.read_bytes()
    tree = ast.parse(source, filename=str(REFERENCE_PATH))
    classes = [node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == "SerialSendQueue"]
    assert len(classes) == 1
    original = copy.deepcopy(classes[0])
    worker = next(node for node in original.body
                  if isinstance(node, ast.FunctionDef) and node.name == "_worker")
    assert len(worker.body) == 1 and isinstance(worker.body[0], ast.While)
    assert isinstance(worker.body[0].test, ast.Constant) and worker.body[0].test.value is True
    assert not worker.body[0].orelse
    worker.body = worker.body[0].body
    transform = WorkerStep()
    transform.visit(worker)
    assert (transform.write_calls, transform.continues) == (1, 1), (
        "Original worker changed; review the deterministic stepping boundary")
    module = ast.fix_missing_locations(ast.Module(body=[original], type_ignores=[]))
    namespace = {"deque": deque, "threading": threading_shim}
    exec(compile(module, "serial_send.py:isolated-original", "exec"), namespace)
    return namespace["SerialSendQueue"], hashlib.sha256(source).hexdigest()


class FakeConnection:
    def __init__(self, token, controller):
        self.token = token
        self.controller = controller
        self.is_open = True

    def close(self):
        self.controller.closed.append(self.token)
        self.is_open = False

    def write(self, _):
        raise AssertionError("The AST driver boundary must prevent real writes")


def initial_state(**overrides):
    state = dict(generation=1, connection=1, port="FIXTURE", baud=115200,
                 isOpen=True, busy=False, operation=None, lockAvailable=True)
    state.update(overrides)
    return state


class OriginalTrace:
    def __init__(self, state):
        self.worker_start_fails = False
        shim = SimpleNamespace(Condition=ImmediateCondition, Thread=lambda **kwargs:
                               InertThread(**kwargs, fail=self.worker_start_fails))
        queue_type, self.reference_hash = isolated_reference(shim)
        self.connections = {}
        self.closed = []
        self.events = []
        self.request_ids = {}
        self.requests = []
        self.worker = None
        self.next_id = 1
        self.api = SimpleNamespace(_serial_lock=NativeStateLock(), emit=self.emit,
                                   serial_running=True)
        self.set_state(state)
        self.queue = queue_type(self.api)

    def emit(self, name, data):
        self.events.append((name, data))

    def set_state(self, state):
        assert set(state) == STATE_KEYS, state
        connection = state["connection"]
        if connection is not None:
            connection = self.connections.setdefault(connection, FakeConnection(connection, self))
            connection.is_open = state["isOpen"]
        self.api._serial_conn = connection
        self.api._serial_generation = state["generation"]
        self.api.current_port = state["port"]
        self.api.current_baud = state["baud"]
        self.api.is_busy = state["busy"]
        self.api.active_operation = state["operation"]
        self.api._serial_lock.available = state["lockAvailable"]

    def state(self):
        conn = self.api._serial_conn
        return dict(generation=self.api._serial_generation,
                    connection=conn.token if conn else None,
                    port=self.api.current_port, baud=self.api.current_baud,
                    isOpen=conn.is_open if conn else False,
                    busy=self.api.is_busy, operation=self.api.active_operation,
                    lockAvailable=self.api._serial_lock.available)

    def summary(self, request):
        if request is None:
            return None
        generation, conn, port, baud, _text, data = request
        return dict(id=self.request_ids[id(request)], generation=generation,
                    connection=conn.token, port=port, baud=baud, bytes=len(data),
                    digest=hashlib.sha256(data).hexdigest())

    def snapshot(self):
        pending = list(self.queue.pending)
        assert self.queue.queued_bytes == sum(len(request[5]) for request in pending) + (
            len(self.queue.active[5]) if self.queue.active else 0)
        assert 0 <= self.queue.queued_bytes <= self.queue.MAX_QUEUED_BYTES
        assert len(pending) + bool(self.queue.active) <= self.queue.MAX_MESSAGES
        return dict(stopped=self.queue.stopped, queuedBytes=self.queue.queued_bytes,
                    pending=[self.summary(request) for request in pending],
                    active=self.summary(self.queue.active))

    def step(self, action):
        before_events, before_closed = len(self.events), len(self.closed)
        effects = dict(write=None, close=None, disconnect=None, echo=None)
        kind = action["action"]
        if kind == "set_state":
            self.set_state(action["state"])
            result = dict(kind="stateChanged")
        elif kind == "submit":
            self.worker_start_fails = action.get("workerStartFails", False)
            accepted = self.queue.submit(action["text"], action.get("ending", "both"))
            if accepted:
                request = self.queue.pending[-1]
                self.requests.append(request)  # Retain tuples so Python cannot reuse their IDs.
                self.request_ids[id(request)] = self.next_id
                result = dict(kind="submit", accepted=True, id=self.next_id, reason=None)
                self.next_id += 1
            else:
                logs = self.events[before_events:]
                message = logs[-1][1]["text"] if logs else ""
                if "exceeds the serial send limit" in message:
                    reason = "messageTooLarge"
                elif "connection is changing" in message:
                    reason = "connectionChanging"
                elif "not connected or reserved" in message:
                    reason = "unavailable"
                elif "queue is full" in message:
                    reason = "queueFull"
                elif "Cannot start serial Send" in message:
                    reason = "workerStartFailed"
                else:
                    assert self.queue.stopped and not message, (message, self.snapshot())
                    reason = "stopped"
                result = dict(kind="submit", accepted=False, id=None, reason=reason)
        elif kind == "begin":
            if self.queue.active is not None:
                result = dict(kind="alreadyActive")
            elif self.queue.stopped:
                result = dict(kind="stopped")
            elif not self.queue.pending:
                result = dict(kind="idle")
            else:
                request_id = self.request_ids[id(self.queue.pending[0])]
                self.worker = self.queue._worker()
                try:
                    conn, data = next(self.worker)
                    assert conn is self.queue.active[1] and data == self.queue.active[5]
                    effects["write"] = self.summary(self.queue.active)
                    result = dict(kind="write", id=request_id)
                except StopIteration:
                    self.worker = None
                    result = dict(kind="skipped", id=request_id)
        elif kind == "finish":
            assert self.worker is not None and self.queue.active is not None
            request_id = self.request_ids[id(self.queue.active)]
            assert action.get("requestId", request_id) == request_id
            try:
                if action.get("error", False):
                    self.worker.throw(OSError("fixture driver failure"))
                else:
                    self.worker.send(action["written"])
                raise AssertionError("One worker iteration must finish after its single attempt")
            except StopIteration:
                self.worker = None
            for event, data in self.events[before_events:]:
                if event == "serial:status":
                    assert effects["disconnect"] is None
                    effects["disconnect"] = data["generation"]
                elif event == "serial:log" and data["tag"] == "dim":
                    assert effects["echo"] is None
                    effects["echo"] = request_id
            closed = self.closed[before_closed:]
            assert len(closed) <= 1
            effects["close"] = closed[0] if closed else None
            result = dict(kind="finished")
        elif kind == "discard":
            token = action.get("connection")
            conn = (self.connections.setdefault(token, FakeConnection(token, self))
                    if token is not None else None)
            self.queue.discard(action.get("generation"), conn)
            result = dict(kind="discarded")
        elif kind == "stop":
            self.queue.stop()
            result = dict(kind="stopped")
        else:
            raise AssertionError(f"Unknown fixture action: {kind}")
        return dict(result=result, snapshot=self.snapshot(), state=self.state(), effects=effects)


def trace(label, actions, state=None):
    return dict(label=label, initialState=state or initial_state(), actions=actions)


def submit(text="fixture", ending="both", **extra):
    return dict(action="submit", text=text, ending=ending, **extra)


def state_change(**changes):
    state = initial_state()
    state.update(changes)
    return dict(action="set_state", state=state)


def complete_trace(label, text, ending):
    length = len((text + {"both": "\r\n", "nl": "\n", "cr": "\r", "none": ""}.get(
        ending, "\r\n")).encode("utf-8", errors="replace"))
    actions = [submit(text, ending)]
    if length <= 65536 and len(text) <= 65536:
        actions += [dict(action="begin"), dict(action="finish", written=length)]
    return trace(label, actions)


def cases():
    result = []
    endings = ["both", "nl", "cr", "none", "", "unknown", " BOTH"]
    texts = ["", "ASCII", "héllo 漢字", "Ω日本語😀", "\0", "\r\n", "\x1b[6n", "a\tβ"]
    for index, (text, ending) in enumerate(itertools.product(texts, endings)):
        result.append(complete_trace(f"encoding-{index}", text, ending))
    for character, lengths in [("x", range(65533, 65538)), ("é", range(32766, 32770)),
                               ("漢", range(21843, 21847)), ("😀", range(16382, 16386))]:
        for length, ending in itertools.product(lengths, endings[:4] + ["unknown"]):
            result.append(complete_trace(f"byte-boundary-{ord(character)}-{length}-{ending}",
                                         character * length, ending))
    for connection, opened, busy, operation, lock in itertools.product(
            [None, 1], [False, True], [False, True], [None, "compile", "upload", "reset"],
            [False, True]):
        state = initial_state()
        state.update(connection=connection, isOpen=opened, busy=busy,
                     operation=operation, lockAvailable=lock)
        result.append(trace(f"admission-{len(result)}", [submit(), dict(action="stop")], state))
    for active in [False, True]:
        actions = [submit("", "none")]
        if active:
            actions.append(dict(action="begin"))
        actions += [submit("", "none") for _ in range(16)]
        actions += [dict(action="discard"), submit("after-discard", "none")]
        if active:
            actions += [dict(action="finish", written=0)]
        result.append(trace(f"message-budget-active-{active}", actions))
        actions = [submit("x" * 65536, "none")]
        if active:
            actions.append(dict(action="begin"))
        actions += [submit("x" * 65536, "none") for _ in range(3)]
        actions += [submit("", "none"), submit("x", "none"), dict(action="discard")]
        if active:
            actions += [submit("x", "none"), dict(action="finish", written=65536)]
        result.append(trace(f"byte-budget-active-{active}", actions))
    changes = [dict(generation=2), dict(connection=2), dict(port="OTHER"), dict(baud=9600),
               dict(baud=None), dict(isOpen=False), dict(busy=True, operation="upload"),
               dict(busy=True, operation="compile"), dict(busy=False, operation="upload")]
    for index, change in enumerate(changes):
        result.append(trace(f"stale-before-write-{index}", [submit("first"), submit("pending"),
                            state_change(**change), dict(action="begin"), dict(action="stop")]))
        for outcome in [dict(written=7), dict(written=0), dict(written=2), dict(written=8),
                        dict(error=True)]:
            result.append(trace(f"state-after-write-{index}-{len(result)}", [submit("first"),
                                submit("pending"), dict(action="begin"), state_change(**change),
                                dict(action="finish", **outcome), dict(action="stop")]))
    for generation, connection, active in itertools.product([None, 1, 2, 9], [None, 1, 2, 9],
                                                           [False, True]):
        actions = [submit("original", "none")]
        if active:
            actions.append(dict(action="begin"))
        actions += [state_change(generation=2, connection=1), submit("generation-two", "none"),
                    state_change(generation=2, connection=2), submit("token-two", "none"),
                    dict(action="discard", generation=generation, connection=connection)]
        if active:
            actions.append(dict(action="finish", error=True))
        actions.append(dict(action="stop"))
        result.append(trace(f"discard-{generation}-{connection}-{active}", actions))
    for active, failure in itertools.product([False, True], [False, True]):
        actions = [submit("first")]
        if active:
            actions.append(dict(action="begin"))
        actions += [submit("pending"), dict(action="stop"), submit("after-stop"),
                    dict(action="begin"), dict(action="stop")]
        if active:
            actions.append(dict(action="finish", **(dict(error=True) if failure else dict(written=7))))
        result.append(trace(f"permanent-stop-{active}-{failure}", actions))
    result += [trace("worker-start-rollback", [submit(workerStartFails=True), submit("second"),
                      submit("third", workerStartFails=True), dict(action="begin"),
                      dict(action="finish", written=8), dict(action="stop")]),
               trace("worker-failure-after-discard", [submit(), dict(action="discard"),
                      submit(workerStartFails=True), dict(action="begin"),
                      dict(action="finish", error=True), dict(action="begin")]),
               trace("idle-and-active-begin", [dict(action="begin"), submit(),
                      dict(action="begin"), dict(action="begin"), dict(action="finish", written=9),
                      dict(action="begin"), dict(action="stop"), dict(action="begin")])]
    for port, baud, generation, connection in [
            ("café 日本語", None, 0, 0), ("COM5", 4294967295, 18446744073709551614, 0),
            ("/dev/fixture", 0, 9007199254740993, 18446744073709551615)]:
        result.append(trace(f"native-token-bits-{generation}-{connection}", [
            submit("identity", "none"), dict(action="begin"),
            dict(action="finish", error=True), dict(action="stop")],
            initial_state(port=port, baud=baud, generation=generation, connection=connection)))
    rng = random.Random(0x53454E44)
    for index in range(96):
        initial = initial_state()
        controller = OriginalTrace(initial)
        actions = []
        for _ in range(64):
            choice = rng.randrange(100)
            if choice < 45:
                action = submit(rng.choice(texts) * rng.randrange(1, 20), rng.choice(endings),
                                workerStartFails=rng.randrange(12) == 0)
            elif choice < 60:
                action = dict(action="begin")
            elif choice < 72 and controller.queue.active:
                length = len(controller.queue.active[5])
                action = dict(action="finish", **rng.choice([
                    dict(written=length), dict(written=0), dict(written=length + 1), dict(error=True)]))
            elif choice < 83:
                action = state_change(generation=rng.randrange(1, 5), connection=rng.choice([None, 1, 2, 3]),
                                      port=rng.choice(["FIXTURE", "COM5", "/dev/fixture", "café 日本語"]),
                                      baud=rng.choice([None, 9600, 115200]),
                                      isOpen=rng.choice([False, True]), busy=rng.choice([False, True]),
                                      operation=rng.choice([None, "compile", "upload"]),
                                      lockAvailable=rng.choice([False, True]))
            elif choice < 98:
                action = dict(action="discard", generation=rng.choice([None, 1, 2]),
                              connection=rng.choice([None, 1, 2]))
            else:
                action = dict(action="stop")
            controller.step(action)
            actions.append(action)
        if controller.queue.active:
            actions.append(dict(action="finish", error=True))
        actions.append(dict(action="stop"))
        result.append(trace(f"deterministic-sequence-{index}", actions, initial))
    return result


def expected_trace(case):
    controller = OriginalTrace(case["initialState"])
    events = [controller.step(action) for action in case["actions"]]
    attempts = [event["effects"]["write"]["id"] for event in events if event["effects"]["write"]]
    assert len(attempts) == len(set(attempts)), "A captured request was attempted twice"
    return dict(events=events), controller.reference_hash


def batches(traces):
    current, size, actions = [], 0, 0
    for case in traces:
        encoded = json.dumps(case, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assert len(encoded) < MAX_BATCH_BYTES - 128, case["label"]
        if current and (size + len(encoded) > MAX_BATCH_BYTES - 128
                        or len(current) >= MAX_BATCH_TRACES
                        or actions + len(case["actions"]) > MAX_BATCH_ACTIONS):
            yield current
            current, size, actions = [], 0, 0
        current.append(case)
        size += len(encoded) + 1
        actions += len(case["actions"])
    if current:
        yield current


def probe_refusals():
    """Typed and protocol refusals are separate from equivalent trace inputs."""
    one = trace("typed-refusal", [])
    valid = dict(schemaVersion=1, traces=[one])
    inputs = [("schema-version", dict(valid, schemaVersion=2)),
              ("boolean-schema", dict(valid, schemaVersion=True)),
              ("unknown-batch-field", dict(valid, device="COM5")),
              ("trace-budget", dict(schemaVersion=1, traces=[one] * 129)),
              ("action-budget", dict(schemaVersion=1, traces=[trace(
                  "excess-actions", [dict(action="begin")] * 4097)]))]
    for label, field, value in [
            ("negative-generation", "generation", -1),
            ("oversized-generation", "generation", 2 ** 64),
            ("fractional-generation", "generation", 1.5),
            ("boolean-token", "connection", True),
            ("negative-token", "connection", -1),
            ("oversized-token", "connection", 2 ** 64),
            ("negative-baud", "baud", -1),
            ("oversized-baud", "baud", 2 ** 32),
            ("nonboolean-busy", "busy", 1),
            ("port-budget", "port", "x" * 4097),
            ("operation-budget", "operation", "x" * 129)]:
        case = trace(label, [], initial_state(**{field: value}))
        inputs.append((label, dict(schemaVersion=1, traces=[case])))
    for label, actions in [
            ("unknown-action", [dict(action="open_device")]),
            ("unknown-action-field", [dict(action="begin", port="COM5")]),
            ("unpaired-surrogate", [submit("\ud800")]),
            ("completion-without-active", [dict(action="finish", written=0)]),
            ("wrong-completion-id", [submit(), dict(action="begin"),
                                     dict(action="finish", requestId=2, written=9)]),
            ("missing-write-result", [submit(), dict(action="begin"), dict(action="finish")]),
            ("typed-generation-exhaustion", [submit(), dict(action="begin"),
                                            dict(action="finish", error=True)])]:
        state = initial_state(generation=2 ** 64 - 1) if label == "typed-generation-exhaustion" else None
        inputs.append((label, dict(schemaVersion=1, traces=[trace(label, actions, state)])))
    payloads = [(label, json.dumps(value, ensure_ascii=True).encode("utf-8"), [])
                for label, value in inputs]
    payloads += [("input-byte-budget", b"x" * (MAX_BATCH_BYTES + 1), []),
                 ("invalid-utf8", b"\xff", []),
                 ("path-argument", b"", ["forbidden-device-or-path"])]
    checks = []
    for label, payload, arguments in payloads:
        result = subprocess.run([str(EXE), *arguments], input=payload,
                                capture_output=True, timeout=15, check=False)
        assert result.returncode != 0 and not result.stderr, (label, result.returncode, result.stderr)
        assert 0 < len(result.stdout) <= 4096, label
        failed = json.loads(result.stdout)
        assert set(failed) == {"schemaVersion", "fixtureOnly", "passed", "error"}, (label, failed)
        assert type(failed["schemaVersion"]) is int and failed["schemaVersion"] == 1
        assert failed["fixtureOnly"] is True and failed["passed"] is False
        assert isinstance(failed["error"], str) and 0 < len(failed["error"]) <= 256, label
        checks.append(label)
    return checks


def main():
    reference_only = sys.argv[1:] == ["--reference-only"]
    assert not sys.argv[1:] or reference_only, "Only --reference-only is supported"
    traces = cases()
    compared_actions, batch_count = 0, 0
    source_hash = None
    for batch in batches(traces):
        expected = []
        for case in batch:
            output, current_hash = expected_trace(case)
            assert source_hash in (None, current_hash), "Original reference changed during verification"
            source_hash = current_hash
            expected.append(output)
        if reference_only:
            continue
        payload = json.dumps(dict(schemaVersion=1, traces=batch), ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
        assert len(payload) <= MAX_BATCH_BYTES
        completed = subprocess.run([str(EXE)], input=payload, capture_output=True,
                                   check=True, timeout=15)
        assert len(completed.stdout) <= MAX_OUTPUT_BYTES and not completed.stderr, completed.stderr
        actual = json.loads(completed.stdout)
        assert set(actual) == {"schemaVersion", "fixtureOnly", "traces"}, actual.keys()
        assert type(actual["schemaVersion"]) is int and actual["schemaVersion"] == 1
        assert actual["fixtureOnly"] is True
        assert len(actual["traces"]) == len(expected)
        for case, before, after in zip(batch, expected, actual["traces"]):
            assert set(after) == {"events"} and len(after["events"]) == len(before["events"]), case["label"]
            for index, (want, got) in enumerate(zip(before["events"], after["events"])):
                assert want == got, (f"{case['label']} action {index} "
                                     f"({case['actions'][index]['action']}): "
                                     f"expected {want!r}, got {got!r}")
                compared_actions += 1
        batch_count += 1
    assert hashlib.sha256(REFERENCE_PATH.read_bytes()).hexdigest() == source_hash
    refusals = [] if reference_only else probe_refusals()
    print(json.dumps(dict(schemaVersion=1, fixtureOnly=True,
                          reference="main/core/serial_send.py::SerialSendQueue (AST-isolated)",
                          referenceSha256=source_hash, traceInputs=len(traces),
                          actionInputs=sum(len(case["actions"]) for case in traces),
                          actionComparisons=compared_actions, probeBatches=batch_count,
                          referenceOnly=reference_only, differentialPassed=not reference_only,
                          structuredRefusalChecks=len(refusals), refusalChecks=refusals,
                          noRealThreads=True, serialDevicesAccessed=False, settingsAccess=False,
                          projectsAccessed=False, hardwareWrites=False,
                          productionSerialQualified=False)))


if __name__ == "__main__":
    main()
