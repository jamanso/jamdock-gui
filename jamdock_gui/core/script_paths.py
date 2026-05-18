"""Locate the bundled bash scripts (``jamlib``, ``jamreceptor``, …).

Resolution order:
    1. Explicit override path (e.g. from QSettings) if one exists.
    2. The script bundled inside the installed package, via
       ``importlib.resources``.
    3. ``shutil.which()`` against ``$PATH``.

The third option lets advanced users keep using their own forked copy of the
bash script while still using the GUI. Never raises silently — if a script
can't be found, ``ScriptNotFoundError`` is thrown.
"""
from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path

SCRIPT_NAMES = ("jamlib", "jamreceptor", "jamqvina", "jamrank", "jamresume")


class ScriptNotFoundError(FileNotFoundError):
    """Raised when a jamdock-suite bash script cannot be located."""


def find_script(name: str, override: str | Path | None = None) -> Path:
    """Return an absolute :class:`Path` to ``name`` or raise.

    Parameters
    ----------
    name:
        One of the entries in :data:`SCRIPT_NAMES`.
    override:
        Optional explicit path supplied by the user (e.g. from settings).
    """
    if name not in SCRIPT_NAMES:
        raise ValueError(f"Unknown jamdock script: {name!r}")

    # 1) explicit override
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return p.resolve()

    # 2) bundled inside the package
    try:
        ref = resources.files("jamdock_gui").joinpath("scripts", name)
        # ``files()`` returns a Traversable; on a regular install this is a
        # real path, but for zip-based installs we'd need ``as_file()``.
        # We don't ship as a zipapp, so this is fine.
        path = Path(str(ref))
        if path.is_file():
            return path.resolve()
    except (FileNotFoundError, ModuleNotFoundError):
        pass

    # 3) $PATH
    on_path = shutil.which(name)
    if on_path:
        return Path(on_path).resolve()

    raise ScriptNotFoundError(
        f"Could not locate the {name!r} script. "
        "Set its path in Settings or add the directory to $PATH."
    )
