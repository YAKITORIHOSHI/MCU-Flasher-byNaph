#!/bin/bash

# Get the directory of this script
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# Prevent Python from creating or using compiled .pyc bytecode files
export PYTHONDONTWRITEBYTECODE=1

# Purge Python bytecode caches on launch
find "$SCRIPT_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null
find "$SCRIPT_DIR" -name "*.pyc" -o -name "*.pyo" -delete 2>/dev/null

echo "============================================="
echo "   MCU Uploader IDE by Naph - Launcher for Ubuntu      "
echo "============================================="

# 1. Check if python3 is installed
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 is not installed."
    echo "Please run the following command to install dependencies:"
    echo "  sudo apt update && sudo apt install python3 python3-pip python3-venv python3-tk -y"
    exit 1
fi

# 2. Check if tkinter is available in system python
if ! python3 -c "import tkinter" &> /dev/null; then
    echo "Error: python3-tk (Tkinter) is not installed."
    echo "Please run the following command to install it:"
    echo "  sudo apt update && sudo apt install python3-tk -y"
    exit 1
fi

# 3. Determine venv location
#    exFAT/NTFS/FAT32 drives don't support symlinks, which python3 -m venv needs.
#    Detect the filesystem type and fall back to ~/.cache if symlinks aren't supported.
FS_TYPE=$(df -T "$SCRIPT_DIR" 2>/dev/null | awk 'NR==2 {print $2}')
case "$FS_TYPE" in
    exfat|ntfs|ntfs-3g|fuseblk|vfat|fat32|msdos)
        VENV_DIR="$HOME/.cache/mcu_flash_gui_venv"
        echo "Note: Project is on a $FS_TYPE drive (no symlink support)."
        echo "  Virtual environment will be created at: $VENV_DIR"
        ;;
    *)
        VENV_DIR="$SCRIPT_DIR/venv"
        ;;
esac

# 4. Create virtual environment if it doesn't exist (or is broken)
if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "Creating virtual environment in $VENV_DIR..."
    rm -rf "$VENV_DIR" 2>/dev/null
    if ! python3 -m venv "$VENV_DIR" 2>&1; then
        echo "Error: Failed to create virtual environment."
        echo "Please make sure python3-venv is installed:"
        echo "  sudo apt update && sudo apt install python3-venv -y"
        exit 1
    fi
    echo "Virtual environment created successfully."
fi

# 5. Activate virtual environment
source "$VENV_DIR/bin/activate"

# 6. Check if tkinter is available inside the venv
if ! python3 -c "import tkinter" &> /dev/null; then
    echo "Warning: tkinter is not working inside the virtual environment."
    echo "Make sure python3-tk is installed: sudo apt install python3-tk"
fi

# 7. Run the bootstrap/GUI script
if [ -f "$SCRIPT_DIR/src/modules/bootstrap_linux.py" ]; then
    python3 "$SCRIPT_DIR/src/modules/bootstrap_linux.py"
elif [ -f "$SCRIPT_DIR/mcu_flash_gui_linux.py" ]; then
    python3 "$SCRIPT_DIR/mcu_flash_gui_linux.py"
else
    echo "Error: Neither src/modules/bootstrap_linux.py nor mcu_flash_gui_linux.py was found in $SCRIPT_DIR."
    exit 1
fi
