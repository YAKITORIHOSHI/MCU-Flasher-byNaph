"""Fedora's package names for the native libraries and tools MCU Flasher checks.

Fedora never goes through Ubuntu's apt-get setup. Each name here is the package
that provides the exact SONAME or /usr/bin path, confirmed in the official Fedora
44 (x86_64, aarch64) and Fedora 45 (x86_64) repository metadata. They do not all
follow from the library's name: libudev is in systemd-libs, secret-tool in libsecret.
"""
from __future__ import annotations

# (ctypes library name, Fedora package), in preflight's order.
SYSTEM_LIBRARIES = (
    ("EGL", "libglvnd-egl"), ("GL", "libglvnd-glx"), ("nss3", "nss"),
    ("nspr4", "nspr"), ("gbm", "mesa-libgbm"), ("xcb-dri3", "libxcb"),
    ("X11", "libX11"), ("X11-xcb", "libX11-xcb"), ("Xext", "libXext"),
    ("Xcomposite", "libXcomposite"), ("Xdamage", "libXdamage"),
    ("Xfixes", "libXfixes"), ("Xrandr", "libXrandr"), ("Xrender", "libXrender"),
    ("Xtst", "libXtst"), ("xkbfile", "libxkbfile"), ("xkbcommon", "libxkbcommon"),
    ("dbus-1", "dbus-libs"), ("fontconfig", "fontconfig"), ("freetype", "freetype"),
    ("glib-2.0", "glib2"), ("xcb", "libxcb"),
    ("asound", "alsa-lib"), ("xcb-cursor", "xcb-util-cursor"),
    ("xkbcommon-x11", "libxkbcommon-x11"), ("xcb-icccm", "xcb-util-wm"),
    ("xcb-image", "xcb-util-image"), ("xcb-keysyms", "xcb-util-keysyms"),
    ("xcb-render-util", "xcb-util-renderutil"), ("xcb-xinerama", "libxcb"),
    ("xcb-xkb", "libxcb"), ("xcb-shape", "libxcb"),
    ("xcb-randr", "libxcb"), ("xcb-sync", "libxcb"),
    ("xcb-shm", "libxcb"), ("xcb-xfixes", "libxcb"),
    ("xcb-render", "libxcb"), ("xcb-util", "xcb-util"),
    ("usb-1.0", "libusb1"), ("udev", "systemd-libs"),
)
# The same ARM64 Qt wheel closure that Ubuntu's preflight lists.
ARM_SYSTEM_LIBRARIES = (
    ("opus", "opus"), ("lcms2", "lcms2"),
    ("minizip", "minizip-ng-compat"), ("snappy", "snappy"),
    ("webp", "libwebp"), ("webpdemux", "libwebp"), ("webpmux", "libwebp"),
    ("brotlidec", "libbrotli"), ("zstd", "libzstd"), ("z", "zlib-ng-compat"),
    ("expat", "expat"), ("ICE", "libICE"), ("SM", "libSM"),
    ("wayland-client", "libwayland-client"), ("wayland-cursor", "libwayland-cursor"),
    ("wayland-egl", "libwayland-egl"),
)
SYSTEM_TOOLS = (("bash", "bash"), ("git", "git-core"), ("make", "make"),
                ("gcc", "gcc"), ("g++", "gcc-c++"),
                ("xdg-open", "xdg-utils"), ("ldd", "glibc-common"), ("rg", "ripgrep"),
                ("secret-tool", "libsecret"),
                ("gnome-keyring-daemon", "gnome-keyring"))
CLIPBOARD_PACKAGES = ("xclip", "wl-clipboard")
# Fedora's python3 includes ensurepip, so a venv needs no separate package.
VENV_PACKAGE = "python3"
# Fedora's ARM64 PyQt5, the counterpart of Ubuntu's python3-pyqt5 packages.
ARM_QT5_PACKAGES = ("python3-qt5", "python3-qscintilla-qt5", "python3-pyqt5-sip")
