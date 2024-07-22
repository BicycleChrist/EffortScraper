import pathlib
import sys


# name of the project folder
toplevel_name = "EffortScraper"

# these directories get added to path
subdir_names = [
    "Boddssuck",
    "CHN",
    "MLBAnalytics",
    "NHLvacuum",
]

# symlinks are dropped into these directories (allowing files there to import this file as well), if they exist
# Each key is a target (directory) name, and the associated value is a list of parents (if empty, assume toplevel). All strings.
# the values get evaluated/resolved to actual paths. If a value matches a previous key, that key's (resolved) paths will be substituted instead.
symlink_into = {
    "MLBAnalytics": [],
    "Imgui": [],
    "Database": [],
}


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
        print(f"toplevel directory: '{toplevel_name}' is not a parent of current directory")
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
            print(f"ERROR: '{name}' does not exist or is not a directory")
        raise ImportError
    if onlyGood: return good_paths
    return [ str(toplevel_path), *good_paths, *sys.path]


# 'relative_to' doesn't accept 'walk_up' until 3.12
def CreateRelativePath(frompath: pathlib.PurePath, topath: pathlib.PurePath, target_is_dir=False):
    toplevel_path = FindToplevelPath()
    target_parents = []
    if target_is_dir and topath.is_relative_to(toplevel_path): 
        target_parents.append(topath)
    
    topath_parents_filtered = [p for p in topath.parents if p.is_relative_to(toplevel_path)]
    target_parents.extend(topath_parents_filtered)
    
    # Simply assigning 'frompath' to 'relative_path' doesn't work; that creates an absolute path
    relative_path = frompath.relative_to(frompath)
    frompath_parents_filtered = [p for p in frompath.parents if p.is_relative_to(toplevel_path)]
    for parent in frompath_parents_filtered:
        if parent in target_parents:  # TODO: handle the case where the target is a dir?
            relative_path = relative_path.joinpath(topath.name)
            return relative_path
        relative_path = relative_path.joinpath('..')
    
    print("error: paths had no common ancestor")
    return None


# TODO: rewrite using match statement or something
# resolves / rewrites the 'symlink_into' dict
def ResolveSymlinkInto():
    toplevel = FindToplevelPath()
    targets = []
    for targetname, parentlist in symlink_into.items():
        if len(parentlist) == 0:  # then it's directly under toplevel
            target = toplevel/targetname
            if not target.exists(): f"could not locate toplevel-directory: '{target}'"; continue;
            parentlist.append(target)
        else:
            resolved_parents = []
            for parent in parentlist:
                if parent in symlink_into.keys():
                    resolved_parents.extend(symlink_into[parent])  # substituting
                else:
                    target_parent = toplevel/parent
                    if not target_parent.exists(): f"could not locate toplevel-directory: '{target_parent}'"; continue;
                    resolved_parents.append(target_parent)
            resolved_targets = [rp/targetname for rp in resolved_parents]
            valid_targets = [rt for rt in resolved_targets if rt.exists() and rt.is_dir()]  # targets are supposed to be dirs
            invalid_targets = [rt for rt in resolved_targets if rt not in valid_targets]
            print(f"{targetname}: {valid_targets}")
            if len(invalid_targets) > 0: print(f"these targets resolved for '{targetname}' could not be located (or location is not a directory): '{invalid_targets}'")
            parentlist.clear()
            parentlist.extend(valid_targets)
        
        targets.extend(parentlist)
    
    return targets


# returns a list of newly-written symlinks (symlink_location, relativePath)
def CreateSymlinks(report_existing=True, overwrite=False) \
    -> list[tuple[pathlib.Path, pathlib.PurePath]]:
    toplevel_path = FindToplevelPath()
    thisfile = pathlib.Path(__file__)  # __file__ returns absolute path, by the way
    assert thisfile.exists(), "how are you running this??"
    # Update_ImportPaths returns a list of strings (for sys.path), so we convert back to pathlib.Path
    target_dirs = ResolveSymlinkInto()
    
    # tuples are: (symlink_location, relativePath)
    written_symlinks: list[tuple[pathlib.Path, pathlib.PurePath]] = []
    for subdirectory in target_dirs:
        symlink_location = subdirectory / thisfile.name
        assert (symlink_location != thisfile), "ERROR: symlink would overwrite this file!"
        # assert (not thisfile.samefile(symlink_location))  # can't do this because it resolves the symlink and the assert fails (if symlink target is correct)
        if symlink_location.exists():
            # before overwrite, verify that the pre-existing file is a symlink (and assume it belongs to this script)
            # we don't use 'samefile()' to compare them, because we can't assume the old symlink's target is correct
            if not symlink_location.is_symlink():
                print(f"ERROR: file already exists at: '{symlink_location.relative_to(toplevel_path)}', but it is NOT a symlink!")
                if overwrite:
                    moved_location = f"{symlink_location.with_suffix('.backup')}"
                    symlink_location.rename(moved_location)
                    print(f"pre-existing (non-symlink) file moved to: {moved_location}")
                else: print(f"WARNING: '{symlink_location}' will NOT be overwitten (not a symlink); skipping"); continue;
            # if pre-existing file IS a symlink 
            elif report_existing: print(f"existing symlink found: '{symlink_location.relative_to(toplevel_path)}'")
            if overwrite: print(f"OVERWRITING '{symlink_location}'"); symlink_location.unlink(missing_ok=True);
            else: continue  # fallthrough if 'overwrite'
        # note: symlink_location must NOT exist after this point; otherwise 'symlink_to' will fail
        # 'symlink_to' does not have any sort of "missing_ok" argument
        
        relativePath = CreateRelativePath(symlink_location, thisfile)
        if relativePath is None: print("failed to construct relativePath while creating symlink!"); continue
        resolved_path = (subdirectory / relativePath).resolve()
        if resolved_path.is_relative_to(toplevel_path):
            symlink_location.symlink_to(relativePath)
            print(f"symlink created at: '{symlink_location}'")
            written_symlinks.append((symlink_location, relativePath))
            # apparently the 'target_is_directory' argument for "symlink_to()" only has an effect on Windows?
        else:
            print(f"ERROR: invalid symlink (outside toplevel): '{resolved_path}'")
            print(f"\t created when resolving relative-path: '{relativePath}' \n\t from subdirectory: '{subdirectory}'\n")
            print("CreateRelativePath() must be bugged. Please fix it.")
            break
    return written_symlinks


if __name__ == "__main__":
    print(f"default sys.path:\n {sys.path} \n")
    newpaths = Update_ImportPaths()
    print(f"updated sys.path:\n {newpaths} \n")
    CreateSymlinks()
    #CreateSymlinks(overwrite=True)
    exit(0)


# called unconditionally on import
sys.path = Update_ImportPaths()
