# Port Windows → Linux Task List

## bootstrap_linux.py
- [ ] Copy bootstrap.py → bootstrap_linux.py
- [ ] Remove Windows-only code (penv hook, junction, win_subprocess_hide, UTF-8 reconfigure)
- [ ] Adapt fonts (Segoe UI → Sans, Consolas → Monospace)
- [ ] Adapt GUI_SCRIPT to point to mcu_flash_gui_linux.py
- [ ] Adapt find_pio() for Linux paths
- [ ] Adapt find_arduino_cli() for Linux paths
- [ ] Replace Arduino-CLI MSI install with .tar.gz download+extract
- [ ] Replace CP210x driver check with kernel-native message
- [ ] Add dialout group check (from existing Linux version)
- [ ] Adapt Arduino IDE detection for Linux (AppImage, Snap, Flatpak)
- [ ] Adapt _is_env_healthy() for Linux venv paths
- [ ] Adapt _spawn_main_gui() (remove STARTUPINFO/DETACHED_PROCESS)
- [ ] Adapt _run_setup_in_thread() for Linux
- [ ] Remove all creationflags=CREATE_NO_WINDOW throughout
- [ ] Adapt main() for Linux

## mcu_flash_gui_linux.py
- [ ] Copy mcu_flash_gui.py → mcu_flash_gui_linux.py
- [ ] Remove Windows-only header code (win_subprocess_hide, junction, UTF-8)
- [ ] Adapt find_pio_executable() for Linux
- [ ] Adapt find_arduino_cli_executable() for Linux
- [ ] Remove all creationflags=CREATE_NO_WINDOW throughout
- [ ] Adapt fonts throughout
- [ ] Adapt main() (remove DPI, Arduino IDE MSI, use -zoomed)
- [ ] Adapt crash guard (remove MessageBoxW fallback)
- [ ] Adapt check_arduino_ide_installed() for Linux

## Verification
- [ ] Syntax check bootstrap_linux.py
- [ ] Syntax check mcu_flash_gui_linux.py
- [ ] Test run via ./runThisOnLinux.sh
