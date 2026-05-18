"""Spawn PyMOL as a detached external process.

Why this exists
---------------
The embedded NGL viewer (``widgets/viewer3d.py``) needs a working WebGL
stack, which WSL2 doesn't provide. PyMOL has its own software OpenGL
fallback and renders fine on virtually any platform — including WSL2 and
remote X servers — so we offer it as an "open externally" path.

PyMOL is launched detached so it survives the GUI being closed. We don't
track its PID or wait on it; the user manages the window themselves.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# Candidate executable names, in order of preference. Includes both the
# common open-source names and Schrödinger's commercial wrapper.
_PYMOL_NAMES = ("pymol", "PyMOL", "pymol2", "pymolserver")


def find_pymol(override: str | Path | None = None) -> str | None:
    """Return an absolute path to a PyMOL launcher, or ``None``.

    *override* (e.g. from QSettings) wins if it points to an existing file.
    """
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return str(p.resolve())

    for name in _PYMOL_NAMES:
        path = shutil.which(name)
        if path:
            return path
    return None


def _make_detached_kwargs() -> dict:
    """OS-specific kwargs to detach the child process from the parent.

    On POSIX we use ``start_new_session`` so PyMOL doesn't receive
    SIGHUP/SIGINT when our terminal closes. On Windows we use the
    ``DETACHED_PROCESS`` creation flag.
    """
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        return {"creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP}
    # POSIX
    return {"start_new_session": True}


def _is_inside_venv(pymol_path: str) -> bool:
    """True iff *pymol_path* lives inside the currently-active virtualenv.

    When True, we want to inherit the venv's env so PyMOL uses the venv's
    Python (and therefore the pymol module the user pip-installed there).
    When False (system PyMOL on PATH), we strip the venv vars so PyMOL's
    wrapper script falls back to the system Python.
    """
    venv = os.environ.get("VIRTUAL_ENV")
    if not venv:
        return False
    try:
        return Path(pymol_path).resolve().is_relative_to(Path(venv).resolve())
    except ValueError:
        return False


def _system_env_for_pymol() -> dict:
    """Copy of ``os.environ`` with the active venv's vars stripped.

    Needed when launching the system PyMOL (``/usr/bin/pymol``) which is a
    wrapper that runs ``python -m pymol``. If we leave the venv's PATH /
    PYTHONHOME / PYTHONPATH in place, that wrapper picks up the venv's
    Python — and the venv usually doesn't have the ``pymol`` module
    installed, so PyMOL crashes immediately with ModuleNotFoundError.
    """
    env = dict(os.environ)
    venv_bin = env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    if venv_bin:
        venv_resolved = str(Path(venv_bin).resolve())
        sep = os.pathsep
        parts = env.get("PATH", "").split(sep)
        parts = [
            p for p in parts
            if p and not str(Path(p).resolve()).startswith(venv_resolved)
        ]
        env["PATH"] = sep.join(parts)
    return env


def _make_clean_env() -> dict:
    """Return a copy of ``os.environ`` with venv overrides stripped.

    On Linux the system PyMOL package (``/usr/bin/pymol``) is a wrapper
    that internally runs ``python3 -m pymol``. When jamdock-gui is launched
    from inside a venv that ``python3`` resolves to the venv's interpreter,
    which doesn't have the ``pymol`` module installed (it lives in the
    system site-packages). Result: ``ModuleNotFoundError: No module named
    'pymol'`` and PyMOL exits with rc=1.

    This helper builds a child-process environment that mimics what the
    user would have at a fresh shell BEFORE activating the venv:

    * ``VIRTUAL_ENV`` removed
    * ``PYTHONHOME`` / ``PYTHONPATH`` removed
    * ``PATH`` with the venv's ``bin/`` directory stripped out

    so the wrapper's ``python3`` resolves to the system interpreter and
    finds the PyMOL module.
    """
    env = dict(os.environ)
    venv = env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    if venv:
        venv_bin = str(Path(venv) / ("Scripts" if sys.platform == "win32" else "bin"))
        path = env.get("PATH", "")
        if path:
            parts = path.split(os.pathsep)
            parts = [p for p in parts if p and p.rstrip(os.sep) != venv_bin.rstrip(os.sep)]
            env["PATH"] = os.pathsep.join(parts)
    return env


def _venv_clean_env() -> dict:
    """Return an env dict with the *active venv* stripped from PATH.

    Why: distros ship PyMOL as a wrapper script that does
    ``python3 -m pymol`` internally. If we inherit the venv's PATH, that
    ``python3`` resolves to ``<venv>/bin/python3`` — which doesn't have
    the system ``pymol`` module installed and crashes with
    ``ModuleNotFoundError: No module named 'pymol'``.

    By dropping ``$VIRTUAL_ENV/bin`` from PATH and unsetting
    ``VIRTUAL_ENV`` / ``PYTHONHOME`` / ``PYTHONPATH`` for the child, we
    let it resolve the system python, where the apt/conda PyMOL lives.
    No-op when no venv is active.
    """
    env = os.environ.copy()
    venv_root = env.get("VIRTUAL_ENV")
    if not venv_root:
        return env

    bin_subdir = "Scripts" if os.name == "nt" else "bin"
    venv_bin = str(Path(venv_root) / bin_subdir)

    parts = env.get("PATH", "").split(os.pathsep)
    cleaned: list[str] = []
    for p in parts:
        if not p:
            continue
        try:
            same = Path(p).resolve(strict=False) == Path(venv_bin).resolve(strict=False)
        except OSError:
            same = False
        if not same:
            cleaned.append(p)
    env["PATH"] = os.pathsep.join(cleaned)

    # Drop venv markers so the child python doesn't accidentally re-enter it.
    env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    return env


def launch_pymol(
    *,
    pymol: str | Path,
    files: list[Path | str] | None = None,
    extra_args: list[str] | None = None,
    workdir: Path | str | None = None,
) -> tuple[bool, str]:
    """Launch PyMOL in a detached child process.

    Files are passed as positional arguments — PyMOL auto-loads
    ``.pdb`` / ``.pdbqt`` as structures and ``.pml`` / ``.py`` as scripts.

    Returns ``(ok, message)``. ``ok`` is True only if the process is still
    alive ~1.5 s after spawning (catches binaries that crash immediately).
    ``message`` carries either the launched command (success) or the error
    output captured from stderr (failure), so the GUI can surface it.
    """
    pymol_path = str(pymol)
    args: list[str] = [pymol_path]
    if extra_args:
        args.extend(extra_args)
    if files:
        for f in files:
            p = Path(f)
            if p.is_file():
                args.append(str(p))
            else:
                log.warning("launch_pymol: skipping missing file %s", p)

    cwd = str(workdir) if workdir else None
    kwargs = _make_detached_kwargs()
    cmdline = " ".join(args)

    # We DO capture stderr to a pipe so we can read it back if PyMOL crashes
    # in the first second. Stdout goes to DEVNULL so PyMOL's chattiness
    # doesn't fill our pipes.
    try:
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            close_fds=(os.name == "posix"),
            env=_venv_clean_env(),
            **kwargs,
        )
    except (OSError, FileNotFoundError) as exc:
        msg = f"spawn failed: {exc}"
        log.warning("launch_pymol: %s — cmd: %s", msg, cmdline)
        return False, f"{msg}\n  command: {cmdline}"

    # Wait briefly to detect immediate failures (bad args, missing libs…).
    # 1.5 s is enough for crashes; PyMOL itself takes longer to draw a window
    # so a still-running process is a strong signal of success.
    try:
        rc = proc.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        # Still running - assume PyMOL is happy. Detach the stderr pipe so
        # PyMOL doesn't block on a full buffer.
        try:
            if proc.stderr:
                proc.stderr.close()
        except Exception:
            pass
        return True, f"launched: {cmdline}"
    else:
        # Process exited within the wait window - definitely not running.
        stderr_text = ""
        try:
            if proc.stderr:
                stderr_text = proc.stderr.read().decode("utf-8", errors="replace")
        except Exception:
            stderr_text = ""
        msg = f"PyMOL exited immediately (rc={rc})"
        if stderr_text.strip():
            tail = stderr_text.strip().splitlines()[-5:]
            msg += "\n  stderr:\n    " + "\n    ".join(tail)
        log.warning("launch_pymol: %s - cmd: %s", msg, cmdline)
        return False, f"{msg}\n  command: {cmdline}"


def open_receptor_with_pockets_and_grid(
    *, pymol, receptor, fpocket_pml=None, grid_box_py=None, workdir=None,
):
    # If fpocket_pml is provided, load ONLY it (the .pml carries its own
    # `load receptor.pdb` with paths relative to its own directory).
    # Passing the receptor too would duplicate the structure.
    if fpocket_pml is not None:
        fpocket_pml = Path(fpocket_pml)
        files = [fpocket_pml]
        cwd = fpocket_pml.parent
    else:
        files = [Path(receptor)]
        cwd = Path(receptor).parent
    if grid_box_py is not None:
        files.append(Path(grid_box_py).resolve())
    return launch_pymol(pymol=pymol, files=files, workdir=cwd)


def open_receptor_with_pose(*, pymol, receptor, pose, workdir=None):
    return launch_pymol(pymol=pymol, files=[receptor, pose], workdir=workdir)
