#!/usr/bin/env python3
"""
EffortOdds Shortcut Creator

This script creates desktop shortcuts for the EffortOdds application on both
Windows and Linux systems. It handles:
- Creating the appropriate shortcut type for each OS
- Properly activating the virtual environment before launching
- Setting up the application icon
- Creating fallback launchers

Place this script in the same directory as your EffortOdds.py file and run it once.
"""

import pathlib
import sys
import os
import subprocess
import shutil
import platform
import traceback

# Import optional dependencies without failing immediately
try:
    import win32com.client
except ImportError:
    win32com = None

try:
    from PIL import Image
except ImportError:
    Image = None

# Configuration - Edit these to match your setup
APP_NAME = "Effort Odds"
SCRIPT_NAME = "EffortOdds.py"
ICON_NAME = "AppIcon.png"

# Default virtual environment paths (modify if your setup is different)
# If set to None, the script will try to detect the virtual environment
VENV_PATH = None  # e.g. "/path/to/venv" or "C:\\path\\to\\venv"

def detect_virtual_env():
    """Attempt to detect the current virtual environment path."""
    # Check if we're running in a virtual environment
    if hasattr(sys, 'real_prefix') or (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix):
        return sys.prefix

    # Check common locations
    script_dir = pathlib.Path(__file__).parent.resolve()
    common_venv_names = ['venv', 'env', '.venv', '.env', 'virtualenv']

    # Check current directory and parent directory
    for venv_name in common_venv_names:
        venv_path = script_dir / venv_name
        if venv_path.exists() and ((venv_path / 'bin').exists() or (venv_path / 'Scripts').exists()):
            return str(venv_path)

        # Check parent directory
        parent_venv_path = script_dir.parent / venv_name
        if parent_venv_path.exists() and ((parent_venv_path / 'bin').exists() or (parent_venv_path / 'Scripts').exists()):
            return str(parent_venv_path)

    return None

def get_activate_command(venv_path):
    """Get the appropriate activate command for the platform."""
    if venv_path is None:
        # If no virtual environment is found, return empty commands
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

    print(f"Warning: Could not find activation script in {venv_path}")
    return "", ""

def create_windows_shortcut():
    """Create a Windows desktop shortcut for EffortOdds with venv activation."""
    try:
        # Check for required modules
        if win32com is None or Image is None:
            print("Required packages missing. Installing...")
            subprocess.run([sys.executable, "-m", "pip", "install", "pywin32", "pillow"], check=True)

            # Re-import after installation
            import win32com.client
            from PIL import Image

        script_dir = pathlib.Path(__file__).parent.resolve()
        script_path = script_dir / SCRIPT_NAME
        desktop = pathlib.Path.home() / "Desktop"

        # Ensure the script exists
        if not script_path.exists():
            print(f"Error: {SCRIPT_NAME} not found in {script_dir}")
            return False

        # Create ICO file from PNG
        icon_path = script_dir / ICON_NAME
        ico_path = script_dir / (ICON_NAME.rsplit('.', 1)[0] + '.ico')

        if not icon_path.exists():
            print(f"Warning: Icon {ICON_NAME} not found in {script_dir}")
            ico_path = None
        else:
            # Convert PNG to ICO if it doesn't exist yet
            if not ico_path.exists():
                try:
                    img = Image.open(icon_path)
                    img.save(ico_path, format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (32, 32), (16, 16)])
                    print(f"Created ICO file at {ico_path}")
                except Exception as e:
                    print(f"Error creating ICO file: {e}")
                    ico_path = icon_path

        # Detect virtual environment
        venv_path = VENV_PATH or detect_virtual_env()
        activate_cmd, activate_prefix = get_activate_command(venv_path)

        # Create batch file launcher to activate venv and run script
        batch_path = script_dir / f"launch_{SCRIPT_NAME.split('.')[0]}.bat"
        with open(batch_path, "w") as f:
            f.write('@echo off\n')
            f.write(f'cd /d "{script_dir}"\n')
            if activate_cmd:
                f.write(f'{activate_cmd}\n')
            f.write(f'python "{script_path}"\n')
            f.write('if errorlevel 1 pause\n')  # Keep window open on error

        print(f"Created batch launcher at {batch_path}")

        # Create desktop shortcut to the batch file
        shortcut_path = desktop / f"{APP_NAME.replace(' ', '')}.lnk"

        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(str(shortcut_path))
        shortcut.TargetPath = str(batch_path)
        if ico_path:
            shortcut.IconLocation = str(ico_path)
        shortcut.WorkingDirectory = str(script_dir)
        shortcut.Description = f"{APP_NAME} - Sports Odds Tracking Application"
        shortcut.save()

        print(f"Created Windows shortcut at {shortcut_path}")
        return True

    except Exception as e:
        print(f"Error creating Windows shortcut: {e}")
        traceback.print_exc()
        return False

def create_linux_shortcut():
    """Create a Linux desktop shortcut for EffortOdds with venv activation."""
    try:
        script_dir = pathlib.Path(__file__).parent.resolve()
        script_path = script_dir / SCRIPT_NAME

        # Ensure the script exists
        if not script_path.exists():
            print(f"Error: {SCRIPT_NAME} not found in {script_dir}")
            return False

        # Detect virtual environment
        venv_path = VENV_PATH or detect_virtual_env()
        activate_cmd, activate_prefix = get_activate_command(venv_path)

        # Create shell script launcher
        shell_path = script_dir / f"launch_{SCRIPT_NAME.split('.')[0]}.sh"
        with open(shell_path, "w") as f:
            f.write('#!/bin/bash\n')
            f.write(f'cd "{script_dir}"\n')
            if activate_cmd:
                f.write(f'{activate_cmd}\n')
            f.write(f'python3 "{script_path}"\n')

        # Make executable
        os.chmod(shell_path, 0o755)
        print(f"Created shell launcher at {shell_path}")

        # Create .desktop file
        desktop_dir = pathlib.Path.home() / ".local" / "share" / "applications"
        desktop_dir.mkdir(parents=True, exist_ok=True)

        icon_path = script_dir / ICON_NAME
        sanitized_name = APP_NAME.lower().replace(' ', '-')
        desktop_file = desktop_dir / f"{sanitized_name}.desktop"

        desktop_content = f"""[Desktop Entry]
Version=1.0
Type=Application
Name={APP_NAME}
Comment=Sports Odds Tracking Application
Exec={shell_path}
Icon={icon_path}
Terminal=false
Categories=Utility;Office;
StartupNotify=true
"""

        with open(desktop_file, "w") as f:
            f.write(desktop_content)

        # Make it executable
        os.chmod(desktop_file, 0o755)
        print(f"Created .desktop file at {desktop_file}")

        # Try to create symlink on desktop
        user_desktop = pathlib.Path.home() / "Desktop"
        if user_desktop.exists():
            desktop_symlink = user_desktop / f"{sanitized_name}.desktop"
            if not desktop_symlink.exists():
                try:
                    # Copy the .desktop file to the Desktop (symlinks don't work well on all Linux distros)
                    shutil.copy2(desktop_file, desktop_symlink)
                    os.chmod(desktop_symlink, 0o755)
                    print(f"Created desktop shortcut at {desktop_symlink}")
                except Exception as e:
                    print(f"Error creating desktop shortcut: {e}")

        # Refresh desktop (might work on some desktop environments)
        try:
            subprocess.run(["update-desktop-database", str(desktop_dir)], check=False)
        except:
            pass

        return True

    except Exception as e:
        print(f"Error creating Linux shortcut: {e}")
        traceback.print_exc()
        return False

def print_system_info():
    """Print system information to help with debugging."""
    print(f"System information:")
    print(f"- Platform: {platform.system()} {platform.release()}")
    print(f"- Python: {platform.python_version()}")
    print(f"- Python executable: {sys.executable}")

    venv_path = detect_virtual_env()
    if venv_path:
        print(f"- Virtual environment detected: {venv_path}")
    else:
        print("- No virtual environment detected")

def main():
    print(f"Creating {APP_NAME} desktop shortcut...")
    print_system_info()

    # Create shortcut based on platform
    if sys.platform == "win32":
        if create_windows_shortcut():
            print("Windows shortcut created successfully!")
    elif sys.platform.startswith("linux"):
        if create_linux_shortcut():
            print("Linux shortcut created successfully!")
    else:
        print(f"Unsupported platform: {sys.platform}")
        print("This script supports Windows and Linux only.")
        return

    print("\nShortcut creation complete!")
    print(f"You can now launch {APP_NAME} from your desktop.")

if __name__ == "__main__":
    main()
