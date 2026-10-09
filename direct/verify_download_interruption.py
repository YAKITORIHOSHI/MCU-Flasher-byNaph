#!/usr/bin/env python3
"""Local HTTP interruption/resume checks; no hardware, settings or installers."""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
from src.modules import arduino_lib_req as browser


class ArchiveServer(ThreadingHTTPServer):
    daemon_threads = True
    mode = "normal"
    etag = '"archive-v1"'


class ArchiveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        server = self.server
        record = {"path": self.path, "range": self.headers.get("Range"),
                  "if_range": self.headers.get("If-Range")}
        server.requests.append(record)
        if server.mode == "refused":
            self.send_error(503)
            return
        range_value = record["range"]
        use_range = bool(range_value) and server.mode != "ignore"
        # Real If-Range behavior: a changed validator sends the whole file.
        if record["if_range"] and record["if_range"] != server.etag and server.mode != "bad_etag":
            use_range = False
        start = int(range_value.split("=", 1)[1].split("-", 1)[0]) if use_range else 0
        data = server.payload[start:]
        self.send_response(206 if use_range else 200)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", server.etag)
        if use_range:
            reported_start = start - 1 if server.mode == "bad_range" else start
            self.send_header("Content-Range", f"bytes {reported_start}-{len(server.payload) - 1}/{len(server.payload)}")
        self.end_headers()
        try:
            if server.mode == "drop" or (server.mode == "drop_once" and len(server.requests) == 1):
                self.wfile.write(data[:65536])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
            elif server.mode == "stall":
                self.wfile.write(data[:32768])
                self.wfile.flush()
                server.sent.set()
                server.release.wait(3)
            elif server.mode == "trickle":
                for byte in data[:100]:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    server.sent.set()
                    if server.release.wait(.02):
                        break
            else:
                self.wfile.write(data)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def log_message(self, *args):
        pass


class DownloadInterruptionChecks(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "temp/audit/download-interruption"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("library.properties", "name=Interruption Fixture\n")
            archive.writestr("payload.bin", bytes(range(256)) * 1024)
        self.payload = stream.getvalue()
        self.metadata = {"size": len(self.payload),
                         "checksum": "SHA-256:" + hashlib.sha256(self.payload).hexdigest()}
        self.server = ArchiveServer(("127.0.0.1", 0), ArchiveHandler)
        self.server.payload = self.payload
        self.server.requests = []
        self.server.release = threading.Event()
        self.server.sent = threading.Event()
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        thread.start()
        self.addCleanup(self.close_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}/archive.zip"
        self.partial = self.folder / "archive.zip.part"
        self.app = object.__new__(browser.ArduinoBrowser)
        self.tab = object()
        self.app.board_tab = object()
        self.app._cancel_event = threading.Event()
        for name in ("_set_progress_determinate", "_update_progress", "_set_status", "_download_done",
                     "_download_cancelled", "_download_error", "_publish_job", "_handoff_board_preparation"):
            setattr(self.app, name, Mock())
        self.app._post_ui = lambda callback, *args: callback(*args)
        self.addCleanup(patch.stopall)
        patch.object(browser, "_PACKAGE_CONNECT_TIMEOUT", .2).start()
        patch.object(browser, "_PACKAGE_READ_TIMEOUT", .2).start()

    def close_server(self):
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()

    def worker(self, *, url=None, metadata=None, option="both"):
        self.app._download_worker(self.tab, url or self.url, "archive.zip", str(self.folder), option,
                                  None, self.metadata if metadata is None else metadata, "fixture-job")

    def seed_checkpoint(self, *, url=None, metadata=None, etag='"archive-v1"', length=65536):
        metadata = self.metadata if metadata is None else metadata
        self.partial.write_bytes(self.payload[:length])
        checksum = browser._parse_checksum(metadata.get("checksum", ""))
        record = {"schema": 1,
                  "source": {"url": url or self.url, "size": int(metadata.get("size", 0)),
                             "checksum": list(checksum) if checksum else None},
                  "etag": etag, "modified": "", "total": len(self.payload)}
        Path(str(self.partial) + ".json").write_text(json.dumps(record), encoding="utf-8")

    def assert_no_success(self):
        self.app._download_done.assert_not_called()
        self.app._handoff_board_preparation.assert_not_called()
        self.assertFalse(any(call.args[0] == "ready" for call in self.app._publish_job.call_args_list))

    def test_midstream_drop_preserves_verified_payload_and_explicit_retry_resumes(self):
        self.server.mode = "drop_once"
        previous = self.folder / "archive.zip"
        previous.write_bytes(b"Previously verified archive")
        installed = self.folder / "archive"
        installed.mkdir()
        (installed / "keep.txt").write_bytes(b"Previous installed library")
        self.worker()
        self.assert_no_success()
        self.assertEqual(len(self.server.requests), 1)
        self.app._download_error.assert_called_once()
        self.assertIn("Connection interrupted", self.app._download_error.call_args.args[1])
        self.assertEqual(previous.read_bytes(), b"Previously verified archive")
        self.assertEqual((installed / "keep.txt").read_bytes(), b"Previous installed library")
        self.assertTrue(self.partial.is_file())
        saved = self.partial.stat().st_size
        self.assertGreater(saved, 0)
        self.worker()
        self.assertEqual(self.server.requests[1]["range"], f"bytes={saved}-")
        self.assertEqual(self.server.requests[1]["if_range"], self.server.etag)
        self.app._download_done.assert_called_once()
        self.assertEqual(previous.read_bytes(), self.payload)
        self.assertTrue((installed / "library.properties").is_file())
        self.assertFalse(self.partial.exists())
        self.assertFalse(Path(str(self.partial) + ".json").exists())

    def test_stalled_connection_fails_bounded_without_fallback_or_success(self):
        self.server.mode = "stall"
        start = time.monotonic()
        self.worker()
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(len(self.server.requests), 1)
        self.assert_no_success()
        self.assertGreater(self.partial.stat().st_size, 0)
        self.app._download_error.assert_called_once()

    def test_cancel_during_stall_preserves_checkpoint_and_does_not_report_failure(self):
        self.server.mode = "stall"
        thread = threading.Thread(target=self.worker, daemon=True)
        start = time.monotonic()
        thread.start()
        self.assertTrue(self.server.sent.wait(1))
        self.app._cancel_event.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - start, 2)
        self.assert_no_success()
        self.app._download_cancelled.assert_called_once()
        self.app._download_error.assert_not_called()
        self.assertTrue(self.partial.exists())
        self.app._cancel_event.clear()
        self.server.mode = "normal"
        self.worker(option="zip")
        self.app._download_done.assert_called_once()
        self.assertEqual((self.folder / "archive.zip").read_bytes(), self.payload)

    def test_urllib_midstream_drop_also_retains_and_resumes(self):
        self.server.mode = "drop_once"
        with patch.object(browser, "requests", None):
            self.worker(option="zip")
            self.assert_no_success()
            self.assertGreater(self.partial.stat().st_size, 0)
            self.worker(option="zip")
        self.app._download_done.assert_called_once()
        self.assertIsNotNone(self.server.requests[1]["range"])
        self.assertEqual((self.folder / "archive.zip").read_bytes(), self.payload)

    def test_cancel_on_a_trickling_socket_does_not_wait_for_full_stream_chunk(self):
        for transport in (browser.requests, None):
            with self.subTest(requests=transport is not None), patch.object(browser, "requests", transport):
                self.app._cancel_event.clear()
                self.app._download_cancelled.reset_mock()
                self.server.sent.clear()
                self.server.mode = "trickle"
                thread = threading.Thread(target=self.worker, daemon=True)
                thread.start()
                self.assertTrue(self.server.sent.wait(1))
                self.app._cancel_event.set()
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assert_no_success()
                self.app._download_cancelled.assert_called_once()
                self.app._download_error.assert_not_called()

    def test_changed_source_or_checksum_never_reuses_another_archives_bytes(self):
        for changed in ("url", "checksum", "unowned"):
            with self.subTest(changed=changed):
                self.app._download_done.reset_mock()
                self.seed_checkpoint(url=self.url + "?previous" if changed == "url" else self.url,
                    metadata=dict(self.metadata, checksum="SHA-256:" + "0" * 64) if changed == "checksum" else self.metadata)
                if changed == "unowned":
                    Path(str(self.partial) + ".json").unlink()
                self.worker(option="zip")
                self.assertIsNone(self.server.requests[-1]["range"])
                self.app._download_done.assert_called_once()
                self.assertEqual((self.folder / "archive.zip").read_bytes(), self.payload)

    def test_range_ignored_or_validator_changed_restarts_instead_of_appending(self):
        for mode in ("ignore", "normal"):
            with self.subTest(mode=mode):
                self.app._download_done.reset_mock()
                self.seed_checkpoint()
                self.server.mode = mode
                self.server.etag = '"archive-v2"' if mode == "normal" else '"archive-v1"'
                self.worker(option="zip")
                self.app._download_done.assert_called_once()
                self.assertEqual((self.folder / "archive.zip").read_bytes(), self.payload)

    def test_false_206_range_or_validator_cannot_promote_archive(self):
        for mode in ("bad_range", "bad_etag"):
            with self.subTest(mode=mode):
                self.app._download_error.reset_mock()
                self.seed_checkpoint()
                self.server.mode = mode
                self.server.etag = '"archive-v2"' if mode == "bad_etag" else '"archive-v1"'
                self.worker(option="zip")
                self.assert_no_success()
                self.app._download_error.assert_called_once()
                self.assertFalse((self.folder / "archive.zip").exists())
                self.assertFalse(self.partial.exists())

    def test_complete_checksum_checkpoint_is_verified_without_replaying_http(self):
        self.seed_checkpoint(length=len(self.payload))
        self.worker(option="zip")
        self.assertEqual(self.server.requests, [])
        self.app._download_done.assert_called_once()
        self.assertEqual((self.folder / "archive.zip").read_bytes(), self.payload)

    def test_cancel_before_start_does_not_start_request_or_erase_verified_archive(self):
        previous = self.folder / "archive.zip"
        previous.write_bytes(b"Previously verified archive")
        self.app._cancel_event.set()
        self.worker(option="zip")
        self.assertEqual(self.server.requests, [])
        self.assert_no_success()
        self.app._download_cancelled.assert_called_once()
        self.assertEqual(previous.read_bytes(), b"Previously verified archive")

    def test_server_error_is_one_explicit_failure_without_second_transport_retry(self):
        self.server.mode = "refused"
        self.seed_checkpoint()
        saved = self.partial.read_bytes()
        self.worker(option="zip")
        self.assertEqual(len(self.server.requests), 1)
        self.assert_no_success()
        self.app._download_error.assert_called_once()
        self.assertIn("503", self.app._download_error.call_args.args[1])
        self.assertEqual(self.partial.read_bytes(), saved)

    def test_without_checksum_only_a_strong_validator_allows_resume(self):
        metadata = {"size": len(self.payload)}
        for etag in ('"archive-v1"', 'W/"archive-v1"', ""):
            with self.subTest(etag=etag):
                self.app._download_done.reset_mock()
                self.seed_checkpoint(metadata=metadata, etag=etag)
                self.worker(metadata=metadata, option="zip")
                self.app._download_done.assert_called_once()
                self.assertEqual(bool(self.server.requests[-1]["range"]), bool(etag and not etag.startswith("W/")))
                self.assertEqual((self.folder / "archive.zip").read_bytes(), self.payload)

    def test_broken_response_close_cannot_mask_interruption_or_remove_checkpoint(self):
        def interrupted_chunks():
            yield self.payload[:65536]
            raise browser.requests.exceptions.ConnectionError("Fixture disconnected")
        response = Mock(status_code=200, raw=None,
                        headers={"Content-Length": str(len(self.payload)), "ETag": self.server.etag},
                        iter_content=Mock(return_value=interrupted_chunks()),
                        close=Mock(side_effect=OSError("Fixture broken socket close")))
        with patch.object(browser.requests, "get", return_value=response):
            self.worker(option="zip")
        self.assert_no_success()
        self.app._download_error.assert_called_once()
        self.assertIn("Connection interrupted", self.app._download_error.call_args.args[1])
        self.assertEqual(self.partial.read_bytes(), self.payload[:65536])
        self.assertTrue(Path(str(self.partial) + ".json").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
