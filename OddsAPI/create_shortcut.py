#!/usr/bin/env python3
"""
EffortOdds Shortcut Creator
Creates desktop shortcuts for the EffortOdds application on both Windows and Linux.
"""

import pathlib
import sys
import os
import subprocess
import shutil
import platform
import traceback
from PIL import Image
import win32com.client

# Import flags for optional dependencies
HAS_WIN32COM = False
HAS_PIL = False

# Configuration
APP_NAME = "Effort Odds"
SCRIPT_NAME = "EffortOdds.py"
ICON_NAME = "AppIcon.png"
VENV_PATH = None  # Set to venv path or None for auto-detection


def get_windows_desktop_path():
    """Get Windows Desktop path, handling OneDrive."""
    potential_paths = [
        pathlib.Path.home() / "Desktop",
        pathlib.Path.home() / "OneDrive" / "Desktop",
    ]

    if HAS_WIN32COM:
        try:
            shell = win32com.client.Dispatch("WScript.Shell")
            desktop = shell.SpecialFolders("Desktop")
            potential_paths.insert(0, pathlib.Path(desktop))
        except Exception:
            pass

    for path in potential_paths:
        if path.exists():
            return path

    return potential_paths[0]


def detect_virtual_env():
    """Detect virtual environment path."""
    if hasattr(sys, 'real_prefix') or (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix):
        return sys.prefix

    script_dir = pathlib.Path(__file__).parent.resolve()
    venv_dirs = ['venv', 'env', '.venv', '.env', 'virtualenv']

    for vdir in venv_dirs:
        for base_dir in [script_dir, script_dir.parent]:
            venv_path = base_dir / vdir
            if venv_path.exists() and ((venv_path / 'bin').exists() or (venv_path / 'Scripts').exists()):
                return str(venv_path)

    return None


def get_activate_command(venv_path):
    """Get venv activation command for the platform."""
    if venv_path is None:
        return "", ""

    venv_path = pathlib.Path(venv_path)

    if sys.platform == "win32":
        activate_script = venv_path / "Scripts" / "activate.bat"
        if not activate_script.exists():
            activate_script = venv_path / "bin" / "activate.bat"

        if activate_script.exists():
            return f'call "{activate_script}"', f'call "{activate_script}" && '
    else:  # Linux/Mac
        activate_script = venv_path / "bin" / "activate"
        if activate_script.exists():
            return f'source "{activate_script}"', f'source "{activate_script}" && '

    print(f"Warning: No activation script found in {venv_path}")
    return "", ""


def find_file(filename, search_dirs=None):
    """Find file in common locations."""
    script_dir = pathlib.Path(__file__).parent.resolve()

    if search_dirs is None:
        search_dirs = [
            script_dir,
            script_dir.parent,
            script_dir / "icons",
            script_dir / "images",
            script_dir / "assets",
            script_dir / "resources",
            pathlib.Path.cwd()
        ]

    for directory in search_dirs:
        file_path = directory / filename
        if file_path.exists():
            return file_path

    # If script file and user input is needed
    if filename == SCRIPT_NAME:
        print(f"\nPlease enter the full path to {filename}:")
        user_path = input("> ").strip()
        if user_path and pathlib.Path(user_path).exists():
            return pathlib.Path(user_path)

    return None


def create_ico_file(png_path):
    """Create ICO file from PNG."""
    if not HAS_PIL or not png_path:
        return None

    try:
        ico_path = png_path.with_suffix('.ico')
        if not ico_path.exists():
            img = Image.open(png_path)
            sizes = [(256, 256), (128, 128), (64, 64), (32, 32), (16, 16)]
            img.save(ico_path, format="ICO", sizes=sizes)
            print(f"Created ICO file at {ico_path}")
        return ico_path
    except Exception as e:
        print(f"Error creating ICO: {e}")
        return png_path


def create_windows_shortcut():
    """Create Windows shortcuts."""
    global HAS_WIN32COM, HAS_PIL

    try:
        if not HAS_WIN32COM or not HAS_PIL:
            print("Installing required packages...")
            subprocess.run([sys.executable, "-m", "pip", "install", "pywin32", "pillow"], check=True)

            try:
                import win32com.client
                HAS_WIN32COM = True
                from PIL import Image
                HAS_PIL = True
            except ImportError as e:
                print(f"Import error after installation: {e}")

        script_path = find_file(SCRIPT_NAME)
        if not script_path:
            return False

        script_dir = script_path.parent
        icon_path = find_file(ICON_NAME)
        desktop = get_windows_desktop_path()

        ico_path = create_ico_file(icon_path) if icon_path else None

        venv_path = VENV_PATH or detect_virtual_env()
        activate_cmd, _ = get_activate_command(venv_path)

        # Create batch launcher for windows to run app from shortcut
        batch_path = script_dir / f"launch_{SCRIPT_NAME.split('.')[0]}.bat"
        with open(batch_path, "w") as f:
            f.write('@echo off\n')
            f.write(f'cd /d "{script_dir}"\n')
            if activate_cmd:
                f.write(f'{activate_cmd}\n')
            f.write(f'python "{script_path}"\n')
            f.write('if errorlevel 1 pause\n')

        app_name = APP_NAME.replace(' ', '')

        # Create shortcut
        if HAS_WIN32COM:
            shortcut_path = desktop / f"{app_name}.lnk"

            # Remove existing shortcut if it exists
            if shortcut_path.exists():
                try:
                    shortcut_path.unlink()
                except:
                    shortcut_path = desktop / f"{app_name}_new.lnk"

            shell = win32com.client.Dispatch("WScript.Shell")
            shortcut = shell.CreateShortCut(str(shortcut_path))
            shortcut.TargetPath = str(batch_path)
            if ico_path:
                shortcut.IconLocation = str(ico_path)
            shortcut.WorkingDirectory = str(script_dir)
            shortcut.Description = f"{APP_NAME} - Sports Odds Tracking Application"

            try:
                shortcut.save()
                print(f"Created shortcut at {shortcut_path}")
            except Exception as e:
                print(f"Error saving shortcut: {e}")
                fallback_batch = desktop / f"{app_name}.bat"
                shutil.copy2(batch_path, fallback_batch)
                print(f"Created fallback batch at {fallback_batch}")
        else:
            # Fallback to batch file on desktop
            desktop_batch = desktop / f"{app_name}.bat"
            shutil.copy2(batch_path, desktop_batch)
            print(f"Created batch launcher at {desktop_batch}")

        return True

    except Exception as e:
        print(f"Error: {e}")
        traceback.print_exc()
        return False


def create_linux_shortcut():
    """Create Linux shortcuts."""
    try:
        script_path = find_file(SCRIPT_NAME)
        if not script_path:
            return False

        script_dir = script_path.parent
        icon_path = find_file(ICON_NAME)

        venv_path = VENV_PATH or detect_virtual_env()
        activate_cmd, _ = get_activate_command(venv_path)

        # Create shell launcher
        shell_path = script_dir / f"launch_{SCRIPT_NAME.split('.')[0]}.sh"
        with open(shell_path, "w") as f:
            f.write('#!/bin/bash\n')
            f.write(f'cd "{script_dir}"\n')
            if activate_cmd:
                f.write(f'{activate_cmd}\n')
            f.write(f'python3 "{script_path}"\n')

        os.chmod(shell_path, 0o755)

        # Create .desktop file
        desktop_dir = pathlib.Path.home() / ".local" / "share" / "applications"
        desktop_dir.mkdir(parents=True, exist_ok=True)

        sanitized_name = APP_NAME.lower().replace(' ', '-')
        desktop_file = desktop_dir / f"{sanitized_name}.desktop"

        desktop_content = f"""[Desktop Entry]
Version=1.0
Type=Application
Name={APP_NAME}
Comment=Sports Odds Tracking Application
Exec={shell_path}
"""
        if icon_path:
            desktop_content += f"Icon={icon_path}\n"

        desktop_content += """Terminal=false
Categories=Utility;Office;
StartupNotify=true
"""

        with open(desktop_file, "w") as f:
            f.write(desktop_content)

        os.chmod(desktop_file, 0o755)

        # Copy to desktop
        user_desktop = pathlib.Path.home() / "Desktop"
        if user_desktop.exists():
            desktop_symlink = user_desktop / f"{sanitized_name}.desktop"
            if not desktop_symlink.exists():
                try:
                    shutil.copy2(desktop_file, desktop_symlink)
                    os.chmod(desktop_symlink, 0o755)
                except Exception as e:
                    print(f"Error creating desktop shortcut: {e}")

        # Refresh desktop
        try:
            subprocess.run(["update-desktop-database", str(desktop_dir)], check=False)
        except:
            pass

        return True

    except Exception as e:
        print(f"Error: {e}")
        traceback.print_exc()
        return False


def print_system_info():
    """Print basic system information."""
    print(f"System: {platform.system()} {platform.release()}")
    print(f"Python: {platform.python_version()} at {sys.executable}")

    venv = detect_virtual_env()
    print(f"Virtual env: {venv or 'None detected'}")
    print(f"Dependencies: win32com={HAS_WIN32COM}, PIL={HAS_PIL}")


def main():
    print(f"Creating {APP_NAME} desktop shortcut...")
    print_system_info()

    if sys.platform == "win32":
        if create_windows_shortcut():
            print("Windows shortcut created successfully!")
    elif sys.platform.startswith("linux"):
        if create_linux_shortcut():
            print("Linux shortcut created successfully!")
    else:
        print(f"Unsupported platform: {sys.platform}")
        return

    print(f"\nShortcut creation complete!")


if __name__ == "__main__":
    main()
