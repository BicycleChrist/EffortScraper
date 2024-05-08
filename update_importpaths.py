# import this file to fix imports from subdirectories

import sys
import pathlib

# add subfolders here as necessary
subfolder_names = [
    "Boddssuck",
    "CHN",
    "MLBAnalytics",
    "NHLvacuum",
]


def Update_ImportPaths():
    unchecked_paths = [ pathlib.Path.cwd() / subname for subname in subfolder_names ]
    good_paths = [ str(path.absolute()) for path in unchecked_paths if path.exists() and path.is_dir() ]
    bad_paths  = [ path.name for path in unchecked_paths if not path.exists() or not path.is_dir() ]
    if len(bad_paths) > 0:
        for name in bad_paths:
            print(f"error: '{name}' does not exist or is not a directory")
        raise ImportError
    return [ *good_paths, *sys.path]


if __name__ == "__main__":
    print(f"default sys.path:\n {sys.path} \n")
    newpaths = Update_ImportPaths()
    print(f"updated sys.path:\n {newpaths} \n")
    exit(0)

# called unconditionally on import
sys.path = Update_ImportPaths()
