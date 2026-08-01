#!/usr/bin/env python3
"""
IDE Path Auto-Repair Utility
Dynamically updates compile_commands.json and .clangd paths when project is moved or user/device changes.
"""

import os
import re

def get_slash_formats(path):
    """Returns a list of tuples with (old_format, representation) for replacement."""
    forward = path.replace("\\", "/")
    single_back = path.replace("/", "\\")
    double_back = path.replace("/", "\\\\")
    return [
        (double_back, "double backslash"),
        (single_back, "single backslash"),
        (forward, "forward slash")
    ]

def fix_paths():
    # 1. Get the current workspace directory dynamically
    workspace_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    print(f"[*] Current workspace directory detected: {workspace_dir}")
    
    # Target files to fix
    clangd_file = os.path.join(workspace_dir, ".clangd")
    commands_file = os.path.join(workspace_dir, "compile_commands.json")
    
    if not os.path.exists(clangd_file) and not os.path.exists(commands_file):
        print("[!] No configuration files (.clangd or compile_commands.json) found in this directory.")
        return

    old_platformio = None
    old_directory = None

    # 2. Inspect compile_commands.json to dynamically learn the previous paths
    if os.path.exists(commands_file):
        try:
            print("[*] Analyzing compile_commands.json to learn old paths...")
            with open(commands_file, "r", encoding="utf-8", errors="ignore") as f:
                # Read first 10,000 chars to find directory and include patterns
                sample = f.read(20000)
                
            # Find directory pattern, e.g. "directory": "c:\\Users\\napht\\..."
            dir_match = re.search(r'"directory"\s*:\s*"([^"]+)"', sample)
            if dir_match:
                # Convert double backslashes to normal slashes for standard base representation
                old_directory = dir_match.group(1).replace("\\\\", "/").replace("\\", "/")
                print(f"    Found old workspace directory path: {old_directory}")
                
            # Find platformio pattern, e.g. C:\\Users\\napht\\.platformio
            pio_match = re.search(r'([a-zA-Z]:[\\/]+[^"\s]+?[\\/]+\.platformio)', sample)
            if pio_match:
                old_platformio = pio_match.group(1).replace("\\\\", "/").replace("\\", "/")
                print(f"    Found old PlatformIO base path: {old_platformio}")
        except Exception as e:
            print(f"[!] Error reading compile_commands.json: {e}")

    # Fallback to standard hardcoded pattern search if we couldn't find them dynamically
    if not old_platformio:
        old_platformio = "C:/Users/napht/.platformio"
    if not old_directory:
        old_directory = "c:/Users/napht/Documents/_MCU_Flash_GUI_V.6-byNAPH"

    # Define target local replacements
    new_platformio = os.path.join(workspace_dir, "env", ".platformio")
    new_directory = workspace_dir

    print(f"[*] Mapping old paths to new local paths:")
    print(f"    PlatformIO: {old_platformio} -> {new_platformio}")
    print(f"    Directory:  {old_directory} -> {new_directory}")

    # Build replacement lists
    replacements = []
    
    # 1. PlatformIO replacements
    old_pio_formats = get_slash_formats(old_platformio)
    new_pio_formats = get_slash_formats(new_platformio)
    for i in range(len(old_pio_formats)):
        replacements.append((old_pio_formats[i][0], new_pio_formats[i][0], f"PlatformIO ({old_pio_formats[i][1]})"))
        
    # 2. Directory replacements
    old_dir_formats = get_slash_formats(old_directory)
    new_dir_formats = get_slash_formats(new_directory)
    for i in range(len(old_dir_formats)):
        replacements.append((old_dir_formats[i][0], new_dir_formats[i][0], f"Directory ({old_dir_formats[i][1]})"))

    # Apply changes to files
    for file_path in [clangd_file, commands_file]:
        if not os.path.exists(file_path):
            continue
            
        print(f"[*] Processing {os.path.basename(file_path)}...")
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
                
            modified = False
            for old, new, desc in replacements:
                count = content.count(old)
                if count > 0:
                    content = content.replace(old, new)
                    print(f"    Replaced {count} occurrences of {desc}")
                    modified = True
                    
            if modified:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)
                print(f"    Successfully updated {os.path.basename(file_path)}")
            else:
                print("    No replacements needed or paths already matching.")
        except Exception as e:
            print(f"[!] Error updating {file_path}: {e}")

    print("[*] Complete! Paths successfully aligned for this device.")

if __name__ == "__main__":
    fix_paths()
