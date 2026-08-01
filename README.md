# 🛠️ MCU Flasher by Naph

> **A modern, dark-themed GUI tool for ESP32/Arduino development — compile, upload, and monitor serial output in one sleek interface.**

![Version](https://img.shields.io/badge/version-V6.0-blue)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| **🔨 One-Click Build & Flash** | Compile and upload to ESP32 via Arduino CLI or PlatformIO |
| **📟 Serial Monitor** | Built-in terminal with ANSI color support, timestamps, and baud rate control |
| **🎨 Modern Dark UI** | Custom-styled Tkinter with Montserrat fonts, dark theme, and responsive layout |
| **✏️ Integrated Code Editor** | Syntax-highlighted editor (QScintilla / Monaco) with project file management |
| **🤖 AI Assistant** | Built-in `dedicated_AI.py` for code generation, debugging, and explanations |
| **📦 Driver & Toolchain Management** | Auto-installs CP210x drivers, Arduino CLI, PlatformIO, msys2, and more |
| **🔒 Single-Instance Guard** | Prevents accidental double-launch during bootstrap/venv setup |
| **📱 Cross-Platform** | Windows (`.vbs` launcher) and Linux (`.sh` + `.AppImage`) support |

---

## 🚀 Quick Start

### Windows

```cmd
# Double-click the launcher
runThisOnWindows.vbs
```

> **First run** will automatically:
> 1. Create a Python virtual environment (`env/`)
> 2. Install dependencies (pyserial, tkinter, etc.)
> 3. Download Arduino CLI / PlatformIO if needed
> 4. Install CP210x USB-to-UART drivers
> 5. Launch the GUI

### Linux

```bash
chmod +x runThisOnLinux.sh
./runThisOnLinux.sh
```

---

## 📁 Project Structure

```
MCU Flasher by Naph/
├── launcher.py           # Entry point — single-instance guard, AppUserModelID
├── mcu_flash_gui.py      # Main GUI application (Tkinter)
├── mcu_flash_gui_linux.py # Linux-specific GUI variant
├── dedicated_AI.py       # AI assistant for code help
├── runThisOnWindows.vbs  # Windows launcher (elevates, hides console)
├── runThisOnLinux.sh     # Linux launcher
├── src/
│   ├── libs/             # Core utilities
│   │   ├── bootstrap.py         # Dependency & toolchain installer
│   │   ├── bootstrap_linux.py   # Linux bootstrap
│   │   ├── win_subprocess_hide.py # Windows hidden subprocess helper
│   │   └── arduino_lib_req.py   # Arduino library resolver
│   ├── dbs/              # Tiny JSON database (project settings, boards)
│   ├── editor/           # Monaco/QScintilla editor assets
│   ├── fonts/            # Montserrat font family
│   └── gui_config.json   # Persisted UI preferences
├── installers/           # Drivers & toolchains (tracked via Git LFS)
│   ├── CP210x/           # Silicon Labs USB-to-UART drivers
│   ├── msys2-*.exe       # MinGW toolchain for PlatformIO
│   ├── arduino-ide_.AppImage # Linux Arduino IDE
│   └── MicrosoftEdgeWebview2Setup.exe
├── bin/                  # Bundled arduino-cli binary
├── index_json/           # Arduino library/package indexes
├── .github/
│   ├── agents/           # Copilot agent definitions
│   └── skills/           # Reusable skill packs
└── logs/                 # Runtime logs & launcher lock file
```

---

## ⚙️ Configuration

The GUI saves preferences to `src/gui_config.json`:

```json
{
  "theme": "dark",
  "baud_rate": 115200,
  "serial_port": "COM3",
  "board": "esp32:esp32:esp32",
  "programmer": "esptool",
  "editor_font_size": 12,
  "auto_scroll_serial": true
}
```

**Board Manager URLs** (auto-configured):
- `https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json`

---

## 🛠️ Development

### Requirements
- Python 3.10+
- Windows 10/11 or Linux (tested on Ubuntu 22.04+)
- Git LFS (`git lfs install`) for large binaries

### Run from Source

```bash
# Windows
python launcher.py

# Linux
python3 launcher.py
```

### Virtual Environment

The bootstrap creates `env/` automatically. To use manually:

```bash
# Windows
.\env\Scripts\activate

# Linux
source env/bin/activate

python mcu_flash_gui.py
```

---

## 📦 Git LFS Tracked Files

Large binaries are stored via Git LFS (see `.gitattributes`):

- `installers/**/*.exe`
- `installers/**/*.AppImage`
- `bin/arduino-cli`

Clone with LFS:
```bash
git lfs install
git clone https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph.git
```

---

## 🤝 Contributing

1. Fork the repo
2. Create a feature branch: `git checkout -b feat/amazing-feature`
3. Commit: `git commit -m 'feat: add amazing feature'`
4. Push: `git push origin feat/amazing-feature`
5. Open a PR

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

---

## 🙏 Acknowledgments

- **Espressif** — ESP32 Arduino core & toolchains
- **Arduino** — Arduino CLI & IDE
- **PlatformIO** — Unified embedded build system
- **Silicon Labs** — CP210x USB-to-UART drivers
- **QScintilla / Monaco Editor** — Code editing components
- **Montserrat Font** — Google Fonts (OFL)

---

## 🔗 Links

- **Repository:** https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph
- **Issues:** https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/issues
- **Releases:** https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/releases

---

> Made with ❤️ by **Naph** — Happy flashing! 🚀