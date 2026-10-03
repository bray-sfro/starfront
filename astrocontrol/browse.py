"""Folders and files on the capture PC, for choosing a path from a browser.

The desktop window asks Windows for a folder dialog; a browser cannot. Its own
file chooser hands the page a file's contents and hides the path, and the
browser may be on a tablet across the room anyway, while the folder that
matters is on the machine the camera is plugged into. So the page asks this
instead, one folder at a time, and walks the capture PC's disk the way a
dialog would.

Nothing here writes. It lists names, and only names: no file is opened.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

#: More than anybody scrolls through; a folder of 40,000 subs should not
#: become a 40,000-row response.
LIMIT = 2000


class BrowseError(RuntimeError):
    pass


def roots() -> list[str]:
    """The drives on Windows; `/` elsewhere."""
    if os.name != "nt":
        return ["/"]
    listdrives = getattr(os, "listdrives", None)       # Python 3.12+
    if listdrives:
        try:
            return sorted(listdrives())
        except OSError:
            pass
    return [f"{letter}:\\" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            if os.path.exists(f"{letter}:\\")]


def _hidden(entry: os.DirEntry) -> bool:
    if entry.name.startswith((".", "$")):
        return True
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & (getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0)
                              | getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0)))


def _start(path: str) -> Path | None:
    """Where to open: the folder itself, a file's folder, or the nearest
    folder above a path that does not exist yet - a capture folder typed
    before it was created should still open somewhere useful."""
    if not path.strip():
        return Path.home()
    here = Path(os.path.expandvars(os.path.expanduser(path.strip())))
    if not here.is_absolute():
        return Path.home()
    while not here.is_dir():
        if here.parent == here:
            return None
        here = here.parent
    return here


def listing(path: str = "", extensions: list[str] | None = None) -> dict[str, Any]:
    """One folder's subfolders, and its files when `extensions` is given.

    `extensions` is a list like `[".fits", ".fit"]`; `["*"]` means every file;
    `None` means folders only. An empty `path` opens the home folder, and
    `path=""` with nothing above it is the list of drives.

    Returns `{"path", "parent", "dirs", "files", "roots", "truncated"}`;
    `path` is `None` when what is being shown is the drive list.
    """
    if path == ":roots":
        return {"path": None, "parent": None, "dirs": roots(), "files": [],
                "roots": roots(), "truncated": False}

    here = _start(path)
    if here is None:
        return listing(":roots", extensions)

    wanted = None if extensions is None else {e.lower() for e in extensions}
    dirs: list[str] = []
    files: list[str] = []
    try:
        with os.scandir(here) as entries:
            for entry in entries:
                try:
                    if _hidden(entry):
                        continue
                    if entry.is_dir():
                        dirs.append(entry.name)
                    elif wanted is not None and entry.is_file() and (
                            "*" in wanted or Path(entry.name).suffix.lower() in wanted):
                        files.append(entry.name)
                except OSError:
                    continue                    # a broken link, a locked file
    except PermissionError as exc:
        raise BrowseError(f"Windows will not let Starfront look in {here}") from exc
    except OSError as exc:
        raise BrowseError(f"{here} could not be read: {exc.strerror or exc}") from exc

    dirs.sort(key=str.lower)
    files.sort(key=str.lower)
    truncated = len(dirs) + len(files) > LIMIT
    if truncated:
        dirs = dirs[:LIMIT]
        files = files[:max(0, LIMIT - len(dirs))]

    # The top of a drive goes up to the list of drives, not nowhere.
    parent = ":roots" if here.parent == here else str(here.parent)
    if parent == ":roots" and os.name != "nt":
        parent = None
    return {"path": str(here), "parent": parent, "dirs": dirs, "files": files,
            "roots": roots(), "truncated": truncated}
