"""Compare Rust serial decoding with the existing Python behavior, without I/O."""
from pathlib import Path
import importlib.util
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("migration_serial_reference", ROOT / "main/core/serial_stream.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
payloads = [b"", bytes(range(256)), b"\r\n\r\r\n\x00\x07\x1b[31m\x7f", "café 日本語 Ω\u0085\u2028\u2029".encode(),
            b"\xf0\x9f\x92\xa9\xed\xa0\x80\xc0\xaf\xe2\x82", b"\xff\xfe\x80\r\xc2\x85\r"]
cases = []
for payload in payloads:
    for split in range(len(payload) + 1):
        cases.append([list(payload[:split]), list(payload[split:])])
    cases.append([[byte] for byte in payload])
expected = []
for chunks in cases:
    decoder = module.SerialTextDecoder()
    output = "".join(decoder.feed(bytes(chunk)) for chunk in chunks) + decoder.feed(b"", final=True)
    expected.append(dict(output=output, invalidBytes=decoder.invalid_bytes, bufferedBytes=0))
exe = ROOT / "desktop/target/debug" / ("codec_probe.exe" if sys.platform == "win32" else "codec_probe")
completed = subprocess.run([str(exe)], input=json.dumps(cases), text=True, capture_output=True, check=True)
actual = json.loads(completed.stdout)
for index, (before, after) in enumerate(zip(expected, actual)):
    assert before == after, f"Serial codec parity case {index}: {before!r} != {after!r}"
assert len(expected) == len(actual)
print(json.dumps(dict(reference="main/core/serial_stream.py", cases=len(cases), passed=True,
                      deviceAccess=False, retainedDecoderBytesMaximum=3)))
