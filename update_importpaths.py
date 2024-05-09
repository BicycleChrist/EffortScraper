import sys
import pathlib


# name of the project folder
toplevel_name = "EffortScraper"

# these directories get added to path
subdir_names = [
    "Boddssuck",
    "CHN",
    "MLBAnalytics",
    "NHLvacuum",
]

# symlinks are dropped into these directories (allowing them to import this file as well)
symlink_into = []
# note: paths listed here must also be in 'subdir_names'; they won't be used otherwise


def FindToplevelPath() -> pathlib.Path:
    cwd = pathlib.Path.cwd()
    if cwd.name == toplevel_name:
        return cwd
    elif toplevel_name in cwd.parts:
        for parent in cwd.parents:
            if parent.name == toplevel_name:
                return parent
        assert False, "Unreachable"
    else:
        print(f"toplevel directory: {toplevel_name} is not a parent of current directory")
        raise ImportError
    assert False, "Unreachable"


# returns only the good paths if onlyGood is True 
def Update_ImportPaths(onlyGood=False):
    toplevel_path = FindToplevelPath()
    unchecked_paths = [ toplevel_path / subname for subname in subdir_names]
    good_paths = [ str(path.absolute()) for path in unchecked_paths if path.exists() and path.is_dir() ]
    bad_paths  = [ path.name for path in unchecked_paths if not path.exists() or not path.is_dir() ]
    if len(bad_paths) > 0:
        for name in bad_paths:
            print(f"error: '{name}' does not exist or is not a directory")
        raise ImportError
    if onlyGood: return good_paths
    return [ *good_paths, *sys.path]


# 'relative_to' doesn't accept 'walk_up' until 3.12
def CreateRelativePath(frompath: pathlib.Path, topath: pathlib.Path, target_is_dir=False):
    target_parents = []
    if target_is_dir: target_parents.append(topath)
    target_parents.extend(topath.parents)
    
    relative_string = ""
    for parent in frompath.parents:
        if parent in target_parents:
            if target_is_dir:
                relative_string = relative_string + parent.name + "/"
            relative_string = relative_string + topath.name
            return relative_string
        relative_string = relative_string + "../"
    print("error: paths had no common ancestor")
    return None


def CreateSymlinks():
    thisfile = pathlib.Path(__file__)  # __file__ returns absolute path, by the way
    assert thisfile.exists(), "how are you running this??"
    # Update_ImportPaths returns a list of strings (for sys.path), so we convert back to pathlib.Path
    good_paths = [pathlib.Path(path) for path in Update_ImportPaths(True)]
    target_dirs = [path for path in good_paths if path.is_dir() and path.name in symlink_into]
    
    for subdirectory in target_dirs:
        symlink_location = subdirectory / thisfile.name
        if symlink_location.exists(): continue
        #symlink_location.symlink_to(thisfile)
        relativePath = CreateRelativePath(symlink_location, thisfile)
        if relativePath is None: print("failed creating symlink!"); continue
        symlink_location.symlink_to(relativePath)
        print(f"symlink created at: {symlink_location}")
    return


if __name__ == "__main__":
    print(f"default sys.path:\n {sys.path} \n")
    newpaths = Update_ImportPaths()
    print(f"updated sys.path:\n {newpaths} \n")
    CreateSymlinks()
    exit(0)


# called unconditionally on import
sys.path = Update_ImportPaths()
