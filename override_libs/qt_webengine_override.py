#!/usr/bin/env python3
"""
Qt WebEngine Library Override Script
====================================

Cross-platform script to override Qt WebEngine and FFmpeg libraries in virtual environments
to resolve symbol mismatch issues between bundled and system libraries.

Supports: Linux and Windows
Author: Generated for EffortScraper project
Date: 2025-07-21
"""

import os
import sys
import shutil
import platform
import argparse
from pathlib import Path
from typing import List, Dict, Tuple, Optional


class LibraryOverride:
    """Handles Qt WebEngine library overrides across platforms"""
    
    def __init__(self, venv_path: Optional[str] = None):
        self.platform = platform.system().lower()
        self.venv_path = Path(venv_path) if venv_path else self._detect_venv()
        self.script_dir = Path(__file__).parent
        self.override_libs_dir = self.script_dir / "override_libs"
        
        # Library mappings for different platforms
        self.library_mappings = self._get_library_mappings()
        
    def _detect_venv(self) -> Path:
        """Auto-detect virtual environment path"""
        # Check if we're in a venv
        if hasattr(sys, 'real_prefix') or (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix):
            return Path(sys.prefix)
        
        # Check common venv locations
        current_dir = Path.cwd()
        for possible_venv in [current_dir / ".venv", current_dir / "venv", current_dir / "env"]:
            if possible_venv.exists() and (possible_venv / "pyvenv.cfg").exists():
                return possible_venv
        
        raise RuntimeError("Could not detect virtual environment. Please specify --venv-path")
    
    def _get_library_mappings(self) -> Dict[str, List[Tuple[str, str, str]]]:
        """Get platform-specific library mappings (source_file, target_file, target_subdir)"""
        if self.platform == "linux":
            return {
                "qt_webengine_pyside6": [
                    ("libQt6WebEngineCore.so.6.9.1", "libQt6WebEngineCore.so.6", "PySide6/Qt/lib"),
                    ("libQt6WebEngineQuick.so.6.9.1", "libQt6WebEngineQuick.so.6", "PySide6/Qt/lib"),
                    ("libQt6WebEngineWidgets.so.6.9.1", "libQt6WebEngineWidgets.so.6", "PySide6/Qt/lib"),
                    ("libQt6WebEngineQuickDelegatesQml.so.6.9.1", "libQt6WebEngineQuickDelegatesQml.so.6", "PySide6/Qt/lib"),
                ],
                "qt_webengine_pyqt6": [
                    ("libQt6WebEngineCore.so.6.9.1", "libQt6WebEngineCore.so.6", "PyQt6/Qt6/lib"),
                    ("libQt6WebEngineQuick.so.6.9.1", "libQt6WebEngineQuick.so.6", "PyQt6/Qt6/lib"),
                    ("libQt6WebEngineWidgets.so.6.9.1", "libQt6WebEngineWidgets.so.6", "PyQt6/Qt6/lib"),
                    ("libQt6WebEngineQuickDelegatesQml.so.6.9.1", "libQt6WebEngineQuickDelegatesQml.so.6", "PyQt6/Qt6/lib"),
                ],
                "ffmpeg_pyside6": [
                    ("libavformat.so.61.7.100", "libavformat.so.61.7.100", "PySide6/Qt/lib"),
                    ("libavformat.so.61.7.100", "libavformat.so.61", "PySide6/Qt/lib"),
                    ("libavformat.so.61.7.100", "libavformat.so", "PySide6/Qt/lib"),
                    ("libavcodec.so.61.19.101", "libavcodec.so.61.19.101", "PySide6/Qt/lib"),
                    ("libavcodec.so.61.19.101", "libavcodec.so.61", "PySide6/Qt/lib"),
                    ("libavcodec.so.61.19.101", "libavcodec.so", "PySide6/Qt/lib"),
                    ("libavutil.so.59.39.100", "libavutil.so.59.39.100", "PySide6/Qt/lib"),
                    ("libavutil.so.59.39.100", "libavutil.so.59", "PySide6/Qt/lib"),
                    ("libavutil.so.59.39.100", "libavutil.so", "PySide6/Qt/lib"),
                ],
                "ffmpeg_pyqt6": [
                    ("libavformat.so.61.7.100", "libavformat.so.61", "PyQt6/Qt6/lib"),
                    ("libavcodec.so.61.19.101", "libavcodec.so.61", "PyQt6/Qt6/lib"),
                    ("libavutil.so.59.39.100", "libavutil.so.59", "PyQt6/Qt6/lib"),
                ]
            }
        elif self.platform == "windows":
            return {
                "qt_webengine_pyside6": [
                    ("Qt6WebEngineCore.dll", "Qt6WebEngineCore.dll", "PySide6/Qt6/bin"),
                    ("Qt6WebEngineQuick.dll", "Qt6WebEngineQuick.dll", "PySide6/Qt6/bin"),
                    ("Qt6WebEngineWidgets.dll", "Qt6WebEngineWidgets.dll", "PySide6/Qt6/bin"),
                    ("Qt6WebEngineQuickDelegatesQml.dll", "Qt6WebEngineQuickDelegatesQml.dll", "PySide6/Qt6/bin"),
                ],
                "qt_webengine_pyqt6": [
                    ("Qt6WebEngineCore.dll", "Qt6WebEngineCore.dll", "PyQt6/Qt6/bin"),
                    ("Qt6WebEngineQuick.dll", "Qt6WebEngineQuick.dll", "PyQt6/Qt6/bin"),
                    ("Qt6WebEngineWidgets.dll", "Qt6WebEngineWidgets.dll", "PyQt6/Qt6/bin"),
                    ("Qt6WebEngineQuickDelegatesQml.dll", "Qt6WebEngineQuickDelegatesQml.dll", "PyQt6/Qt6/bin"),
                ],
                "ffmpeg_pyside6": [
                    ("avformat-61.dll", "avformat-61.dll", "PySide6/Qt6/bin"),
                    ("avcodec-61.dll", "avcodec-61.dll", "PySide6/Qt6/bin"),
                    ("avutil-59.dll", "avutil-59.dll", "PySide6/Qt6/bin"),
                ],
                "ffmpeg_pyqt6": [
                    ("avformat-61.dll", "avformat-61.dll", "PyQt6/Qt6/bin"),
                    ("avcodec-61.dll", "avcodec-61.dll", "PyQt6/Qt6/bin"),
                    ("avutil-59.dll", "avutil-59.dll", "PyQt6/Qt6/bin"),
                ]
            }
        else:
            raise RuntimeError(f"Unsupported platform: {self.platform}")
    
    def _get_site_packages_path(self) -> Path:
        """Get site-packages path for the virtual environment"""
        if self.platform == "linux":
            return self.venv_path / "lib" / "python3.13" / "site-packages"
        elif self.platform == "windows":
            return self.venv_path / "Lib" / "site-packages"
    
    def backup_library(self, lib_path: Path) -> bool:
        """Create backup of existing library"""
        if not lib_path.exists():
            return False
        
        backup_path = lib_path.with_suffix(lib_path.suffix + ".backup")
        if backup_path.exists():
            print(f"Backup already exists: {backup_path}")
            return True
            
        try:
            shutil.copy2(lib_path, backup_path)
            print(f"Backed up: {lib_path} -> {backup_path}")
            return True
        except Exception as e:
            print(f"Failed to backup {lib_path}: {e}")
            return False
    
    def override_library(self, source_lib: str, target_lib: str, target_subdir: str) -> bool:
        """Override a single library"""
        source_path = self.override_libs_dir / source_lib
        site_packages = self._get_site_packages_path()
        target_dir = site_packages / target_subdir
        target_path = target_dir / target_lib
        
        if not source_path.exists():
            print(f"Source library not found: {source_path}")
            return False
        
        if not target_dir.exists():
            print(f"Target directory not found: {target_dir}")
            return False
        
        # Backup existing library
        if target_path.exists():
            if not self.backup_library(target_path):
                return False
            
            # Remove existing file/symlink
            try:
                if target_path.is_symlink():
                    target_path.unlink()
                else:
                    target_path.unlink()
            except Exception as e:
                print(f"Failed to remove existing library {target_path}: {e}")
                return False
        
        # Copy or symlink the new library
        try:
            if self.platform == "linux":
                # Create symlink on Linux
                target_path.symlink_to(source_path.absolute())
                print(f"Symlinked: {target_path} -> {source_path}")
            else:
                # Copy file on Windows
                shutil.copy2(source_path, target_path)
                print(f"Copied: {source_path} -> {target_path}")
            return True
        except Exception as e:
            print(f"Failed to override {target_path}: {e}")
            return False
    
    def perform_override(self, library_type: str = "all") -> bool:
        """Perform library override"""
        if not self.override_libs_dir.exists():
            print(f"Override libraries directory not found: {self.override_libs_dir}")
            return False
        
        success_count = 0
        total_count = 0
        
        # Determine which library types to override
        if library_type == "all":
            types_to_override = list(self.library_mappings.keys())
        elif library_type == "qt_webengine":
            types_to_override = ["qt_webengine_pyside6", "qt_webengine_pyqt6"]
        elif library_type == "ffmpeg":
            types_to_override = ["ffmpeg_pyside6", "ffmpeg_pyqt6"]
        else:
            types_to_override = [library_type]
        
        for lib_type in types_to_override:
            if lib_type not in self.library_mappings:
                print(f"Unknown library type: {lib_type}")
                continue
                
            print(f"\nOverriding {lib_type} libraries...")
            mappings = self.library_mappings[lib_type]
            
            for source_lib, target_lib, target_subdir in mappings:
                total_count += 1
                if self.override_library(source_lib, target_lib, target_subdir):
                    success_count += 1
        
        print(f"\nOverride complete: {success_count}/{total_count} libraries processed successfully")
        return success_count == total_count
    
    def revert_overrides(self) -> bool:
        """Revert all overrides by restoring from backups"""
        site_packages = self._get_site_packages_path()
        reverted_count = 0
        
        print("Reverting library overrides...")
        
        # Get all possible target directories from mappings
        target_subdirs = set()
        for mappings in self.library_mappings.values():
            for _, _, target_subdir in mappings:
                target_subdirs.add(target_subdir)
        
        for target_subdir in target_subdirs:
            target_dir = site_packages / target_subdir
            if not target_dir.exists():
                continue
                
            print(f"\nProcessing: {target_dir}")
            
            # Find all backup files
            backup_files = list(target_dir.glob("*.backup"))
            for backup_file in backup_files:
                original_file = backup_file.with_suffix("")
                
                try:
                    # Remove current file/symlink
                    if original_file.exists():
                        original_file.unlink()
                    
                    # Restore from backup
                    shutil.move(str(backup_file), str(original_file))
                    print(f"Restored: {original_file}")
                    reverted_count += 1
                    
                except Exception as e:
                    print(f"Failed to revert {original_file}: {e}")
        
        print(f"\nRevert complete: {reverted_count} libraries restored")
        return reverted_count > 0
    
    def list_overrides(self) -> None:
        """List current overrides and backups"""
        site_packages = self._get_site_packages_path()
        
        print("Current library overrides:")
        print("=" * 50)
        
        for lib_type, mappings in self.library_mappings.items():
            print(f"\n{lib_type.upper()}:")
            
            for source_lib, target_lib, target_subdir in mappings:
                target_dir = site_packages / target_subdir
                target_path = target_dir / target_lib
                backup_path = target_dir / (target_lib + ".backup")
                
                if target_path.exists():
                    status = "OVERRIDDEN" if backup_path.exists() else "ORIGINAL"
                    if self.platform == "linux" and target_path.is_symlink():
                        link_target = target_path.readlink()
                        print(f"  {target_lib}: {status} -> {link_target}")
                    else:
                        print(f"  {target_lib}: {status}")
                else:
                    print(f"  {target_lib}: MISSING")


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Qt WebEngine Library Override Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python qt_webengine_override.py override           # Override all libraries
  python qt_webengine_override.py override --type qt_webengine  # Override only Qt WebEngine
  python qt_webengine_override.py revert             # Revert all overrides
  python qt_webengine_override.py list               # List current overrides
  python qt_webengine_override.py --venv-path /path/to/venv override
        """
    )
    
    parser.add_argument(
        "action",
        choices=["override", "revert", "list"],
        help="Action to perform"
    )
    
    parser.add_argument(
        "--type",
        choices=["all", "qt_webengine", "ffmpeg", "qt_webengine_pyside6", "qt_webengine_pyqt6", "ffmpeg_pyside6", "ffmpeg_pyqt6"],
        default="all",
        help="Type of libraries to override (default: all)"
    )
    
    parser.add_argument(
        "--venv-path",
        help="Path to virtual environment (auto-detected if not specified)"
    )
    
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without actually doing it"
    )
    
    args = parser.parse_args()
    
    try:
        override_tool = LibraryOverride(args.venv_path)
        
        print(f"Platform: {override_tool.platform}")
        print(f"Virtual Environment: {override_tool.venv_path}")
        print(f"Override Libraries: {override_tool.override_libs_dir}")
        print()
        
        if args.dry_run:
            print("DRY RUN MODE - No changes will be made")
            print()
        
        if args.action == "override":
            if args.dry_run:
                print("Would override libraries...")
                override_tool.list_overrides()
            else:
                success = override_tool.perform_override(args.type)
                sys.exit(0 if success else 1)
                
        elif args.action == "revert":
            if args.dry_run:
                print("Would revert overrides...")
            else:
                success = override_tool.revert_overrides()
                sys.exit(0 if success else 1)
                
        elif args.action == "list":
            override_tool.list_overrides()
            
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()