#!/usr/bin/env python3
"""Serial-list metadata regressions with mocked enumeration and persistence."""
from __future__ import annotations

import ast
import importlib
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from serial.tools.list_ports_common import ListPortInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def info(device="/dev/ttyUSB0", description="n/a", hwid="n/a", **metadata):
    # The normal pyserial defaults are retained without filesystem link checks.
    port = ListPortInfo(device, skip_link_detection=True)
    port.description, port.hwid = description, hwid
    for name, value in metadata.items():
        setattr(port, name, value)
    return port


class StopAfterOnePoll:
    def __init__(self):
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def wait(self, timeout):
        self.waits.append(timeout)
        # Initial wait, confirmation wait, then final adaptive wait.
        if len(self.waits) >= 3:
            self.stopped = True
        return self.stopped


class UbuntuPortChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helper = importlib.import_module("main.platforms.ubuntu_ports")
        tree = ast.parse((ROOT / "main/web_bridge.py").read_text(encoding="utf-8"))
        backend = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                       and node.name == "MCUWebBackendAPI")
        names = {"_scan_ports", "refresh_ports", "_port_monitor_loop", "_init_hardware",
                 "_serial_handoff_pending", "_clear_disconnected_port"}
        functions = [node for node in backend.body if isinstance(node, ast.FunctionDef)
                     and node.name in names]
        if {node.name for node in functions} != names:
            raise AssertionError("The isolated monitor fixture must include every cleanup helper")
        cls.fixture_tree = ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(
            name="PortFixture", bases=[], keywords=[], body=functions, decorator_list=[]
        )], type_ignores=[]))

    def fixture(self, ports, host="linux"):
        comports = Mock(return_value=ports)
        claim = Mock(return_value=True)
        scope = {"sys": SimpleNamespace(platform=host), "re": re,
                 "serial": SimpleNamespace(tools=SimpleNamespace(list_ports=SimpleNamespace(
                     comports=comports))), "claim_serial_port": claim,
                 "WindowsError": OSError, "port_entry": self.helper.port_entry,
                 "ubuntu_port_entry": self.helper.port_entry, "ubuntu_ports": self.helper}
        exec(compile(self.fixture_tree, "<isolated-port-backend>", "exec"), scope)
        api = scope["PortFixture"]()
        api.emit = Mock()
        api.current_port = ""
        api.current_baud = 115200
        api._last_known_ports = []
        return api, comports, claim, scope

    def test_pyserial_default_placeholders_are_hidden(self):
        port = ListPortInfo("/dev/ttyS0", skip_link_detection=True)
        self.assertEqual((port.description, port.hwid), ("n/a", "n/a"))
        self.assertIsNone(self.helper.port_entry(port))
        for description in (None, "", "  ", "N/A", " n/A \t"):
            with self.subTest(description=description):
                self.assertIsNone(self.helper.port_entry(info(description=description)))

    def test_device_name_and_link_only_metadata_are_hidden(self):
        for description in ("ttyS0", "/dev/ttyS0", " n/a "):
            for hwid in ("n/a", "", "LINK=/dev/ttyS0", "n/a LINK=/dev/ttyS0"):
                with self.subTest(description=description, hwid=hwid):
                    self.assertIsNone(self.helper.port_entry(info("/dev/ttyS0", description, hwid)))

    def test_compound_descriptions_omit_placeholders_and_keep_real_details(self):
        self.assertIsNone(self.helper.port_entry(info(description=" n/a - N/A ")))
        port = info(description="None - UART interface - n/a")
        entry = self.helper.port_entry(port)
        self.assertEqual(entry["description"], "UART interface")
        self.assertEqual(port.description, "None - UART interface - n/a")
        self.assertEqual(port.hwid, "n/a")

    def test_detailed_usb_native_and_legacy_descriptions_are_retained(self):
        for device, description, hwid in (
            ("/dev/ttyUSB0", "USB-SERIAL CH340", "USB VID:PID=1A86:7523"),
            ("/dev/ttyACM0", "ESP32 USB JTAG/serial debug unit", "USB VID:PID=303A:1001"),
            ("/dev/rfcomm0", "Bluetooth serial adapter", "n/a"),
            ("/dev/ttyS1", "Onboard UART", "PNP0501"),
        ):
            with self.subTest(device=device):
                entry = self.helper.port_entry(info(device, description, hwid))
                self.assertIsNotNone(entry)
                self.assertEqual(entry["device"], device)
                self.assertEqual(entry["description"], description)

    def test_missing_descriptions_use_actual_device_metadata(self):
        for metadata, expected in (({"product": "CP2102 USB to UART"}, "CP2102"),
                                   ({"interface": "CDC serial interface"}, "CDC"),
                                   ({"manufacturer": "Silicon Labs"}, "Silicon Labs")):
            with self.subTest(metadata=metadata):
                entry = self.helper.port_entry(info(description=" n/A ", **metadata))
                self.assertIsNotNone(entry)
                self.assertIn(expected, entry["description"])
        entry = self.helper.port_entry(info(description="n/a", hwid="USB VID:PID=10C4:EA60"))
        self.assertIsNotNone(entry)
        self.assertIn("10C4:EA60", entry["description"])
        self.assertIsNotNone(self.helper.port_entry(info("/dev/ttyS0", "ttyS0", "PNP0501")))

    def test_blank_device_and_placeholder_fallbacks_are_rejected(self):
        self.assertIsNone(self.helper.port_entry(info("", "USB serial adapter", "USB ID")))
        self.assertIsNone(self.helper.port_entry(info("  ", "USB serial adapter", "USB ID")))
        self.assertIsNone(self.helper.port_entry(info(product="N/A", interface=" ", manufacturer="n/a")))

    def test_helper_does_not_open_or_probe_ports(self):
        with patch("serial.Serial", side_effect=AssertionError("No serial device may be opened")), \
                patch("builtins.open", side_effect=AssertionError("Metadata helper must not read files")):
            self.assertIsNone(self.helper.port_entry(info()))
            self.assertIsNotNone(self.helper.port_entry(info(description="USB Serial")))

    def test_actual_linux_scan_filters_placeholders_and_sorts_retained_devices(self):
        api, comports, _, _ = self.fixture([
            info("/dev/ttyUSB10", "USB ten"), info("/dev/ttyS4"),
            info("/dev/ttyUSB2", "USB two"), info("/dev/ttyUSB1", product="USB one"),
        ])
        ports = api._scan_ports()
        self.assertEqual([entry["device"] for entry in ports],
                         ["/dev/ttyUSB1", "/dev/ttyUSB2", "/dev/ttyUSB10"])
        self.assertTrue(all(entry["description"].strip().lower() != "n/a" for entry in ports))
        comports.assert_called_once_with()

    def test_refresh_publishes_only_filtered_entries_and_updates_cache(self):
        api, _, _, _ = self.fixture([info("/dev/ttyS0"), info(description="USB adapter")])
        ports = api.refresh_ports()
        self.assertEqual([entry["device"] for entry in ports], ["/dev/ttyUSB0"])
        self.assertEqual(api._last_known_ports, ports)
        self.assertIsNot(api._last_known_ports, ports)
        api.emit.assert_called_once_with("ports:updated", ports)

    def test_initial_hardware_list_is_filtered_without_selection(self):
        api, _, claim, _ = self.fixture([info("/dev/ttyS0"), info(description="USB adapter")])
        bus = SimpleNamespace(ports_updated=Mock())
        api._get_qt_signals = Mock(return_value=bus)
        api._stop_port_monitor = SimpleNamespace(is_set=lambda: False)
        api._init_hardware()
        self.assertEqual([entry["device"] for entry in api._last_known_ports], ["/dev/ttyUSB0"])
        bus.ports_updated.emit.assert_called_once_with(api._last_known_ports)
        self.assertEqual(api.current_port, "")
        claim.assert_not_called()

    def test_filtered_selection_removal_uses_existing_disconnect_cleanup(self):
        api, comports, claim, _ = self.fixture([info()])
        api.current_port = "/dev/ttyUSB0"
        api._last_known_ports = [{"device": api.current_port, "description": "USB adapter", "hwid": "USB ID"}]
        api._init_hardware = Mock()
        api._stop_port_monitor = StopAfterOnePoll()
        api._stop_serial_monitor = Mock()
        api._sync_project_hardware_state = Mock()
        api.is_busy = False
        api._port_monitor_loop()
        self.assertEqual(comports.call_count, 2)
        self.assertEqual(api.current_port, "")
        self.assertEqual(api._last_known_ports, [])
        api._stop_serial_monitor.assert_called_once_with()
        claim.assert_called_once_with("")
        api._sync_project_hardware_state.assert_called_once_with()
        calls = api.emit.call_args_list
        names = [call.args[0] for call in calls]
        self.assertLess(names.index("port:selected"), names.index("ports:updated"))
        api.emit.assert_any_call("serial:status", {"connected": False, "port": "", "baud": 115200})
        api.emit.assert_any_call("ports:updated", [])

    def test_windows_enumeration_and_registry_fallback_remain_unchanged(self):
        api, comports, _, scope = self.fixture([info("COM9"), info("COM2", "USB adapter", "USB ID")], "win32")
        entries = [("\\Device\\CP210x", "COM3", 1), ("\\Device\\CH341", "COM4", 1),
                   ("\\Device\\USB", "COM10", 1), ("\\Device\\Serial0", "COM1", 1),
                   ("\\Device\\duplicate", "COM9", 1)]
        registry = SimpleNamespace(HKEY_LOCAL_MACHINE=object(), OpenKey=Mock(return_value=object()),
                                   EnumValue=Mock(side_effect=entries + [OSError("End")]), CloseKey=Mock())
        forbidden = Mock(side_effect=AssertionError("Windows must retain its existing enumeration"))
        scope["port_entry"] = scope["ubuntu_port_entry"] = forbidden
        with patch.dict(sys.modules, {"winreg": registry}), patch.object(self.helper, "port_entry", forbidden):
            ports = api._scan_ports()
        self.assertEqual([entry["device"] for entry in ports], ["COM1", "COM2", "COM3", "COM4", "COM9", "COM10"])
        self.assertEqual(next(entry for entry in ports if entry["device"] == "COM9"),
                         {"device": "COM9", "description": "n/a", "hwid": "n/a"})
        self.assertEqual(next(entry for entry in ports if entry["device"] == "COM3")["description"],
                         "Silicon Labs CP210x USB to UART Bridge (COM3)")
        self.assertEqual(next(entry for entry in ports if entry["device"] == "COM4")["description"],
                         "USB-SERIAL CH340 (COM4)")
        forbidden.assert_not_called()
        comports.assert_called_once_with()
        registry.CloseKey.assert_called_once_with(registry.OpenKey.return_value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
